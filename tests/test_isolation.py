import pytest

from albert_sandbox import config, db, netctl, reconcile, sandboxes
from albert_sandbox.errors import Conflict, SandboxError

NET = "albert-sandbox"


def _args(env):
    return env["docker"].run_calls[-1]


def test_new_sandbox_is_isolated_and_hardened(env, make_client):
    client, _ = make_client()
    result = sandboxes.create(client["id"])
    args = _args(env)
    assert args[args.index("--network") + 1] == NET
    assert "--init" in args
    drops = [args[i + 1] for i, a in enumerate(args) if a == "--cap-drop"]
    assert {"NET_RAW", "MKNOD"} <= set(drops)
    assert not any("SYS_ADMIN" in a for a in args)
    assert not any("unconfined" in a for a in args)
    assert env["docker"].networks[NET]["Options"]["com.docker.network.bridge.enable_icc"] == "false"
    assert env["firewall"]["ok"] is True
    assert result["outdated"] == []


def test_no_sandbox_without_firewall(env, make_client):
    client, _ = make_client()
    env["firewall"]["fail"] = "iptables not found"
    with pytest.raises(SandboxError):
        sandboxes.create(client["id"])
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0] == 0
    assert env["docker"].containers == {}


def test_network_with_inter_container_traffic_is_refused(env, make_client):
    client, _ = make_client()
    env["docker"].networks[NET] = {"Options": {"com.docker.network.bridge.enable_icc": "true"},
                                   "IPAM": {"Config": [{"Subnet": "172.30.0.0/16"}]}}
    with pytest.raises(netctl.NetworkError):
        sandboxes.create(client["id"])


