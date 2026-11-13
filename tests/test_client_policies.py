"""Client settings that ALBERT | AI has to react to: running limit and persistence."""
import sqlite3
from pathlib import Path

import pytest

from albert_sandbox import clients, config, db, nginxctl, sandboxes, web
from albert_sandbox.errors import Forbidden, QuotaExceeded


@pytest.fixture
def http(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    app = web.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def _count_sandboxes():
    with db.session() as conn:
        return conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0]


# --------------------------------------------------------------------------
# running limit
# --------------------------------------------------------------------------

def test_running_limit_refuses_second_running_sandbox(env, make_client):
    client, _ = make_client(quota_running=1)
    a = sandboxes.create(client["id"])["name"]
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.create(client["id"])
    err = exc.value
    assert err.code == "running_limit_exceeded" and err.status == 409
    assert err.details["runningSandboxes"] == [a] and err.details["limit"] == 1
    assert a in err.message and "stop" in err.message.lower()
    assert _count_sandboxes() == 1 and list(env["docker"].containers) == [a]

    sandboxes.stop(a)
    b = sandboxes.create(client["id"])["name"]  # a is stopped: the slot is free again
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.start(a)
    assert exc.value.code == "running_limit_exceeded" and exc.value.details["runningSandboxes"] == [b]
    sandboxes.restart(b)  # restarting the running one stays within the limit
    sandboxes.stop(b)
    assert sandboxes.start(a)["status"] == "running"


def test_running_limit_zero_and_unlimited(env, make_client):
    blocked, _ = make_client("Gesperrt", quota_running=0)
    with pytest.raises(QuotaExceeded) as exc:
        sandboxes.create(blocked["id"])
    assert exc.value.code == "running_limit_exceeded" and "not allowed to run" in exc.value.message

    free, _ = make_client("Frei")
    for _ in range(3):
        sandboxes.create(free["id"])
    with db.session() as conn:
        assert clients.usage(conn, free["id"], env["docker"].running_names())["running"] == 3


def test_running_limit_via_client_api(env, make_client, http):
    _, key = make_client(quota_running=1)
    first = http.post("/containers", json={}, headers=auth(key)).get_json()["container"]["name"]
    second = http.post("/containers", json={}, headers=auth(key))
    assert second.status_code == 409
    body = second.get_json()
    assert body["error"] == "running_limit_exceeded" and body["runningSandboxes"] == [first]
    assert first in body["message"]
    quota = http.get("/quota", headers=auth(key)).get_json()
    assert quota["quota"]["running"] == 1 and quota["usage"]["running"] == 1


def test_lowering_the_running_limit_keeps_running_sandboxes(env, make_client):
    client, _ = make_client()
    a = sandboxes.create(client["id"])["name"]
    b = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        clients.update(conn, client["id"], {"quota_running": 1})
    assert env["docker"].running_names() == {a, b}  # nothing is stopped automatically
    sandboxes.stop(b)
    with pytest.raises(QuotaExceeded):
        sandboxes.start(b)


# --------------------------------------------------------------------------
# persistent sandboxes
# --------------------------------------------------------------------------

def test_new_clients_may_not_create_persistent_sandboxes(env, make_client):
    client, _ = make_client()
    assert client["allow_persistent"] == 0
    with pytest.raises(Forbidden) as exc:
        sandboxes.create(client["id"], persistent=True)
    assert exc.value.code == "persistent_not_allowed" and exc.value.status == 403
    assert "administrator" in exc.value.message
    assert _count_sandboxes() == 0 and env["docker"].containers == {}

    name = sandboxes.create(client["id"])["name"]
    with pytest.raises(Forbidden):
        sandboxes.set_persistent(name, True)
    assert sandboxes.set_persistent(name, False)["persistent"] is False
    assert sandboxes.describe(name)["persistentAllowed"] is False


