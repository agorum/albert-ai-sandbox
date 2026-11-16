"""Sandbox lifecycle: create, start, stop, restart, remove, resize.

Every operation keeps database, Docker container, disk image and nginx route
consistent. Failures during creation are rolled back completely; failures
during removal leave the sandbox in state ``removing`` so the reconcile step
finishes the job later. Nothing is ever left behind silently.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import access, clients, db, dockerctl, lifecycle, netctl, nginxctl, ports, profiles, storage
from .config import settings
from .errors import BadRequest, Conflict, Forbidden, HostFull, NotFound, QuotaExceeded, SandboxError, Unavailable
from .locking import host_lock

STATE_CREATING = "creating"
STATE_READY = "ready"
STATE_REMOVING = "removing"
STATE_ERROR = "error"  # rebuild failed; kept (with data) until an admin acts
STATE_REBUILDING = "rebuilding"  # recreate in progress; never touched by reconcile

CONTAINER_PORTS = {"novnc": 6081, "mcphub": 3000, "filesvc": 4000}
# Working directory of the AI in a sandbox: the file service's directory, the
# only one whose files can be handed to the user (upload/download).
WORK_DIR = "/tmp/albert-files"
LEGACY_VNC_PASSWORD = "albert"
# Containers get the internal tokens of MCP Hub and file service (nginx sends
# them, see nginxctl/access). Older containers lack the label -> "auth".
AUTH_LABEL = "albert.auth"
AUTH_VERSION = "2"


def new_credentials() -> Dict[str, str]:
    """Fresh VNC password and internal tokens (create, and every rebuild)."""
    return {"vnc_password": secrets.token_urlsafe(6)[:8], "filesvc_token": secrets.token_hex(16),
            "mcphub_token": secrets.token_urlsafe(32)}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def parse_time(value: Optional[str]) -> Optional[float]:
    """Parse ISO-8601 / docker timestamps (with nanoseconds, 'Z')."""
    if not value or value.startswith("0001-01-01"):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
    if match:
        head, frac, tz = match.groups()
        frac = (frac or "")[:7]  # '.' + max 6 digits
        text = head + frac + tz
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def host_cpus() -> int:
    return os.cpu_count() or 1


def _meminfo_mb(field: str) -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def host_memory_mb() -> int:
    return _meminfo_mb("MemTotal")


def host_available_mb() -> int:
    """Memory the host could still hand out right now (MemAvailable); 0 = unknown."""
    return _meminfo_mb("MemAvailable")


def memory_guard(conn: sqlite3.Connection, running: Iterable[str], exclude: Optional[str] = None) -> Dict[str, Any]:
    """RAM limits promised to running sandboxes (all clients) against what the host may promise:
    ALBERT_MEMORY_OVERCOMMIT x (host RAM - ALBERT_MEMORY_RESERVE_MB). Sandboxes without a RAM
    limit (migrated from the former version) count with the default limit."""
    s = settings()
    running_set = set(running)
    committed = 0
    for row in conn.execute("SELECT name, state, memory_mb FROM sandboxes"):
        if exclude and row["name"] == exclude:
            continue
        if row["name"] in running_set or row["state"] == STATE_CREATING:
            committed += int(row["memory_mb"] or s.default_memory_mb)
    total = host_memory_mb()
    limit = int(max(0, total - s.memory_reserve_mb) * s.memory_overcommit) \
        if s.memory_overcommit > 0 and total else None
    return {"committedMb": committed, "limitMb": limit, "hostMb": total, "availableMb": host_available_mb(),
            "factor": s.memory_overcommit, "reserveMb": s.memory_reserve_mb}


def check_host_memory(conn: sqlite3.Connection, running: Iterable[str], add_memory_mb: int, *,
                      client_id: Optional[int] = None, owner: Optional[str] = None,
                      exclude: Optional[str] = None) -> None:
    """Refuse another running sandbox (or more RAM for one) the host cannot hold."""
    s = settings()
    if s.memory_overcommit <= 0 or add_memory_mb <= 0:
        return
    running = set(running)
    guard = memory_guard(conn, running, exclude=exclude)
    own = clients.usage(conn, client_id, running, owner=owner)["runningNames"] if client_id is not None else []
    hint = (("Stop one of your running sandboxes you do not need (" + ", ".join(own)
             + ", POST /containers/<name>/stop) or try again later.") if own
            else "Try again later or ask an administrator.")
    details = {"resource": "hostMemory", "limit": guard["limitMb"], "used": guard["committedMb"],
               "requested": add_memory_mb, "availableMb": guard["availableMb"], "runningSandboxes": own}
    if guard["limitMb"] is not None and guard["committedMb"] + add_memory_mb > guard["limitMb"]:
        raise HostFull(
            f"The sandbox server is fully booked: running sandboxes already have {guard['committedMb']} MB of RAM, "
            f"another {add_memory_mb} MB would exceed what this host can hold ({guard['limitMb']} MB). " + hint,
            details=details)
    if guard["availableMb"] and guard["availableMb"] < s.memory_reserve_mb:
        raise HostFull(f"The sandbox server is short of memory right now ({guard['availableMb']} MB free). " + hint,
                       details=details)


def normalize_requested_name(text: str) -> str:
    sanitized = re.sub(r"[^a-z0-9.-]", "-", (text or "").lower())
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    sanitized = re.sub(r"\.{2,}", ".", sanitized)
    sanitized = sanitized.strip("-.")
    return sanitized[:42].strip("-.") or "custom"


def generate_name(conn: sqlite3.Connection, hint: Optional[str] = None) -> str:
    prefix = "sbx"
    if hint:
        prefix = f"sbx-{normalize_requested_name(hint)}"
    existing_docker = set(dockerctl.managed_container_names())
    for _ in range(20):
        name = f"{prefix}-{secrets.token_hex(8)}"
        taken = conn.execute("SELECT 1 FROM sandboxes WHERE name=?", (name,)).fetchone()
        if not taken and name not in existing_docker:
            return name
    raise SandboxError("could not generate a unique sandbox name")


def resolve_resources(client: sqlite3.Row, requested: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    defaults = clients.sandbox_defaults(client)
    requested = requested or {}

    def pick(*keys: str) -> Any:
        for key in keys:
            if requested.get(key) not in (None, ""):
                return requested[key]
        return None

    try:
        cpus = float(pick("cpus") or defaults["cpus"])
        memory_mb = int(float(pick("memoryMb", "memory_mb") or defaults["memory_mb"]))
        disk_gb = int(float(pick("diskGb", "disk_gb") or defaults["disk_gb"]))
    except (TypeError, ValueError):
        raise BadRequest("resources must be numeric (cpus, memoryMb, diskGb)")
    # The client's per-sandbox limit is a ceiling: requests may ask for less only.
    for key, value, limit, unit in (("cpus", cpus, defaults["cpus"], "cores"),
                                    ("memoryMb", memory_mb, defaults["memory_mb"], "MB"),
                                    ("diskGb", disk_gb, defaults["disk_gb"], "GB")):
        if value > limit + 1e-9:
            raise QuotaExceeded(
                f"Requested {key}={value:g} exceeds the per-sandbox limit of this client ({limit:g} {unit})",
                code="sandbox_limit_exceeded", details={"resource": key, "limit": limit, "requested": value})
    validate_resources(cpus, memory_mb, disk_gb)
    return {"cpus": round(cpus, 2), "memory_mb": memory_mb, "disk_gb": disk_gb}


def validate_resources(cpus: Optional[float], memory_mb: Optional[int], disk_gb: Optional[int]) -> None:
    s = settings()
    if cpus is not None and not (0.1 <= cpus <= host_cpus()):
        raise BadRequest(f"cpus must be between 0.1 and {host_cpus()} (host cores)")
    total_mem = host_memory_mb()
    if memory_mb is not None:
        if memory_mb < s.min_memory_mb:
            raise BadRequest(f"memoryMb must be at least {s.min_memory_mb}")
        if total_mem and memory_mb > total_mem:
            raise BadRequest(f"memoryMb must not exceed host memory ({total_mem} MB)")
    if disk_gb is not None and not (1 <= disk_gb <= s.max_disk_gb):
        raise BadRequest(f"diskGb must be between 1 and {s.max_disk_gb}")


def urls(name: str) -> Dict[str, str]:
    base = access.public_url()
    return {
        "desktop": f"{base}/{name}/",
        "mcphub": f"{base}/{name}/mcphub/mcp",
        "files": f"{base}/{name}/files/",
        "filesUpload": f"{base}/{name}/files/upload",
        "filesDownloadPattern": f"{base}/{name}/files/download?path={WORK_DIR}/<uuid.ext>",
    }


def _port_str(value: Optional[int]) -> str:
    return str(value) if value else ""


def _docker_time(value: Optional[str]) -> Optional[str]:
    if not value or value.startswith("0001-01-01"):
        return None
    return value


def owner_context(conn: sqlite3.Connection, client_ids: Optional[List[int]] = None) -> Dict[int, Dict[str, Any]]:
    """What a client's sandboxes should look like: image id, network, profile, permissions.

    ``client_ids`` None means all clients.
    """
    if client_ids is None:
        owners = conn.execute("SELECT * FROM api_keys").fetchall()
    else:
        ids = sorted(set(client_ids))
        owners = conn.execute(f"SELECT * FROM api_keys WHERE id IN ({','.join('?' for _ in ids)})",
                              ids).fetchall() if ids else []
    result: Dict[int, Dict[str, Any]] = {}
    if not owners:
        return result
    glob = lifecycle.global_values(conn)
    # Profiles are few: load them once; the shipped image id only when needed.
    profile_rows = {r["id"]: r for r in conn.execute("SELECT * FROM profiles")}
    base = {"id": None, "loaded": False}

    def base_id() -> Optional[str]:
        if not base["loaded"]:
            base["id"], base["loaded"] = dockerctl.image_id(settings().docker_image) or None, True
        return base["id"]

    for owner in owners:
        default_row = profile_rows.get(owner["profile_id"]) if owner["profile_id"] else None
        result[owner["id"]] = {
            "profile_rows": profile_rows,
            "base_id": base_id,
            "allowed_ids": set(clients.allowed_profile_ids(conn, owner["id"])),
            "allow_standard": bool(owner["allow_standard_image"]),
            "offline": not clients.internet_allowed(owner),
            "placement": netctl.placement(owner),
            "profile": profiles.summary(default_row),
            "persistent_allowed": clients.persistent_allowed(owner),
            "label": owner["label"] or "",
            "lifecycle": lifecycle.effective(owner, glob),
        }
    return result


def serialize(row: sqlite3.Row, info: Optional[Dict[str, Any]] = None, *,
              context: Optional[Dict[str, Any]] = None, with_client_label: bool = False) -> Dict[str, Any]:
    """Public representation; keeps the field names of the previous API.

    ``context`` is the owner's entry from ``owner_context`` (expected image and
    network, profile, persistence permission). VNC password and internal
    tokens are never part of it: nginx uses them, clients do not need them.
    """
    context = context or {}
    name = row["name"]
    state = (info or {}).get("State") or {}
    running = bool(state.get("Running"))
    if row["state"] in (STATE_CREATING, STATE_REMOVING, STATE_REBUILDING, STATE_ERROR):
        status = row["state"]
    elif running:
        status = "running"
    else:
        status = "stopped"
    data: Dict[str, Any] = {
        "id": (info or {}).get("Id") or row["container_id"],
        "name": name,
        "sandboxName": name,
        "image": ((info or {}).get("Config") or {}).get("Image") or row["image"],
        "status": status,
        "running": running,
        "startedAt": _docker_time(state.get("StartedAt")),
        "finishedAt": _docker_time(state.get("FinishedAt")),
        "createdAt": iso(row["created_at"]),
        "persistent": bool(row["persistent"]),
        "ports": {
            "novnc": _port_str(row["novnc_port"]),
            "vnc": _port_str(row["vnc_port"]),
            "mcphub": _port_str(row["mcphub_port"]),
            "filesvc": _port_str(row["filesvc_port"]),
        },
        "urls": urls(name),
        # where the AI works and stores results (the file service's directory)
        "workDir": WORK_DIR,
        "resources": {
            "cpus": row["cpus"],
            "memoryMb": row["memory_mb"],
            "diskGb": row["disk_gb"],
            "storage": row["storage"],
        },
        "usage": {
            "diskUsedBytes": row["disk_used_bytes"],
            "rootfsUsedBytes": row["rootfs_used_bytes"],
            "updatedAt": iso(row["usage_updated_at"]),
        },
        "lastActivityAt": iso(row["last_activity_at"]),
        "clientId": row["api_key_id"],
        # Pseudonymous ALBERT user the sandbox is bound to (X-Albert-User), None = no binding.
        "user": row["owner"],
    }
    profile_row = None
    if context:
        pid = _row_profile_id(row)
        profile_row = context["profile_rows"].get(pid) if pid else None
        expected = profile_row["image_id"] if profile_row else (context["base_id"]() if not pid else None)
        allowed = context["allow_standard"] if not pid else pid in context["allowed_ids"]
    else:
        expected, allowed = None, True
    # Non-empty: the container predates the current image/hardening/network
    # setup ("recreate" rebuilds it with the same data, ports and credentials),
    # or its profile is no longer allowed for the client ("profile": a rebuild
    # switches it to the client's default profile).
    data["outdated"] = outdated_reasons(info, expected, context.get("placement") or bool(context.get("offline"))) \
        + ([] if allowed else ["profile"])
    if context:
        # Internet access of the client, software profile of this sandbox (None = shipped image).
        data["internet"] = not context["offline"]
        data["profile"] = profiles.summary(profile_row)
        # Whether the owning client may switch this sandbox to persistent.
        data["persistentAllowed"] = context["persistent_allowed"]
        if with_client_label:
            data["clientLabel"] = context["label"]
        # Lifetime: stopped when idle (all), removed by age (non-persistent only).
        eff = context["lifecycle"]
        due = lifecycle.deadlines(row, eff)
        data["lifecycle"] = {"stopAfterIdleSeconds": eff["inactivitySeconds"],
                             "deleteWhenUnusedAfter": iso(due["idle"]), "deleteAtLatest": iso(due["latest"])}
        data["expiresAt"] = iso(due["latest"] or due["idle"])
    if row["error"]:
        data["lastError"] = row["error"]
    return data


def get_row(conn: sqlite3.Connection, name: str, client_id: Optional[int] = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
    if not row or (client_id is not None and row["api_key_id"] != client_id):
        raise NotFound(f"Container '{name}' not found", code="not_found")
    return row


def present(conn: sqlite3.Connection, name: str) -> Dict[str, Any]:
    """Current public representation of one sandbox."""
    row = get_row(conn, name)
    context = owner_context(conn, [row["api_key_id"]]).get(row["api_key_id"])
    return serialize(row, dockerctl.inspect_one(name), context=context)


def _offline(client: sqlite3.Row) -> bool:
    return not clients.internet_allowed(client)


def _placement(client: sqlite3.Row) -> "netctl.Placement":
    """Network of the client's sandboxes (shared online/offline network or its own one)."""
    return netctl.placement(client)


