"""Sandbox profiles (own images per client) and internet access per client."""
import sqlite3
from pathlib import Path

import pytest

from albert_sandbox import clients, config, db, netctl, profiles, reconcile, sandboxes, web
from albert_sandbox.errors import BadRequest, Conflict, Unavailable

ONLINE, OFFLINE = "albert-sandbox", "albert-sandbox-offline"
DEAD_DNS = "127.0.0.254"


@pytest.fixture
def http(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    app = web.create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    # The admin UI only answers on its own nginx server (port), which sets this header.
    client.environ_base["HTTP_X_ALBERT_ADMIN_GATE"] = "1"
    return client


def _login(http):
    from albert_sandbox import admins
    with db.session() as conn:
        admins.create(conn, "profile-admin", "sehr-geheim-123")
    response = http.post("/admin/api/login", json={"username": "profile-admin", "password": "sehr-geheim-123"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.get_json()["csrf"]}


def _profile(name="Office", **fields):
    fields.setdefault("apt_packages", "libreoffice tesseract-ocr-deu")
    with db.session() as conn:
        return profiles.create(conn, name, **fields)


def _built(name="Office", **fields):
    row = _profile(name, **fields)
    profiles.request_build(row["id"], actor="test")
    with db.session() as conn:
        return profiles.get(conn, row["id"])


def _status(profile_id):
    with db.session() as conn:
        return profiles.serialize(conn, profiles.get(conn, profile_id), profiles.base_image_id())


def _networks(env, name):
    return list(env["docker"].containers[name]["NetworkSettings"]["Networks"])


def _dns(env, name):
    return env["docker"].containers[name]["HostConfig"].get("Dns") or []


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------

def test_package_lists_are_validated(env):
    assert profiles.parse_packages("apt", "libreoffice  # Office\n tesseract-ocr-deu=4.1.1-2 libreoffice") == \
        ["libreoffice", "tesseract-ocr-deu=4.1.1-2"]
    assert profiles.parse_packages("pip", "pandas openpyxl==3.1.5 requests[socks]>=2.31,<3") == \
        ["pandas", "openpyxl==3.1.5", "requests[socks]>=2.31,<3"]
    assert profiles.parse_packages("npm", ["@mermaid-js/mermaid-cli@11.4.2", "typescript"]) == \
        ["@mermaid-js/mermaid-cli@11.4.2", "typescript"]
    for kind, bad in (("apt", "curl;rm -rf /"), ("apt", "$(id)"), ("pip", "x && y"), ("npm", "a`b`")):
        with pytest.raises(BadRequest) as exc:
            profiles.parse_packages(kind, bad)
        assert exc.value.code == "invalid_package"


def test_dockerfile_builds_on_the_shipped_image(env):
    row = _profile(pip_packages="pandas", npm_packages="typescript", setup_script="echo hallo > /etc/motd")
    text = profiles.dockerfile(row)
    assert text.splitlines()[1] == "FROM albert-ai-sandbox:latest"
    assert "apt-get install -y libreoffice tesseract-ocr-deu" in text
    assert "pip3 install --no-cache-dir pandas" in text and "npm install -g --no-fund --no-audit typescript" in text
    assert "COPY setup.sh" in text and text.rstrip().endswith("USER root")


def test_build_assign_and_create_sandbox_from_profile(env, make_client):
    row = _built(setup_script="echo ok")
    status = _status(row["id"])
    assert status["status"] == "ready" and status["image"] == "albert-ai-sandbox-profile-1:1"
    build = env["docker"].builds[-1]
    assert build["labels"] == {"albert.profile": "1", "albert.profile.seq": "1"} and build["setup"] == "echo ok\n"
    log = profiles.read_log(row["id"])
    assert "naming to docker.io/library/albert-ai-sandbox-profile-1:1" in log["text"] and not log["running"]

    client, _ = make_client(profile_id=row["id"])
    result = sandboxes.create(client["id"])
    assert env["docker"].run_calls[-1][-1] == "albert-ai-sandbox-profile-1:1"
    assert env["disks"].disks[result["name"]]["populated"] == "albert-ai-sandbox-profile-1:1"
    assert result["profile"]["name"] == "Office" and result["profile"]["packages"]["apt"][0] == "libreoffice"
    assert result["internet"] is True and result["outdated"] == []


def test_unbuilt_profile_refuses_new_sandboxes(env, make_client):
    row = _profile()
    client, _ = make_client(profile_id=row["id"])
    with pytest.raises(Unavailable) as exc:
        sandboxes.create(client["id"])
    assert exc.value.code == "profile_not_ready" and "Office" in exc.value.message
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0] == 0
    with db.session() as conn, pytest.raises(BadRequest):
        clients.update(conn, client["id"], {"profile_id": 999})


def test_profile_outdated_after_change_and_base_update(env, make_client):
    row = _built()
    client, _ = make_client(profile_id=row["id"])
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        profiles.update(conn, row["id"], {"pip_packages": "pandas"})
    assert _status(row["id"])["outdatedReasons"] == ["config"]
    # only base image updates are rebuilt automatically (install.sh)
    assert profiles.rebuild_outdated("test") == {}
    env["docker"].images["albert-ai-sandbox:latest"] = "sha256:newbase"
    assert _status(row["id"])["outdatedReasons"] == ["config", "base"]
    assert profiles.rebuild_outdated("test") == {"Office": "ok"}
    status = _status(row["id"])
    assert status["status"] == "ready" and status["image"] == "albert-ai-sandbox-profile-1:2"
    assert "pip3 install --no-cache-dir pandas" in env["docker"].builds[-1]["dockerfile"]
    # the sandbox still runs the previous profile image until it is rebuilt
    assert sandboxes.describe(name)["outdated"] == ["image"]
    assert sandboxes.recreate(name)["outdated"] == []
    assert env["docker"].run_calls[-1][-1] == "albert-ai-sandbox-profile-1:2"


def test_failed_build_keeps_previous_image(env, make_client):
    row = _built()
    with db.session() as conn:
        profiles.update(conn, row["id"], {"apt_packages": "gibtsnicht"})
    env["docker"].fail_build = "E: Unable to locate package gibtsnicht"
    profiles.request_build(row["id"], actor="test")
    status = _status(row["id"])
    assert status["build"]["state"] == "failed" and "Unable to locate package" in status["build"]["error"]
    assert status["image"] == "albert-ai-sandbox-profile-1:1" and status["status"] == "outdated"
    client, _ = make_client(profile_id=row["id"])
    assert env["docker"].run_calls == [] and sandboxes.create(client["id"])["profile"]["id"] == row["id"]
    assert env["docker"].run_calls[-1][-1] == "albert-ai-sandbox-profile-1:1"


def test_one_build_at_a_time_and_stale_builds_expire(env):
    row = _profile()
    profiles.request_build(row["id"], actor="test", spawn=False)
    with pytest.raises(Conflict):
        profiles.request_build(row["id"], actor="test")
    with db.session() as conn:
        conn.execute("UPDATE profiles SET build_heartbeat_at=0, build_started_at=0")
    preview = reconcile.reconcile(dry_run=True)
    assert ("fail-build", "Office") in {(a["action"], a["target"]) for a in preview["actions"]}
    with db.session() as conn:
        profiles.list_all(conn)  # listing (admin UI) expires it as well
    assert _status(row["id"])["status"] == "failed"
    assert "interrupted" in _status(row["id"])["build"]["error"]
    profiles.request_build(row["id"], actor="test")
    assert _status(row["id"])["status"] == "ready"


def test_delete_profile_and_cleanup_of_old_images(env, make_client):
    row = _built()
    client, _ = make_client(profile_id=row["id"])
    name = sandboxes.create(client["id"])["name"]
    profiles.request_build(row["id"], actor="test")  # -> :2, the sandbox still runs :1
    assert {"albert-ai-sandbox-profile-1:1", "albert-ai-sandbox-profile-1:2"} <= set(env["docker"].images)
    reconcile.reconcile()
    assert "albert-ai-sandbox-profile-1:1" in env["docker"].images  # still used by a container
    sandboxes.recreate(name)
    reconcile.reconcile()
    assert "albert-ai-sandbox-profile-1:1" not in env["docker"].images
    with db.session() as conn, pytest.raises(BadRequest) as exc:
        profiles.delete(conn, row["id"])
    assert exc.value.code == "profile_in_use"
    sandboxes.remove(name)
    with db.session() as conn:
        clients.update(conn, client["id"], {"profile_id": None})
        with pytest.raises(BadRequest):  # still allowed for the client
            profiles.delete(conn, row["id"])
        clients.update(conn, client["id"], {"allowed_profile_ids": []})
        profiles.delete(conn, row["id"])
    report = reconcile.reconcile()
    assert ("remove-image", "albert-ai-sandbox-profile-1:2") in {(a["action"], a["target"]) for a in report["actions"]}
    assert not any(t.startswith("albert-ai-sandbox-profile") for t in env["docker"].images)


# --------------------------------------------------------------------------
# internet access
# --------------------------------------------------------------------------

def test_offline_client_gets_offline_network_and_dead_dns(env, make_client):
    client, _ = make_client(allow_internet=False)
    result = sandboxes.create(client["id"])
    args = env["docker"].run_calls[-1]
    assert args[args.index("--network") + 1] == OFFLINE and args[args.index("--dns") + 1] == DEAD_DNS
    assert env["docker"].networks[OFFLINE]["Options"]["com.docker.network.bridge.enable_icc"] == "false"
    assert result["internet"] is False and result["outdated"] == []
    online, _ = make_client("Online")
    sandboxes.create(online["id"])
    args = env["docker"].run_calls[-1]
    assert args[args.index("--network") + 1] == ONLINE and "--dns" not in args


def test_switching_internet_moves_and_rebuilds_sandboxes(env, make_client):
    client, _ = make_client()
    running = sandboxes.create(client["id"])["name"]
    stopped = sandboxes.create(client["id"])["name"]
    sandboxes.stop(stopped)
    with db.session() as conn:
        clients.update(conn, client["id"], {"allow_internet": False})
    assert set(sandboxes.describe(running)["outdated"]) == {"network"}
    results = sandboxes.apply_network_policy(client["id"], actor="test")
    assert results == {running: "ok", stopped: "ok"}
    for name in (running, stopped):
        assert _networks(env, name) == [OFFLINE] and _dns(env, name) == [DEAD_DNS]
        assert sandboxes.describe(name)["outdated"] == []
    assert sandboxes.describe(running)["status"] == "running"
    assert sandboxes.describe(stopped)["status"] == "stopped"
    # and back: nothing left over from the offline setting
    with db.session() as conn:
        clients.update(conn, client["id"], {"allow_internet": True})
    sandboxes.apply_network_policy(client["id"], actor="test")
    assert _networks(env, running) == [ONLINE] and _dns(env, running) == []


def test_start_rebuilds_a_sandbox_with_the_wrong_dns(env, make_client):
    client, _ = make_client(allow_internet=False)
    name = sandboxes.create(client["id"])["name"]
    sandboxes.stop(name)
    with db.session() as conn:
        clients.update(conn, client["id"], {"allow_internet": True})
    result = sandboxes.start(name)
    assert result["status"] == "running" and result["outdated"] == [] and result["internet"] is True
    assert _networks(env, name) == [ONLINE] and _dns(env, name) == []


def test_reconcile_enforces_the_offline_network(env, make_client):
    client, _ = make_client(allow_internet=False)
    name = sandboxes.create(client["id"])["name"]
    env["docker"].containers[name]["NetworkSettings"]["Networks"] = {ONLINE: {}}
    env["docker"].add_container("sbx-unknown-1", {"albert.manager": "1"}, networks=[ONLINE])
    reconcile.reconcile()
    assert _networks(env, name) == [OFFLINE]
    assert _networks(env, "sbx-unknown-1") == [OFFLINE]  # unknown containers: most restrictive


def test_reconcile_rebuilds_running_sandbox_with_wrong_dns(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        clients.update(conn, client["id"], {"allow_internet": False})
    report = reconcile.reconcile()
    kinds = {(a["action"], a["target"]) for a in report["actions"]}
    assert ("isolate-network", name) in kinds and ("rebuild-network", name) in kinds
    assert _networks(env, name) == [OFFLINE] and _dns(env, name) == [DEAD_DNS]
    assert sandboxes.describe(name)["status"] == "running"


# --------------------------------------------------------------------------
# APIs
# --------------------------------------------------------------------------

def test_client_api_reports_internet_and_profile(env, make_client, http):
    row = _built(pip_packages="pandas")
    client, key = make_client(profile_id=row["id"], allow_internet=False)
    quota = http.get("/quota", headers={"Authorization": f"Bearer {key}", "X-Albert-User": "u1"}).get_json()
    assert quota["internetAccess"] is False
    assert quota["profile"]["name"] == "Office" and quota["profile"]["packages"]["pip"] == ["pandas"]
    created = http.post("/containers", headers={"Authorization": f"Bearer {key}", "X-Albert-User": "u1"},
                        json={}).get_json()["container"]
    assert created["internet"] is False and created["profile"]["id"] == row["id"]


def test_admin_api_profiles_and_client_settings(env, make_client, http):
    h = _login(http)
    response = http.post("/admin/api/profiles", headers=h, json={
        "name": "Daten", "description": "Pandas & Co.",
        "packages": {"apt": "jq", "pip": ["pandas", "openpyxl==3.1.5"], "npm": ""}})
    assert response.status_code == 201
    profile = response.get_json()["profile"]
    assert profile["status"] == "new" and profile["packages"]["pip"] == ["pandas", "openpyxl==3.1.5"]
    assert "FROM albert-ai-sandbox:latest" in profile["dockerfile"]
    bad = http.post("/admin/api/profiles", headers=h, json={"name": "X", "packages": {"apt": "a;b"}})
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_package"

    built = http.post(f"/admin/api/profiles/{profile['id']}/build", headers=h)
    assert built.status_code == 202
    assert http.get(f"/admin/api/profiles/{profile['id']}").get_json()["profile"]["status"] == "ready"
    log = http.get(f"/admin/api/profiles/{profile['id']}/log?offset=0").get_json()
    assert log["seq"] == 1 and log["offset"] > 0 and not log["running"]

    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    data = http.patch(f"/admin/api/clients/{client['id']}", headers=h,
                      json={"profileId": profile["id"], "allowInternet": False}).get_json()["client"]
    assert data["profile"] == {"id": profile["id"], "name": "Daten"} and data["allowInternet"] is False
    assert data["networkPolicy"] == "applying"
    # applied right away (inline in tests); the rebuild for the DNS setting keeps the sandbox's
    # own profile (the shipped image) - a new default only applies to new sandboxes
    assert _networks(env, name) == [OFFLINE] and _dns(env, name) == [DEAD_DNS]
    assert sandboxes.describe(name)["outdated"] == []
    assert env["docker"].run_calls[-1][-1] == "albert-ai-sandbox:latest"
    assert data["allowedProfileIds"] == [profile["id"]] and data["allowStandardImage"] is True
    other = _built("Anders", apt_packages="jq")
    http.patch(f"/admin/api/clients/{client['id']}", headers=h, json={"profileId": other["id"]})
    assert sandboxes.describe(name)["outdated"] == []

    listed = {p["name"]: p for p in http.get("/admin/api/profiles").get_json()["profiles"]}
    assert listed["Anders"]["clients"] == [{"id": client["id"], "label": "Kunde", "default": True}]
    assert listed["Daten"]["clients"] == [{"id": client["id"], "label": "Kunde", "default": False}]
    in_use = http.delete(f"/admin/api/profiles/{other['id']}", headers=h)
    assert in_use.status_code == 400 and in_use.get_json()["error"] == "profile_in_use"
    http.patch(f"/admin/api/clients/{client['id']}", headers=h, json={"allowedProfileIds": [other["id"]]})
    assert http.delete(f"/admin/api/profiles/{profile['id']}", headers=h).status_code == 200
    overview = http.get("/admin/api/overview").get_json()
    assert overview["profiles"] == 1 and overview["clientsOffline"] == 1
    assert overview["isolation"]["offline"]["network"] == OFFLINE


# --------------------------------------------------------------------------
# upgrade
# --------------------------------------------------------------------------

def test_upgrade_keeps_internet_and_standard_image(env):
    path = config.settings().db_path
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(db.BASE_SCHEMA)
    conn.executescript(db.V2_SCHEMA)
    for column, ctype in db.API_KEY_V2_COLUMNS.items():
        conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ctype}")
    for column, (ctype, _) in db.API_KEY_V4_COLUMNS.items():
        conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ctype}")
    conn.execute("INSERT INTO api_keys(key_hash, label, created_at) VALUES(?, 'Alt', 1)", ("b" * 64,))
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # idempotent
    with db.session() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        old = conn.execute("SELECT allow_internet, profile_id FROM api_keys").fetchone()
        assert old["allow_internet"] == 1 and old["profile_id"] is None
        new, _ = clients.create(conn, "Neu")
        assert new["allow_internet"] == 1
        assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0


def test_offline_network_subnet_is_checked(env, make_client):
    client, _ = make_client(allow_internet=False)
    env["docker"].networks[OFFLINE] = {"Options": {"com.docker.network.bridge.enable_icc": "false"},
                                       "IPAM": {"Config": [{"Subnet": "10.99.0.0/16"}]}}
    with pytest.raises(netctl.NetworkError) as exc:
        sandboxes.create(client["id"])
    assert "ALBERT_OFFLINE_SUBNET" in exc.value.message
