"""Profiles a client may choose from, the default profile and the profile of each sandbox."""
import sqlite3

import pytest

from albert_sandbox import cli, clients, db, profiles, sandboxes, web
from albert_sandbox.errors import BadRequest, Forbidden, NotFound, Unavailable

STANDARD_IMAGE = "albert-ai-sandbox:latest"


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
        admins.create(conn, "pc-admin", "sehr-geheim-123")
    data = client.post("/admin/api/login", json={"username": "pc-admin", "password": "sehr-geheim-123"})
    client.csrf = {"X-CSRF-Token": data.get_json()["csrf"]}
    return client


def _built(name, **fields):
    with db.session() as conn:
        row = profiles.create(conn, name, **fields)
    profiles.request_build(row["id"], actor="test")
    with db.session() as conn:
        return profiles.get(conn, row["id"])


def _client(client_id):
    with db.session() as conn:
        return clients.get(conn, client_id)


def _allowed(client_id):
    with db.session() as conn:
        return clients.allowed(conn, clients.get(conn, client_id))


def _image(env, name):
    return env["docker"].containers[name]["Config"]["Image"]


# --------------------------------------------------------------------------
# rules for the allowed list
# --------------------------------------------------------------------------

def test_new_clients_allow_exactly_their_default(env, make_client):
    office = _built("Office", apt_packages="libreoffice")
    plain, _ = make_client("ohne")
    assert _allowed(plain["id"]) == {"standard": True, "profileIds": []}
    with_profile, _ = make_client("mit", profile_id=office["id"])
    assert _allowed(with_profile["id"]) == {"standard": False, "profileIds": [office["id"]]}
    both, _ = make_client("beide", profile_id=office["id"], allowed_profile_ids=[office["id"]],
                          allow_standard_image=True)
    assert _allowed(both["id"]) == {"standard": True, "profileIds": [office["id"]]}


def test_default_must_stay_allowed_and_one_entry_is_required(env, make_client):
    office = _built("Office")
    data = _built("Daten")
    client, _ = make_client(profile_id=office["id"])
    with db.session() as conn:
        with pytest.raises(BadRequest) as exc:
            clients.update(conn, client["id"], {"allowed_profile_ids": [data["id"]]})
        assert exc.value.code == "profile_default_not_allowed"
        with pytest.raises(BadRequest) as exc:
            clients.update(conn, client["id"], {"allowed_profile_ids": [], "allow_standard_image": False})
        assert exc.value.code in ("profile_default_not_allowed", "no_profile_allowed")
        # a new default is allowed automatically, the former one stays allowed
        clients.update(conn, client["id"], {"profile_id": data["id"]})
    assert _allowed(client["id"])["profileIds"] == [office["id"], data["id"]]
    with db.session() as conn:
        clients.update(conn, client["id"], {"profile_id": None})
    assert _allowed(client["id"]) == {"standard": True, "profileIds": [office["id"], data["id"]]}


def test_standard_is_a_reserved_profile_name(env):
    with db.session() as conn:
        with pytest.raises(BadRequest):
            profiles.create(conn, "Standard")


def test_upgrade_keeps_each_client_on_its_profile(env):
    office = _built("Office")
    path = env["settings"].db_path
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE client_profiles")
    conn.execute("ALTER TABLE api_keys DROP COLUMN allow_standard_image")
    conn.execute("ALTER TABLE sandboxes DROP COLUMN profile_id")
    conn.execute("INSERT INTO api_keys(key_hash, label, created_at, profile_id) VALUES('h1', 'mit', 1, ?)",
                 (office["id"],))
    conn.execute("INSERT INTO api_keys(key_hash, label, created_at) VALUES('h2', 'ohne', 1)")
    with_id = conn.execute("SELECT id FROM api_keys WHERE label='mit'").fetchone()[0]
    conn.execute("INSERT INTO sandboxes(name, api_key_id, state, created_at) VALUES('sbx-alt', ?, 'ready', 1)",
                 (with_id,))
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()
    db.init_db()
    with db.session() as c:
        mit = c.execute("SELECT * FROM api_keys WHERE label='mit'").fetchone()
        ohne = c.execute("SELECT * FROM api_keys WHERE label='ohne'").fetchone()
        assert clients.allowed(c, mit) == {"standard": False, "profileIds": [office["id"]]}
        assert clients.allowed(c, ohne) == {"standard": True, "profileIds": []}
        assert c.execute("SELECT profile_id FROM sandboxes WHERE name='sbx-alt'").fetchone()[0] == office["id"]
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 9


# --------------------------------------------------------------------------
# client API
# --------------------------------------------------------------------------

