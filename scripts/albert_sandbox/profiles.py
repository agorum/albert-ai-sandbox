"""Sandbox profiles: extra software on top of the shipped sandbox image.

A profile lists OS packages (apt), Python packages (pip), global Node packages
(npm) and optional setup commands. Building it produces a Docker image
``FROM`` the shipped image (``ALBERT_DOCKER_IMAGE``) with that software added;
the shipped image stays the maintained base. Clients reference a profile
(``api_keys.profile_id``, NULL = standard image) and their sandboxes are
created from the profile image.

Package names are validated (and shell-quoted), so the structured fields
cannot inject anything into the build. The setup commands are free text by
design: they run as root inside the build container (never on the host) and
are meant for administrators, e.g. to add a vendor repository.

Builds run outside the web service (``systemd-run``; fallback: a detached
process), so restarting the manager does not kill them. A build job waits for
a host-wide lock (one build at a time, state ``queued``), writes the docker
output to ``<data>/profiles/<id>/build-<seq>.log`` and keeps a heartbeat in
the database; a build whose heartbeat stops is marked failed. The previous
image stays in use until a build succeeds.

A profile image is outdated when its configuration changed after the build or
when the shipped base image was rebuilt (``install.sh`` rebuilds such profiles
automatically). Sandboxes then show the reason ``image`` and are updated with
``recreate`` (data is kept).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import db, dockerctl
from .config import settings
from .errors import BadRequest, Conflict, NotFound, SandboxError, Unavailable

# Bump when the generated Dockerfile changes in a way that needs a rebuild.
TEMPLATE_VERSION = 2
LABEL_PROFILE = "albert.profile"
LABEL_PROFILE_SEQ = "albert.profile.seq"
STALE_SECONDS = 120
HEARTBEAT_SECONDS = 10
KEEP_LOGS = 5
MAX_PACKAGES = 300
MAX_SETUP_CHARS = 20000

BUILD_ACTIVE = ("queued", "building")

PACKAGE_RULES = {
    # name[:arch][=version]
    "apt": (re.compile(r"^[a-z0-9][a-z0-9+.\-]*(:[a-z0-9]+)?(=[A-Za-z0-9.+~:\-]+)?$"),
            "e.g. libreoffice or tesseract-ocr-deu=4.1.1-2"),
    # name[extras][<op>version[,<op>version]]
    "pip": (re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[A-Za-z0-9._,\-]+\])?"
                       r"((===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!\-]+(,(===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!\-]+)*)?$"),
            "e.g. pandas or openpyxl==3.1.5"),
    # [@scope/]name[@version]
    "npm": (re.compile(r"^(@[a-z0-9][a-z0-9._\-~]*/)?[a-z0-9][a-z0-9._\-~]*(@[A-Za-z0-9.^~<>=*+\-]+)?$"),
            "e.g. typescript or @mermaid-js/mermaid-cli@11.4.2"),
}
PACKAGE_COLUMNS = {"apt": "apt_packages", "pip": "pip_packages", "npm": "npm_packages"}
TEXT_FIELDS = ("name", "description", "setup_script")
EDITABLE_FIELDS = TEXT_FIELDS + tuple(PACKAGE_COLUMNS.values())


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

def parse_packages(kind: str, value: Any) -> List[str]:
    """Package list from text (whitespace/newline separated, '#' comments) or a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        tokens = [str(v).strip() for v in value]
    elif isinstance(value, str):
        tokens = []
        for line in value.splitlines():
            tokens += line.split("#", 1)[0].split()
    else:
        raise BadRequest(f"{kind} packages must be text or a list")
    pattern, example = PACKAGE_RULES[kind]
    result: List[str] = []
    for token in tokens:
        if not token:
            continue
        if not pattern.match(token):
            raise BadRequest(f"invalid {kind} package '{token[:80]}' ({example})", code="invalid_package",
                             details={"kind": kind, "package": token[:80]})
        if token not in result:
            result.append(token)
    if len(result) > MAX_PACKAGES:
        raise BadRequest(f"too many {kind} packages (max {MAX_PACKAGES})")
    return result


