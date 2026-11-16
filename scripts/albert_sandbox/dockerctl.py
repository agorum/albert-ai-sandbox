"""Thin wrapper around the docker CLI.

The docker CLI is used instead of the Python SDK on purpose: it is always
present on the host, matches the daemon's API version and avoids the
``requests``/``requests-unixsocket`` incompatibilities seen with docker-py.
"""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

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


def create_container(args: Sequence[str]) -> str:
    """Like run_container, but the container is only created, not started."""
    proc = _run(["create", *args], timeout=180)
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


def network_inspect(name: str) -> Optional[Dict[str, Any]]:
    proc = _run(["network", "inspect", name], timeout=30, check=False)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(_text(proc) or "[]")
    except ValueError:
        return None
    return payload[0] if payload else None


def network_create(name: str, subnet: str, bridge: str, labels: Dict[str, str]) -> None:
    args = ["network", "create", "--driver", "bridge", "--subnet", subnet,
            "-o", "com.docker.network.bridge.enable_icc=false",
            "-o", f"com.docker.network.bridge.name={bridge}"]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    _run([*args, name], timeout=60)


def network_connect(network: str, container: str) -> None:
    _run(["network", "connect", network, container], timeout=60)


def network_disconnect(network: str, container: str) -> None:
    _run(["network", "disconnect", "-f", network, container], timeout=60)


def network_remove(name: str) -> bool:
    return _run(["network", "rm", name], timeout=60, check=False).returncode == 0


def network_list() -> List[Dict[str, Any]]:
    """All docker networks with name, IPv4 subnets, labels and attached containers."""
    ids = [line.strip() for line in _text(_run(["network", "ls", "-q"], timeout=30)).splitlines() if line.strip()]
    if not ids:
        return []
    try:
        payload = json.loads(_text(_run(["network", "inspect", *ids], timeout=60, check=False)) or "[]")
    except ValueError:
        payload = []
    result = []
    for item in payload or []:
        subnets = [c["Subnet"] for c in ((item.get("IPAM") or {}).get("Config") or [])
                   if c.get("Subnet") and ":" not in c["Subnet"]]
        result.append({"name": item.get("Name"), "subnets": subnets, "labels": item.get("Labels") or {},
                       "containers": len(item.get("Containers") or {})})
    return result


def copy_out_tar(container: str, path: str) -> Optional[bytes]:
    """Tar stream of a directory inside a (running or stopped) container."""
    proc = _run(["cp", f"{container}:{path}", "-"], timeout=600, check=False)
    return proc.stdout if proc.returncode == 0 else None


def copy_in_tar(container: str, dest_dir: str, data: bytes) -> None:
    _run(["cp", "-", f"{container}:{dest_dir}"], timeout=600, input_bytes=data)


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


def build_streaming(tag: str, context_dir: str, labels: Dict[str, str], on_line: Callable[[str], None],
                    timeout: float) -> int:
    """``docker build`` with plain progress output passed line by line to ``on_line``.

    Returns the exit code; on timeout the build is killed and 124 is returned.
    """
    args = ["docker", "build", "--progress=plain", "-t", tag]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    args.append(context_dir)
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise DockerError("docker binary not found", 127) from exc
    killed = threading.Event()

    def kill() -> None:
        killed.set()
        proc.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            on_line(raw.decode("utf-8", "replace").rstrip("\n"))
        proc.wait()
    finally:
        timer.cancel()
    return 124 if killed.is_set() else proc.returncode


def image_inspect(image: str) -> Optional[Dict[str, Any]]:
    proc = _run(["image", "inspect", image], timeout=30, check=False)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(_text(proc) or "[]")
    except ValueError:
        return None
    return payload[0] if payload else None


def images_with_label(label: str) -> List[Dict[str, str]]:
    """Tagged images carrying ``label``: [{"ref": "repo:tag", "id": "sha256:..."}]."""
    proc = _run(["image", "ls", "--no-trunc", "--filter", f"label={label}",
                 "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"], timeout=60, check=False)
    result = []
    for line in _text(proc).splitlines():
        parts = line.split()
        if len(parts) == 2 and not parts[0].startswith("<none>"):
            result.append({"ref": parts[0], "id": parts[1]})
    return result


def image_remove(ref: str) -> bool:
    return _run(["image", "rm", ref], timeout=120, check=False).returncode == 0


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
