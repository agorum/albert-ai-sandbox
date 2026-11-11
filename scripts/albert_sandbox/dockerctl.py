"""Thin wrapper around the docker CLI.

The docker CLI is used instead of the Python SDK on purpose: it is always
present on the host, matches the daemon's API version and avoids the
``requests``/``requests-unixsocket`` incompatibilities seen with docker-py.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Sequence

LABEL_MANAGER = "albert.manager"
LABEL_SANDBOX = "albert.sandbox"
LABEL_CLIENT = "albert.client_id"
LABEL_APIKEY_HASH = "albert.apikey_hash"


class DockerError(RuntimeError):
    def __init__(self, message: str, returncode: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def _run(args: Sequence[str], timeout: float = 60, check: bool = True,
         input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            timeout=timeout,
            input=input_bytes,
        )
    except FileNotFoundError as exc:
        raise DockerError("docker binary not found", 127) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"docker {' '.join(args[:2])} timed out after {timeout}s", 124) from exc
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise DockerError(f"docker {args[0]} failed: {stderr or 'exit ' + str(proc.returncode)}",
                          proc.returncode, stderr)
    return proc


def _text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace")


def available() -> bool:
    try:
        _run(["info", "--format", "{{.ServerVersion}}"], timeout=10)
        return True
    except DockerError:
        return False


def image_id(image: str) -> Optional[str]:
    proc = _run(["image", "inspect", "--format", "{{.Id}}", image], timeout=30, check=False)
    if proc.returncode != 0:
        return None
    return _text(proc).strip() or None


def inspect(names: Iterable[str], size: bool = False) -> Dict[str, Dict[str, Any]]:
    """Inspect containers; returns {name: payload} for the ones that exist."""
    names = [n for n in names if n]
    if not names:
        return {}
    args = ["container", "inspect"]
    if size:
        args.append("--size")
    proc = _run([*args, *names], timeout=60, check=False)
    # Exit code is non-zero if *any* name is missing, but stdout still
    # contains the payloads of the existing ones.
    try:
        payload = json.loads(_text(proc) or "[]")
    except ValueError:
        payload = []
    result: Dict[str, Dict[str, Any]] = {}
    for item in payload or []:
        name = (item.get("Name") or "").lstrip("/")
        if name:
            result[name] = item
    return result


def inspect_one(name: str, size: bool = False) -> Optional[Dict[str, Any]]:
    return inspect([name], size=size).get(name)


def managed_container_names(all_states: bool = True) -> List[str]:
    args = ["ps", "--filter", f"label={LABEL_MANAGER}=1", "--format", "{{.Names}}"]
    if all_states:
        args.insert(1, "-a")
    proc = _run(args, timeout=30)
    return [line.strip() for line in _text(proc).splitlines() if line.strip()]


def running_names() -> set:
    return set(managed_container_names(all_states=False))


def run_container(args: Sequence[str]) -> str:
    proc = _run(["run", "-d", *args], timeout=180)
    return _text(proc).strip()


def start(name: str) -> None:
    _run(["start", name], timeout=120)


def stop(name: str, timeout_seconds: int = 10) -> None:
    _run(["stop", "-t", str(timeout_seconds), name], timeout=timeout_seconds + 60)


def remove(name: str) -> bool:
    """Force-remove a container. Returns False if it did not exist."""
    proc = _run(["rm", "-f", "-v", name], timeout=120, check=False)
    if proc.returncode == 0:
        return True
    stderr = proc.stderr.decode("utf-8", "replace")
    if "No such container" in stderr:
        return False
    raise DockerError(f"docker rm failed: {stderr.strip()}", proc.returncode, stderr)


def update_limits(name: str, cpus: Optional[float], memory_mb: Optional[int],
                  pids_limit: Optional[int] = None) -> None:
    args: List[str] = ["update"]
    if cpus is not None:
        args += ["--cpus", f"{cpus:g}"]
    if memory_mb is not None:
        args += ["--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m"]
    if pids_limit is not None:
        args += ["--pids-limit", str(pids_limit)]
    if len(args) == 1:
        return
    _run([*args, name], timeout=60)


def volume_names() -> List[str]:
    proc = _run(["volume", "ls", "-q"], timeout=30)
    return [line.strip() for line in _text(proc).splitlines() if line.strip()]


def volume_remove(name: str) -> bool:
    proc = _run(["volume", "rm", "-f", name], timeout=60, check=False)
    return proc.returncode == 0


def volume_mountpoint(name: str) -> Optional[str]:
    proc = _run(["volume", "inspect", "--format", "{{.Mountpoint}}", name], timeout=30, check=False)
    if proc.returncode != 0:
        return None
    return _text(proc).strip() or None


def export_home_tar(image: str) -> bytes:
    """Return a tar stream of /home/ubuntu from the image (skeleton home)."""
    proc = _run(["run", "--rm", "--network", "none", "--entrypoint", "tar", image,
                 "-C", "/home/ubuntu", "-cf", "-", "."], timeout=300)
    return proc.stdout


def stats(names: Iterable[str]) -> Dict[str, Dict[str, str]]:
    names = [n for n in names if n]
    if not names:
        return {}
    proc = _run(["stats", "--no-stream", "--format", "{{json .}}", *names], timeout=60, check=False)
    result: Dict[str, Dict[str, str]] = {}
    for line in _text(proc).splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("Name"):
            result[item["Name"]] = item
    return result


def build(image: str, context_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "build", "-t", image, context_dir])


def system_df() -> Dict[str, Any]:
    proc = _run(["system", "df", "--format", "{{json .}}"], timeout=120, check=False)
    rows = {}
    for line in _text(proc).splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        rows[item.get("Type", "?")] = item
    return rows