def packages(row: sqlite3.Row) -> Dict[str, List[str]]:
    return {kind: [p for p in (row[col] or "").split("\n") if p] for kind, col in PACKAGE_COLUMNS.items()}


def validate_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        if key == "name":
            name = (value or "").strip() if isinstance(value, str) else ""
            if not name:
                raise BadRequest("name is required")
            if name.lower() == STANDARD_NAME:
                raise BadRequest(f"'{STANDARD_NAME}' is reserved for the shipped image")
            if len(name) > 80:
                raise BadRequest("name too long (max 80 characters)")
            clean[key] = name
        elif key == "description":
            if value is not None and not isinstance(value, str):
                raise BadRequest("description must be text")
            text = (value or "").strip()
            if len(text) > 1000:
                raise BadRequest("description too long (max 1000 characters)")
            clean[key] = text
        elif key == "setup_script":
            if value is not None and not isinstance(value, str):
                raise BadRequest("setup script must be text")
            text = (value or "").replace("\r\n", "\n").strip("\n")
            if len(text) > MAX_SETUP_CHARS:
                raise BadRequest(f"setup script too long (max {MAX_SETUP_CHARS} characters)")
            clean[key] = text
        else:
            for kind, column in PACKAGE_COLUMNS.items():
                if key == column:
                    clean[key] = "\n".join(parse_packages(kind, value))
    return clean


