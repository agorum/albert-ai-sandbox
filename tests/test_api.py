import pytest

from albert_sandbox import db, web


@pytest.fixture
def app(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    application = web.create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def http(app):
    return app.test_client()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_client_api_is_compatible(env, make_client, http):
    _, key = make_client()
    assert http.get("/containers").status_code == 401
    assert http.get("/containers", headers=auth("wrong")).status_code == 401

    created = http.post("/containers", json={"name": "Agent Lab"}, headers=auth(key))
    assert created.status_code == 201
    c = created.get_json()["container"]
    for field in ("name", "sandboxName", "status", "ports", "urls", "vncPassword", "fileServiceToken", "persistent"):
        assert field in c
    assert c["name"].startswith("sbx-agent-lab-")
    assert c["urls"]["desktop"] == f"https://sandbox.test/{c['name']}/"
    assert c["urls"]["mcphub"].endswith("/mcphub/mcp")
    assert c["urls"]["filesUpload"].endswith("/files/upload")
    assert set(c["ports"]) == {"novnc", "vnc", "mcphub", "filesvc"}

    listing = http.get("/containers", headers=auth(key)).get_json()["containers"]
    assert [x["name"] for x in listing] == [c["name"]]
    assert "vncPassword" not in listing[0]

    name = c["name"]
    assert http.get(f"/containers/{name}", headers=auth(key)).get_json()["container"]["status"] == "running"
    assert http.post(f"/containers/{name}/stop", headers=auth(key)).get_json()["container"]["status"] == "stopped"
    assert http.post(f"/containers/{name}/start", headers=auth(key)).get_json()["container"]["status"] == "running"
    assert http.patch(f"/containers/{name}/persistent", json={"persistent": True},
                      headers=auth(key)).get_json() == {"name": name, "persistent": True}
    assert http.post(f"/containers/{name}/persistent", json={"persistent": False},
                     headers=auth(key)).get_json()["persistent"] is False
    assert http.delete(f"/containers/{name}", headers=auth(key)).get_json() == {"deleted": name}
    assert http.get(f"/containers/{name}", headers=auth(key)).status_code == 404


def test_client_api_quota_errors_and_quota_endpoint(env, make_client, http):
    _, key = make_client(quota_sandboxes=1, default_cpus=1)
    assert http.post("/containers", json={}, headers=auth(key)).status_code == 201
    second = http.post("/containers", json={}, headers=auth(key))
    assert second.status_code == 409
    assert second.get_json()["error"] == "quota_exceeded"
    too_big = http.post("/containers", json={"resources": {"cpus": 8}}, headers=auth(key))
    assert too_big.status_code == 409 and too_big.get_json()["error"] == "sandbox_limit_exceeded"
    quota = http.get("/quota", headers=auth(key)).get_json()
    assert quota["quota"]["sandboxes"] == 1 and quota["usage"]["sandboxes"] == 1
    assert quota["defaults"]["effective"]["memoryMb"] == 1024


def test_foreign_sandbox_is_404(env, make_client, http):
    _, key_a = make_client("A")
    _, key_b = make_client("B")
    name = http.post("/containers", json={}, headers=auth(key_a)).get_json()["container"]["name"]
    for method, path in (("get", f"/containers/{name}"), ("post", f"/containers/{name}/stop"),
                         ("delete", f"/containers/{name}")):
        assert getattr(http, method)(path, headers=auth(key_b)).status_code == 404


def _login(http, username="root-admin", password="sehr-geheim-123"):
    from albert_sandbox import admins
    with db.session() as conn:
        if not admins.find_by_name(conn, username):
            admins.create(conn, username, password)
    response = http.post("/admin/api/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.get_json()["csrf"]


def test_admin_requires_login_and_csrf(env, http):
    assert http.get("/admin/api/clients").status_code == 401
    session = http.get("/admin/api/session")
    assert session.status_code == 401 and session.get_json()["hasAdmins"] is False
    assert http.post("/admin/api/login", json={"username": "x", "password": "y"}).status_code == 409
    csrf = _login(http)
    assert http.post("/admin/api/clients", json={"label": "A"}).status_code == 403
    ok = http.post("/admin/api/clients", json={"label": "A"}, headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 201 and len(ok.get_json()["apiKey"]) > 20
    assert http.post("/admin/api/login", json={"username": "root-admin", "password": "falsch"}).status_code == 401


def test_admin_client_management_flow(env, http):
    csrf = _login(http)
    h = {"X-CSRF-Token": csrf}
    created = http.post("/admin/api/clients", headers=h, json={
        "label": "Kunde X", "defaultCpus": 2, "defaultMemoryMb": 2048, "quotaCpus": 4, "quotaSandboxes": 3})
    client = created.get_json()["client"]
    key = created.get_json()["apiKey"]
    assert client["defaults"]["effective"] == {"cpus": 2.0, "memoryMb": 2048, "diskGb": 10}
    assert client["quota"]["cpus"] == 4 and client["quota"]["memoryMb"] is None

    sbx = http.post("/containers", json={}, headers=auth(key)).get_json()["container"]["name"]
    detail = http.get(f"/admin/api/clients/{client['id']}").get_json()["client"]
    assert detail["usage"]["running"] == 1 and detail["sandboxes"][0]["name"] == sbx

    patched = http.patch(f"/admin/api/clients/{client['id']}", headers=h,
                         json={"quotaCpus": None, "defaultDiskGb": 20}).get_json()["client"]
    assert patched["quota"]["cpus"] is None and patched["defaults"]["diskGb"] == 20

    resized = http.patch(f"/admin/api/sandboxes/{sbx}", headers=h, json={"cpus": 3, "persistent": True})
    assert resized.get_json()["sandbox"]["resources"]["cpus"] == 3
    assert http.post(f"/admin/api/sandboxes/{sbx}/stop", headers=h).get_json()["sandbox"]["status"] == "stopped"
    assert http.get(f"/admin/api/sandboxes/{sbx}/stats").status_code == 200

    new_key = http.post(f"/admin/api/clients/{client['id']}/rotate-key", headers=h).get_json()["apiKey"]
    assert http.get("/containers", headers=auth(key)).status_code == 401
    assert http.get("/containers", headers=auth(new_key)).status_code == 200

    deleted = http.delete(f"/admin/api/clients/{client['id']}", headers=h).get_json()
    assert deleted["removedSandboxes"] == [sbx]
    assert env["docker"].containers == {}
    audit = http.get("/admin/api/audit").get_json()["entries"]
    assert {"client.delete", "sandbox.remove", "client.rotate_key"} <= {e["action"] for e in audit}


def test_admin_user_management(env, http):
    csrf = _login(http)
    h = {"X-CSRF-Token": csrf}
    assert http.post("/admin/api/admins", headers=h, json={"username": "zwei", "password": "kurz"}).status_code == 400
    second = http.post("/admin/api/admins", headers=h, json={"username": "zwei", "password": "langes-passwort-1"})
    assert second.status_code == 201
    admins_list = http.get("/admin/api/admins").get_json()["admins"]
    me = next(a for a in admins_list if a["self"])
    assert http.delete(f"/admin/api/admins/{me['id']}", headers=h).status_code == 400
    assert http.post(f"/admin/api/admins/{me['id']}/password", headers=h,
                     json={"currentPassword": "falsch", "password": "neues-passwort-1"}).status_code == 400
    assert http.post(f"/admin/api/admins/{me['id']}/password", headers=h,
                     json={"currentPassword": "sehr-geheim-123", "password": "neues-passwort-1"}).status_code == 200
    other = second.get_json()["admin"]["id"]
    assert http.delete(f"/admin/api/admins/{other}", headers=h).status_code == 200


def test_password_change_logs_out_other_sessions(env, app):
    first, second = app.test_client(), app.test_client()
    csrf1 = _login(first)
    _login(second)
    me = next(a for a in first.get("/admin/api/admins").get_json()["admins"] if a["self"])
    assert first.post(f"/admin/api/admins/{me['id']}/password", headers={"X-CSRF-Token": csrf1},
                      json={"currentPassword": "sehr-geheim-123", "password": "ganz-neues-pw-1"}).status_code == 200
    assert first.get("/admin/api/clients").status_code == 200      # own session stays valid
    assert second.get("/admin/api/clients").status_code == 401     # other session is logged out


def test_admin_input_validation(env, http):
    h = {"X-CSRF-Token": _login(http)}
    assert http.post("/admin/api/clients", headers=h, json={"label": 123}).status_code == 400
    assert http.post("/admin/api/clients", headers=h, json={"label": "A", "defaultMemoryMb": 100}).status_code == 400
    assert http.post("/admin/api/clients", headers=h, json={"label": "A", "defaultCpus": 64}).status_code == 400
    assert http.post("/admin/api/clients", headers=h, json={"label": "A", "defaultDiskGb": 5000}).status_code == 400
    body = http.get("/admin/api/nope").get_json()
    assert body["message"] and body["error"]


def test_login_throttle(env, http):
    _login(http)
    for _ in range(5):
        http.post("/admin/api/login", json={"username": "root-admin", "password": "nope"})
    assert http.post("/admin/api/login", json={"username": "root-admin", "password": "sehr-geheim-123"}).status_code == 429
