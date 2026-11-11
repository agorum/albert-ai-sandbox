"""Test fixtures: isolated settings, an in-memory fake Docker and fake disks.

The fakes replace the functions in ``dockerctl`` and ``storage`` that touch the
host, so the lifecycle, quota, reconcile and API logic can be tested without
root, Docker or loop devices. nginx route files are written for real into a
temporary directory (reloads are disabled).
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from albert_sandbox import config, db, dockerctl, ports, sandboxes, storage  # noqa: E402


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class FakeDocker:
    def __init__(self) -> None:
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.volumes: set = set()
        self.run_calls: List[List[str]] = []
        self.fail_run: Optional[str] = None
        self.updates: List[tuple] = []

    def add_container(self, name: str, labels: Dict[str, str], running: bool = True,
                      created: Optional[float] = None, image: str = "albert-ai-sandbox:latest",
                      host_config: Optional[Dict[str, Any]] = None) -> None:
        created = created if created is not None else time.time()
        self.containers[name] = {
            "Id": f"id-{name}",
            "Name": f"/{name}",
            "Created": _iso(created),
            "State": {"Running": running, "Status": "running" if running else "exited",
                      "StartedAt": _iso(created), "FinishedAt": "0001-01-01T00:00:00Z"},
            "Config": {"Image": image, "Labels": dict(labels)},
            "HostConfig": host_config or {},
            "SizeRw": 1234,
        }

    # --- dockerctl API -----------------------------------------------------
    def available(self) -> bool:
        return True

    def image_id(self, image: str) -> Optional[str]:
        return "sha256:fakeimage"

    def inspect(self, names, size: bool = False):
        return {n: self.containers[n] for n in names if n in self.containers}

    def inspect_one(self, name, size: bool = False):
        return self.containers.get(name)

    def managed_container_names(self, all_states: bool = True):
        return [n for n, c in self.containers.items()
                if c["Config"]["Labels"].get("albert.manager") == "1"
                and (all_states or c["State"]["Running"])]

    def running_names(self):
        return set(self.managed_container_names(all_states=False))

    def run_container(self, args):
        self.run_calls.append(list(args))
        if self.fail_run:
            raise dockerctl.DockerError(self.fail_run)
        name = args[args.index("--name") + 1]
        labels = {}
        for i, a in enumerate(args):
            if a == "--label":
                k, v = args[i + 1].split("=", 1)
                labels[k] = v
        self.add_container(name, labels, running=True, image=args[-1])
        return f"id-{name}"

    def start(self, name):
        self.containers[name]["State"]["Running"] = True
        self.containers[name]["State"]["StartedAt"] = _iso(time.time())

    def stop(self, name, timeout_seconds: int = 15):
        self.containers[name]["State"]["Running"] = False

    def remove(self, name):
        return self.containers.pop(name, None) is not None

    def update_limits(self, name, cpus, memory_mb, pids_limit=None):
        self.updates.append((name, cpus, memory_mb))

    def volume_names(self):
        return sorted(self.volumes)

    def volume_remove(self, name):
        if name in self.volumes:
            self.volumes.discard(name)
            return True
        return False

    def volume_mountpoint(self, name):
        return None

    def stats(self, names):
        return {n: {"Name": n, "CPUPerc": "1.00%", "MemUsage": "100MiB / 1GiB"} for n in names}


class FakeStorage:
    def __init__(self) -> None:
        self.disks: Dict[str, Dict[str, Any]] = {}
        self.fail_create: Optional[str] = None

    def create(self, name, size_gb):
        if self.fail_create:
            raise storage.StorageError(self.fail_create)
        self.disks[name] = {"size": size_gb, "mounted": True}

    def populate_home(self, name, image):
        self.disks[name]["populated"] = image

    def remove(self, name):
        self.disks.pop(name, None)

    def mount(self, name):
        if name not in self.disks:
            raise storage.StorageError("disk image missing")
        self.disks[name]["mounted"] = True

    def umount(self, name):
        if name in self.disks:
            self.disks[name]["mounted"] = False

    def is_mounted(self, name):
        return bool(self.disks.get(name, {}).get("mounted"))

    def exists(self, name):
        return name in self.disks

    def usage(self, name):
        disk = self.disks.get(name)
        return (1024 * 1024, disk["size"] * 1024 ** 3) if disk else None

    def resize(self, name, size_gb):
        self.disks[name]["size"] = size_gb

    def list_names(self):
        return {n: {"image": True, "mountpoint": True} for n in self.disks}


@pytest.fixture
def env(tmp_path, monkeypatch):
    config.reset()
    monkeypatch.setenv("ALBERT_INSTALL_DIR", str(tmp_path / "opt"))
    monkeypatch.setenv("ALBERT_ENV_FILE", str(tmp_path / "albert.env"))
    monkeypatch.setenv("ALBERT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("ALBERT_NGINX_DIR", str(tmp_path / "nginx"))
    monkeypatch.setenv("ALBERT_NGINX_RELOAD", "0")
    monkeypatch.setenv("ALBERT_PUBLIC_URL", "https://sandbox.test")
    monkeypatch.setenv("ALBERT_NGINX_ACCESS_LOG", str(tmp_path / "access.log"))
    monkeypatch.setenv("ALBERT_MCPHUB_SETTLE_SECONDS", "0")
    for key in ("ALBERT_DEFAULT_CPUS", "ALBERT_DEFAULT_MEMORY_MB", "ALBERT_DEFAULT_DISK_GB",
                "ALBERT_MAX_AGE_SECONDS", "ALBERT_INACTIVITY_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    s = config.settings()

    fake = FakeDocker()
    for attr in ("available", "image_id", "inspect", "inspect_one", "managed_container_names", "running_names",
                 "run_container", "start", "stop", "remove", "update_limits", "volume_names", "volume_remove",
                 "volume_mountpoint", "stats"):
        monkeypatch.setattr(dockerctl, attr, getattr(fake, attr))
    disks = FakeStorage()
    for attr in ("create", "populate_home", "remove", "mount", "umount", "is_mounted", "exists", "usage",
                 "resize", "list_names"):
        monkeypatch.setattr(storage, attr, getattr(disks, attr))
    monkeypatch.setattr(ports, "listening_ports", lambda: set())
    monkeypatch.setattr(sandboxes, "wait_for_mcphub", lambda port: True)
    monkeypatch.setattr(sandboxes, "host_cpus", lambda: 16)
    monkeypatch.setattr(sandboxes, "host_memory_mb", lambda: 65536)

    db.init_db()
    yield {"settings": s, "docker": fake, "disks": disks, "tmp": tmp_path}
    config.reset()


@pytest.fixture
def make_client(env):
    from albert_sandbox import clients

    def _make(label: str = "Kunde", **fields):
        with db.session() as conn:
            row, raw = clients.create(conn, label, **fields)
        return row, raw

    return _make
