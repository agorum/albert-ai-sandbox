#!/usr/bin/env python3
"""Monitor nginx access log and stop idle sandbox containers."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = BASE_DIR / "data" / "container-activity.json"
DEFAULT_LOG_PATH = Path(os.environ.get("ALBERT_NGINX_ACCESS_LOG", "/var/log/nginx/access.log"))
DEFAULT_THRESHOLD_SECONDS = int(os.environ.get("ALBERT_INACTIVITY_SECONDS", "600"))
MANAGER_SCRIPT = Path(os.environ.get(
    "ALBERT_MANAGER_SCRIPT",
    "/opt/albert-ai-sandbox-manager/scripts/albert-ai-sandbox-manager.sh",
))
REGISTRY_FILE = Path(
    os.environ.get(
        "ALBERT_REGISTRY_FILE",
        "/opt/albert-ai-sandbox-manager/config/container-registry.json",
    )
)
STATE_PATH = Path(os.environ.get("ALBERT_INACTIVITY_STATE", str(DEFAULT_STATE_PATH)))
THRESHOLD_SECONDS = DEFAULT_THRESHOLD_SECONDS
LOG_PATH = DEFAULT_LOG_PATH

LINE_TS_PATTERN = re.compile(r"\[(?P<ts>[^\]]+)\]")
# Extract request path from the quoted request portion (e.g. "GET /path HTTP/1.1")
REQUEST_PATH_PATTERN = re.compile(r'"[A-Z]+ ([^" ]+)')


def debug(msg: str) -> None:
    if os.environ.get("ALBERT_INACTIVITY_DEBUG"):
        print(f"[DEBUG] {msg}")


@dataclass
class ContainerRecord:
    name: str
    key_hash: str
    created_at: float


def load_state(state_path: Path) -> Dict[str, Dict[str, float]]:
    if not state_path.exists():
        return {"log": {}, "containers": {}, "running_since": {}, "stop_history": {}}
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key in ("containers", "running_since", "stop_history"):
            data.setdefault(key, {})
        data.setdefault("log", {})
        return data
    except Exception as exc:  # pragma: no cover - defensive path
        print(f"[WARN] Failed to load state file {state_path}: {exc}", file=sys.stderr)
        return {"log": {}, "containers": {}, "running_since": {}, "stop_history": {}}


def save_state(state_path: Path, state: Dict[str, Dict[str, float]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh)
    tmp_path.replace(state_path)


def parse_nginx_timestamp(value: str) -> Optional[float]:
    """Return epoch seconds from nginx log timestamp."""
    try:
        dt = datetime.strptime(value, "%d/%b/%Y:%H:%M:%S %z")
        return dt.timestamp()
    except Exception:
        return None


def parse_docker_timestamp(value: Optional[str]) -> Optional[float]:
    if not value or value.startswith("0001-01-01T00:00:00"):
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        frac = ""
        tz = ""
        for char in tail:
            if char.isdigit():
                frac += char
            else:
                tz = tail[len(frac):]
                break
        else:
            tz = ""
        frac = (frac + "000000")[:6]
        text = head + "." + frac + tz
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def extract_container_from_path(path: str) -> Optional[str]:
    if not path.startswith("/"):
        return None
    segments = path.split("/", 2)
    if len(segments) < 2:
        return None
    candidate = segments[1]
    if not candidate:
        return None
    # Skip known non-container routes
    if candidate in {"manager", "mcphub"}:
        return None
    return candidate


def iter_new_log_lines(log_path: Path, state: Dict[str, Dict[str, float]]) -> Iterable[str]:
    if not log_path.exists():
        return []
    log_state = state.setdefault("log", {})
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            stat = handle.fileno()
            file_stat = os.fstat(stat)
            inode = log_state.get("inode")
            pos = log_state.get("pos", 0)
            if inode == file_stat.st_ino and isinstance(pos, (int, float)) and pos <= file_stat.st_size:
                handle.seek(int(pos))
            else:
                # Log rotated or state missing; read from start
                debug("Log rotation detected or no previous position; reading from beginning")
            for line in handle:
                yield line
            log_state["pos"] = handle.tell()
            log_state["inode"] = file_stat.st_ino
    except FileNotFoundError:
        return []


def update_activity_from_logs(state: Dict[str, Dict[str, float]]) -> None:
    for line in iter_new_log_lines(LOG_PATH, state):
        ts_match = LINE_TS_PATTERN.search(line)
        if not ts_match:
            continue
        epoch = parse_nginx_timestamp(ts_match.group("ts"))
        if epoch is None:
            continue
        req_match = REQUEST_PATH_PATTERN.search(line)
        if not req_match:
            continue
        path = req_match.group(1)
        name = extract_container_from_path(path)
        if not name:
            continue
        containers_state = state.setdefault("containers", {})
        previous = containers_state.get(name, 0)
        if epoch > previous:
            containers_state[name] = epoch
            debug(f"Activity recorded for {name} at {epoch}")


def inspect_container_state(name: str) -> Optional[Dict[str, object]]:
    try:
        result = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    return data[0]


def list_managed_container_names() -> Iterable[str]:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=albert.manager=1",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    names = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            names.append(line)
    return names


def collect_container_record(name: str) -> Optional[Tuple[ContainerRecord, Dict[str, object], Optional[float]]]:
    info = inspect_container_state(name)
    if not info:
        return None
    state = info.get("State") or {}
    if not state.get("Running"):
        return None
    labels = (info.get("Config") or {}).get("Labels") or {}
    key_hash = labels.get("albert.apikey_hash")
    if not key_hash:
        return None
    created_at = parse_docker_timestamp(info.get("Created"))
    started_at = parse_docker_timestamp(state.get("StartedAt"))
    record = ContainerRecord(
        name=name,
        key_hash=key_hash,
        created_at=created_at or started_at or 0,
    )
    return record, state, started_at


def load_registry() -> Dict[str, Dict[str, str]]:
    if not REGISTRY_FILE.exists():
        return {}
    try:
        with REGISTRY_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    registry: Dict[str, Dict[str, str]] = {}
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            registry[item["name"]] = item
    return registry


def collect_active_ports() -> Dict[int, bool]:
    if shutil.which("ss") is None:
        return {}
    try:
        result = subprocess.run(
            ["ss", "-tan"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    active: Dict[int, bool] = {}
    for line in result.stdout.splitlines():
        if "ESTAB" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        active[port] = True
    return active


def stop_container(name: str, key_hash: str) -> bool:
    if not MANAGER_SCRIPT.exists():
        print(f"[WARN] Manager script not found at {MANAGER_SCRIPT}, skipping stop for {name}")
        return False
    env = os.environ.copy()
    env.setdefault("ALBERT_STATUS_SKIP_STATS", "1")
    cmd = [
        str(MANAGER_SCRIPT),
        "stop",
        name,
        "--api-key-hash",
        key_hash,
        "--json",
        "--non-interactive",
        "--quiet",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    except subprocess.SubprocessError as exc:
        print(f"[ERROR] Failed to stop container {name}: {exc}")
        return False
    if result.returncode != 0:
        print(
            f"[WARN] Stop command for {name} failed (rc={result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
        return False
    print(f"[INFO] Stopped idle container {name}")
    return True


def evaluate_and_stop_idle(state: Dict[str, Dict[str, float]], now: float) -> None:
    containers_state = state.setdefault("containers", {})
    running_since_state = state.setdefault("running_since", {})
    registry = load_registry()
    active_ports = collect_active_ports()
    for name in list_managed_container_names():
        collected = collect_container_record(name)
        if not collected:
            running_since_state.pop(name, None)
            continue
        record, _, started_at = collected
        registry_entry = registry.get(record.name, {})
        candidate_ports = []
        for key in ("port", "vnc_port", "mcphub_port", "filesvc_port"):
            value = registry_entry.get(key)
            if value and str(value).isdigit():
                candidate_ports.append(int(value))
        if any(active_ports.get(port) for port in candidate_ports):
            running_since_state[record.name] = max(started_at or now, running_since_state.get(record.name, 0), record.created_at or 0)
            containers_state[record.name] = now
            debug(f"Container {record.name} has active connections; skipping stop check")
            continue
        if started_at is None:
            started_at = running_since_state.get(record.name)
        if started_at is None:
            started_at = now
        prev_started = running_since_state.get(record.name, 0)
        if started_at > prev_started:
            running_since_state[record.name] = started_at
        fallback = max(
            running_since_state.get(record.name, 0),
            float(containers_state.get(record.name, 0)),
            record.created_at or 0,
        )
        last_seen = fallback or now
        inactivity = now - last_seen
        debug(f"Container {record.name} inactivity {inactivity:.1f}s (last {last_seen})")
        if inactivity < THRESHOLD_SECONDS:
            continue
        if stop_container(record.name, record.key_hash):
            running_since_state.pop(record.name, None)
            containers_state[record.name] = now


def main() -> int:
    if shutil.which("docker") is None:
        print("[WARN] Docker binary not found; skipping inactivity check")
        return 0
    state = load_state(STATE_PATH)
    update_activity_from_logs(state)
    now = time.time()
    evaluate_and_stop_idle(state, now)
    save_state(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
