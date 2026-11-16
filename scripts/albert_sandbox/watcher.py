"""Periodic housekeeping (systemd timer, every minute).

1. Record activity per sandbox from the nginx access log and from open
   connections to the sandbox ports.
2. Stop sandboxes idle for the inactivity time (data is kept).
3. Remove non-persistent sandboxes past their removal deadlines and warn on
   the desktop before the max lifetime ends one (see lifecycle.py).
4. Reconcile Docker / nginx / disks with the database (cleanup).
5. Refresh disk usage figures for the admin UI.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from datetime import datetime
from typing import Dict, Iterable, Optional, Set

from . import access, db, dockerctl, lifecycle, reconcile, sandboxes
from .config import settings
from .errors import SandboxError

LINE_TS_PATTERN = re.compile(r"\[(?P<ts>[^\]]+)\]")
REQUEST_PATH_PATTERN = re.compile(r'"[A-Z]+ ([^" ]+)')
FIRST_READ_MAX_BYTES = 50 * 1024 * 1024
ROOTFS_INTERVAL = 300
VOLUME_INTERVAL = 900


def log(msg: str) -> None:
    print(msg, flush=True)


def _parse_nginx_ts(value: str) -> Optional[float]:
    try:
        return datetime.strptime(value, "%d/%b/%Y:%H:%M:%S %z").timestamp()
    except ValueError:
        return None


def collect_log_activity(conn, names: Set[str]) -> Dict[str, float]:
    path = settings().nginx_access_log
    activity: Dict[str, float] = {}
    try:
        fh = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return activity
    with fh:
        st = os.fstat(fh.fileno())
        state = db.kv_get(conn, "access_log", {}) or {}
        pos = state.get("pos", 0)
        if state.get("inode") == st.st_ino and isinstance(pos, int) and pos <= st.st_size:
            fh.seek(pos)
        elif st.st_size > FIRST_READ_MAX_BYTES:
            fh.seek(st.st_size)
        for line in fh:
            req = REQUEST_PATH_PATTERN.search(line)
            if not req:
                continue
            path_part = req.group(1)
            if not path_part.startswith("/"):
                continue
            candidate = path_part.split("/", 2)[1]
            if candidate not in names:
                continue
            ts_match = LINE_TS_PATTERN.search(line)
            ts = _parse_nginx_ts(ts_match.group("ts")) if ts_match else None
            if ts and ts > activity.get(candidate, 0):
                activity[candidate] = ts
        db.kv_set(conn, "access_log", {"inode": st.st_ino, "pos": fh.tell()})
    return activity


def established_ports() -> Set[int]:
    try:
        out = subprocess.run(["ss", "-Htn", "state", "established"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    result: Set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        # Recv-Q Send-Q Local Peer
        for addr in parts[2:4]:
            try:
                result.add(int(addr.rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                continue
    return result


def _ports(row) -> Iterable[int]:
    for key in ("novnc_port", "vnc_port", "mcphub_port", "filesvc_port"):
        if row[key]:
            yield int(row[key])


def _effective_by_client(conn) -> Dict[int, Dict]:
    glob = lifecycle.global_values(conn)
    return {c["id"]: lifecycle.effective(c, glob) for c in conn.execute("SELECT * FROM api_keys")}


def update_activity_and_stop_idle(now: float) -> None:
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM sandboxes WHERE state=?", (sandboxes.STATE_READY,)).fetchall()
        names = {r["name"] for r in rows}
        activity = collect_log_activity(conn, names)
        busy = established_ports()
        for row in rows:
            name = row["name"]
            last = activity.get(name)
            if any(p in busy for p in _ports(row)):
                last = now
            if last and last > (row["last_activity_at"] or 0):
                conn.execute("UPDATE sandboxes SET last_activity_at=? WHERE name=?", (int(last), name))
        rows = conn.execute("SELECT * FROM sandboxes WHERE state=?", (sandboxes.STATE_READY,)).fetchall()
        effective = _effective_by_client(conn)

    if not any(e["inactivitySeconds"] > 0 for e in effective.values()):
        return
    running = dockerctl.running_names()
    infos = dockerctl.inspect([r["name"] for r in rows if r["name"] in running])
    for row in rows:
        name = row["name"]
        inactivity = (effective.get(row["api_key_id"]) or {}).get("inactivitySeconds", 0)
        if name not in running or inactivity <= 0:
            continue
        started = sandboxes.parse_time(((infos.get(name) or {}).get("State") or {}).get("StartedAt"))
        last_seen = max(row["last_activity_at"] or 0, row["last_started_at"] or 0, started or 0,
                        row["created_at"] or 0)
        if now - last_seen < inactivity:
            continue
        try:
            sandboxes.stop(name, actor="watcher")
            log(f"[INFO] Stopped idle sandbox {name} (idle {int(now - last_seen)}s)")
        except SandboxError as exc:
            log(f"[WARN] Could not stop idle sandbox {name}: {exc.message}")
        except dockerctl.DockerError as exc:
            log(f"[WARN] Could not stop idle sandbox {name}: {exc}")


def purge_desktop_links(now: float) -> None:
    """Drop expired one-time desktop links and desktop sessions."""
    with db.session() as conn:
        access.purge_expired(conn, int(now))


def remove_expired(now: float) -> None:
    """Non-persistent sandboxes: removed past the max age once not in use, past the
    max lifetime in any case. Persistent sandboxes are never removed here."""
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM sandboxes WHERE state=? AND persistent=0",
                            (sandboxes.STATE_READY,)).fetchall()
        effective = _effective_by_client(conn)
    if not rows:
        return
    running = dockerctl.running_names()
    for row in rows:
        eff = effective.get(row["api_key_id"])
        if not eff:
            continue
        due = lifecycle.deadlines(row, eff)
        if due["latest"] and now >= due["latest"]:
            reason = "max lifetime reached"
        elif due["idle"] and now >= due["idle"] and not lifecycle.in_use(row, eff, row["name"] in running, now):
            reason = "max age reached, not in use"
        else:
            continue
        try:
            sandboxes.remove(row["name"], actor="watcher")
            log(f"[INFO] Removed sandbox {row['name']}: {reason} (age {int(now - row['created_at'])}s)")
        except SandboxError as exc:
            log(f"[WARN] Could not remove expired sandbox {row['name']}: {exc.message}")


def warn_lifetime(now: float) -> None:
    """Tell the desktop user before the max lifetime removes a running sandbox."""
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM sandboxes WHERE state=? AND persistent=0",
                            (sandboxes.STATE_READY,)).fetchall()
        effective = _effective_by_client(conn)
        warned = db.kv_get(conn, "lifetime_warned", {}) or {}
    due_soon = {}
    for row in rows:
        eff = effective.get(row["api_key_id"])
        latest = lifecycle.deadlines(row, eff)["latest"] if eff else None
        if latest and 0 < latest - now <= lifecycle.WARN_SECONDS:
            due_soon[row["name"]] = latest
    if not due_soon:
        if warned:
            with db.session() as conn:
                db.kv_set(conn, "lifetime_warned", {})
        return
    running = dockerctl.running_names()
    for name, latest in due_soon.items():
        if warned.get(name) == latest or name not in running:
            continue
        minutes = max(1, int(round((latest - now) / 60)))
        try:
            dockerctl.notify_desktop(name, "Sandbox wird beendet",
                                     f"Diese Sandbox wird in {minutes} Minuten gelöscht: die maximale "
                                     "Laufzeit ist erreicht. Bitte Ergebnisse jetzt sichern.")
            log(f"[INFO] Warned desktop of {name}: removal in {minutes} min (max lifetime)")
        except dockerctl.DockerError as exc:
            log(f"[WARN] Could not warn the desktop of {name}: {exc}")
        warned[name] = latest
    with db.session() as conn:
        db.kv_set(conn, "lifetime_warned", {n: t for n, t in warned.items() if n in due_soon})


def refresh_usage(now: float) -> None:
    with db.session() as conn:
        last_rootfs = db.kv_get(conn, "usage_rootfs_at", 0) or 0
        last_volumes = db.kv_get(conn, "usage_volumes_at", 0) or 0
    with_rootfs = now - last_rootfs >= ROOTFS_INTERVAL
    with_volumes = now - last_volumes >= VOLUME_INTERVAL
    sandboxes.refresh_usage(include_rootfs=with_rootfs, include_volumes=with_volumes)
    with db.session() as conn:
        if with_rootfs:
            db.kv_set(conn, "usage_rootfs_at", int(now))
        if with_volumes:
            db.kv_set(conn, "usage_volumes_at", int(now))


def run_once() -> int:
    s = settings()
    lock_path = s.data_dir / ".watcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("[INFO] Another watcher run is active; skipping")
            return 0
        db.init_db()
        if not dockerctl.available():
            log("[WARN] Docker not reachable; skipping run")
            return 0
        now = time.time()
        for step in (update_activity_and_stop_idle, remove_expired, warn_lifetime, purge_desktop_links):
            try:
                step(now)
            except Exception as exc:  # keep the other steps running
                log(f"[ERROR] {step.__name__}: {exc}")
        try:
            report = reconcile.reconcile(actor="watcher")
            for item in report["actions"]:
                log(f"[INFO] reconcile {item['action']} {item['target']}: {item['detail']}")
            for err in report["errors"]:
                log(f"[WARN] reconcile: {err}")
        except Exception as exc:
            log(f"[ERROR] reconcile: {exc}")
        try:
            refresh_usage(now)
        except Exception as exc:
            log(f"[ERROR] refresh_usage: {exc}")
    return 0
