"""Internal targets ("allow nets"): global (admin settings / ALBERT_ALLOW_NETS)
and per client, with an own sandbox network for clients with own targets."""
import sqlite3

import pytest

from albert_sandbox import cli, clients, config, db, netctl, reconcile, sandboxes, web
from albert_sandbox.errors import BadRequest

NET = "albert-sandbox"


@pytest.fixture
def admin(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    app = web.create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    client.environ_base["HTTP_X_ALBERT_ADMIN_GATE"] = "1"
    from albert_sandbox import admins
    with db.session() as conn:
        admins.create(conn, "net-admin", "sehr-geheim-123")
    data = client.post("/admin/api/login", json={"username": "net-admin", "password": "sehr-geheim-123"})
    assert data.status_code == 200
    client.csrf = {"X-CSRF-Token": data.get_json()["csrf"]}
    return client


def _networks(env, name):
    return sorted(env["docker"].containers[name]["NetworkSettings"]["Networks"])


def _client(client_id):
    with db.session() as conn:
        return clients.get(conn, client_id)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_targets_are_normalised():
    assert netctl.parse_nets("10.0.5.20\n10.0.5.1/24, 10.0.5.20 ;192.168.7.0/24") == \
        ["10.0.5.20/32", "10.0.5.0/24", "192.168.7.0/24"]
    assert netctl.parse_nets(["10.0.5.20", " 10.0.6.0/24 "]) == ["10.0.5.20/32", "10.0.6.0/24"]
    assert netctl.parse_nets("") == [] and netctl.parse_nets(None) == [] and netctl.parse_nets([]) == []


@pytest.mark.parametrize("value", ["kein-netz", "10.0.5.300", "fd00::1", "0.0.0.0/0", "172.30.4.0/24",
                                   "172.31.0.9", "172.29.3.0/24", "172.16.0.0/12", ["10.0.0.1", 5]])
def test_invalid_targets_are_refused(env, value):
    with pytest.raises(BadRequest) as exc:
        netctl.parse_nets(value)
    assert exc.value.code == "invalid_net"


# --------------------------------------------------------------------------
# global targets
# --------------------------------------------------------------------------

def test_global_targets_come_from_the_config_until_set_in_the_ui(env, admin):
    config.configure(allow_nets=("10.0.9.0/24",))
    view = admin.get("/admin/api/settings").get_json()["settings"]
    assert view["allowNets"]["nets"] == ["10.0.9.0/24"] and view["allowNets"]["source"] == "config"
    applied = env["firewall"]["applied"]
    saved = admin.put("/admin/api/settings/allow-nets", json={"allowNets": ["10.0.5.20", "10.0.6.0/24"]},
                      headers=admin.csrf)
    assert saved.status_code == 200
    info = saved.get_json()["settings"]["allowNets"]
    assert info["nets"] == ["10.0.5.20/32", "10.0.6.0/24"] and info["source"] == "settings"
    assert info["configNets"] == ["10.0.9.0/24"]
    assert env["firewall"]["applied"] == applied + 1  # applied at once
    assert netctl.policy()[0] == ["10.0.5.20/32", "10.0.6.0/24"]
    # empty list = nothing allowed (not: back to the config file)
    admin.put("/admin/api/settings/allow-nets", json={"allowNets": []}, headers=admin.csrf)
    assert netctl.policy()[0] == []
    reset = admin.put("/admin/api/settings/allow-nets", json={"reset": True}, headers=admin.csrf)
    assert reset.get_json()["settings"]["allowNets"]["source"] == "config"
    assert netctl.policy()[0] == ["10.0.9.0/24"]


def test_invalid_or_refused_global_targets_change_nothing(env, admin):
    admin.put("/admin/api/settings/allow-nets", json={"allowNets": ["10.0.5.0/24"]}, headers=admin.csrf)
    bad = admin.put("/admin/api/settings/allow-nets", json={"allowNets": ["0.0.0.0/0"]}, headers=admin.csrf)
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_net"
    env["firewall"]["fail"] = "iptables broke"
    refused = admin.put("/admin/api/settings/allow-nets", json={"allowNets": ["10.0.8.0/24"]}, headers=admin.csrf)
    assert refused.get_json()["error"] == "firewall_failed"
    assert netctl.policy()[0] == ["10.0.5.0/24"]


def test_cli_sets_global_and_client_targets(env, make_client, capsys):
    client, _ = make_client()
    assert cli.main(["allow-nets", "set", "10.0.5.0/24", "10.0.6.7"]) == 0
    assert netctl.policy()[0] == ["10.0.5.0/24", "10.0.6.7/32"]
    assert cli.main(["client", "set", str(client["id"]), "--allow-nets", "10.0.7.0/24,10.0.7.9"]) == 0
    row = _client(client["id"])
    assert clients.allow_nets(row) == ["10.0.7.0/24", "10.0.7.9/32"] and row["net_subnet"] == "172.29.0.0/24"
    capsys.readouterr()
    assert cli.main(["allow-nets", "show"]) == 0
    out = capsys.readouterr().out
    assert "10.0.5.0/24" in out and "10.0.7.9/32" in out and f"{NET}-c{client['id']}" in out
    assert cli.main(["client", "set", str(client["id"]), "--allow-nets", "none"]) == 0
    assert clients.allow_nets(_client(client["id"])) == []
    assert cli.main(["allow-nets", "reset"]) == 0
    assert netctl.global_allow_nets()["source"] == "config"


# --------------------------------------------------------------------------
# targets per client
# --------------------------------------------------------------------------

def test_client_with_own_targets_gets_an_own_network(env, admin):
    created = admin.post("/admin/api/clients", json={"label": "Kunde A", "allowNets": "10.0.5.20"},
                         headers=admin.csrf).get_json()["client"]
    assert created["allowNets"] == ["10.0.5.20/32"] and created["netSubnet"] == "172.29.0.0/24"
    assert created["network"] == f"{NET}-c{created['id']}"
    other = admin.post("/admin/api/clients", json={"label": "Kunde B", "allowNets": ["10.0.6.0/24"]},
                       headers=admin.csrf).get_json()["client"]
    assert other["netSubnet"] == "172.29.1.0/24"
    plain = admin.post("/admin/api/clients", json={"label": "Kunde C"}, headers=admin.csrf).get_json()["client"]
    assert plain["netSubnet"] is None and plain["network"] == NET and plain["allowNets"] == []

    name = sandboxes.create(created["id"])["name"]
    net = f"{NET}-c{created['id']}"
    assert _networks(env, name) == [net]
    info = env["docker"].networks[net]
    assert info["IPAM"]["Config"][0]["Subnet"] == "172.29.0.0/24"
    assert info["Options"]["com.docker.network.bridge.enable_icc"] == "false"
    assert info["Labels"] == {"albert.manager": "1", "albert.client_id": str(created["id"])}
    assert sandboxes.present(db.connect(), name)["outdated"] == []
    assert _networks(env, sandboxes.create(plain["id"])["name"]) == [NET]


def test_subnet_skips_networks_docker_already_uses(env, make_client):
    env["docker"].networks["fremd"] = {"Name": "fremd", "IPAM": {"Config": [{"Subnet": "172.29.0.0/23"}]}}
    client, _ = make_client(allow_nets="10.0.5.0/24")
    assert client["net_subnet"] == "172.29.2.0/24"


def test_first_own_target_moves_existing_sandboxes_without_rebuild(env, admin, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    before = env["docker"].containers[name]["Id"]
    with db.session() as conn:
        password = conn.execute("SELECT vnc_password FROM sandboxes WHERE name=?", (name,)).fetchone()[0]
    res = admin.patch(f"/admin/api/clients/{client['id']}", json={"allowNets": ["10.0.5.0/24"]},
                      headers=admin.csrf).get_json()["client"]
    assert res["networkPolicy"] == "applying"
    assert _networks(env, name) == [f"{NET}-c{client['id']}"]
    assert env["docker"].containers[name]["Id"] == before  # moved, not rebuilt
    with db.session() as conn:
        assert conn.execute("SELECT vnc_password FROM sandboxes WHERE name=?", (name,)).fetchone()[0] == password
    # Removing the targets keeps the own network (no second move), only the rules change.
    res = admin.patch(f"/admin/api/clients/{client['id']}", json={"allowNets": []},
                      headers=admin.csrf).get_json()["client"]
    assert res["allowNets"] == [] and res["netSubnet"] == "172.29.0.0/24" and "networkPolicy" not in res
    assert _networks(env, name) == [f"{NET}-c{client['id']}"]
    assert netctl.policy()[1] == [netctl.ClientRule("172.29.0.0/24", False, ())]


def test_offline_client_with_own_targets(env, admin, make_client):
    client, _ = make_client(allow_internet=0, allow_nets="10.0.5.0/24")
    name = sandboxes.create(client["id"])["name"]
    container = env["docker"].containers[name]
    assert _networks(env, name) == [f"{NET}-c{client['id']}"]
    assert container["HostConfig"]["Dns"] == [env["settings"].offline_dns]
    # Internet on: same network, the DNS setting needs a rebuild.
    admin.patch(f"/admin/api/clients/{client['id']}", json={"allowInternet": True}, headers=admin.csrf)
    container = env["docker"].containers[name]
    assert _networks(env, name) == [f"{NET}-c{client['id']}"] and not container["HostConfig"].get("Dns")
    assert netctl.policy()[1] == [netctl.ClientRule("172.29.0.0/24", False, ("10.0.5.0/24",))]


def test_reconcile_puts_sandboxes_into_their_own_network(env, make_client):
    client, _ = make_client(allow_nets="10.0.5.0/24")
    name = sandboxes.create(client["id"])["name"]
    own = f"{NET}-c{client['id']}"
    env["docker"].network_disconnect(own, name)
    env["docker"].network_connect(NET, name)
    result = reconcile.reconcile()
    assert any(a["action"] == "isolate-network" and a["target"] == name for a in result["actions"])
    assert _networks(env, name) == [own]


def test_reconcile_removes_networks_of_deleted_clients(env, make_client):
    keep, _ = make_client("bleibt", allow_nets="10.0.5.0/24")
    gone, _ = make_client("weg", allow_nets="10.0.6.0/24")
    for c in (keep, gone):
        netctl.ensure_network(netctl.placement(_client(c["id"])))
    name = sandboxes.create(gone["id"])["name"]
    sandboxes.delete_client(gone["id"])
    assert name not in env["docker"].containers
    result = reconcile.reconcile()
    assert {"action": "remove-network", "target": f"{NET}-c{gone['id']}",
            "detail": "network of a deleted client"} in result["actions"]
    assert f"{NET}-c{gone['id']}" not in env["docker"].networks
    assert f"{NET}-c{keep['id']}" in env["docker"].networks
    assert NET in env["docker"].networks  # shared networks are never touched


# --------------------------------------------------------------------------
# firewall rules
# --------------------------------------------------------------------------

def test_firewall_rules_per_client_network(env):
    config.configure(block_nets=("10.0.0.0/8", "169.254.0.0/16"))
    rules = [netctl.ClientRule("172.29.0.0/24", False, ("10.0.7.0/24",)),
             netctl.ClientRule("172.29.1.0/24", True, ("10.0.8.5/32", "10.0.5.0/24"))]
    fwd, inp, fp = netctl.desired_rules("172.30.0.0/16", "172.31.0.0/16", ["10.0.5.0/24"], rules)
    flat = [" ".join(r) for r in fwd]
    # online client: global + own targets, then the internal networks are dropped, the internet stays
    a = [r for r in flat if r.startswith("-s 172.29.0.0/24")]
    assert a == ["-s 172.29.0.0/24 -d 10.0.5.0/24 -j RETURN", "-s 172.29.0.0/24 -d 10.0.7.0/24 -j RETURN",
                 "-s 172.29.0.0/24 -d 10.0.0.0/8 -j DROP", "-s 172.29.0.0/24 -d 169.254.0.0/16 -j DROP",
                 "-s 172.29.0.0/24 -j RETURN"]
    # offline client: only its targets (no duplicates), then everything is dropped
    b = [r for r in flat if r.startswith("-s 172.29.1.0/24")]
    assert b == ["-s 172.29.1.0/24 -d 10.0.5.0/24 -j RETURN", "-s 172.29.1.0/24 -d 10.0.8.5/32 -j RETURN",
                 "-s 172.29.1.0/24 -j DROP"]
    # the shared networks do not get the targets of the clients
    assert not any("10.0.7.0/24" in r or "10.0.8.5" in r for r in flat if r.startswith("-s 172.3"))
    # client networks the rules do not know get nothing
    assert flat[-1] == "-s 172.29.0.0/16 -j DROP"
    assert netctl.desired_rules("172.30.0.0/16", "172.31.0.0/16", ["10.0.5.0/24"], rules[:1])[2] != fp


def test_policy_reads_the_database_and_fails_closed(env, make_client, monkeypatch):
    make_client("a", allow_nets="10.0.7.0/24")
    make_client("b", allow_internet=0, allow_nets="10.0.8.0/24")
    make_client("c")
    with db.session() as conn:
        netctl.set_global_allow_nets(conn, "10.0.5.0/24")
    assert netctl.policy() == (["10.0.5.0/24"], [netctl.ClientRule("172.29.0.0/24", False, ("10.0.7.0/24",)),
                                                 netctl.ClientRule("172.29.1.0/24", True, ("10.0.8.0/24",))])

    def broken():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "session", broken)
    assert netctl.policy() == ([], [])


def test_upgrade_adds_the_target_columns(env):
    path = env["settings"].db_path
    conn = sqlite3.connect(str(path))
    conn.execute("DROP INDEX idx_api_keys_net_subnet")
    conn.execute("ALTER TABLE api_keys DROP COLUMN net_subnet")
    conn.execute("ALTER TABLE api_keys DROP COLUMN allow_nets")
    conn.execute("INSERT INTO api_keys(key_hash, label, created_at) VALUES('h1', 'Bestand', 1)")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()
    db.init_db()
    with db.session() as c:
        row = c.execute("SELECT * FROM api_keys WHERE label='Bestand'").fetchone()
        assert row["allow_nets"] is None and row["net_subnet"] is None
        assert netctl.placement(row).network == NET
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 7