def get(conn: sqlite3.Connection, profile_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        raise NotFound(f"profile {profile_id} not found")
    return row


def find(conn: sqlite3.Connection, ref: Any) -> sqlite3.Row:
    """Profile by id or (case-insensitive) name."""
    text = str(ref).strip()
    if text.isdigit():
        return get(conn, int(text))
    row = conn.execute("SELECT * FROM profiles WHERE name=?", (text,)).fetchone()
    if not row:
        raise NotFound(f"profile '{text}' not found")
    return row


def list_all(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    expire_stale(conn)
    return conn.execute("SELECT * FROM profiles ORDER BY name COLLATE NOCASE").fetchall()


def create(conn: sqlite3.Connection, name: str, **fields: Any) -> sqlite3.Row:
    clean = validate_fields({"name": name, **fields})
    if conn.execute("SELECT 1 FROM profiles WHERE name=?", (clean["name"],)).fetchone():
        raise Conflict(f"a profile named '{clean['name']}' already exists")
    now = int(time.time())
    columns = ["created_at", "updated_at"] + list(clean)
    values = [now, now] + list(clean.values())
    cur = conn.execute(f"INSERT INTO profiles({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       values)
    return get(conn, cur.lastrowid)


CLONE_SUFFIX = " (Kopie)"


def _free_clone_name(conn: sqlite3.Connection, source: str) -> str:
    base = (source + CLONE_SUFFIX)[:80]
    name, n = base, 2
    while conn.execute("SELECT 1 FROM profiles WHERE name=?", (name,)).fetchone():
        suffix = f" {n}"
        name, n = base[:80 - len(suffix)] + suffix, n + 1
    return name


def clone(conn: sqlite3.Connection, profile_id: int, name: Optional[str] = None) -> sqlite3.Row:
    """New profile with the settings of another one (description, packages, setup
    script). Not built, assigned to no client: a variant is edited and built anew."""
    source = get(conn, profile_id)
    fields = {"description": source["description"], "setup_script": source["setup_script"],
              **{column: source[column] for column in PACKAGE_COLUMNS.values()}}
    wanted = name.strip() if isinstance(name, str) and name.strip() else _free_clone_name(conn, source["name"])
    return create(conn, wanted, **fields)


def update(conn: sqlite3.Connection, profile_id: int, fields: Dict[str, Any]) -> sqlite3.Row:
    get(conn, profile_id)
    clean = validate_fields({k: v for k, v in fields.items() if k in EDITABLE_FIELDS})
    if "name" in clean and conn.execute("SELECT 1 FROM profiles WHERE name=? AND id<>?",
                                        (clean["name"], profile_id)).fetchone():
        raise Conflict(f"a profile named '{clean['name']}' already exists")
    if clean:
        conn.execute(f"UPDATE profiles SET {', '.join(k + '=?' for k in clean)}, updated_at=? WHERE id=?",
                     [*clean.values(), int(time.time()), profile_id])
    return get(conn, profile_id)


def clients_using(conn: sqlite3.Connection, profile_id: int) -> List[sqlite3.Row]:
    """Clients that may use the profile (allowed or default); column is_default."""
    return conn.execute(
        "SELECT k.id, k.label, (k.profile_id IS ?) AS is_default FROM api_keys k "
        "WHERE k.profile_id=? OR EXISTS (SELECT 1 FROM client_profiles cp "
        "WHERE cp.client_id=k.id AND cp.profile_id=?) ORDER BY k.label",
        (profile_id, profile_id, profile_id)).fetchall()


def sandboxes_using(conn: sqlite3.Connection, profile_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM sandboxes WHERE profile_id=?", (profile_id,)).fetchone()[0]


def delete(conn: sqlite3.Connection, profile_id: int) -> sqlite3.Row:
    """Delete a profile that no client uses. Its images are removed by reconcile."""
    row = get(conn, profile_id)
    users = clients_using(conn, profile_id)
    if users:
        names = ", ".join((u["label"] or f"#{u['id']}") for u in users)
        raise BadRequest(f"profile is still allowed for: {names}", code="profile_in_use",
                         details={"clients": [u["id"] for u in users]})
    used = sandboxes_using(conn, profile_id)
    if used:
        raise BadRequest(f"{used} sandbox(es) still run with this profile; remove or rebuild them first",
                         code="profile_in_use", details={"sandboxes": used})
    if row["build_state"] in BUILD_ACTIVE:
        raise Conflict("profile is being built; wait until the build has finished")
    conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    shutil.rmtree(profile_dir(profile_id), ignore_errors=True)
    return row


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def config_hash(row: sqlite3.Row) -> str:
    payload = json.dumps([TEMPLATE_VERSION, packages(row), row["setup_script"] or ""], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def base_image_id() -> Optional[str]:
    return dockerctl.image_id(settings().docker_image)


def outdated_reasons(row: sqlite3.Row, base_id: Optional[str]) -> List[str]:
    if not row["image"]:
        return []
    reasons = []
    if row["image_config_hash"] != config_hash(row):
        reasons.append("config")
    if base_id and row["image_base_id"] and row["image_base_id"] != base_id:
        reasons.append("base")
    return reasons


def status(row: sqlite3.Row, base_id: Optional[str]) -> str:
    if row["build_state"] in BUILD_ACTIVE:
        return row["build_state"]
    if not row["image"]:
        return "failed" if row["build_state"] == "failed" else "new"
    return "outdated" if outdated_reasons(row, base_id) else "ready"


def serialize(conn: sqlite3.Connection, row: sqlite3.Row, base_id: Optional[str],
              with_clients: bool = True) -> Dict[str, Any]:
    from .sandboxes import iso  # local import: sandboxes imports this module
    data: Dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "packages": packages(row),
        "setupScript": row["setup_script"] or "",
        "status": status(row, base_id),
        "outdatedReasons": outdated_reasons(row, base_id),
        "image": row["image"],
        "imageId": row["image_id"],
        "imageSizeBytes": row["image_size_bytes"],
        "builtAt": iso(row["built_at"]),
        "build": {
            "seq": row["build_seq"],
            "state": row["build_state"],
            "startedAt": iso(row["build_started_at"]),
            "finishedAt": iso(row["build_finished_at"]),
            "error": row["build_error"],
            "actor": row["build_actor"],
        },
        "createdAt": iso(row["created_at"]),
        "updatedAt": iso(row["updated_at"]),
    }
    if with_clients:
        data["clients"] = [{"id": c["id"], "label": c["label"] or "", "default": bool(c["is_default"])}
                           for c in clients_using(conn, row["id"])]
        data["sandboxes"] = sandboxes_using(conn, row["id"])
    return data


STANDARD_NAME = "standard"
STANDARD_DESCRIPTION = ("Shipped image without extra software: Ubuntu desktop with Firefox, Chrome, Python 3 and "
                        "Node.js; more can be installed with apt, pip or npm if the client has internet access.")
AI_PACKAGES_MAX = 100


def ai_view(row: Optional[sqlite3.Row], default: bool) -> Optional[Dict[str, Any]]:
    """What ALBERT | AI learns about a profile it may choose (None = not offered:
    never built, or the last build failed without an image). No setup script."""
    if row is None:
        return {"name": STANDARD_NAME, "description": STANDARD_DESCRIPTION, "packages": [],
                "default": default, "state": "ready"}
    if row["image"]:
        state = "ready"
    elif row["build_state"] in BUILD_ACTIVE:
        state = "building"
    else:
        return None
    names = [p for kind in ("apt", "pip", "npm") for p in packages(row)[kind]][:AI_PACKAGES_MAX]
    return {"name": row["name"], "description": row["description"] or "", "packages": names,
            "default": default, "state": state}


def summary(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Short form for sandbox objects and the client API."""
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "description": row["description"] or "",
            "packages": packages(row)}


def expire_stale(conn: sqlite3.Connection) -> int:
    """Mark builds whose job stopped sending heartbeats as failed."""
    limit = int(time.time()) - STALE_SECONDS
    return conn.execute(
        "UPDATE profiles SET build_state='failed', build_finished_at=?, "
        "build_error='Build interrupted (the build job stopped unexpectedly). Start it again.' "
        "WHERE build_state IN ('queued','building') AND COALESCE(build_heartbeat_at, build_started_at, 0) < ?",
        (int(time.time()), limit)).rowcount


def image_of(row: Optional[sqlite3.Row]) -> str:
    """Image tag of a profile (None = shipped image); refused while it was never built."""
    if row is None:
        return settings().docker_image
    if not row["image"]:
        raise Unavailable(
            f"The sandbox profile '{row['name']}' has not been built yet, so no sandbox can be created with it. "
            "Choose another profile or ask an administrator to build it (admin UI: Profile).",
            code="profile_not_ready")
    return row["image"]


def target_image(conn: sqlite3.Connection, client: sqlite3.Row) -> Tuple[str, Optional[sqlite3.Row]]:
    """Image tag of the client's default profile, and the profile."""
    row = get(conn, client["profile_id"]) if client["profile_id"] else None
    return image_of(row), row


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def profile_dir(profile_id: int) -> Path:
    return settings().profiles_dir / str(int(profile_id))


def log_path(profile_id: int, seq: int) -> Path:
    return profile_dir(profile_id) / f"build-{int(seq)}.log"


def image_tag(profile_id: int, seq: int) -> str:
    return f"{settings().profile_image_prefix}-{int(profile_id)}:{int(seq)}"


def dockerfile(row: sqlite3.Row) -> str:
    pk = packages(row)
    lines = [
        f"# Generated by the ALBERT sandbox manager for profile {row['id']} ({row['name']}) - do not edit.",
        f"FROM {settings().docker_image}",
        "USER root",
        "ENV DEBIAN_FRONTEND=noninteractive",
    ]
    if pk["apt"]:
        lines.append("RUN apt-get update && apt-get install -y " + " ".join(shlex.quote(p) for p in pk["apt"])
                     + " && apt-get clean && rm -rf /var/lib/apt/lists/*")
    if pk["pip"]:
        # system-wide on purpose (the sandbox user should see the packages); no root warning in the log
        lines.append("RUN PIP_ROOT_USER_ACTION=ignore pip3 install --no-cache-dir "
                     + " ".join(shlex.quote(p) for p in pk["pip"]))
    if pk["npm"]:
        lines.append("RUN npm install -g --no-fund --no-audit " + " ".join(shlex.quote(p) for p in pk["npm"]))
    if (row["setup_script"] or "").strip():
        lines += ["COPY setup.sh /tmp/albert-profile-setup.sh",
                  "RUN bash -euo pipefail /tmp/albert-profile-setup.sh && rm -f /tmp/albert-profile-setup.sh"]
    lines.append("USER root")
    return "\n".join(lines) + "\n"


def write_context(row: sqlite3.Row, seq: int) -> Path:
    ctx = profile_dir(row["id"]) / f"context-{int(seq)}"
    shutil.rmtree(ctx, ignore_errors=True)
    ctx.mkdir(parents=True)
    (ctx / "Dockerfile").write_text(dockerfile(row), encoding="utf-8")
    if (row["setup_script"] or "").strip():
        (ctx / "setup.sh").write_text(row["setup_script"].rstrip("\n") + "\n", encoding="utf-8")
    return ctx


def request_build(profile_id: int, actor: str, spawn: bool = True) -> sqlite3.Row:
    """Queue a build of the profile and start the build job (unless ``spawn`` is False)."""
    with db.session() as conn:
        expire_stale(conn)
        row = get(conn, profile_id)
        if row["build_state"] in BUILD_ACTIVE:
            raise Conflict(f"profile '{row['name']}' is already being built")
        if not base_image_id():
            raise SandboxError(f"Docker image '{settings().docker_image}' not found. Run 'build' first.",
                               code="image_missing")
        now = int(time.time())
        seq = row["build_seq"] + 1
        claimed = conn.execute(
            "UPDATE profiles SET build_seq=?, build_state='queued', build_started_at=?, build_heartbeat_at=?, "
            "build_finished_at=NULL, build_error=NULL, build_actor=? WHERE id=? AND build_seq=?",
            (seq, now, now, actor, profile_id, row["build_seq"])).rowcount
        if claimed != 1:
            raise Conflict(f"profile '{row['name']}' is already being built")
        db.audit(conn, actor, "profile.build", row["name"], {"profileId": profile_id, "seq": seq})
        row = get(conn, profile_id)
    profile_dir(profile_id).mkdir(parents=True, exist_ok=True)
    log_path(profile_id, seq).write_text("", encoding="utf-8")
    if spawn:
        try:
            spawn_build(profile_id, seq)
        except Exception as exc:
            _finish(profile_id, seq, ok=False, error=f"could not start the build job: {exc}")
            raise SandboxError(f"could not start the build job: {exc}", code="build_failed")
    return row


def spawn_build(profile_id: int, seq: int) -> None:
    """Run ``profile build-job`` outside this process (survives manager restarts)."""
    package_root = str(Path(__file__).resolve().parent.parent)
    cmd = [sys.executable, "-m", "albert_sandbox.cli", "profile", "build-job", str(int(profile_id)), str(int(seq))]
    env = {k: v for k, v in os.environ.items() if k.startswith(("ALBERT_", "MANAGER_"))}
    if shutil.which("systemd-run") and hasattr(os, "geteuid") and os.geteuid() == 0:
        args = ["systemd-run", "--quiet", "--collect", f"--unit=albert-profile-build-{int(profile_id)}-{int(seq)}",
                f"--description=ALBERT sandbox profile build {int(profile_id)}/{int(seq)}",
                f"--setenv=PYTHONPATH={package_root}", "--setenv=PYTHONUNBUFFERED=1"]
        args += [f"--setenv={k}={v}" for k, v in sorted(env.items())]
        proc = subprocess.run(args + cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"systemd-run exit {proc.returncode}")
        return
    subprocess.Popen(cmd, env={**os.environ, "PYTHONPATH": package_root}, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def _finish(profile_id: int, seq: int, ok: bool, error: Optional[str] = None,
            image: Optional[Dict[str, Any]] = None) -> None:
    now = int(time.time())
    with db.session() as conn:
        if ok and image:
            conn.execute(
                "UPDATE profiles SET image=?, image_id=?, image_config_hash=?, image_base_id=?, "
                "image_size_bytes=?, built_at=?, build_state='ok', build_finished_at=?, build_error=NULL "
                "WHERE id=? AND build_seq=?",
                (image["tag"], image["id"], image["config_hash"], image["base_id"], image["size"], now, now,
                 profile_id, seq))
        else:
            conn.execute("UPDATE profiles SET build_state='failed', build_finished_at=?, build_error=? "
                         "WHERE id=? AND build_seq=?", (now, (error or "build failed")[:4000], profile_id, seq))
        row = conn.execute("SELECT name FROM profiles WHERE id=?", (profile_id,)).fetchone()
        db.audit(conn, "build", "profile.build_ok" if ok else "profile.build_failed",
                 row["name"] if row else str(profile_id),
                 {"profileId": profile_id, "seq": seq, **({"error": (error or "")[:500]} if not ok else {})})


class _Heartbeat:
    """Keeps build_heartbeat_at fresh while a build job is alive."""

    def __init__(self, profile_id: int, seq: int) -> None:
        self.profile_id, self.seq = profile_id, seq
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def beat(self) -> None:
        with db.session() as conn:
            conn.execute("UPDATE profiles SET build_heartbeat_at=? WHERE id=? AND build_seq=? "
                         "AND build_state IN ('queued','building')", (int(time.time()), self.profile_id, self.seq))

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self.beat()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "_Heartbeat":
        self.beat()
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def run_build(profile_id: int, seq: int, echo: Optional[Callable[[str], None]] = None) -> bool:
    """The build job: waits for the build lock, runs docker build, records the result."""
    s = settings()
    path = log_path(profile_id, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    tail: List[str] = []

    with open(path, "a", encoding="utf-8") as log, _Heartbeat(profile_id, seq):
        def write(line: str) -> None:
            log.write(line + "\n")
            log.flush()
            tail.append(line)
            del tail[:-25]
            if echo:
                echo(line)

        def current() -> Optional[sqlite3.Row]:
            with db.session() as conn:
                row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            return row if row and row["build_seq"] == seq and row["build_state"] in BUILD_ACTIVE else None

        if current() is None:
            write("Build was cancelled or replaced by a newer one.")
            return False
        lock_path = s.data_dir / ".profile-build.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock:
            waiting = False
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting:
                        write("Waiting for another profile build to finish ...")
                        waiting = True
                    time.sleep(2)
            row = current()
            if row is None:
                write("Build was cancelled or replaced by a newer one.")
                return False
            with db.session() as conn:
                conn.execute("UPDATE profiles SET build_state='building', build_heartbeat_at=? "
                             "WHERE id=? AND build_seq=?", (int(time.time()), profile_id, seq))
            ctx: Optional[Path] = None
            try:
                base_id = base_image_id()
                if not base_id:
                    raise SandboxError(f"base image {s.docker_image} not found")
                ctx = write_context(row, seq)
                tag = image_tag(profile_id, seq)
                write(f"=== Profile '{row['name']}', build {seq}: {tag} from {s.docker_image} ({base_id[:19]})")
                for line in dockerfile(row).splitlines():
                    write(f"    {line}")
                started = time.monotonic()
                code = dockerctl.build_streaming(tag, str(ctx), {LABEL_PROFILE: str(profile_id),
                                                                 LABEL_PROFILE_SEQ: str(seq)},
                                                 write, s.profile_build_timeout)
                if code == 124:
                    raise SandboxError(f"build timed out after {s.profile_build_timeout}s")
                if code != 0:
                    raise SandboxError(f"docker build failed (exit {code})")
                info = dockerctl.image_inspect(tag) or {}
                image = {"tag": tag, "id": info.get("Id") or dockerctl.image_id(tag), "size": info.get("Size"),
                         "config_hash": config_hash(row), "base_id": base_id}
                if not image["id"]:
                    raise SandboxError("image missing after a successful build")
                write(f"=== Done in {int(time.monotonic() - started)}s: {tag}")
                _finish(profile_id, seq, ok=True, image=image)
                return True
            except Exception as exc:
                message = exc.message if isinstance(exc, SandboxError) else str(exc)
                write(f"=== FAILED: {message}")
                _finish(profile_id, seq, ok=False, error=message + "\n" + "\n".join(tail[-15:]))
                return False
            finally:
                if ctx is not None:
                    shutil.rmtree(ctx, ignore_errors=True)
                _prune_logs(profile_id)


def _prune_logs(profile_id: int) -> None:
    logs = sorted(profile_dir(profile_id).glob("build-*.log"),
                  key=lambda p: int(p.stem.split("-", 1)[1]) if p.stem.split("-", 1)[1].isdigit() else 0)
    for old in logs[:-KEEP_LOGS]:
        old.unlink(missing_ok=True)


def read_log(profile_id: int, seq: Optional[int] = None, offset: int = 0,
             limit: int = 256 * 1024) -> Dict[str, Any]:
    with db.session() as conn:
        expire_stale(conn)
        row = get(conn, profile_id)
    seq = int(seq or row["build_seq"])
    path = log_path(profile_id, seq)
    text, size = "", 0
    if path.exists():
        size = path.stat().st_size
        offset = max(0, min(int(offset), size))
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(limit)
        text = chunk.decode("utf-8", "replace")
        offset += len(chunk)
    running = seq == row["build_seq"] and row["build_state"] in BUILD_ACTIVE
    return {"seq": seq, "offset": offset, "size": size, "text": text, "running": running,
            "state": row["build_state"] if seq == row["build_seq"] else None}


def rebuild_outdated(actor: str, echo: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Rebuild every profile whose image was built on an older base image
    (install.sh, after the shipped image was updated). Profiles that are only
    outdated because an admin changed them are left to the admin."""
    base_id = base_image_id()
    with db.session() as conn:
        todo = [r["id"] for r in list_all(conn)
                if r["image"] and r["build_state"] not in BUILD_ACTIVE and "base" in outdated_reasons(r, base_id)]
    results: Dict[str, str] = {}
    for profile_id in todo:
        try:
            row = request_build(profile_id, actor, spawn=False)
        except SandboxError as exc:
            results[str(profile_id)] = exc.message
            continue
        ok = run_build(profile_id, row["build_seq"], echo=echo)
        results[row["name"]] = "ok" if ok else "failed"
    return results


# --------------------------------------------------------------------------
# cleanup (reconcile)
# --------------------------------------------------------------------------

def unused_images(container_image_ids: Iterable[str]) -> List[Dict[str, str]]:
    """Profile images neither current for a profile nor used by any container."""
    with db.session() as conn:
        rows = conn.execute("SELECT id, image, image_id, build_state FROM profiles").fetchall()
    if any(r["build_state"] in BUILD_ACTIVE for r in rows):
        return []  # a build may be about to tag its image
    keep_refs = {r["image"] for r in rows if r["image"]}
    keep_ids = set(container_image_ids)
    result = []
    for image in dockerctl.images_with_label(LABEL_PROFILE):
        if image["ref"] in keep_refs or image["id"] in keep_ids:
            continue
        if not image["ref"].startswith(settings().profile_image_prefix + "-"):
            continue
        result.append(image)
    return result


def stray_dirs() -> List[Path]:
    """Build directories of deleted profiles."""
    root = settings().profiles_dir
    if not root.exists():
        return []
    with db.session() as conn:
        ids = {str(r["id"]) for r in conn.execute("SELECT id FROM profiles")}
    return [p for p in root.iterdir() if p.is_dir() and p.name not in ids]