def test_client_lists_and_chooses_profiles(env, app, make_client):
    office = _built("Office", description="LibreOffice und Texterkennung", apt_packages="libreoffice",
                    pip_packages="pandas==2.2.0", setup_script="echo geheim")
    with db.session() as conn:
        unbuilt = profiles.create(conn, "Neu")
        building = profiles.create(conn, "Baut")
        conn.execute("UPDATE profiles SET build_state='building' WHERE id=?", (building["id"],))
    client, key = make_client(profile_id=office["id"],
                              allowed_profile_ids=[office["id"], unbuilt["id"], building["id"]],
                              allow_standard_image=True)
    http = app.test_client()
    headers = {"Authorization": f"Bearer {key}", "X-Albert-User": "u1"}
    assert "profile-choice" in http.get("/health", headers=headers).get_json()["features"]
    listed = http.get("/profiles", headers=headers).get_json()["profiles"]
    assert [p["name"] for p in listed] == ["standard", "Baut", "Office"]  # never built: not offered
    by_name = {p["name"]: p for p in listed}
    assert by_name["Office"] == {"name": "Office", "description": "LibreOffice und Texterkennung",
                                 "packages": ["libreoffice", "pandas==2.2.0"], "default": True, "state": "ready"}
    assert by_name["Baut"]["state"] == "building" and by_name["standard"]["default"] is False
    assert "setup" not in str(listed).lower() and "geheim" not in str(listed)

    default = http.post("/containers", json={}, headers=headers).get_json()["container"]
    assert default["profile"]["name"] == "Office"
    assert _image(env, default["name"]) == office["image"]
    plain = http.post("/containers", json={"profile": "standard"}, headers=headers).get_json()["container"]
    assert plain["profile"] is None and _image(env, plain["name"]) == STANDARD_IMAGE
    same = http.post("/containers", json={"profile": "office"}, headers=headers)  # case-insensitive
    assert same.status_code == 201
    not_built = http.post("/containers", json={"profile": "Neu"}, headers=headers)
    assert not_built.status_code == 503 and not_built.get_json()["error"] == "profile_not_ready"


def test_profile_outside_the_list_is_refused(env, app, make_client):
    office = _built("Office")
    _built("Fremd")
    client, key = make_client(profile_id=office["id"])
    http = app.test_client()
    headers = {"Authorization": f"Bearer {key}", "X-Albert-User": "u1"}
    refused = http.post("/containers", json={"profile": "Fremd"}, headers=headers)
    assert refused.status_code == 403 and refused.get_json()["error"] == "profile_not_allowed"
    assert refused.get_json()["allowed"] == ["Office"]
    assert http.post("/containers", json={"profile": "standard"}, headers=headers).status_code == 403
    unknown = http.post("/containers", json={"profile": "gibtsnicht"}, headers=headers)
    assert unknown.status_code == 404 and unknown.get_json()["error"] == "profile_not_found"


# --------------------------------------------------------------------------
# sandboxes keep their profile
# --------------------------------------------------------------------------

def test_sandbox_keeps_its_profile_when_the_default_changes(env, make_client):
    office = _built("Office")
    data = _built("Daten")
    client, _ = make_client(profile_id=office["id"])
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        clients.update(conn, client["id"], {"profile_id": data["id"]})
    assert sandboxes.describe(name)["outdated"] == [] and sandboxes.describe(name)["profile"]["name"] == "Office"
    sandboxes.recreate(name)
    assert _image(env, name) == office["image"]
    assert sandboxes.describe(name)["profile"]["name"] == "Office"


def test_withdrawn_profile_marks_outdated_and_rebuild_takes_the_default(env, make_client):
    office = _built("Office")
    data = _built("Daten")
    client, _ = make_client(profile_id=data["id"], allowed_profile_ids=[office["id"], data["id"]])
    name = sandboxes.create(client["id"], profile="Office")["name"]
    with db.session() as conn:
        clients.update(conn, client["id"], {"allowed_profile_ids": [data["id"]]})
    assert sandboxes.describe(name)["outdated"] == ["profile"]
    assert name in env["docker"].running_names()  # keeps running
    sandboxes.recreate(name)
    described = sandboxes.describe(name)
    assert described["profile"]["name"] == "Daten" and described["outdated"] == []
    assert _image(env, name) == data["image"]


def test_admin_rebuilds_with_another_allowed_profile(env, admin, make_client):
    office = _built("Office")
    other = _built("Fremd")
    client, _ = make_client(profile_id=office["id"], allow_standard_image=True,
                            allowed_profile_ids=[office["id"]])
    name = sandboxes.create(client["id"])["name"]
    res = admin.post(f"/admin/api/sandboxes/{name}/recreate", headers=admin.csrf, json={"profileId": None})
    assert res.status_code == 200 and res.get_json()["sandbox"]["profile"] is None
    assert _image(env, name) == STANDARD_IMAGE
    refused = admin.post(f"/admin/api/sandboxes/{name}/recreate", headers=admin.csrf,
                         json={"profileId": other["id"]})
    assert refused.status_code == 403 and refused.get_json()["error"] == "profile_not_allowed"
    back = admin.post(f"/admin/api/sandboxes/{name}/recreate", headers=admin.csrf, json={"profileId": office["id"]})
    assert back.get_json()["sandbox"]["profile"]["name"] == "Office"


def test_profile_in_use_by_a_sandbox_cannot_be_deleted(env, make_client):
    office = _built("Office")
    client, _ = make_client(profile_id=office["id"])
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        clients.update(conn, client["id"], {"profile_id": None, "allowed_profile_ids": []})
        with pytest.raises(BadRequest) as exc:
            profiles.delete(conn, office["id"])
    assert "sandbox" in exc.value.message
    sandboxes.remove(name)
    with db.session() as conn:
        profiles.delete(conn, office["id"])


def test_cli_profile_choice(env, make_client):
    office = _built("Office")
    client, key = make_client()
    assert cli.main(["client", "set", str(client["id"]), "--profiles", "standard,Office"]) == 0
    assert _allowed(client["id"]) == {"standard": True, "profileIds": [office["id"]]}
    assert cli.main(["--api-key", key, "create", "--profile", "Office"]) == 0
    with db.session() as conn:
        name = conn.execute("SELECT name FROM sandboxes WHERE api_key_id=?", (client["id"],)).fetchone()[0]
    assert _image(env, name) == office["image"]
    assert cli.main(["recreate", name, "--profile", "standard"]) == 0
    assert _image(env, name) == STANDARD_IMAGE
