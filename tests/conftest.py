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

from albert_sandbox import config, db, dockerctl, netctl, ports, sandboxes, storage  # noqa: E402


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


CURRENT_IMAGE_ID = "sha256:currentimage"


class FakeDocker:
    def __init__(self) -> None:
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.volumes: set = set()
        self.run_calls: List[List[str]] = []
        self.fail_run: Optional[str] = None
        self.fail_image: Optional[str] = None  # only runs with this image fail
        self.updates: List[tuple] = []
        self.networks: Dict[str, Dict[str, Any]] = {}
        self.copied_in: List[tuple] = []
        self.files_tar: Dict[str, bytes] = {}

    def add_container(self, name: str, labels: Dict[str, str], running: bool = True,
                      created: Optional[float] = None, image: str = "albert-ai-sandbox:latest",
                      host_config: Optional[Dict[str, Any]] = None, networks: Optional[List[str]] = None,
                      image_id: str = "sha256:oldimage") -> None:
        created = created if created is not None else time.time()
        self.containers[name] = {
            "Id": f"id-{name}",
            "Name": f"/{name}",
            "Created": _iso(created),
            "Image": image_id,
            "State": {"Running": running, "Status": "running" if running else "exited",
                      "StartedAt": _iso(created), "FinishedAt": "0001-01-01T00:00:00Z"},
            "Config": {"Image": image, "Labels": dict(labels)},
            "HostConfig": host_config if host_config is not None
            else {"CapAdd": ["CAP_SYS_ADMIN"], "SecurityOpt": ["seccomp=unconfined"]},
            "NetworkSettings": {"Networks": {n: {} for n in (networks or ["bridge"])}},
            "SizeRw": 1234,
        }

    # --- dockerctl API -----------------------------------------------------
    def available(self) -> bool:
        return True

    def image_id(self, image: str) -> Optional[str]:
        return CURRENT_IMAGE_ID

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

    def run_container(self, args, running: bool = True):
        self.run_calls.append(list(args))
        if self.fail_run or (self.fail_image and args[-1] == self.fail_image):
            raise dockerctl.DockerError(self.fail_run or "image broken")
        name = args[args.index("--name") + 1]
        if name in self.containers:
            raise dockerctl.DockerError(f"Conflict. The container name {name} is already in use")
        labels: Dict[str, str] = {}
        host_config: Dict[str, Any] = {"CapAdd": None, "SecurityOpt": None, "CapDrop": [], "Init": False}
        network = "bridge"
        for i, a in enumerate(args):
            if a == "--label":
                k, v = args[i + 1].split("=", 1)
                labels[k] = v
            elif a == "--network":
                network = args[i + 1]
            elif a == "--cap-drop":
                host_config["CapDrop"].append(args[i + 1])
            elif a == "--cap-add":
                host_config["CapAdd"] = (host_config["CapAdd"] or []) + [args[i + 1]]
            elif a == "--init":
                host_config["Init"] = True
        image_id = CURRENT_IMAGE_ID if args[-1] == "albert-ai-sandbox:latest" else args[-1]
        self.add_container(name, labels, running=running, image=args[-1], host_config=host_config,
                           networks=[network], image_id=image_id)
        return f"id-{name}"

    def create_container(self, args):
        return self.run_container(args, running=False)

    def network_inspect(self, name):
        return self.networks.get(name)

    def network_create(self, name, subnet, bridge, labels):
        self.networks[name] = {"Name": name, "Options": {"com.docker.network.bridge.enable_icc": "false"},
                               "IPAM": {"Config": [{"Subnet": subnet}]}}

    def network_connect(self, network, container):
        self.containers[container]["NetworkSettings"]["Networks"][network] = {}

    def network_disconnect(self, network, container):
        self.containers[container]["NetworkSettings"]["Networks"].pop(network, None)

    def copy_out_tar(self, container, path):
        return self.files_tar.get(container)

    def copy_in_tar(self, container, dest, data):
        self.copied_in.append((container, dest, data))

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
                 "run_container", "create_container", "start", "stop", "remove", "update_limits", "volume_names",
                 "volume_remove", "volume_mountpoint", "stats", "network_inspect", "network_create",
                 "network_connect", "network_disconnect", "copy_out_tar", "copy_in_tar"):
        monkeypatch.setattr(dockerctl, attr, getattr(fake, attr))
    firewall = {"ok": False, "applied": 0, "fail": None}

    def fake_apply():
        if firewall["fail"]:
            raise netctl.NetworkError(firewall["fail"])
        firewall["ok"] = True
        firewall["applied"] += 1
        return {"enabled": True, "applied": True}

    monkeypatch.setattr(netctl, "firewall_status", lambda: {"enabled": True, "ok": firewall["ok"]})
    monkeypatch.setattr(netctl, "apply_firewall", fake_apply)
    disks = FakeStorage()
    for attr in ("create", "populate_home", "remove", "mount", "umount", "is_mounted", "exists", "usage",
                 "resize", "list_names"):
        monkeypatch.setattr(storage, attr, getattr(disks, attr))
    monkeypatch.setattr(ports, "listening_ports", lambda: set())
    monkeypatch.setattr(sandboxes, "wait_for_mcphub", lambda port: True)
    monkeypatch.setattr(sandboxes, "host_cpus", lambda: 16)
    monkeypatch.setattr(sandboxes, "host_memory_mb", lambda: 65536)

    db.init_db()
    yield {"settings": s, "docker": fake, "disks": disks, "tmp": tmp_path, "firewall": firewall}
    config.reset()


@pytest.fixture
def make_client(env):
    from albert_sandbox import clients

    def _make(label: str = "Kunde", **fields):
        with db.session() as conn:
            row, raw = clients.create(conn, label, **fields)
        return row, raw

    return _make