def _running() -> set:
    try:
        return dockerctl.running_names()
    except dockerctl.DockerError as exc:
        raise Unavailable(f"Docker not reachable: {exc}")


def wait_for_mcphub(port: Optional[int]) -> bool:
    if not port:
        return True
    s = settings()
    deadline = time.monotonic() + s.mcphub_timeout
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                pass
            break
        except urllib.error.HTTPError:
            break  # any HTTP answer means the hub is up
        except (urllib.error.URLError, OSError):
            time.sleep(s.mcphub_poll_interval)
    else:
        return False
    if s.mcphub_settle_seconds:
        time.sleep(s.mcphub_settle_seconds)
    return True


# Hardening applied to every sandbox container. Chrome runs with --no-sandbox,
# so neither CAP_SYS_ADMIN nor an unconfined seccomp profile is needed.
# --init gives the container a real PID 1 that forwards SIGTERM (fast, clean
# stop); NET_RAW/MKNOD are not needed by the desktop and ease attacks on the
# bridge (ARP/packet spoofing) or device creation.
HARDENING_ARGS = ["--init", "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD"]


def _docker_run_args(row: sqlite3.Row, client: sqlite3.Row, image: Optional[str] = None) -> List[str]:
    s = settings()
    name = row["name"]
    args = [
        "--name", name,
        "--hostname", name[:63],
        "--label", f"{dockerctl.LABEL_MANAGER}=1",
        "--label", f"{dockerctl.LABEL_SANDBOX}={name}",
        "--label", f"{dockerctl.LABEL_CLIENT}={client['id']}",
        "--label", f"{dockerctl.LABEL_APIKEY_HASH}={client['key_hash']}",
        "--label", f"{AUTH_LABEL}={AUTH_VERSION}",
        "--restart", "unless-stopped",
        *netctl.run_args(_placement(client)),
        *HARDENING_ARGS,
        # If the host runs out of memory, processes in sandboxes are ended first
        # (manager and nginx have -800); not part of the "outdated" check.
        "--oom-score-adj", "500",
        "--pids-limit", str(s.pids_limit),
    ]
    if row["cpus"]:
        args += ["--cpus", f"{float(row['cpus']):g}"]
    memory_mb = int(row["memory_mb"]) if row["memory_mb"] else None
    if memory_mb:
        args += ["--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m"]
    shm_mb = max(64, min(s.shm_max_mb, memory_mb // 2)) if memory_mb else s.shm_max_mb
    args += [
        "--shm-size", f"{shm_mb}m",
        "-p", f"127.0.0.1:{row['novnc_port']}:{CONTAINER_PORTS['novnc']}",
        "-p", f"127.0.0.1:{row['mcphub_port']}:{CONTAINER_PORTS['mcphub']}",
        "-p", f"127.0.0.1:{row['filesvc_port']}:{CONTAINER_PORTS['filesvc']}",
        "-e", "VNC_PORT=5901",
        "-e", "NO_VNC_PORT=6081",
        "-e", "MCP_HUB_PORT=3000",
        "-e", "FILE_SERVICE_PORT=4000",
        "-e", f"VNC_PASSWORD={row['vnc_password']}",
    ]
    # Internal tokens: the file service and MCP Hub only answer requests that
    # nginx forwards after the manager checked the client's API key.
    if row["filesvc_token"]:
        args += ["-e", f"FILE_SERVICE_TOKEN={row['filesvc_token']}"]
    if row["mcphub_token"]:
        args += ["-e", f"MCPHUB_BEARER_TOKEN={row['mcphub_token']}"]
    if row["storage"] == "volume":
        # Sandbox migrated from the former version: data stays in its docker volume.
        args += ["-v", f"{name}_data:/home/ubuntu"]
    else:
        args += ["-v", f"{storage.home_path(name)}:/home/ubuntu",
                 "-v", f"{storage.files_path(name)}:{WORK_DIR}"]
    args.append(image or row["image"])
    return args


def outdated_reasons(info: Optional[Dict[str, Any]], current_image_id: Optional[str],
                     offline: "netctl.Target" = False) -> List[str]:
    """Why a container should be rebuilt ('recreate') to get the current setup.

    ``current_image_id`` is the image the owner's sandboxes should run (standard
    or profile image), ``offline`` their network (a Placement, or a bool for the
    shared networks: whether they must be cut off from the internet).
    """
    if not info:
        return []
    reasons: List[str] = []
    host_cfg = info.get("HostConfig") or {}
    if current_image_id and info.get("Image") and info.get("Image") != current_image_id:
        reasons.append("image")
    caps = [c.upper().replace("CAP_", "") for c in (host_cfg.get("CapAdd") or [])]
    dropped = [c.upper().replace("CAP_", "") for c in (host_cfg.get("CapDrop") or [])]
    if "SYS_ADMIN" in caps or any("unconfined" in o for o in (host_cfg.get("SecurityOpt") or [])) \
            or not host_cfg.get("Init") or "NET_RAW" not in dropped:
        reasons.append("hardening")
    if not netctl.is_isolated(info, offline) or not netctl.dns_matches(info, offline):
        reasons.append("network")
    labels = (info.get("Config") or {}).get("Labels") or {}
    if labels.get(AUTH_LABEL) != AUTH_VERSION:
        reasons.append("auth")
    return reasons


def _ensure_route(row: sqlite3.Row) -> None:
    with host_lock():
        if nginxctl.write(row):
            nginxctl.reload()


def _row_profile_id(row: Any) -> Optional[int]:
    try:
        return row["profile_id"]
    except (IndexError, KeyError):
        return None


KEEP = object()  # recreate: keep the sandbox's profile (if still allowed)


def resolve_profile(conn: sqlite3.Connection, client: sqlite3.Row, ref: Any) -> Optional[sqlite3.Row]:
    """Profile for a new sandbox: ``ref`` None = the client's default, "standard" = the
    shipped image, otherwise a profile name (case-insensitive) or id the client may use.
    Returns the profile row (None = shipped image)."""
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        return profiles.get(conn, client["profile_id"]) if client["profile_id"] else None
    text = str(ref).strip()
    row = None
    if text.lower() != profiles.STANDARD_NAME:
        row = conn.execute("SELECT * FROM profiles WHERE name=?", (text,)).fetchone()
        if row is None and text.isdigit():
            row = conn.execute("SELECT * FROM profiles WHERE id=?", (int(text),)).fetchone()
        if row is None:
            raise NotFound(f"profile '{text}' does not exist", code="profile_not_found")
    if not clients.profile_allowed(conn, client, row["id"] if row else None):
        names = [profiles.STANDARD_NAME] if client["allow_standard_image"] else []
        names += [r["name"] for r in conn.execute(
            "SELECT p.name FROM profiles p JOIN client_profiles cp ON cp.profile_id=p.id "
            "WHERE cp.client_id=? ORDER BY p.name COLLATE NOCASE", (client["id"],))]
        raise Forbidden(f"profile '{text}' is not allowed for this client; allowed: {', '.join(names)} "
                        "(GET /profiles lists them)", code="profile_not_allowed", details={"allowed": names})
    return row


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

def create(client_id: int, requested_name: Optional[str] = None, persistent: bool = False,
           resources: Optional[Dict[str, Any]] = None, actor: str = "api",
           owner: Optional[str] = None, profile: Any = None) -> Dict[str, Any]:
    """Create a sandbox. ``owner``: ALBERT user it is bound to (X-Albert-User);
    ``profile``: name of an allowed profile (None = the client's default)."""
    s = settings()
    with db.session() as conn:
        client = clients.get(conn, client_id)
        if persistent:
            _require_persistent_allowed(client)
        res = resolve_resources(client, resources)
        profile_row = resolve_profile(conn, client, profile)
        image = profiles.image_of(profile_row)
        if not dockerctl.image_id(image):
            raise SandboxError(f"Docker image '{image}' not found. Run 'build' first.", code="image_missing")
        # Never start a sandbox without its network isolation in place.
        netctl.ensure_ready(_placement(client))
        with host_lock():
            running = _running()
            clients.check_quota(conn, client, running, owner=owner, add_cpus=res["cpus"],
                                add_memory_mb=res["memory_mb"],
                                add_disk_gb=res["disk_gb"], add_sandboxes=1, add_running=1)
            check_host_memory(conn, running, int(res["memory_mb"] or s.default_memory_mb),
                              client_id=client["id"], owner=owner)
            name = generate_name(conn, requested_name)
            novnc, mcphub, filesvc = ports.allocate(conn, 3)
            now = int(time.time())
            cred = new_credentials()
            with db.transaction(conn):
                conn.execute(
                    """INSERT INTO sandboxes(name, api_key_id, image, state, created_at, persistent,
                         novnc_port, mcphub_port, filesvc_port, vnc_password, filesvc_token, mcphub_token,
                         cpus, memory_mb, disk_gb, storage, last_activity_at, last_started_at, owner, profile_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (name, client_id, image, STATE_CREATING, now, 1 if persistent else 0,
                     novnc, mcphub, filesvc, cred["vnc_password"], cred["filesvc_token"], cred["mcphub_token"],
                     res["cpus"], res["memory_mb"], res["disk_gb"], "image", now, now, owner,
                     profile_row["id"] if profile_row else None),
                )
                db.audit(conn, actor, "sandbox.create", name, {"clientId": client_id, **res,
                                                               "persistent": bool(persistent),
                                                               "profile": profile_row["name"] if profile_row
                                                               else profiles.STANDARD_NAME,
                                                               **({"user": owner} if owner else {})})
        row = get_row(conn, name)
        try:
            storage.create(name, res["disk_gb"])
            storage.populate_home(name, image)
            container_id = dockerctl.run_container(_docker_run_args(row, client))
            conn.execute("UPDATE sandboxes SET container_id=? WHERE name=?", (container_id, name))
            if not wait_for_mcphub(row["mcphub_port"]):
                raise SandboxError(f"MCP Hub in container '{name}' did not become ready within "
                                   f"{s.mcphub_timeout}s", code="mcphub_timeout")
            with host_lock():
                nginxctl.write(row)
                nginxctl.reload()
                conn.execute("UPDATE sandboxes SET state=?, error=NULL WHERE name=?", (STATE_READY, name))
        except Exception as exc:
            _destroy(conn, get_row(conn, name), actor="system", reason=f"create failed: {exc}", quiet=True)
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(f"Sandbox create failed: {exc}", code="create_failed")
        result = present(conn, name)
        result["result"] = "created"
        return result


def describe(name: str, client_id: Optional[int] = None) -> Dict[str, Any]:
    with db.session() as conn:
        get_row(conn, name, client_id)
        return present(conn, name)


def list_sandboxes(client_id: Optional[int] = None, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """All sandboxes, those of a client, or those of one ALBERT user of a client."""
    with db.session() as conn:
        if client_id is None:
            rows = conn.execute("SELECT * FROM sandboxes ORDER BY created_at").fetchall()
        elif owner is not None:
            rows = conn.execute("SELECT * FROM sandboxes WHERE api_key_id=? AND owner=? ORDER BY created_at",
                                (client_id, owner)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sandboxes WHERE api_key_id=? ORDER BY created_at",
                                (client_id,)).fetchall()
        contexts = owner_context(conn, [r["api_key_id"] for r in rows]) if rows else {}
    infos = dockerctl.inspect([r["name"] for r in rows]) if rows else {}
    return [serialize(r, infos.get(r["name"]), context=contexts.get(r["api_key_id"]), with_client_label=True)
            for r in rows]


def start(name: str, client_id: Optional[int] = None, actor: str = "api") -> Dict[str, Any]:
    with db.session() as conn:
        row = get_row(conn, name, client_id)
        if row["state"] != STATE_READY:
            raise Conflict(f"Sandbox '{name}' is {row['state']}")
        info = dockerctl.inspect_one(name)
        if not info:
            raise NotFound(f"Container '{name}' is missing (will be cleaned up)", code="not_found")
        if not (info.get("State") or {}).get("Running"):
            client = clients.get(conn, row["api_key_id"])
            offline = _placement(client)
            netctl.ensure_ready(offline)
            with host_lock():
                running = _running()
                clients.check_quota(conn, client, running, owner=row["owner"], add_cpus=row["cpus"] or 0,
                                    add_memory_mb=row["memory_mb"] or 0, add_running=1, exclude=name)
                check_host_memory(conn, running, int(row["memory_mb"] or settings().default_memory_mb),
                                  client_id=client["id"], owner=row["owner"], exclude=name)
                # The DNS setting is fixed when a container is created: after the
                # client's internet access changed, rebuild it (data is kept).
                rebuild = not netctl.dns_matches(info, offline)
                if not rebuild:
                    if row["storage"] == "image":
                        storage.mount(name)
                    # Containers from older versions sit in the shared default
                    # bridge - move them into their isolated network first.
                    netctl.isolate(name, info, offline)
                    dockerctl.start(name)
                    now = int(time.time())
                    conn.execute("UPDATE sandboxes SET last_started_at=?, last_activity_at=?, error=NULL "
                                 "WHERE name=?", (now, now, name))
                    db.audit(conn, actor, "sandbox.start", name)
            if rebuild:
                return recreate(name, client_id, actor=actor, run=True)
            if not wait_for_mcphub(row["mcphub_port"]):
                raise SandboxError(f"MCP Hub in container '{name}' did not become ready",
                                   code="mcphub_timeout")
        _ensure_route(row)
        return present(conn, name)


def stop(name: str, client_id: Optional[int] = None, actor: str = "api") -> Dict[str, Any]:
    with db.session() as conn:
        row = get_row(conn, name, client_id)
        if row["state"] in (STATE_CREATING, STATE_REBUILDING):
            raise Conflict(f"Sandbox '{name}' is {row['state']}")
        info = dockerctl.inspect_one(name)
        if info and (info.get("State") or {}).get("Running"):
            if actor != "watcher":
                # A stop by a user or admin counts as use (a restart must not look unused
                # to the watcher); the idle stop of the watcher does not.
                conn.execute("UPDATE sandboxes SET last_activity_at=? WHERE name=?", (int(time.time()), name))
            dockerctl.stop(name)
            db.audit(conn, actor, "sandbox.stop", name)
        return present(conn, name)


def restart(name: str, client_id: Optional[int] = None, actor: str = "api") -> Dict[str, Any]:
    stop(name, client_id, actor=actor)
    return start(name, client_id, actor=actor)


def recreate(name: str, client_id: Optional[int] = None, actor: str = "admin",
             force: bool = False, run: Optional[bool] = None, profile: Any = KEEP) -> Dict[str, Any]:
    """Rebuild the container with the current image, hardening and network.

    The image is the current one of the sandbox's profile - or of the client's
    default profile once that profile is no longer allowed; ``profile`` (name,
    "standard") switches to another allowed profile. The network follows the
    owner's internet access. Data, ports and limits stay
    the same; VNC password and internal tokens are renewed (values that ever
    showed up in old chats or logs become worthless), open desktops end. Disk-image sandboxes keep everything (home and
    uploads live on the disk image); sandboxes migrated from the former
    version keep their home volume and get their upload directory copied into
    the new container. A running sandbox is running again afterwards, a
    stopped one stays stopped (``run`` overrides that). If the rebuild fails,
    the previous image is restored; if even that fails the sandbox is set to
    state ``error`` (never removed automatically). ``force`` also allows a
    sandbox stuck in ``rebuilding`` (interrupted run) to be rebuilt again.
    """
    allowed = (STATE_READY, STATE_ERROR, STATE_REBUILDING) if force else (STATE_READY, STATE_ERROR)
    with db.session() as conn:
        row = get_row(conn, name, client_id)
        if row["state"] not in allowed:
            raise Conflict(f"Sandbox '{name}' is {row['state']}")
        client = clients.get(conn, row["api_key_id"])
        if profile is not KEEP:
            profile_row = resolve_profile(conn, client, profile if profile is not None else profiles.STANDARD_NAME)
        elif clients.profile_allowed(conn, client, _row_profile_id(row)):
            profile_row = profiles.get(conn, row["profile_id"]) if _row_profile_id(row) else None
        else:
            profile_row = resolve_profile(conn, client, None)  # no longer allowed: the default
        target_profile_id = profile_row["id"] if profile_row else None
        image = target_image = profiles.image_of(profile_row)
        if not dockerctl.image_id(image):
            raise SandboxError(f"Docker image '{image}' not found. Run 'build' first.", code="image_missing")
        netctl.ensure_ready(_placement(client))
        info = dockerctl.inspect_one(name)
        was_running = bool(info and (info.get("State") or {}).get("Running"))
        start_after = was_running if run is None else run
        old_image = (info or {}).get("Image") or row["image"]
        # 'rebuilding' keeps reconcile away while the sandbox has no container;
        # the conditional update makes concurrent rebuilds of one sandbox impossible.
        placeholders = ",".join("?" for _ in allowed)
        claimed = conn.execute(f"UPDATE sandboxes SET state=? WHERE name=? AND state IN ({placeholders})",
                               (STATE_REBUILDING, name, *allowed)).rowcount
        if claimed != 1:
            raise Conflict(f"Sandbox '{name}' is already being rebuilt")
        try:
            if was_running:
                dockerctl.stop(name)
            uploads = dockerctl.copy_out_tar(name, WORK_DIR) \
                if (info and row["storage"] == "volume") else None
            if row["storage"] == "image":
                storage.mount(name)
        except Exception as exc:
            conn.execute("UPDATE sandboxes SET state=? WHERE name=?", (row["state"], name))
            raise SandboxError(f"Recreate of '{name}' aborted, nothing changed: {exc}", code="recreate_failed")

        rotated = False

        def renew_credentials() -> None:
            """New VNC password and internal tokens - only once the old container is
            gone, so the database never holds tokens a running container does not know."""
            nonlocal rotated
            if rotated:
                return
            cred = new_credentials()
            conn.execute("UPDATE sandboxes SET vnc_password=?, filesvc_token=?, mcphub_token=? WHERE name=?",
                         (cred["vnc_password"], cred["filesvc_token"], cred["mcphub_token"], name))
            access.revoke_desktop(conn, name)
            rotated = True

        def build(img: str) -> str:
            args = _docker_run_args(get_row(conn, name), client, img)
            return dockerctl.run_container(args) if start_after else dockerctl.create_container(args)

        error: Optional[str] = None
        try:
            dockerctl.remove(name)
            renew_credentials()
            container_id = build(image)
        except Exception as exc:
            error = f"recreate with {image} failed: {exc}"
            try:
                dockerctl.remove(name)
                renew_credentials()
                container_id = build(old_image)
                image = old_image
            except Exception as exc2:
                conn.execute("UPDATE sandboxes SET state=?, error=? WHERE name=?",
                             (STATE_ERROR, f"{error}; restoring the old image failed: {exc2}"[:2000], name))
                db.audit(conn, actor, "sandbox.recreate_failed", name, {"error": str(exc2)})
                raise SandboxError(f"Recreate of '{name}' failed and the old container could not be "
                                   f"restored: {exc2}", code="recreate_failed")
        if uploads:
            try:
                dockerctl.copy_in_tar(name, "/tmp", uploads)
            except dockerctl.DockerError as exc:
                error = (error + "; " if error else "") + f"uploads not copied: {exc}"
        now = int(time.time())
        conn.execute(
            "UPDATE sandboxes SET state=?, container_id=?, image=?, vnc_port=NULL, error=?, "
            "last_started_at=CASE WHEN ? THEN ? ELSE last_started_at END, "
            "last_activity_at=CASE WHEN ? THEN ? ELSE last_activity_at END, "
            "profile_id=CASE WHEN ? THEN ? ELSE profile_id END WHERE name=?",
            (STATE_READY, container_id, image, error, 1 if start_after else 0, now,
             1 if start_after and not was_running else 0, now,
             1 if image == target_image else 0, target_profile_id, name),  # not on fallback to the old image
        )
        db.audit(conn, actor, "sandbox.recreate", name, {"image": image, "error": error,
                                                         "profile": profile_row["name"] if profile_row
                                                         else profiles.STANDARD_NAME,
                                                         "internet": not _offline(client)})
        if start_after:
            wait_for_mcphub(row["mcphub_port"])
        _ensure_route(get_row(conn, name))
        if error:
            raise SandboxError(f"Sandbox '{name}' was rebuilt with problems: {error}", code="recreate_partial")
        return present(conn, name)


def _require_persistent_allowed(client: sqlite3.Row) -> None:
    if clients.persistent_allowed(client):
        return
    with db.session() as conn:
        eff = lifecycle.effective(client, lifecycle.global_values(conn))
    rules = []
    if eff["maxAgeSeconds"]:
        rules.append(f"removed automatically once it is not used any more {eff['maxAgeSeconds'] / 3600:g} hours "
                     "after creation")
    if eff["maxLifetimeSeconds"]:
        rules.append(f"at the latest {eff['maxLifetimeSeconds'] / 3600:g} hours after creation")
    raise Forbidden(
        "Persistent sandboxes are not allowed for this client. Use a non-persistent sandbox instead"
        + (f" (it is {', '.join(rules)})" if rules else "")
        + " or ask an administrator to allow persistent sandboxes for this client.",
        code="persistent_not_allowed")


def set_persistent(name: str, persistent: bool, client_id: Optional[int] = None,
                   actor: str = "api") -> Dict[str, Any]:
    with db.session() as conn:
        row = get_row(conn, name, client_id)
        if persistent and not row["persistent"]:
            # Switching it off is always possible, also after the permission was withdrawn.
            _require_persistent_allowed(clients.get(conn, row["api_key_id"]))
        conn.execute("UPDATE sandboxes SET persistent=? WHERE name=?", (1 if persistent else 0, name))
        db.audit(conn, actor, "sandbox.persistent", name, {"persistent": bool(persistent)})
        return {"name": name, "persistent": bool(persistent)}


def update_resources(name: str, cpus: Optional[float] = None, memory_mb: Optional[int] = None,
                     disk_gb: Optional[int] = None, client_id: Optional[int] = None,
                     actor: str = "admin") -> Dict[str, Any]:
    validate_resources(cpus, memory_mb, disk_gb)
    with db.session() as conn:
        row = get_row(conn, name, client_id)
        if row["state"] != STATE_READY:
            raise Conflict(f"Sandbox '{name}' is {row['state']}")
        client = clients.get(conn, row["api_key_id"])
        if disk_gb is not None and row["storage"] != "image":
            raise BadRequest("legacy sandboxes (docker volume) have no disk limit; recreate to use one")
        if disk_gb is not None and row["disk_gb"] and disk_gb < row["disk_gb"]:
            raise BadRequest("the disk of a sandbox can only grow")
        with host_lock():
            running = _running()
            is_running = name in running
            add_cpus = (cpus - (row["cpus"] or 0)) if (cpus is not None and is_running) else 0
            add_mem = (memory_mb - (row["memory_mb"] or 0)) if (memory_mb is not None and is_running) else 0
            add_disk = (disk_gb - (row["disk_gb"] or 0)) if disk_gb is not None else 0
            clients.check_quota(conn, client, running, add_cpus=max(add_cpus, 0),
                                add_memory_mb=max(add_mem, 0), add_disk_gb=max(add_disk, 0))
            check_host_memory(conn, running, max(add_mem, 0))
            if cpus is not None or memory_mb is not None:
                dockerctl.update_limits(name, cpus, memory_mb)
            if disk_gb is not None and disk_gb != row["disk_gb"]:
                storage.resize(name, disk_gb)
            changes = {k: v for k, v in (("cpus", cpus), ("memory_mb", memory_mb), ("disk_gb", disk_gb))
                       if v is not None}
            if changes:
                conn.execute(f"UPDATE sandboxes SET {', '.join(k + '=?' for k in changes)} WHERE name=?",
                             [*changes.values(), name])
                db.audit(conn, actor, "sandbox.resources", name, changes)
        return present(conn, name)


def remove(name: str, client_id: Optional[int] = None, actor: str = "api") -> None:
    with db.session() as conn:
        row = get_row(conn, name, client_id)
        if row["state"] == STATE_CREATING and time.time() - row["created_at"] < settings().orphan_grace_seconds:
            raise Conflict(f"Sandbox '{name}' is still being created")
        if row["state"] == STATE_REBUILDING:
            raise Conflict(f"Sandbox '{name}' is being rebuilt")
        _destroy(conn, row, actor=actor, reason="removed")


def _destroy(conn: sqlite3.Connection, row: sqlite3.Row, actor: str, reason: str,
             quiet: bool = False) -> None:
    """Remove every trace of a sandbox. Idempotent; keeps the DB row on error."""
    name = row["name"]
    conn.execute("UPDATE sandboxes SET state=? WHERE name=?", (STATE_REMOVING, name))
    errors: List[str] = []
    try:
        dockerctl.remove(name)
    except dockerctl.DockerError as exc:
        errors.append(str(exc))
    if row["storage"] == "volume":
        dockerctl.volume_remove(f"{name}_data")
    try:
        storage.remove(name)
    except storage.StorageError as exc:
        errors.append(str(exc))
    try:
        with host_lock():
            if nginxctl.remove(name):
                nginxctl.reload()
    except nginxctl.NginxError as exc:
        errors.append(str(exc))
    if errors:
        conn.execute("UPDATE sandboxes SET error=? WHERE name=?", ("; ".join(errors)[:2000], name))
        if not quiet:
            raise SandboxError(f"Removing '{name}' was incomplete, cleanup will be retried: "
                               + "; ".join(errors), code="remove_incomplete")
        return
    with db.transaction(conn):
        conn.execute("DELETE FROM sandboxes WHERE name=?", (name,))
        conn.execute("DELETE FROM containers WHERE name=?", (name,))
        access.revoke_desktop(conn, name)
        db.audit(conn, actor, "sandbox.remove", name, {"reason": reason})


def network_change(before: sqlite3.Row, after: sqlite3.Row) -> Dict[str, bool]:
    """What a change of a client needs: new firewall rules, moving its sandboxes."""
    internet = bool(before["allow_internet"]) != bool(after["allow_internet"])
    subnet = before["net_subnet"] != after["net_subnet"]
    return {"firewall": internet or subnet or clients.allow_nets(before) != clients.allow_nets(after),
            "move": internet or subnet}


def apply_network_policy(client_id: int, actor: str = "admin") -> Dict[str, str]:
    """Bring all sandboxes of a client in line with its internet access and network.

    First every container is moved into the right network (takes effect at
    once, also for running sandboxes), then containers whose DNS setting does
    not match are rebuilt (data is kept; running ones are running again
    afterwards). Returns {sandbox: "ok" | "unchanged" | error message}.
    """
    with db.session() as conn:
        client = clients.get(conn, client_id)
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sandboxes WHERE api_key_id=? AND state IN (?, ?) ORDER BY created_at",
            (client_id, STATE_READY, STATE_ERROR))]
    offline = _placement(client)
    netctl.ensure_ready(offline)
    infos = dockerctl.inspect(names) if names else {}
    results: Dict[str, str] = {}
    for name in names:
        info = infos.get(name)
        if not info:
            continue
        try:
            moved = netctl.isolate(name, info, offline)
            results[name] = "ok" if moved else "unchanged"
        except (dockerctl.DockerError, SandboxError) as exc:
            results[name] = str(exc)
    for name in names:
        info = infos.get(name)
        if not info or netctl.dns_matches(info, offline):
            continue
        try:
            recreate(name, actor=actor)
            results[name] = "ok"
        except SandboxError as exc:
            results[name] = exc.message
    with db.session() as conn:
        db.audit(conn, actor, "client.network_policy", client["label"] or str(client_id),
                 {"clientId": client_id, "internet": not offline.offline, "network": offline.network,
                  "sandboxes": results})
    return results


def delete_client(client_id: int, actor: str = "admin") -> Dict[str, Any]:
    """Remove all sandboxes of a client, then the client itself."""
    removed: List[str] = []
    failed: Dict[str, str] = {}
    with db.session() as conn:
        client = clients.get(conn, client_id)
        names = [r["name"] for r in conn.execute("SELECT name FROM sandboxes WHERE api_key_id=?", (client_id,))]
        for name in names:
            try:
                _destroy(conn, get_row(conn, name), actor=actor, reason="client deleted")
                removed.append(name)
            except SandboxError as exc:
                failed[name] = exc.message
        if failed:
            raise SandboxError("Client not deleted, some sandboxes could not be removed",
                               code="client_delete_incomplete", details={"failed": failed, "removed": removed})
        with db.transaction(conn):
            clients.delete_record(conn, client_id)
            db.audit(conn, actor, "client.delete", client["label"] or str(client_id),
                     {"clientId": client_id, "sandboxes": removed})
    if client["net_subnet"]:
        # Drop the rules of its own network at once (reconcile removes the network itself).
        try:
            netctl.ensure_firewall()
        except SandboxError:
            pass  # the watcher re-applies the rules within a minute
    return {"deleted": client_id, "removedSandboxes": removed}


# --------------------------------------------------------------------------
# usage + legacy migration
# --------------------------------------------------------------------------

def refresh_usage(include_rootfs: bool = True, include_volumes: bool = True) -> int:
    """Measure disk usage of all sandboxes and store it in the database."""
    with db.session() as conn:
        rows = conn.execute("SELECT name, storage FROM sandboxes WHERE state=?", (STATE_READY,)).fetchall()
        if not rows:
            return 0
        infos = dockerctl.inspect([r["name"] for r in rows], size=True) if include_rootfs else {}
        now = int(time.time())
        for r in rows:
            name = r["name"]
            disk_used: Optional[int] = None
            if r["storage"] == "image":
                measured = storage.usage(name)
                disk_used = measured[0] if measured else None
            elif include_volumes:
                mountpoint = dockerctl.volume_mountpoint(f"{name}_data")
                disk_used = storage.dir_size(mountpoint) if mountpoint else None
            rootfs = (infos.get(name) or {}).get("SizeRw") if include_rootfs else None
            conn.execute(
                "UPDATE sandboxes SET disk_used_bytes=COALESCE(?, disk_used_bytes), "
                "rootfs_used_bytes=COALESCE(?, rootfs_used_bytes), usage_updated_at=? WHERE name=?",
                (disk_used, rootfs, now, name),
            )
        return len(rows)


def migrate_legacy_registry() -> Dict[str, Any]:
    """Import sandboxes from the old JSON registry (pre 2.0) into SQLite."""
    s = settings()
    report: Dict[str, Any] = {"imported": [], "skipped": {}}
    path = s.legacy_registry
    if not path.exists():
        return report
    try:
        entries = json.loads(path.read_text(encoding="utf-8") or "[]")
    except ValueError:
        entries = []
    if not isinstance(entries, list):
        entries = []
    names = [e.get("name") for e in entries if isinstance(e, dict) and e.get("name")]
    infos = dockerctl.inspect(names) if names else {}

    def as_port(value: Any) -> Optional[int]:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    with db.session() as conn:
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"]
            if conn.execute("SELECT 1 FROM sandboxes WHERE name=?", (name,)).fetchone():
                report["skipped"][name] = "already imported"
                continue
            info = infos.get(name)
            if not info:
                report["skipped"][name] = "container missing"
                continue
            labels = (info.get("Config") or {}).get("Labels") or {}
            owner = conn.execute("SELECT id FROM api_keys WHERE key_hash=?",
                                 (labels.get(dockerctl.LABEL_APIKEY_HASH, ""),)).fetchone()
            if not owner:
                owner = conn.execute("SELECT api_key_id AS id FROM containers WHERE name=?", (name,)).fetchone()
            if not owner:
                report["skipped"][name] = "owner API key unknown"
                continue
            host_cfg = info.get("HostConfig") or {}
            nano = host_cfg.get("NanoCpus") or 0
            mem = host_cfg.get("Memory") or 0
            created = parse_time(entry.get("created")) or parse_time(info.get("Created")) or time.time()
            conn.execute(
                """INSERT INTO sandboxes(name, api_key_id, container_id, image, state, created_at, persistent,
                     novnc_port, vnc_port, mcphub_port, filesvc_port, vnc_password, filesvc_token,
                     cpus, memory_mb, disk_gb, storage, last_activity_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, owner["id"], info.get("Id"), (info.get("Config") or {}).get("Image"), STATE_READY,
                 int(created), 1 if entry.get("persistent") else 0,
                 as_port(entry.get("port")), as_port(entry.get("vnc_port")), as_port(entry.get("mcphub_port")),
                 as_port(entry.get("filesvc_port")), entry.get("vnc_password") or LEGACY_VNC_PASSWORD,
                 entry.get("filesvc_token") or None,
                 round(nano / 1e9, 2) if nano else None, int(mem // (1024 * 1024)) if mem else None,
                 None, "volume", int(time.time())),
            )
            report["imported"].append(name)
        db.audit(conn, "system", "migrate.registry", str(path), report)
    backup = path.with_name(path.name + f".migrated-{int(time.time())}")
    path.rename(backup)
    report["backup"] = str(backup)
    return report


def host_overview() -> Dict[str, Any]:
    s = settings()
    with db.session() as conn:
        rows = conn.execute("SELECT name, cpus, memory_mb, disk_gb, disk_used_bytes FROM sandboxes").fetchall()
        client_count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
        offline_clients = conn.execute("SELECT COUNT(*) FROM api_keys WHERE allow_internet=0").fetchone()[0]
        legacy_clients = conn.execute("SELECT COUNT(*) FROM api_keys WHERE legacy_access=1").fetchone()[0]
        profile_count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        tls_state = db.kv_get(conn, "tls", {}) or {}
        allow = netctl.global_allow_nets(conn)
        client_networks = conn.execute("SELECT COUNT(*) FROM api_keys WHERE net_subnet IS NOT NULL").fetchone()[0]
    try:
        running = dockerctl.running_names()
        docker_ok = True
    except dockerctl.DockerError:
        running, docker_ok = set(), False
    active = [r for r in rows if r["name"] in running]
    with db.session() as conn:
        memory_guard_view = memory_guard(conn, running) if s.memory_overcommit > 0 else None
        lifecycle_view = lifecycle.global_values(conn)
    try:
        firewall = netctl.firewall_status()
    except SandboxError as exc:
        firewall = {"enabled": s.firewall, "ok": False, "error": exc.message}
    net_info = dockerctl.network_inspect(s.network_name) if docker_ok else None
    offline_info = dockerctl.network_inspect(s.offline_network_name) if docker_ok else None
    isolation = {
        "network": s.network_name if net_info else None,
        "subnet": netctl.network_subnet(net_info) if net_info else s.network_subnet,
        "interContainerBlocked": ((net_info or {}).get("Options") or {}).get(
            "com.docker.network.bridge.enable_icc") == "false",
        "firewall": firewall,
        "blockedNets": list(s.block_nets),
        # Internal targets of all sandboxes (admin UI or ALBERT_ALLOW_NETS) ...
        "allowedNets": allow["nets"],
        "allowedNetsSource": allow["source"],
        # ... and clients with own targets in an own network each.
        "clientNetworks": client_networks,
        "clientSubnetPool": s.client_subnet_pool,
        # Sandboxes of clients without internet access (created on first use).
        "offline": {
            "network": s.offline_network_name if offline_info else None,
            "subnet": netctl.network_subnet(offline_info, offline=True) if offline_info
            else s.offline_network_subnet,
            "dns": s.offline_dns,
        },
    }
    return {
        "isolation": isolation,
        "host": {
            "cpus": host_cpus(),
            "memoryMb": host_memory_mb(),
            "storage": storage.filesystem_usage(s.storage_dir if s.storage_dir.exists() else s.storage_dir.parent),
            "dockerRoot": storage.filesystem_usage(Path("/var/lib/docker")),
            "dockerReachable": docker_ok,
        },
        "clients": client_count,
        "clientsOffline": offline_clients,
        # Clients whose sandboxes still accept MCP/files without API key (legacy access).
        "clientsLegacy": legacy_clients,
        "https": {"active": bool(tls_state.get("active")), "host": tls_state.get("fqdn")},
        "profiles": profile_count,
        "sandboxes": {"total": len(rows), "running": len(active)},
        # RAM limits of running sandboxes against what the host may promise (create/start beyond: host_full).
        "memoryGuard": memory_guard_view,
        "allocated": {
            "cpusRunning": round(sum(r["cpus"] or 0 for r in active), 2),
            "memoryMbRunning": sum(r["memory_mb"] or 0 for r in active),
            "diskGbTotal": sum(r["disk_gb"] or 0 for r in rows),
            "diskUsedBytes": sum(r["disk_used_bytes"] or 0 for r in rows),
        },
        "defaults": {"cpus": s.default_cpus, "memoryMb": s.default_memory_mb, "diskGb": s.default_disk_gb},
        "limits": {"maxCpus": host_cpus(), "minMemoryMb": s.min_memory_mb, "maxMemoryMb": host_memory_mb(),
                   "maxDiskGb": s.max_disk_gb},
        # Non-persistent sandboxes are removed after maxAgeSeconds; all stop when idle.
        # Global lifetime of sandboxes (admin UI or albert.env); clients may override it.
        "lifecycle": lifecycle_view,
        "publicUrl": access.public_url(),
        "adminPort": s.admin_port,
    }
