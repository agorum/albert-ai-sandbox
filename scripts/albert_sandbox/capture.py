"""Capture the software of a running sandbox as a profile.

Only package lists are captured, never files or the home directory: the new
profile is built like any other (``FROM`` the shipped image, packages
installed from their repositories), so it is reproducible and stays small.

* apt: packages installed by hand (``apt-mark showmanual``) that the image of
  the sandbox does not have, without versions.
* pip: packages installed with pip (system-wide or with ``--user`` in the
  home directory), with versions (``name==version``).
* npm: global packages, with versions (``name@version``).
* APT sources and keys added in the sandbox (``/etc/apt/sources.list.d``,
  keyrings) become the profile's preparation script, which runs before the
  packages are installed.

What cannot be captured this way is reported as hints for the administrator:
files added or changed outside the package management (``docker diff``,
minus files that belong to packages) and packages that were removed.

Everything read from a sandbox is untrusted: the commands run as the sandbox
user, their output is size-limited and every package name is validated (and
later shell-quoted) like an administrator's input. A proposal from ALBERT |
AI is never built before an administrator accepted it.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import db, dockerctl, profiles
from .errors import BadRequest, Conflict, NotFound, SandboxError

USER = "ubuntu"
MAX_SOURCE_BYTES = 64 * 1024
MAX_SOURCE_FILES = 20
MAX_HINTS = 50
MAX_CLASSIFY = 20000
MAX_LISTED = 50
LOCK_WAIT_SECONDS = 60
_capture_lock = threading.Lock()  # one capture at a time: each one starts a container

COLLECT_SCRIPT = r"""
set +e
echo '@@apt'
apt-mark showmanual 2>/dev/null
echo '@@installed'
dpkg-query -W -f '${db:Status-Abbrev}|${binary:Package}|${Version}\n' 2>/dev/null
echo '@@pip'
python3 -m pip list -v --format=json --disable-pip-version-check 2>/dev/null
echo
echo '@@npm'
npm ls -g --json --depth=0 --prefix /usr 2>/dev/null
echo
echo '@@npmuser'
if [ "$(npm config get prefix 2>/dev/null)" != "/usr" ]; then npm ls -g --json --depth=0 2>/dev/null; fi
echo
echo '@@sources'
for f in /etc/apt/sources.list /etc/apt/sources.list.d/* /etc/apt/keyrings/* /usr/share/keyrings/* \
         /etc/apt/trusted.gpg /etc/apt/trusted.gpg.d/*; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  s=$(stat -c %s -- "$f")
  if [ "$s" -gt 65536 ]; then echo "BIG $f"; continue; fi
  h=$(sha256sum < "$f" 2>/dev/null | cut -c1-64)
  if [ -z "$h" ]; then echo "UNREADABLE $f"; continue; fi
  echo "FILE $h $f"
done
echo '@@end'
"""

READ_SCRIPT = r'for f in "$@"; do printf "%s %s\n" "$(base64 -w0 -- "$f" 2>/dev/null)" "$f"; done'

# Reads a JSON list of paths on stdin; prints for each: type, owning package,
# whether pip installed it, link target. Only the asked paths are kept in memory.
CLASSIFY_SCRIPT = r"""
import glob, json, os, stat, sys
wanted = set(json.load(sys.stdin))
owner = {}
for lst in glob.glob('/var/lib/dpkg/info/*.list'):
    pkg = os.path.basename(lst)[:-5]
    try:
        with open(lst, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                p = line.rstrip('\n')
                if p in wanted and p not in owner:
                    owner[p] = pkg
    except OSError:
        pass
pip = set()
try:
    import importlib.metadata as md
    for d in md.distributions():
        for f in (d.files or []):
            p = os.path.normpath(str(d.locate_file(f)))
            if p in wanted:
                pip.add(p)
except Exception:
    pass
out = {}
for p in wanted:
    t, link = '?', ''
    try:
        st = os.lstat(p)
        t = 'd' if stat.S_ISDIR(st.st_mode) else 'l' if stat.S_ISLNK(st.st_mode) else 'f'
        if t == 'l':
            link = os.readlink(p)
    except OSError:
        pass
    out[p] = [t, owner.get(p, ''), p in pip, link]
json.dump(out, sys.stdout)
"""

SOURCE_PATH = re.compile(
    r"^/(etc/apt/sources\.list|etc/apt/trusted\.gpg"
    r"|(etc/apt/sources\.list\.d|etc/apt/keyrings|usr/share/keyrings|etc/apt/trusted\.gpg\.d)"
    r"/[A-Za-z0-9][A-Za-z0-9._+-]*)$")

# Changes that are no software of their own (runtime data, caches, package
# managers' own trees, files the sandbox writes at every start).
SKIP_PREFIXES = (
    "/tmp", "/var/tmp", "/var/log", "/var/cache", "/var/lib", "/var/backups", "/var/mail", "/var/spool",
    "/var/run", "/run", "/proc", "/sys", "/dev", "/home", "/root/.cache", "/root/.npm", "/root/.local/state",
    "/usr/lib/node_modules", "/usr/local/lib/node_modules", "/etc/apt", "/app", "/usr/sbin/docker-init",
    "/etc/alternatives", "/etc/systemd", "/etc/rc0.d", "/etc/rc1.d", "/etc/rc2.d", "/etc/rc3.d", "/etc/rc4.d",
    "/etc/rc5.d", "/etc/rc6.d", "/etc/rcS.d", "/etc/ssl/certs", "/usr/share/mime", "/usr/share/icons",
    "/usr/share/info/dir",
)
SKIP_FILES = {
    "/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/etc/mtab", "/etc/machine-id", "/etc/.pwd.lock",
    "/etc/passwd", "/etc/passwd-", "/etc/group", "/etc/group-", "/etc/shadow", "/etc/shadow-",
    "/etc/gshadow", "/etc/gshadow-", "/etc/subuid", "/etc/subuid-", "/etc/subgid", "/etc/subgid-",
    "/etc/shells", "/etc/ld.so.cache",
}
SKIP_NAMES = {"mimeinfo.cache", "icon-theme.cache", "gschemas.compiled", "loaders.cache", "locale-archive",
              "giomodule.cache", "immodules.cache"}
SKIP_STARTS = ("/usr/local/lib/python3",)  # pip's own tree (python3.12/dist-packages ...), captured via pip
PIP_DIRS = ("/usr/local/lib/python", "/.local/lib/python")


class CaptureError(SandboxError):
    status = 502
    code = "capture_failed"


# --------------------------------------------------------------------------
# collecting
# --------------------------------------------------------------------------

def _sections(text: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def _json(lines: Optional[List[str]]) -> Any:
    text = "\n".join(lines or []).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _npm(value: Any) -> Dict[str, str]:
    deps = value.get("dependencies") if isinstance(value, dict) else None
    if not isinstance(deps, dict):
        return {}
    result = {}
    for name, item in deps.items():
        version = item.get("version") if isinstance(item, dict) else None
        if isinstance(name, str) and isinstance(version, str):
            result[name] = version
    return result


def parse(text: str) -> Dict[str, Any]:
    """Output of COLLECT_SCRIPT -> package and source lists (shapes checked, nothing trusted)."""
    sec = _sections(text)
    if "end" not in sec:
        raise CaptureError("the sandbox did not answer the inspection completely")
    installed: Dict[str, str] = {}
    for line in sec.get("installed", []):
        parts = line.split("|")
        # only installed packages ("ii"); removed ones keep their configuration ("rc")
        if len(parts) == 3 and parts[0].strip() == "ii" and parts[1]:
            installed[parts[1]] = parts[2]
    pip: Dict[str, Tuple[str, str, str]] = {}
    for item in _json(sec.get("pip")) or []:
        if not isinstance(item, dict):
            continue
        name, version, location = item.get("name"), item.get("version"), item.get("location") or ""
        if isinstance(name, str) and isinstance(version, str) and isinstance(location, str):
            pip[_pip_key(name)] = (name, version, location)
    npm = _npm(_json(sec.get("npm")))
    npm.update(_npm(_json(sec.get("npmuser"))))
    sources: Dict[str, str] = {}
    problems: List[str] = []
    for line in sec.get("sources", []):
        parts = line.split(" ", 2)
        if parts[0] == "FILE" and len(parts) == 3:
            sources[parts[2]] = parts[1]
        elif parts[0] in ("BIG", "UNREADABLE") and len(parts) >= 2:
            problems.append(f"{' '.join(parts[1:])}: " + ("larger than 64 KB" if parts[0] == "BIG" else "not readable"))
    return {"apt": [p.strip() for p in sec.get("apt", []) if p.strip()], "installed": installed,
            "pip": pip, "npm": npm, "sources": sources, "sourceProblems": problems}


def _pip_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _token_key(kind: str, token: str) -> str:
    if kind == "apt":
        return token.split("=", 1)[0].split(":", 1)[0]
    if kind == "pip":
        return _pip_key(re.split(r"[\[<>=!~]", token, 1)[0])
    if token.startswith("@"):
        scope, _, rest = token[1:].partition("/")
        return "@" + scope + "/" + rest.split("@", 1)[0]
    return token.split("@", 1)[0]


def _valid(kind: str, token: str) -> bool:
    return bool(profiles.PACKAGE_RULES[kind][0].match(token))


# --------------------------------------------------------------------------
# comparing
# --------------------------------------------------------------------------

def compare(sandbox: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """New packages in the sandbox compared to its image, and what was removed."""
    skipped: List[str] = []
    new: Dict[str, List[str]] = {"apt": [], "pip": [], "npm": []}
    base_apt = set(base["apt"])
    for name in sandbox["apt"]:
        if name in base_apt:
            continue
        (new["apt"] if _valid("apt", name) else skipped).append(name if _valid("apt", name) else f"apt: {name[:80]}")
    for key, (name, version, location) in sorted(sandbox["pip"].items()):
        if not any(d in location for d in PIP_DIRS):
            continue  # Python packages of the distribution (apt) or of other environments
        old = base["pip"].get(key)
        if old and old[1] == version:
            continue
        token = f"{name}=={version}"
        (new["pip"] if _valid("pip", token) else skipped).append(token if _valid("pip", token) else f"pip: {token[:80]}")
    for name, version in sorted(sandbox["npm"].items()):
        if base["npm"].get(name) == version:
            continue
        token = f"{name}@{version}"
        (new["npm"] if _valid("npm", token) else skipped).append(token if _valid("npm", token) else f"npm: {token[:80]}")
    installed_names = {n.split(":", 1)[0] for n in sandbox["installed"]}
    removed = [p for p in base["apt"] if p not in set(sandbox["apt"]) and p.split(":", 1)[0] not in installed_names]
    sources = sorted(p for p, sha in sandbox["sources"].items() if base["sources"].get(p) != sha)
    return {"packages": new, "removed": removed[:MAX_LISTED], "sources": sources,
            "skipped": (skipped + sandbox["sourceProblems"])[:MAX_LISTED]}


def _source_files(name: str, paths: List[str], skipped: List[str]) -> Dict[str, bytes]:
    wanted = []
    for path in paths:
        if not SOURCE_PATH.match(path):
            skipped.append(f"APT source {path[:120]}: unusual file name")
        elif len(wanted) >= MAX_SOURCE_FILES:
            skipped.append(f"APT source {path}: more than {MAX_SOURCE_FILES} files")
        else:
            wanted.append(path)
    if not wanted:
        return {}
    out = dockerctl.exec_capture(name, ["bash", "-c", READ_SCRIPT, "albert-read", *wanted], USER,
                                 max_bytes=MAX_SOURCE_FILES * MAX_SOURCE_BYTES * 2)
    files: Dict[str, bytes] = {}
    for line in out.decode("utf-8", "replace").splitlines():
        encoded, _, path = line.partition(" ")
        if path not in wanted:
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            skipped.append(f"APT source {path}: not readable")
            continue
        if not data or len(data) > MAX_SOURCE_BYTES:
            skipped.append(f"APT source {path}: empty or larger than 64 KB")
            continue
        files[path] = data
    return files


def prepare_script(files: Dict[str, bytes], sandbox: str, when: Optional[float] = None) -> str:
    """Shell commands that write the captured APT sources and keys (everything quoted)."""
    if not files:
        return ""
    day = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
    lines = [f"# APT sources and keys captured from sandbox {sandbox} on {day}"]
    for parent in sorted({p.rsplit("/", 1)[0] for p in files}):
        lines.append(f"install -d -m 0755 {shlex.quote(parent)}")
    # keys first, then the sources that refer to them
    for path in sorted(files, key=lambda p: (not p.endswith((".gpg", ".asc", ".pgp")), p)):
        data = files[path]
        text: Optional[str] = None
        if b"\x00" not in data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is not None and not path.endswith((".gpg", ".pgp")):
            content = " ".join(shlex.quote(line) for line in text.splitlines())
            lines.append(f"printf '%s\\n' {content} > {shlex.quote(path)}")
        else:
            lines.append(f"printf '%s' {shlex.quote(base64.b64encode(data).decode('ascii'))} | base64 -d > "
                         f"{shlex.quote(path)}")
        lines.append(f"chmod 0644 {shlex.quote(path)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# hints: changes outside the package management
# --------------------------------------------------------------------------

def _skip_path(path: str) -> bool:
    if path in SKIP_FILES or "/__pycache__" in path or path.endswith(".pyc"):
        return True
    if path.rsplit("/", 1)[-1] in SKIP_NAMES:
        return True
    if path.startswith(SKIP_STARTS):
        return True
    return any(path == p or path.startswith(p + "/") for p in SKIP_PREFIXES)


def hints(name: str, sandbox: Dict[str, Any], base: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    entries = [(k, p) for k, p in dockerctl.diff(name) if k in ("A", "C") and not _skip_path(p)][:MAX_CLASSIFY]
    if not entries:
        return [], 0
    raw = dockerctl.exec_capture(name, ["python3", "-c", CLASSIFY_SCRIPT], USER,
                                 input_bytes=json.dumps([p for _, p in entries]).encode("utf-8"))
    try:
        info = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    changed_pkgs = {p for p, v in sandbox["installed"].items() if base["installed"].get(p) != v}
    result: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for kind, path in sorted(entries, key=lambda e: e[1].split("/")):
        item = info.get(path)
        t, owner, pip_owned, link = (item if isinstance(item, list) and len(item) == 4 else ["?", "", False, ""])
        owner = owner if isinstance(owner, str) else ""
        if t == "d" and kind == "C":
            continue  # a directory whose content changed
        if pip_owned is True or (owner and (owner in changed_pkgs or kind == "A")):
            continue  # belongs to a package that is captured (or to the package management)
        if t == "l" and isinstance(link, str) and "node_modules/" in link:
            continue  # bin link of a global npm package
        if current and path.startswith(current["path"] + "/"):
            current["files"] += 1
            continue
        hint = {"path": path, "change": "added" if kind == "A" else "changed",
                "kind": {"d": "dir", "l": "link"}.get(t, "file"), "files": 0}
        result.append(hint)
        current = hint if hint["kind"] == "dir" and kind == "A" else None
    return result[:MAX_HINTS], max(0, len(result) - MAX_HINTS)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

def capture(name: str, image: str, with_hints: bool = True) -> Dict[str, Any]:
    """Compare a running sandbox with the image it was created from."""
    try:
        sandbox = parse(dockerctl.exec_capture(name, ["bash", "-c", COLLECT_SCRIPT], USER).decode("utf-8", "replace"))
        base = parse(dockerctl.run_capture(image, ["bash", "-c", COLLECT_SCRIPT], USER).decode("utf-8", "replace"))
    except dockerctl.DockerError as exc:
        raise CaptureError(f"could not inspect the sandbox: {exc}") from exc
    result = compare(sandbox, base)
    skipped = result["skipped"]
    try:
        files = _source_files(name, result["sources"], skipped) if result["sources"] else {}
        found, more = hints(name, sandbox, base) if with_hints else ([], 0)
    except dockerctl.DockerError as exc:
        raise CaptureError(f"could not inspect the sandbox: {exc}") from exc
    return {"packages": result["packages"], "prepareScript": prepare_script(files, name),
            "aptSources": sorted(files), "removedPackages": result["removed"], "hints": found,
            "hintsMore": more, "skipped": skipped[:MAX_LISTED]}


def merge(based_on: Optional[sqlite3.Row], captured: Dict[str, Any]) -> Dict[str, str]:
    """Profile fields: the packages of the sandbox's profile plus the captured ones
    (a captured version replaces the profile's entry of the same package)."""
    old = profiles.packages(based_on) if based_on is not None else {"apt": [], "pip": [], "npm": []}
    fields: Dict[str, str] = {}
    for kind, column in profiles.PACKAGE_COLUMNS.items():
        merged = list(old[kind])
        for token in captured["packages"][kind]:
            key = _token_key(kind, token)
            hits = [i for i, t in enumerate(merged) if _token_key(kind, t) == key]
            if hits:
                merged[hits[0]] = token
            else:
                merged.append(token)
        fields[column] = "\n".join(merged)
    prepare = [based_on["prepare_script"] if based_on is not None else "", captured["prepareScript"]]
    fields["prepare_script"] = "\n\n".join(p.strip("\n") for p in prepare if p and p.strip())
    fields["setup_script"] = (based_on["setup_script"] or "") if based_on is not None else ""
    return fields


def has_changes(captured: Dict[str, Any]) -> bool:
    return any(captured["packages"][k] for k in ("apt", "pip", "npm")) or bool(captured["prepareScript"])


def default_description(captured: Dict[str, Any], based_on: Optional[sqlite3.Row]) -> str:
    names = [_token_key(k, t) for k in ("apt", "pip", "npm") for t in captured["packages"][k]]
    text = (f"{based_on['name']} plus " if based_on is not None else "") + ", ".join(names[:12])
    return (text + (" …" if len(names) > 12 else ""))[:1000]


def view(fields: Dict[str, str], captured: Dict[str, Any], based_on: Optional[sqlite3.Row]) -> Dict[str, Any]:
    """What the caller sees of a capture (preview or saved proposal)."""
    return {
        "basedOn": based_on["name"] if based_on is not None else profiles.STANDARD_NAME,
        "packages": {k: [p for p in fields[c].split("\n") if p] for k, c in profiles.PACKAGE_COLUMNS.items()},
        "captured": captured["packages"],
        "aptSources": captured["aptSources"],
        "removedPackages": captured["removedPackages"],
        "hints": captured["hints"],
        "hintsMore": captured["hintsMore"],
        "skipped": captured["skipped"],
    }


def info_json(captured: Dict[str, Any]) -> str:
    """Stored with the profile for the administrator (capture_info)."""
    keep = {k: captured[k] for k in ("captured", "aptSources", "removedPackages", "hints", "hintsMore", "skipped")
            if k in captured}
    return json.dumps(keep)


def check_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequest("name is required (name of the new profile)")
    return profiles.validate_fields({"name": value})["name"]


def from_sandbox(name: str, *, profile_name: Any, description: Any, preview: bool, proposal: bool,
                 actor: str) -> Tuple[Dict[str, Any], Optional[sqlite3.Row]]:
    """Capture a running sandbox. ``preview``: only show what would be saved.
    Otherwise create the profile (``proposal``: from ALBERT | AI, waits for an
    administrator). Access to the sandbox is checked by the caller."""
    with db.session() as conn:
        row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
        if not row:
            raise NotFound(f"Container '{name}' not found", code="not_found")
        based_on = None
        if row["profile_id"]:
            based_on = conn.execute("SELECT * FROM profiles WHERE id=?", (row["profile_id"],)).fetchone()
        wanted: Optional[str] = None
        if not preview or profile_name not in (None, ""):
            wanted = check_name(profile_name)
            if conn.execute("SELECT 1 FROM profiles WHERE name=?", (wanted,)).fetchone():
                raise Conflict(f"a profile named '{wanted}' already exists; choose another name",
                               code="profile_name_taken")
        if proposal and not preview:
            pending = profiles.open_proposals(conn, row["api_key_id"])
            if len(pending) >= profiles.MAX_OPEN_PROPOSALS:
                raise Conflict(f"{len(pending)} profile proposals are already waiting for an administrator "
                               f"(at most {profiles.MAX_OPEN_PROPOSALS}); ask an administrator to accept or "
                               "delete them first", code="proposal_limit",
                               details={"limit": profiles.MAX_OPEN_PROPOSALS, "open": [p["name"] for p in pending]})
        desc = profiles.validate_fields({"description": description})["description"] if description else ""
    info = dockerctl.inspect_one(name)
    if not info or not (info.get("State") or {}).get("Running"):
        raise Conflict(f"Sandbox '{name}' is not running; start it first", code="sandbox_not_running")
    if not _capture_lock.acquire(timeout=LOCK_WAIT_SECONDS):
        raise Conflict("another sandbox is being captured right now; try again in a minute", code="capture_busy")
    try:
        captured = capture(name, info.get("Image") or "")
    finally:
        _capture_lock.release()
    if not has_changes(captured):
        extra = (" Only files outside the package management changed (see hints); they cannot be captured."
                 if captured["hints"] else "")
        raise BadRequest(f"Nothing to save: no packages or APT sources were added in sandbox '{name}' "
                         f"compared to its profile ({based_on['name'] if based_on else profiles.STANDARD_NAME})."
                         + extra, code="no_changes",
                         details={"hints": captured["hints"], "removedPackages": captured["removedPackages"]})
    fields = merge(based_on, captured)
    desc = desc or default_description(captured, based_on)
    result = view(fields, captured, based_on)
    result.update({"name": wanted, "description": desc})
    if preview:
        return result, None
    stored = dict(captured, captured=captured["packages"])
    with db.session() as conn:
        try:
            created = profiles.create_captured(
                conn, wanted or "", {**fields, "description": desc}, proposal=proposal,
                client_id=row["api_key_id"], sandbox=name, owner=row["owner"],
                based_on=based_on["name"] if based_on is not None else None, info=info_json(stored))
        except Conflict as exc:
            raise Conflict(exc.message + "; choose another name", code="profile_name_taken") from exc
        db.audit(conn, actor, "profile.propose" if proposal else "profile.capture", created["name"],
                 {"profileId": created["id"], "sandbox": name,
                  "packages": sum(len(v) for v in captured["packages"].values())})
    result.update({"id": created["id"], "state": "proposed" if proposal else "new"})
    return result, created
