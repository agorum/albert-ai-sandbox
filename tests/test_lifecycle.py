import pytest

from albert_sandbox import db, nginxctl, sandboxes
from albert_sandbox.errors import Conflict, NotFound, QuotaExceeded, SandboxError


def _arg(args, flag):
    return args[args.index(flag) + 1]


def test_create_uses_global_defaults_1cpu_2gb(env, make_client):
    client, _ = make_client()
    result = sandboxes.create(client["id"])
    args = env["docker"].run_calls[-1]
    assert _arg(args, "--cpus") == "1"
    assert _arg(args, "--memory") == "2048m"
    assert _arg(args, "--memory-swap") == "2048m"
    assert "--pids-limit" in args
    assert result["resources"] == {"cpus": 1.0, "memoryMb": 2048, "diskGb": 10, "storage": "image"}
    assert env["disks"].disks[result["name"]]["size"] == 10
    # ports only on loopback, no VNC port published
    published = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
    assert len(published) == 3 and all(p.startswith("127.0.0.1:") for p in published)
    assert nginxctl.conf_path(result["name"]).exists()
    assert result["status"] == "running"
    assert result["vncPassword"] and result["fileServiceToken"]
    with db.session() as conn:
        assert conn.execute("SELECT state FROM sandboxes").fetchone()["state"] == "ready"


def test_client_limit_is_default_and_ceiling(env, make_client):
    client, _ = make_client(default_cpus=2, default_memory_mb=2048, default_disk_gb=20)
    full = sandboxes.create(client["id"])
    assert full["resources"]["cpus"] == 2 and full["resources"]["memoryMb"] == 2048
    smaller = sandboxes.create(client["id"], resources={"cpus": 1, "memoryMb": 1024, "diskGb": 5})
    assert smaller["resources"] == {"cpus": 1.0, "memoryMb": 1024, "diskGb": 5, "storage": "image"}
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.create(client["id"], resources={"cpus": 4})
    assert exc.value.code == "sandbox_limit_exceeded"


def test_cpu_and_ram_quota_count_running_sandboxes_only(env, make_client):
    client, _ = make_client(quota_cpus=2, quota_memory_mb=4096)  # two sandboxes à 1 core / 2 GB
    a = sandboxes.create(client["id"])
    sandboxes.create(client["id"])
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.create(client["id"])
    assert exc.value.details["resource"] == "cpus"
    sandboxes.stop(a["name"])
    sandboxes.create(client["id"])  # a is stopped -> capacity free again
    with pytest.raises(QuotaExceeded):
        sandboxes.start(a["name"])


def test_ram_quota(env, make_client):
    client, _ = make_client(quota_memory_mb=4096)
    sandboxes.create(client["id"])
    sandboxes.create(client["id"])
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.create(client["id"])
    assert exc.value.details["resource"] == "memoryMb"
    assert exc.value.details["used"] == 4096


def test_disk_quota_counts_stopped_sandboxes(env, make_client):
    client, _ = make_client(quota_disk_gb=20)
    a = sandboxes.create(client["id"])
    sandboxes.create(client["id"])
    sandboxes.stop(a["name"])
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.create(client["id"])
    assert exc.value.details["resource"] == "diskGb"


def test_sandbox_count_quota(env, make_client):
    client, _ = make_client(quota_sandboxes=1)
    sandboxes.create(client["id"])
    with pytest.raises(QuotaExceeded):
        sandboxes.create(client["id"])


def test_failed_create_is_rolled_back_completely(env, make_client):
    client, _ = make_client()
    env["docker"].fail_run = "boom"
    with pytest.raises(SandboxError):
        sandboxes.create(client["id"], requested_name="broken")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0] == 0
    assert env["disks"].disks == {}
    assert nginxctl.list_names() == []


def test_failed_disk_creation_is_rolled_back(env, make_client):
    client, _ = make_client()
    env["disks"].fail_create = "mkfs failed"
    with pytest.raises(SandboxError):
        sandboxes.create(client["id"])
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0] == 0
    assert env["docker"].containers == {}


def test_remove_cleans_everything(env, make_client):
    client, _ = make_client()
    result = sandboxes.create(client["id"])
    name = result["name"]
    sandboxes.remove(name)
    assert name not in env["docker"].containers
    assert name not in env["disks"].disks
    assert not nginxctl.conf_path(name).exists()
    with pytest.raises(NotFound):
        sandboxes.describe(name)


def test_other_client_cannot_see_or_touch_sandbox(env, make_client):
    owner, _ = make_client("A")
    other, _ = make_client("B")
    name = sandboxes.create(owner["id"])["name"]
    for op in (sandboxes.describe, sandboxes.stop, sandboxes.start, sandboxes.remove):
        with pytest.raises(NotFound):
            op(name, other["id"])
    assert sandboxes.list_sandboxes(other["id"]) == []


def test_update_resources_respects_quota_and_disk_only_grows(env, make_client):
    client, _ = make_client(quota_cpus=3)
    name = sandboxes.create(client["id"])["name"]
    updated = sandboxes.update_resources(name, cpus=3, memory_mb=2048, disk_gb=15)
    assert updated["resources"]["cpus"] == 3 and updated["resources"]["diskGb"] == 15
    assert env["docker"].updates[-1] == (name, 3, 2048)
    with pytest.raises(QuotaExceeded):
        sandboxes.update_resources(name, cpus=4)
    with pytest.raises(SandboxError):
        sandboxes.update_resources(name, disk_gb=5)


def test_delete_client_removes_all_sandboxes(env, make_client):
    client, _ = make_client()
    names = [sandboxes.create(client["id"])["name"] for _ in range(2)]
    result = sandboxes.delete_client(client["id"])
    assert sorted(result["removedSandboxes"]) == sorted(names)
    assert env["docker"].containers == {}
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0


def test_start_of_creating_sandbox_is_conflict(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        conn.execute("UPDATE sandboxes SET state='creating' WHERE name=?", (name,))
    with pytest.raises(Conflict):
        sandboxes.start(name)


def test_name_normalisation():
    assert sandboxes.normalize_requested_name("Mein Test_01!!") == "mein-test-01"
    assert sandboxes.normalize_requested_name("..--") == "custom"
    assert len(sandboxes.normalize_requested_name("x" * 100)) == 42
