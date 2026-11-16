"""Lifetime of sandboxes (stop when idle, remove non-persistent ones), profile
cloning and password confirmation of critical admin actions."""
import sqlite3
import time

import pytest

from albert_sandbox import cli, clients, db, lifecycle, profiles, sandboxes, watcher, web
from albert_sandbox.errors import BadRequest, Forbidden

H = 3600
PW = "sehr-geheim-123"


@pytest.fixture
def app(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    application = web.create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def admin(app):
    client = app.test_client()
    client.environ_base["HTTP_X_ALBERT_ADMIN_GATE"] = "1"
    from albert_sandbox import admins
    with db.session() as conn:
        admins.create(conn, "life-admin", PW)
    data = client.post("/admin/api/login", json={"username": "life-admin", "password": PW})
    client.csrf = {"X-CSRF-Token": data.get_json()["csrf"]}
    return client


def _age(name, created, activity=None):
    with db.session() as conn:
        conn.execute("UPDATE sandboxes SET created_at=?, last_started_at=?, last_activity_at=? WHERE name=?",
                     (int(created), int(created), int(activity if activity is not None else created), name))


def _names():
    with db.session() as conn:
        return {r["name"] for r in conn.execute("SELECT name FROM sandboxes")}


def _set_global(**values):
    with db.session() as conn:
        return lifecycle.set_global(conn, values, now=1)  # long ago: no grace period in these tests


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def test_defaults_and_rules_per_kind(env, make_client):
    with db.session() as conn:
        glob = lifecycle.global_values(conn)
    assert (glob["inactivitySeconds"], glob["maxAgeSeconds"], glob["maxLifetimeSeconds"]) == (600, 4 * H, 24 * H)
    assert glob["source"] == "config"
    client, _ = make_client(allow_persistent=True)
    now = time.time()
    used = sandboxes.create(client["id"])["name"]
    unused = sandboxes.create(client["id"])["name"]
    ancient = sandboxes.create(client["id"])["name"]
    persistent = sandboxes.create(client["id"], persistent=True)["name"]
    _age(used, now - 5 * H, activity=now - 60)        # older than 4 h, but in use
    _age(unused, now - 5 * H, activity=now - 2 * H)   # older than 4 h, unused
    _age(ancient, now - 25 * H, activity=now - 60)    # beyond the max lifetime, still in use
    _age(persistent, now - 30 * H, activity=now - 60)
    watcher.remove_expired(now)
    left = _names()
    assert used in left and persistent in left
    assert unused not in left and ancient not in left
    # persistent sandboxes are never stopped because of their age either
    assert persistent in env["docker"].running_names()


def test_in_use_sandbox_is_removed_once_unused(env, make_client):
    client, _ = make_client()
    now = time.time()
    name = sandboxes.create(client["id"])["name"]
    _age(name, now - 5 * H, activity=now - 60)
    watcher.remove_expired(now)
    assert name in _names()
    sandboxes.stop(name, actor="watcher")  # idle stop: no use any more
    watcher.remove_expired(now + 600)
    assert name not in _names()


def test_restart_past_the_max_age_is_safe(env, make_client):
    """A restart is stop + start; a watcher run in between must not remove the sandbox."""
    client, _ = make_client()
    now = time.time()
    name = sandboxes.create(client["id"])["name"]
    _age(name, now - 5 * H, activity=now - 3600)   # past 4 h, last used an hour ago, still running
    sandboxes.stop(name)                            # first half of a restart (by the client)
    watcher.remove_expired(time.time())             # watcher runs in between
    assert name in _names()
    sandboxes.start(name)
    assert name in env["docker"].running_names()
    # stopped by a user and then left alone: removed once the inactivity time has passed
    sandboxes.stop(name)
    watcher.remove_expired(time.time() + 30)
    assert name in _names()
    watcher.remove_expired(time.time() + 601)
    assert name not in _names()


def test_client_values_override_the_global_ones(env, make_client):
    eight, _ = make_client("acht Stunden", max_age_seconds=8 * H)
    plain, _ = make_client("global")
    now = time.time()
    a = sandboxes.create(eight["id"])["name"]
    b = sandboxes.create(plain["id"])["name"]
    for name in (a, b):
        _age(name, now - 5 * H, activity=now - 2 * H)
    watcher.remove_expired(now)
    assert a in _names() and b not in _names()


def test_idle_stop_per_client_and_off(env, make_client, monkeypatch):
    monkeypatch.setattr(watcher, "established_ports", lambda: set())
    never, _ = make_client("nie anhalten", inactivity_seconds=0)
    plain, _ = make_client("global")
    now = time.time()
    a = sandboxes.create(never["id"])["name"]
    b = sandboxes.create(plain["id"])["name"]
    for name in (a, b):
        _age(name, now - H, activity=now - H)
        env["docker"].containers[name]["State"]["StartedAt"] = "2020-01-01T00:00:00Z"
    watcher.update_activity_and_stop_idle(now)
    running = env["docker"].running_names()
    assert a in running and b not in running


def test_shorter_deadline_gives_existing_sandboxes_the_full_new_period(env, make_client):
    _set_global(maxAgeSeconds=24 * H)
    client, _ = make_client()
    now = time.time()
    name = sandboxes.create(client["id"])["name"]
    _age(name, now - 10 * H, activity=now - 9 * H)
    with db.session() as conn:
        lifecycle.set_global(conn, {"maxAgeSeconds": 4 * H}, now=int(now))
    watcher.remove_expired(now + 60)
    assert name in _names()  # would be due since 6 h, but the clock starts at the change
    watcher.remove_expired(now + 4 * H + 60)
    assert name not in _names()


def test_shortening_per_client_moves_its_clock(env, make_client):
    client, _ = make_client(max_age_seconds=24 * H)
    with db.session() as conn:
        updated = clients.update(conn, client["id"], {"max_age_seconds": 2 * H})
        assert updated["lifecycle_changed_at"] >= int(time.time()) - 5
        longer = clients.update(conn, client["id"], {"max_age_seconds": 3 * H})
        assert longer["lifecycle_changed_at"] == updated["lifecycle_changed_at"]  # longer: no new grace


@pytest.mark.parametrize("values", [{"maxLifetimeSeconds": 2 * H}, {"maxAgeSeconds": -1}, {"inactivitySeconds": 30},
                                    {"maxAgeSeconds": "viel"}])
def test_invalid_values_are_refused(env, values):
    with db.session() as conn:
        with pytest.raises(BadRequest) as exc:
            lifecycle.set_global(conn, values)
    assert exc.value.code == "invalid_lifecycle"


def test_client_lifetime_shorter_than_max_age_is_refused(env, make_client):
    client, _ = make_client()
    with db.session() as conn:
        with pytest.raises(BadRequest):
            clients.update(conn, client["id"], {"max_lifetime_seconds": H})  # global max age is 4 h
        clients.update(conn, client["id"], {"max_lifetime_seconds": H, "max_age_seconds": 0})  # max age off


def test_upgrade_starts_the_clock_anew(env):
    path = env["settings"].db_path
    conn = sqlite3.connect(str(path))
    for column in ("inactivity_seconds", "max_age_seconds", "max_lifetime_seconds", "lifecycle_changed_at"):
        conn.execute(f"ALTER TABLE api_keys DROP COLUMN {column}")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()
    db.init_db()
    with db.session() as c:
        assert db.kv_get(c, lifecycle.CHANGED_KEY) >= int(time.time()) - 5
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 8


def test_desktop_is_warned_once_before_the_max_lifetime(env, make_client, monkeypatch):
    calls = []
    monkeypatch.setattr(watcher.dockerctl, "notify_desktop", lambda name, title, text: calls.append((name, text)))
    client, _ = make_client()
    now = time.time()
    soon = sandboxes.create(client["id"])["name"]
    later = sandboxes.create(client["id"])["name"]
    _age(soon, now - 24 * H + 600, activity=now)
    _age(later, now - 20 * H, activity=now)
    watcher.warn_lifetime(now)
    watcher.warn_lifetime(now + 60)
    assert [c[0] for c in calls] == [soon] and "in 10 Minuten gelöscht" in calls[0][1]


# --------------------------------------------------------------------------
# API and admin UI endpoints
# --------------------------------------------------------------------------

def test_client_api_reports_the_rules_and_deadlines(env, app, make_client):
    http = app.test_client()
    client, key = make_client(allow_persistent=True, max_lifetime_seconds=12 * H)
    headers = {"Authorization": f"Bearer {key}", "X-Albert-User": "u1"}
    quota = http.get("/quota", headers=headers).get_json()
    assert quota["lifecycle"] == {"stopAfterIdleSeconds": 600, "deleteNonPersistentAfterSeconds": 4 * H,
                                  "maxLifetimeSeconds": 12 * H}
    normal = http.post("/containers", json={}, headers=headers).get_json()["container"]
    assert normal["lifecycle"]["deleteWhenUnusedAfter"] and normal["lifecycle"]["deleteAtLatest"]
    assert normal["expiresAt"] == normal["lifecycle"]["deleteAtLatest"]
    kept = http.post("/containers", json={"persistent": True}, headers=headers).get_json()["container"]
    assert kept["expiresAt"] is None and kept["lifecycle"]["deleteAtLatest"] is None
    assert kept["lifecycle"]["stopAfterIdleSeconds"] == 600


def test_persistent_refusal_names_the_rules(env, make_client):
    client, _ = make_client()
    with pytest.raises(Forbidden) as exc:
        sandboxes.create(client["id"], persistent=True)
    assert "4 hours" in exc.value.message and "24 hours" in exc.value.message


def test_admin_sets_global_and_client_values(env, admin, make_client):
    saved = admin.put("/admin/api/settings/lifecycle", headers=admin.csrf,
                      json={"inactivitySeconds": 900, "maxAgeSeconds": 2 * H, "maxLifetimeSeconds": 8 * H})
    view = saved.get_json()["settings"]["lifecycle"]
    assert (view["inactivitySeconds"], view["maxAgeSeconds"], view["maxLifetimeSeconds"], view["source"]) == \
        (900, 2 * H, 8 * H, "settings")
    bad = admin.put("/admin/api/settings/lifecycle", headers=admin.csrf, json={"maxLifetimeSeconds": H})
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_lifecycle"
    client, _ = make_client()
    res = admin.patch(f"/admin/api/clients/{client['id']}", headers=admin.csrf,
                      json={"maxAgeSeconds": 6 * H, "maxLifetimeSeconds": None}).get_json()["client"]
    assert res["lifecycle"]["own"]["maxAgeSeconds"] == 6 * H
    assert res["lifecycle"]["effective"] == {"inactivitySeconds": 900, "maxAgeSeconds": 6 * H,
                                             "maxLifetimeSeconds": 8 * H}
    assert res["lifecycle"]["sources"]["maxAgeSeconds"] == "client"
    listing = admin.get("/admin/api/settings").get_json()["settings"]["lifecycle"]
    assert listing["clientsWithOwn"] == [{"id": client["id"], "label": client["label"]}]
    reset = admin.put("/admin/api/settings/lifecycle", headers=admin.csrf, json={"reset": True})
    assert reset.get_json()["settings"]["lifecycle"]["source"] == "config"


def test_cli_lifecycle(env, make_client, capsys):
    assert cli.main(["lifecycle", "set", "--inactivity-minutes", "15", "--max-age-hours", "2"]) == 0
    with db.session() as conn:
        glob = lifecycle.global_values(conn)
    assert glob["inactivitySeconds"] == 900 and glob["maxAgeSeconds"] == 2 * H
    client, _ = make_client()
    assert cli.main(["client", "set", str(client["id"]), "--max-age-hours", "6,5"]) == 0
    with db.session() as conn:
        assert clients.get(conn, client["id"])["max_age_seconds"] == int(6.5 * H)
    assert cli.main(["client", "set", str(client["id"]), "--max-age-hours", "none"]) == 0
    with db.session() as conn:
        assert clients.get(conn, client["id"])["max_age_seconds"] is None
    capsys.readouterr()
    assert cli.main(["lifecycle", "reset"]) == 0
    assert "Persistent sandboxes are never removed" in capsys.readouterr().out


# --------------------------------------------------------------------------
# profile cloning
# --------------------------------------------------------------------------

def test_clone_copies_settings_only(env, make_client):
    with db.session() as conn:
        source = profiles.create(conn, "Office", description="Büro", apt_packages="libreoffice tesseract-ocr-deu",
                                 pip_packages="pandas", setup_script="echo hallo")
        conn.execute("UPDATE profiles SET image='albert-ai-sandbox-profile-1:1', image_id='sha256:x', "
                     "build_state='ok' WHERE id=?", (source["id"],))
    make_client(profile_id=source["id"])
    with db.session() as conn:
        copy = profiles.clone(conn, source["id"])
        second = profiles.clone(conn, source["id"])
        named = profiles.clone(conn, source["id"], "Office 2")
        assert copy["name"] == "Office (Kopie)" and second["name"] == "Office (Kopie) 2" and named["name"] == "Office 2"
        assert profiles.packages(copy) == profiles.packages(source)
        assert copy["description"] == "Büro" and copy["setup_script"] == "echo hallo"
        assert copy["image"] is None and copy["build_state"] == "none"
        assert profiles.clients_using(conn, copy["id"]) == []


def test_clone_via_api_and_cli(env, admin, capsys):
    with db.session() as conn:
        source = profiles.create(conn, "Basis", npm_packages="typescript")
    res = admin.post(f"/admin/api/profiles/{source['id']}/clone", headers=admin.csrf, json={})
    assert res.status_code == 201 and res.get_json()["profile"]["name"] == "Basis (Kopie)"
    taken = admin.post(f"/admin/api/profiles/{source['id']}/clone", headers=admin.csrf, json={"name": "Basis"})
    assert taken.status_code == 409
    assert cli.main(["profile", "clone", "Basis", "--name", "Basis CLI"]) == 0
    with db.session() as conn:
        assert profiles.find(conn, "Basis CLI")["npm_packages"] == "typescript"


# --------------------------------------------------------------------------
# password confirmation
# --------------------------------------------------------------------------

def test_critical_admin_actions_need_the_password(env, admin):
    h = admin.csrf
    missing = admin.put("/admin/api/settings/admin-access", headers=h, json={"allow": ""})
    assert missing.status_code == 403 and missing.get_json()["error"] == "password_required"
    wrong = admin.post("/admin/api/admins", headers=h,
                       json={"username": "neu", "password": "langes-passwort-1", "confirmPassword": "falsch"})
    assert wrong.status_code == 403
    assert admin.delete("/admin/api/settings/tls", headers=h).status_code == 403
    created = admin.post("/admin/api/admins", headers=h,
                         json={"username": "neu", "password": "langes-passwort-1", "confirmPassword": PW})
    assert created.status_code == 201
    other = created.get_json()["admin"]["id"]
    assert admin.post(f"/admin/api/admins/{other}/password", headers=h,
                      json={"password": "noch-ein-passwort-1"}).status_code == 403
    assert admin.post(f"/admin/api/admins/{other}/password", headers=h,
                      json={"password": "noch-ein-passwort-1", "confirmPassword": PW}).status_code == 200
    assert admin.delete(f"/admin/api/admins/{other}", headers=h, json={"confirmPassword": PW}).status_code == 200
    with db.session() as conn:
        failed = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='admin.confirm_failed'").fetchone()[0]
    assert failed == 4


def test_repeated_wrong_passwords_are_throttled(env, admin):
    for _ in range(5):
        admin.put("/admin/api/settings/admin-access", headers=admin.csrf, json={"allow": "", "confirmPassword": "x"})
    blocked = admin.put("/admin/api/settings/admin-access", headers=admin.csrf,
                        json={"allow": "", "confirmPassword": PW})
    assert blocked.status_code == 429 and blocked.get_json()["error"] == "throttled"