def test_allowed_client_and_withdrawn_permission(env, make_client):
    client, _ = make_client(allow_persistent=True)
    name = sandboxes.create(client["id"], persistent=True)["name"]
    assert sandboxes.describe(name)["persistent"] is True

    with db.session() as conn:
        clients.update(conn, client["id"], {"allow_persistent": False})
    # Existing persistent sandboxes are left alone (no surprise deletion) ...
    assert sandboxes.describe(name)["persistent"] is True
    assert sandboxes.set_persistent(name, True)["persistent"] is True  # unchanged -> no error
    # ... but can be switched off, and not on again.
    sandboxes.set_persistent(name, False)
    with pytest.raises(Forbidden):
        sandboxes.set_persistent(name, True)


def test_persistent_refused_via_client_and_admin_api(env, make_client, http):
    _, key = make_client()
    refused = http.post("/containers", json={"persistent": True}, headers=auth(key))
    assert refused.status_code == 403
    assert refused.get_json()["error"] == "persistent_not_allowed"
    name = http.post("/containers", json={}, headers=auth(key)).get_json()["container"]["name"]
    patch = http.patch(f"/containers/{name}/persistent", json={"persistent": True}, headers=auth(key))
    assert patch.status_code == 403 and patch.get_json()["error"] == "persistent_not_allowed"
    assert http.get("/quota", headers=auth(key)).get_json()["persistentAllowed"] is False


def _login(http):
    from albert_sandbox import admins
    with db.session() as conn:
        admins.create(conn, "policy-admin", "sehr-geheim-123")
    response = http.post("/admin/api/login", json={"username": "policy-admin", "password": "sehr-geheim-123"})
    assert response.status_code == 200
    return response.get_json()["csrf"]


def test_admin_api_switches_permission(env, make_client, http):
    client, _ = make_client()
    h = {"X-CSRF-Token": _login(http)}
    data = http.patch(f"/admin/api/clients/{client['id']}", headers=h,
                      json={"allowPersistent": True, "quotaRunning": 3}).get_json()["client"]
    assert data["allowPersistent"] is True and data["quota"]["running"] == 3
    bad = http.patch(f"/admin/api/clients/{client['id']}", headers=h, json={"allowPersistent": "vielleicht"})
    assert bad.status_code == 400


# --------------------------------------------------------------------------
# upgrade of an existing installation
# --------------------------------------------------------------------------

def test_upgrade_keeps_persistent_sandboxes_for_existing_clients(env):
    path = config.settings().db_path
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(db.BASE_SCHEMA)
    for column, ctype in db.API_KEY_V2_COLUMNS.items():
        conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ctype}")
    conn.execute("INSERT INTO api_keys(key_hash, label, created_at) VALUES(?, 'Alt', 1)", ("a" * 64,))
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    db.init_db()
    with db.session() as conn:
        old = conn.execute("SELECT * FROM api_keys WHERE label='Alt'").fetchone()
        assert old["allow_persistent"] == 1 and old["quota_running"] is None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        new, _ = clients.create(conn, "Neu")
        assert new["allow_persistent"] == 0
    db.init_db()  # idempotent: later runs never grant the permission to newer clients
    with db.session() as conn:
        assert conn.execute("SELECT allow_persistent FROM api_keys WHERE label='Neu'").fetchone()[0] == 0


# --------------------------------------------------------------------------
# desktop of a stopped sandbox
# --------------------------------------------------------------------------

def test_desktop_route_shows_offline_page_instead_of_502(env):
    conf = nginxctl.render("sbx-a", 20000, 20001, 20002, "pw")
    desktop = conf.split("location /sbx-a/ {", 1)[1].split("}", 1)[0]
    assert "error_page 502 504 = @albert_sandbox_offline;" in desktop
    offline = (Path(__file__).resolve().parents[1] / "nginx" / "sandbox-offline.conf").read_text(encoding="utf-8")
    assert "location @albert_sandbox_offline" in offline
