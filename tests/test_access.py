"""Access to sandboxes: API key and user binding, nginx checks, desktop links,
admin port, HTTPS (security update of the ALBERT integration)."""
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from albert_sandbox import access, clients, config, db, nginxctl, sandboxes, tlsctl, web
from albert_sandbox.errors import BadRequest


@pytest.fixture
def app(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    application = web.create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def http(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    client = app.test_client()
    client.environ_base["HTTP_X_ALBERT_ADMIN_GATE"] = "1"
    return client


def auth(key, user=None):
    headers = {"Authorization": f"Bearer {key}"}
    if user:
        headers["X-Albert-User"] = user
    return headers


INTERNAL = {"X-Albert-Internal": "1"}


def _row(name):
    with db.session() as conn:
        return conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()


def _legacy_client(make_client, label="Alt"):
    row, key = make_client(label)
    with db.session() as conn:
        conn.execute("UPDATE api_keys SET legacy_access=1 WHERE id=?", (row["id"],))
    return row, key


# --------------------------------------------------------------------------
# client API: user binding
# --------------------------------------------------------------------------

def test_new_clients_need_the_user_header(env, make_client, http):
    _, key = make_client()
    assert http.get("/health", headers=auth(key)).status_code == 200
    refused = http.post("/containers", json={}, headers=auth(key))
    assert refused.status_code == 400 and refused.get_json()["error"] == "user_required"
    assert http.get("/quota", headers=auth(key)).get_json()["error"] == "user_required"
    bad = http.get("/containers", headers=auth(key, "bad user!"))
    assert bad.status_code == 400 and bad.get_json()["error"] == "invalid_user"


def test_sandboxes_are_bound_to_their_user(env, make_client, http):
    _, key = make_client()
    alice, bob = auth(key, "alice"), auth(key, "bob")
    created = http.post("/containers", json={}, headers=alice)
    assert created.status_code == 201
    name = created.get_json()["container"]["name"]
    assert created.get_json()["container"]["user"] == "alice"
    assert [c["name"] for c in http.get("/containers", headers=alice).get_json()["containers"]] == [name]
    assert http.get("/containers", headers=bob).get_json()["containers"] == []
    for method, path in (("get", f"/containers/{name}"), ("post", f"/containers/{name}/stop"),
                         ("post", f"/containers/{name}/desktop"), ("delete", f"/containers/{name}")):
        assert getattr(http, method)(path, headers=bob).status_code == 404, path
    assert http.post(f"/containers/{name}/stop", headers=alice).status_code == 200
    assert http.delete(f"/containers/{name}", headers=alice).status_code == 200


def test_legacy_client_works_without_user_and_hands_sandboxes_over(env, make_client, http):
    _, key = _legacy_client(make_client)
    old = http.post("/containers", json={}, headers=auth(key))
    assert old.status_code == 201 and old.get_json()["container"]["user"] is None
    name = old.get_json()["container"]["name"]
    assert http.get(f"/containers/{name}", headers=auth(key)).status_code == 200
    # First ALBERT user that addresses it takes it over (migration), others no longer see it.
    assert http.get(f"/containers/{name}", headers=auth(key, "alice")).get_json()["container"]["user"] == "alice"
    assert http.get(f"/containers/{name}", headers=auth(key, "bob")).status_code == 404
    assert _row(name)["owner"] == "alice"
    with db.session() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE target=?", (name,))]
    assert "sandbox.claim" in actions


def test_health_announces_the_interface(env, make_client, http):
    _, key = make_client()
    data = http.get("/health", headers=auth(key))  # no user needed here
    assert data.status_code == 200 and data.headers["X-Albert-Sandbox-Api"] == "3"
    body = data.get_json()
    assert body["api"] == 3 and {"user-binding", "service-auth", "desktop-link"} <= set(body["features"])
    assert body["legacyAccess"] is False and body["https"] is False
    assert http.get("/health").status_code == 401  # still needs the API key


def test_quota_and_limits_name_only_own_sandboxes(env, make_client, http):
    _, key = make_client(quota_running=2)
    a = http.post("/containers", json={}, headers=auth(key, "alice")).get_json()["container"]["name"]
    b = http.post("/containers", json={}, headers=auth(key, "bob")).get_json()["container"]["name"]
    usage = http.get("/quota", headers=auth(key, "alice")).get_json()["usage"]
    assert usage["running"] == 2 and usage["runningNames"] == [a]
    refused = http.post("/containers", json={}, headers=auth(key, "carol"))
    assert refused.status_code == 409 and refused.get_json()["error"] == "running_limit_exceeded"
    body = refused.get_json()
    assert body["runningSandboxes"] == [] and a not in body["message"] and b not in body["message"]
    refused = http.post("/containers", json={}, headers=auth(key, "alice")).get_json()
    assert refused["runningSandboxes"] == [a] and b not in refused["message"]


def test_strict_client_does_not_take_over_unbound_sandboxes(env, make_client, http):
    client, key = make_client()
    name = sandboxes.create(client["id"])["name"]  # operator/CLI: no user
    assert http.get(f"/containers/{name}", headers=auth(key, "alice")).status_code == 404


# --------------------------------------------------------------------------
# nginx auth_request
# --------------------------------------------------------------------------

def test_authorize_mcp_and_files(env, make_client):
    client, key = make_client()
    _, other_key = make_client("B")
    name = sandboxes.create(client["id"], owner="alice")["name"]
    for kind in ("mcp", "files"):
        assert access.authorize(kind, name, f"Bearer {key}", "alice")[0] == 204
        assert access.authorize(kind, name, f"Bearer {key}", "bob") == (403, "sandbox of another user")
        assert access.authorize(kind, name, f"Bearer {key}", None) == (403, "user required")
        assert access.authorize(kind, name, f"Bearer {other_key}", "alice") == (403, "sandbox of another client")
        assert access.authorize(kind, name, "Bearer albert", "alice") == (401, "API key required")
        assert access.authorize(kind, name, None, None) == (401, "API key required")
    assert access.authorize("mcp", "sbx-unknown", f"Bearer {key}", "alice")[0] == 403
    assert access.authorize("nope", name, f"Bearer {key}", "alice")[0] == 403


def test_authorize_legacy_client_keeps_former_access(env, make_client):
    client, key = _legacy_client(make_client)
    name = sandboxes.create(client["id"])["name"]
    assert access.authorize("mcp", name, "Bearer albert", None) == (204, "legacy")
    assert access.authorize("files", name, None, None) == (204, "legacy")
    assert access.authorize("mcp", name, f"Bearer {key}", None) == (204, "client")
    assert access.authorize("desktop", name)[0] == 204


def test_authorize_desktop_needs_session_and_running_sandbox(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"], owner="alice")["name"]
    assert access.authorize("desktop", name) == (401, "desktop link required")
    with db.session() as conn:
        link = access.create_desktop_link(conn, name, "test")
        ticket = link["url"].split("ticket=", 1)[1]
        cookie, max_age = access.redeem_ticket(conn, name, ticket)
        assert access.redeem_ticket(conn, name, ticket) is None  # single use
    assert max_age == 8 * 3600
    assert access.authorize("desktop", name, desktop_cookie=cookie)[0] == 204
    assert access.authorize("desktop", name, desktop_cookie="forged")[0] == 401
    assert access.authorize("desktop-page", name, desktop_cookie=cookie, running=set()) == (403, "stopped")
    assert access.authorize("desktop-page", name, desktop_cookie=cookie, running={name})[0] == 204
    # Rebuilding renews the credentials and ends open desktops.
    sandboxes.recreate(name)
    assert access.authorize("desktop", name, desktop_cookie=cookie)[0] == 401


def test_internal_endpoints_only_for_nginx(env, make_client, http):
    client, key = make_client()
    name = sandboxes.create(client["id"], owner="alice")["name"]
    assert http.get(f"/internal/auth/mcp/{name}", headers=auth(key, "alice")).status_code == 404
    ok = http.get(f"/internal/auth/mcp/{name}", headers={**INTERNAL, **auth(key, "alice")})
    assert ok.status_code == 204 and ok.headers["X-Albert-Reason"] == "owner"
    assert http.get(f"/internal/auth/files/{name}", headers={**INTERNAL, **auth(key, "bob")}).status_code == 403
    assert http.get(f"/internal/auth/files/{name}", headers=INTERNAL).status_code == 401


def test_desktop_link_flow(env, make_client, http, app):
    _, key = make_client()
    name = http.post("/containers", json={}, headers=auth(key, "alice")).get_json()["container"]["name"]
    created = http.post(f"/containers/{name}/desktop", headers=auth(key, "alice"))
    assert created.status_code == 201
    link = created.get_json()["desktop"]
    assert link["url"].startswith(f"https://sandbox.test/{name}/?ticket=") and link["validSeconds"] == 120
    ticket = link["url"].split("ticket=", 1)[1]

    opened = http.get(f"/internal/desktop/{name}/entry?ticket={ticket}", headers=INTERNAL)
    assert opened.status_code == 302
    location = opened.headers["Location"]
    password = _row(name)["vnc_password"]
    # Settings only in the fragment: never sent to a server, never logged.
    assert location.startswith(f"/{name}/vnc.html#") and f"password={password}" in location
    assert "?" not in location
    cookie = opened.headers["Set-Cookie"]
    assert f"Path=/{name}/" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert access.authorize("desktop", name, desktop_cookie=value)[0] == 204

    # The link works once; afterwards the browser's desktop session decides.
    browser = app.test_client(use_cookies=False)
    again = browser.get(f"/internal/desktop/{name}/entry?ticket={ticket}",
                        headers={**INTERNAL, "Cookie": f"{access.DESKTOP_COOKIE}={value}"})
    assert again.headers["Location"].startswith(f"/{name}/vnc.html#") and "Set-Cookie" not in again.headers
    expired = browser.get(f"/internal/desktop/{name}/entry?ticket={ticket}", headers=INTERNAL)
    assert expired.status_code == 302 and expired.headers["Location"] == f"/{name}/vnc.html"
    assert "Set-Cookie" not in expired.headers


def test_desktop_link_expires(env, make_client, monkeypatch):
    client, _ = make_client()
    name = sandboxes.create(client["id"], owner="alice")["name"]
    with db.session() as conn:
        ticket = access.create_desktop_link(conn, name, "test")["url"].split("ticket=", 1)[1]
    real = time.time
    monkeypatch.setattr(access.time, "time", lambda: real() + 121)
    with db.session() as conn:
        assert access.redeem_ticket(conn, name, ticket) is None
        assert access.purge_expired(conn) >= 1


def test_removed_sandbox_drops_desktop_links(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    with db.session() as conn:
        access.create_desktop_link(conn, name, "test")
    sandboxes.remove(name)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM desktop_tickets").fetchone()[0] == 0


# --------------------------------------------------------------------------
# nginx configuration
# --------------------------------------------------------------------------

def test_sandbox_route_checks_every_service(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    row = _row(name)
    conf = nginxctl.conf_path(name).read_text(encoding="utf-8")
    # Exact paths only: the MCP Hub dashboard and its API stay unreachable.
    assert f"location /{name}/mcphub/" not in conf and f"location = /{name}/mcphub/sse {{" in conf
    mcp = conf.split(f"location = /{name}/mcphub/mcp {{", 1)[1].split("\n}", 1)[0]
    assert f"auth_request /_albert_auth/mcp/{name};" in mcp
    assert f'proxy_set_header Authorization "Bearer {row["mcphub_token"]}";' in mcp
    assert 'proxy_set_header Cookie "";' in mcp and 'proxy_set_header X-Albert-User "";' in mcp
    files = conf.split(f"location /{name}/files/ {{", 1)[1].split("\n}", 1)[0]
    assert f"auth_request /_albert_auth/files/{name};" in files
    assert f'Bearer {row["filesvc_token"]}' in files and "sandbox; default-src 'none'" in files
    socket = conf.split(f"location /{name}/websockify {{", 1)[1].split("\n}", 1)[0]
    assert f"auth_request /_albert_auth/desktop/{name};" in socket and 'proxy_set_header Cookie "";' in socket
    static = conf.split(f"location /{name}/ {{", 1)[1].split("\n}", 1)[0]
    assert f"alias {env['settings'].novnc_dir}/;" in static  # noVNC files from the host, not the container
    assert row["vnc_password"] not in conf  # the password only travels in the URL fragment


def test_route_of_container_without_tokens_uses_former_key(env):
    conf = nginxctl.render({"name": "sbx-old", "novnc_port": 20000, "mcphub_port": 20001, "filesvc_port": 20002,
                            "vnc_password": "pw", "filesvc_token": None, "mcphub_token": None})
    assert 'proxy_set_header Authorization "Bearer albert";' in conf
    assert 'proxy_set_header Authorization "";' in conf  # file service without token


def test_site_without_and_with_https(env):
    plain = nginxctl.render_site(None, [])
    assert "listen 80 default_server;" in plain and "listen 443" not in plain
    assert "listen 8443;" in plain and "deny all;" not in plain
    assert "return 302 http://$host:8443/manager/admin/;" in plain
    assert 'proxy_set_header X-Albert-Admin-Gate "1";' in plain
    main = plain.split("# Admin UI (own port", 1)[0]
    assert 'proxy_set_header X-Albert-Admin-Gate "";' in main and 'X-Albert-Admin-Gate "1"' not in main
    restricted = nginxctl.render_site(None, ["203.0.113.0/24"])
    assert "allow 127.0.0.1;" in restricted and "allow 203.0.113.0/24;" in restricted and "deny all;" in restricted
    tls = nginxctl.render_site({"active": True, "cert": "/c/full.pem", "key": "/c/key.pem"}, [])
    assert "listen 443 ssl http2 default_server;" in tls and "listen 8443 ssl http2;" in tls
    assert tls.count("ssl_certificate /c/full.pem;") == 2
    http80 = tls.split("listen 443", 1)[0]
    assert "https_required" in http80 and "return 301 https://$host$request_uri;" in http80
    assert ".well-known/acme-challenge" in http80


def test_write_site_keeps_previous_on_nginx_error(env, monkeypatch):
    s = config.configure(nginx_site_file=env["tmp"] / "site", nginx_site_link=env["tmp"] / "enabled" / "site")
    assert nginxctl.write_site(None, []) is True
    before = s.nginx_site_file.read_text(encoding="utf-8")
    assert s.nginx_site_link.resolve() == s.nginx_site_file.resolve()

    def broken():
        raise nginxctl.NginxError("nginx -t failed")

    monkeypatch.setattr(nginxctl, "reload", broken)
    with pytest.raises(nginxctl.NginxError):
        nginxctl.write_site(None, ["10.0.0.0/8"])
    assert s.nginx_site_file.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# admin UI: own port, allowed addresses
# --------------------------------------------------------------------------

def test_admin_ui_only_through_admin_port(env, http, admin):
    assert http.get("/admin/api/session").status_code == 404
    assert admin.get("/admin/api/session").status_code == 401  # reachable, login needed


def test_admin_address_list(env, app):
    with db.session() as conn:
        access.set_admin_allow(conn, ["203.0.113.0/24"], "203.0.113.9")
        with pytest.raises(BadRequest) as exc:
            access.set_admin_allow(conn, ["198.51.100.1"], "203.0.113.9")
        assert exc.value.code == "lockout"
        with pytest.raises(BadRequest):
            access.set_admin_allow(conn, ["kein-netz"], "203.0.113.9")
    outside = app.test_client()
    outside.environ_base.update({"HTTP_X_ALBERT_ADMIN_GATE": "1", "REMOTE_ADDR": "198.51.100.7"})
    assert outside.get("/admin/api/session").status_code == 403
    inside = app.test_client()
    inside.environ_base.update({"HTTP_X_ALBERT_ADMIN_GATE": "1", "REMOTE_ADDR": "203.0.113.20"})
    assert inside.get("/admin/api/session").status_code == 401
    assert access.ip_allowed("127.0.0.1", ["203.0.113.0/24"])  # SSH tunnel always works


def _login(client):
    from albert_sandbox import admins
    with db.session() as conn:
        if admins.count(conn) == 0:
            admins.create(conn, "root-admin", "sehr-geheim-123")
    data = client.post("/admin/api/login", json={"username": "root-admin", "password": "sehr-geheim-123"})
    assert data.status_code == 200
    return {"X-CSRF-Token": data.get_json()["csrf"]}


def test_admin_cookie_is_scoped_to_the_manager(env, app):
    client = app.test_client()
    client.environ_base.update({"HTTP_X_ALBERT_ADMIN_GATE": "1", "HTTP_X_FORWARDED_PREFIX": "/manager",
                                "HTTP_X_FORWARDED_PROTO": "https"})
    response = client.post("/admin/api/login", json={"username": "x", "password": "y"})
    assert response.status_code == 409  # no admin yet, but no cookie leak either
    _login_client = client
    from albert_sandbox import admins
    with db.session() as conn:
        admins.create(conn, "root-admin", "sehr-geheim-123")
    ok = _login_client.post("/admin/api/login", json={"username": "root-admin", "password": "sehr-geheim-123"})
    cookie = ok.headers["Set-Cookie"]
    assert "Path=/manager/" in cookie and "Secure" in cookie and "HttpOnly" in cookie


def test_admin_api_settings_and_legacy_switch(env, admin, make_client):
    h = _login(admin)
    client, _ = make_client()
    assert admin.get(f"/admin/api/clients/{client['id']}").get_json()["client"]["legacyAccess"] is False
    updated = admin.patch(f"/admin/api/clients/{client['id']}", json={"legacyAccess": True}, headers=h)
    assert updated.get_json()["client"]["legacyAccess"] is True
    view = admin.get("/admin/api/settings").get_json()["settings"]
    assert view["active"] is False and view["adminPort"] == 8443 and view["adminAllow"] == []
    saved = admin.put("/admin/api/settings/admin-access", json={"allow": "10.0.0.0/16\n127.0.0.1"}, headers=h)
    assert saved.status_code == 200 and saved.get_json()["settings"]["adminAllow"] == ["10.0.0.0/16", "127.0.0.1/32"]
    name = sandboxes.create(client["id"])["name"]
    link = admin.post(f"/admin/api/sandboxes/{name}/desktop", headers=h)
    assert link.status_code == 201 and "ticket=" in link.get_json()["desktop"]["url"]


def test_upgrade_keeps_existing_clients_on_legacy_access(env):
    # Simulate a v5 database: the column does not exist yet.
    path = env["settings"].db_path
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE api_keys DROP COLUMN legacy_access")
    conn.execute("INSERT INTO api_keys(key_hash, label, created_at) VALUES('h1', 'Bestand', 1)")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()
    db.init_db()
    with db.session() as c:
        assert c.execute("SELECT legacy_access FROM api_keys WHERE label='Bestand'").fetchone()[0] == 1
        row, _ = clients.create(c, "Neu")
        assert row["legacy_access"] == 0
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# --------------------------------------------------------------------------
# HTTPS
# --------------------------------------------------------------------------

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def _self_signed(tmp: Path, host: str, name: str = "c"):
    key, cert = tmp / f"{name}.key", tmp / f"{name}.pem"
    san = f"IP:{host}" if host.replace(".", "").isdigit() else f"DNS:{host}"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "30", "-subj", f"/CN={host}", "-addext", f"subjectAltName={san}"],
                   check=True, capture_output=True)
    return cert.read_text(), key.read_text()


@pytest.fixture
def tls_env(env):
    s = config.configure(nginx_site_file=env["tmp"] / "site", nginx_site_link=env["tmp"] / "enabled" / "site",
                         tls_dir=env["tmp"] / "tls", acme_dir=env["tmp"] / "acme",
                         letsencrypt_dir=env["tmp"] / "le")
    access.reset_url_cache()
    yield s
    access.reset_url_cache()


@needs_openssl
def test_own_certificate(env, tls_env):
    cert, key = _self_signed(env["tmp"], "sandbox.example.com")
    other_cert, other_key = _self_signed(env["tmp"], "sandbox.example.com", "d")
    with pytest.raises(BadRequest) as exc:
        tlsctl.install_custom(cert, "", other_key, "sandbox.example.com", actor="test")
    assert exc.value.code == "tls_key_mismatch"
    with pytest.raises(BadRequest) as exc:
        tlsctl.install_custom(cert, "", key, "other.example.com", actor="test")
    assert exc.value.code == "tls_name_mismatch"
    with pytest.raises(BadRequest) as exc:
        tlsctl.install_custom(key, "", key, "sandbox.example.com", actor="test")
    assert exc.value.code == "tls_key_in_certificate"
    assert not tls_env.nginx_site_file.exists()  # nothing activated by the failed attempts

    info = tlsctl.install_custom(cert, "", key, "Sandbox.Example.com", actor="test")
    assert info["active"] and info["mode"] == "custom" and info["host"] == "sandbox.example.com"
    assert info["certificate"]["selfSigned"] and info["certificate"]["daysLeft"] in (29, 30)
    assert info["publicUrl"] == "https://sandbox.example.com"
    assert info["adminUrl"] == "https://sandbox.example.com:8443/manager/admin/"
    site = tls_env.nginx_site_file.read_text(encoding="utf-8")
    keyfile = Path(site.split("ssl_certificate_key ", 1)[1].split(";", 1)[0])
    assert keyfile.read_text() == key and oct(keyfile.stat().st_mode & 0o777) == "0o600"
    # Links in API answers now use HTTPS.
    assert sandboxes.urls("sbx-x")["desktop"] == "https://sandbox.example.com/sbx-x/"
    off = tlsctl.disable(actor="test")
    assert off["active"] is False and "listen 443" not in tls_env.nginx_site_file.read_text(encoding="utf-8")


@needs_openssl
def test_own_certificate_for_an_ip_address(env, tls_env):
    cert, key = _self_signed(env["tmp"], "10.0.1.61")
    assert tlsctl.install_custom(cert, "", key, "10.0.1.61", actor="test")["host"] == "10.0.1.61"


def test_letsencrypt_with_fake_certbot(env, tls_env):
    fake = env["tmp"] / "certbot"
    fake.write_text("#!/bin/sh\n"
                    "while [ $# -gt 0 ]; do case \"$1\" in -d) host=\"$2\"; shift;; esac; shift; done\n"
                    f"mkdir -p {tls_env.letsencrypt_dir}/live/$host\n"
                    f"echo cert > {tls_env.letsencrypt_dir}/live/$host/fullchain.pem\n"
                    f"echo key > {tls_env.letsencrypt_dir}/live/$host/privkey.pem\n")
    os.chmod(fake, 0o755)
    config.configure(certbot=str(fake))
    with pytest.raises(BadRequest):
        tlsctl.start_letsencrypt("10.0.1.61", "ops@example.com", True, actor="test")  # IP: not for LE
    with pytest.raises(BadRequest):
        tlsctl.start_letsencrypt("sandbox.example.com", "ops@example.com", False, actor="test")  # terms
    tlsctl.start_letsencrypt("sandbox.example.com", "ops@example.com", True, actor="test")
    tlsctl.run_letsencrypt("test")
    with db.session() as conn:
        state = tlsctl.load(conn)
    assert state["active"] and state["mode"] == "letsencrypt" and state["job"]["state"] == "done"
    assert "listen 443 ssl" in tls_env.nginx_site_file.read_text(encoding="utf-8")
    assert (tls_env.letsencrypt_dir / "renewal-hooks" / "deploy" / "albert-sandbox-nginx.sh").exists()


def test_letsencrypt_failure_keeps_http(env, tls_env):
    fake = env["tmp"] / "certbot"
    fake.write_text("#!/bin/sh\necho 'Challenge failed for domain sandbox.example.com' >&2\nexit 1\n")
    os.chmod(fake, 0o755)
    config.configure(certbot=str(fake))
    tlsctl.start_letsencrypt("sandbox.example.com", "ops@example.com", True, actor="test")
    tlsctl.run_letsencrypt("test")
    info = tlsctl.status()
    assert info["active"] is False and info["job"]["state"] == "failed"
    assert "Challenge failed" in info["job"]["error"]


def test_waitress_keeps_forwarded_headers(env, monkeypatch):
    """waitress drops X-Forwarded-* by default: every request would look like 127.0.0.1 over HTTP."""
    import waitress
    from albert_sandbox import netctl
    seen = {}
    monkeypatch.setattr(netctl, "ensure_ready", lambda *a, **k: None)
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: seen.update(kw))
    web.serve()
    assert seen["clear_untrusted_proxy_headers"] is False and seen["host"] == "127.0.0.1"


def test_reload_waits_for_new_nginx_workers(env, monkeypatch):
    """systemctl reload returns before nginx uses the new routes; the manager waits for new workers."""
    config.configure(nginx_reload=True)
    calls = []
    states = iter([{10, 11}, {10, 11}, {10, 11}, {20, 21}])
    monkeypatch.setattr(nginxctl, "_workers", lambda: next(states))
    monkeypatch.setattr(nginxctl.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(nginxctl.time, "sleep", lambda s: None)
    nginxctl.reload()
    assert calls == [["nginx", "-t"], ["systemctl", "reload", "nginx"]]
    with pytest.raises(StopIteration):
        next(states)  # polled until the new workers showed up