def test_start_moves_old_container_into_isolated_network(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    sandboxes.stop(name)
    env["docker"].containers[name]["NetworkSettings"]["Networks"] = {"bridge": {}}
    sandboxes.start(name)
    assert list(env["docker"].containers[name]["NetworkSettings"]["Networks"]) == [NET]


def test_reconcile_isolates_running_containers_and_repairs_firewall(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    env["docker"].containers[name]["NetworkSettings"]["Networks"] = {"bridge": {}, NET: {}}
    env["firewall"]["ok"] = False
    preview = reconcile.reconcile(dry_run=True)
    kinds = {(a["action"], a["target"]) for a in preview["actions"]}
    assert ("isolate-network", name) in kinds and ("apply-firewall", "iptables") in kinds
    assert "bridge" in env["docker"].containers[name]["NetworkSettings"]["Networks"]
    reconcile.reconcile()
    assert list(env["docker"].containers[name]["NetworkSettings"]["Networks"]) == [NET]
    assert env["firewall"]["ok"] is True
    assert reconcile.reconcile()["actions"] == []


def test_outdated_detection(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    assert sandboxes.describe(name)["outdated"] == []
    container = env["docker"].containers[name]
    container["Image"] = "sha256:oldimage"
    container["HostConfig"]["CapAdd"] = ["CAP_SYS_ADMIN"]
    assert set(sandboxes.describe(name)["outdated"]) == {"image", "hardening"}


def test_recreate_keeps_ports_credentials_and_data(env, make_client):
    client, _ = make_client()
    created = sandboxes.create(client["id"])
    name = created["name"]
    container = env["docker"].containers[name]
    container["Image"] = "sha256:oldimage"
    container["HostConfig"] = {"CapAdd": ["CAP_SYS_ADMIN"], "SecurityOpt": ["seccomp=unconfined"]}
    container["NetworkSettings"]["Networks"] = {"bridge": {}}

    result = sandboxes.recreate(name)
    assert result["outdated"] == []
    assert result["status"] == "running"
    assert result["ports"] == created["ports"]
    assert name in env["disks"].disks  # disk image (home + uploads) untouched
    with db.session() as conn:
        row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
    assert row["vnc_password"] == created["vncPassword"] and row["state"] == "ready"
    args = _args(env)
    assert f"VNC_PASSWORD={created['vncPassword']}" in args


def test_recreate_of_stopped_sandbox_keeps_it_stopped(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    sandboxes.stop(name)
    assert sandboxes.recreate(name)["status"] == "stopped"


def test_recreate_legacy_sandbox_keeps_volume_and_uploads(env, make_client):
    client, _ = make_client("Legacy")
    s = config.settings()
    s.legacy_registry.parent.mkdir(parents=True, exist_ok=True)
    s.legacy_registry.write_text('[{"name": "sbx-old-1", "port": "6080", "vnc_port": "5900", '
                                 '"mcphub_port": "7080", "filesvc_port": "7280"}]')
    env["docker"].add_container("sbx-old-1", {"albert.manager": "1", "albert.apikey_hash": client["key_hash"]})
    sandboxes.migrate_legacy_registry()
    env["docker"].files_tar["sbx-old-1"] = b"TAR"

    result = sandboxes.recreate("sbx-old-1")
    args = _args(env)
    assert "sbx-old-1_data:/home/ubuntu" in args
    assert "--cpus" not in args  # legacy sandbox without limits stays without limits
    assert env["docker"].copied_in == [("sbx-old-1", "/tmp", b"TAR")]
    assert result["ports"]["vnc"] == "" and result["ports"]["novnc"] == "6080"
    assert result["outdated"] == []


def test_failed_recreate_restores_old_image(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    env["docker"].containers[name]["Image"] = "sha256:oldimage"
    env["docker"].fail_image = "albert-ai-sandbox:latest"
    with pytest.raises(SandboxError) as exc:
        sandboxes.recreate(name)
    assert exc.value.code == "recreate_partial"
    assert name in env["docker"].containers
    assert env["docker"].containers[name]["Config"]["Image"] == "sha256:oldimage"
    with db.session() as conn:
        row = conn.execute("SELECT state, error FROM sandboxes WHERE name=?", (name,)).fetchone()
    assert row["state"] == "ready" and "failed" in row["error"]


def test_failed_recreate_without_fallback_keeps_sandbox_in_error(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    env["docker"].fail_run = "daemon gone"
    with pytest.raises(SandboxError):
        sandboxes.recreate(name)
    with db.session() as conn:
        assert conn.execute("SELECT state FROM sandboxes WHERE name=?", (name,)).fetchone()[0] == "error"
    reconcile.reconcile()  # must not delete a sandbox in state error
    assert name in env["disks"].disks
    env["docker"].fail_run = None
    # The container is gone, so the rebuilt one is created stopped and started on demand.
    assert sandboxes.recreate(name)["status"] == "stopped"
    assert sandboxes.start(name)["status"] == "running"


def test_rebuilding_sandbox_is_protected(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        conn.execute("UPDATE sandboxes SET state='rebuilding', created_at=0 WHERE name=?", (name,))
    env["docker"].containers.pop(name)
    reconcile.reconcile()
    assert name in env["disks"].disks
    for op in (sandboxes.recreate, sandboxes.remove, sandboxes.stop):
        with pytest.raises(Conflict):
            op(name)
    sandboxes.recreate(name, force=True)
    assert name in env["docker"].containers


def test_boot_firewall_never_touches_docker(env, monkeypatch):
    from albert_sandbox import cli, dockerctl
    calls = {}

    def fake_apply(use_docker=True):
        calls["use_docker"] = use_docker
        return {"enabled": True, "applied": True, "subnet": "172.30.0.0/16"}

    def docker_forbidden(*_a, **_k):
        raise AssertionError("docker must not be contacted at boot (socket activation deadlock)")

    monkeypatch.setattr(netctl, "apply_firewall", fake_apply)
    for attr in ("network_inspect", "available", "network_create", "managed_container_names"):
        monkeypatch.setattr(dockerctl, attr, docker_forbidden)
    assert cli.main(["firewall", "apply", "--boot"]) == 0
    assert calls["use_docker"] is False


def test_network_subnet_mismatch_is_reported(env, make_client):
    client, _ = make_client()
    env["docker"].networks[NET] = {"Options": {"com.docker.network.bridge.enable_icc": "false"},
                                   "IPAM": {"Config": [{"Subnet": "172.31.0.0/16"}]}}
    with pytest.raises(netctl.NetworkError) as exc:
        sandboxes.create(client["id"])
    assert "ALBERT_NETWORK_SUBNET" in exc.value.message


def test_firewall_rules_follow_configuration(env):
    config.configure(block_nets=("10.0.0.0/8", "169.254.0.0/16"), allow_nets=("10.0.5.0/24",))
    fwd, inp, fp1 = netctl.desired_rules("172.30.0.0/16")
    flat = [" ".join(r) for r in fwd]
    assert flat[0].startswith("-m conntrack --ctstate RELATED,ESTABLISHED")
    allow = flat.index("-s 172.30.0.0/16 -d 10.0.5.0/24 -j RETURN")
    drop = flat.index("-s 172.30.0.0/16 -d 10.0.0.0/8 -j DROP")
    assert allow < drop
    assert "-s 172.30.0.0/16 -d 169.254.0.0/16 -j DROP" in flat
    assert [" ".join(r) for r in inp][-1] == "-j DROP"
    config.configure(allow_nets=())
    assert netctl.desired_rules("172.30.0.0/16")[2] != fp1
