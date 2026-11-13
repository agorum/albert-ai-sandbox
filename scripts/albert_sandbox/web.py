"""Flask application: client REST API (Bearer API key) and admin UI/API.

Client API (unchanged paths and payloads, plus additions):
  GET    /health
  GET    /quota                         (new) quota, usage, defaults, persistentAllowed,
                                        internetAccess, profile
  POST   /containers                    body: name, persistent, resources{cpus,memoryMb,diskGb}
  GET    /containers
  GET    /containers/<name>
  POST   /containers/<name>/start|stop|restart
  PATCH  /containers/<name>/persistent  (POST also accepted)
  POST   /containers/<name>/desktop     (new) one-time link to the desktop
  DELETE /containers/<name>
Header X-Albert-User binds sandboxes to an ALBERT user (required unless the
client has legacy access, see access.py).

Admin UI: /admin/ (behind nginx: /manager/admin/ on the admin port only),
JSON API under /admin/api/. Authentication with admin users (session cookie +
CSRF header).

Internal (nginx only, never reachable from outside):
  GET    /internal/auth/<kind>/<name>   auth_request for MCP, files, desktop
  GET    /internal/desktop/<name>/entry desktop entry (one-time link -> cookie)
"""
from __future__ import annotations

import functools
import os
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from flask import Blueprint, Flask, Response, g, jsonify, redirect, request, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, access, admins, clients, db, dockerctl, nginxctl, profiles, reconcile, sandboxes, \
    storage, tlsctl
from .config import settings
from .errors import BadRequest, SandboxError

STATIC_DIR = Path(__file__).resolve().parent / "static" / "admin"

client_api = Blueprint("client_api", __name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
internal_bp = Blueprint("internal", __name__, url_prefix="/internal")


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def _body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _secret_key() -> bytes:
    path = settings().secret_key_file
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(48)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return data


# --------------------------------------------------------------------------
# client API
# --------------------------------------------------------------------------

def _client_auth() -> Tuple[Optional[Any], Optional[Tuple[Any, int]]]:
    # "error" keeps the wording of the previous versions; "message" matches all other errors.
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        msg = "Missing or invalid Authorization header"
        return None, (jsonify({"error": msg, "message": msg}), 401)
    key = auth.split(None, 1)[1].strip()
    with db.session() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", (clients.hash_key(key),)).fetchone()
    if not row:
        return None, (jsonify({"error": "Invalid API key", "message": "Invalid API key"}), 401)
    return row, None


def client_required(fn: Callable) -> Callable:
    """API key of a client; X-Albert-User (user binding) unless the client has legacy access."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        client, err = _client_auth()
        if err:
            return err
        g.client = client
        g.albert_user = access.clean_user(request.headers.get(access.USER_HEADER))
        g.client_actor = f"client:{client['id']}" + (f"/{g.albert_user}" if g.albert_user else "")
        if fn.__name__ != "health":
            access.require_user(client, g.albert_user)
        return fn(*args, **kwargs)
    return wrapper


def _own_sandbox(name: str) -> None:
    """404 unless the sandbox belongs to this client (and, with X-Albert-User, to this user)."""
    with db.session() as conn:
        access.check_sandbox(conn, name, g.client, g.albert_user, g.client_actor)


# Version of the client interface. Clients (ALBERT | AI) use it to tell this
# server from older ones (no "api" field) and switch their behaviour.
API_VERSION = 3
API_FEATURES = (
    "quota",          # GET /quota
    "profiles",       # software profiles, internetAccess/profile in /quota
    "user-binding",   # X-Albert-User; lists and actions limited to the user's sandboxes
    "service-auth",   # MCP Hub and file service need the API key (+ user)
    "desktop-link",   # POST /containers/<name>/desktop, one-time links
    "no-secrets",     # create returns neither VNC password nor file token
)


@client_api.after_request
def api_version_header(response: Any) -> Any:
    response.headers["X-Albert-Sandbox-Api"] = str(API_VERSION)
    return response


@client_api.get("/health")
@client_required
def health() -> Any:
    tls = tlsctl.status_brief()
    return jsonify({"status": "ok", "docker": "up" if dockerctl.available() else "down",
                    "api": API_VERSION, "features": list(API_FEATURES),
                    # The client's own settings, so it can tell which access it gets.
                    "legacyAccess": clients.legacy_access(g.client), "https": tls["active"]})


@client_api.get("/quota")
@client_required
def client_quota() -> Any:
    with db.session() as conn:
        client = clients.get(conn, g.client["id"])
        # Totals for the whole client; runningNames only the asking user's sandboxes.
        usage = clients.usage(conn, client["id"], dockerctl.running_names(), owner=g.albert_user)
        profile = profiles.get(conn, client["profile_id"]) if client["profile_id"] else None
    return jsonify({"quota": clients.quotas(client), "usage": usage,
                    "defaults": _defaults_dict(client),
                    "persistentAllowed": clients.persistent_allowed(client),
                    # Without internet access sandboxes cannot download anything (no DNS either);
                    # the profile lists the software installed on top of the standard image.
                    "internetAccess": clients.internet_allowed(client),
                    "profile": profiles.summary(profile)})


@client_api.post("/containers")
@client_required
def create_container() -> Any:
    body = _body()
    resources = body.get("resources") if isinstance(body.get("resources"), dict) else {}
    for key in ("cpus", "memoryMb", "diskGb"):
        if key in body and key not in resources:
            resources[key] = body[key]
    result = sandboxes.create(g.client["id"], requested_name=body.get("name") or None,
                              persistent=_bool(body.get("persistent")), resources=resources,
                              actor=g.client_actor, owner=g.albert_user)
    return jsonify({"container": result}), 201


@client_api.get("/containers")
@client_required
def list_containers() -> Any:
    items = sandboxes.list_sandboxes(g.client["id"], owner=g.albert_user)
    for item in items:
        item.pop("clientLabel", None)
    return jsonify({"containers": items})


@client_api.get("/containers/<name>")
@client_required
def get_container(name: str) -> Any:
    _own_sandbox(name)
    return jsonify({"container": sandboxes.describe(name, g.client["id"])})


@client_api.post("/containers/<name>/start")
@client_required
def start_container(name: str) -> Any:
    _own_sandbox(name)
    return jsonify({"container": sandboxes.start(name, g.client["id"], actor=g.client_actor)})


@client_api.post("/containers/<name>/stop")
@client_required
def stop_container(name: str) -> Any:
    _own_sandbox(name)
    return jsonify({"container": sandboxes.stop(name, g.client["id"], actor=g.client_actor)})


@client_api.post("/containers/<name>/restart")
@client_required
def restart_container(name: str) -> Any:
    _own_sandbox(name)
    return jsonify({"container": sandboxes.restart(name, g.client["id"], actor=g.client_actor)})


@client_api.route("/containers/<name>/persistent", methods=["PATCH", "POST"])
@client_required
def persistent_container(name: str) -> Any:
    _own_sandbox(name)
    body = _body()
    return jsonify(sandboxes.set_persistent(name, _bool(body.get("persistent"), True), g.client["id"],
                                            actor=g.client_actor))


@client_api.post("/containers/<name>/desktop")
@client_required
def desktop_container(name: str) -> Any:
    """One-time link to the desktop (valid for a short time, opens a desktop session in the browser)."""
    with db.session() as conn:
        access.check_sandbox(conn, name, g.client, g.albert_user, g.client_actor)
        link = access.create_desktop_link(conn, name, g.client_actor)
    return jsonify({"desktop": link}), 201


@client_api.delete("/containers/<name>")
@client_required
def delete_container(name: str) -> Any:
    _own_sandbox(name)
    sandboxes.remove(name, g.client["id"], actor=g.client_actor)
    return jsonify({"deleted": name})


# --------------------------------------------------------------------------
# internal endpoints for nginx (auth_request, desktop entry)
# --------------------------------------------------------------------------

@internal_bp.before_request
def internal_only() -> Any:
    # nginx sets this header only on its internal routes and clears it on /manager/.
    if request.headers.get("X-Albert-Internal") != "1":
        return jsonify({"error": "Not found", "message": "Not found"}), 404
    return None


@internal_bp.get("/auth/<kind>/<name>")
def internal_auth(kind: str, name: str) -> Any:
    running = dockerctl.running_names() if kind == "desktop-page" else None
    status, reason = access.authorize(kind, name, authorization=request.headers.get("Authorization"),
                                      user_header=request.headers.get(access.USER_HEADER),
                                      desktop_cookie=request.cookies.get(access.DESKTOP_COOKIE),
                                      running=running)
    response = Response(status=status)
    response.headers["X-Albert-Reason"] = reason
    response.headers["Cache-Control"] = "no-store"
    return response


@internal_bp.get("/desktop/<name>/entry")
def internal_desktop_entry(name: str) -> Any:
    """/<name>/ of a sandbox: a one-time link becomes a desktop session (cookie), then the noVNC page.

    Without a valid link or session the browser lands on the noVNC page, where
    nginx shows "link expired" instead.
    """
    if not nginxctl.NAME_RE.match(name):
        return jsonify({"error": "Not found", "message": "Not found"}), 404
    ticket = request.args.get("ticket", "")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
        if not row:
            return jsonify({"error": "Not found", "message": "Not found"}), 404
        client = conn.execute("SELECT * FROM api_keys WHERE id=?", (row["api_key_id"],)).fetchone()
        session_cookie = access.redeem_ticket(conn, name, ticket) if ticket else None
        allowed = bool(session_cookie) or access.session_valid(
            conn, name, request.cookies.get(access.DESKTOP_COOKIE, "")) or bool(client and clients.legacy_access(client))
        if session_cookie:
            db.audit(conn, f"ip:{request.remote_addr}", "sandbox.desktop_open", name)
    response = Response(status=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Location"] = access.desktop_location(name, row["vnc_password"]) if allowed \
        else f"/{name}/vnc.html"
    if session_cookie:
        value, max_age = session_cookie
        response.set_cookie(access.DESKTOP_COOKIE, value, max_age=max_age, path=f"/{name}/",
                            secure=request.scheme == "https", httponly=True, samesite="Lax")
    return response


# --------------------------------------------------------------------------
# admin auth
# --------------------------------------------------------------------------

class _LoginThrottle:
    """Slow down password guessing: per IP, exponential lockout after 5 failures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Tuple[int, float]] = {}

    def check(self, key: str) -> float:
        with self._lock:
            fails, until = self._state.get(key, (0, 0.0))
            return max(0.0, until - time.time())

    def failure(self, key: str) -> None:
        with self._lock:
            fails, _ = self._state.get(key, (0, 0.0))
            fails += 1
            delay = 0.0 if fails < 5 else min(900.0, 30.0 * (2 ** (fails - 5)))
            self._state[key] = (fails, time.time() + delay)
            if len(self._state) > 10000:
                self._state.clear()

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


throttle = _LoginThrottle()


def _current_admin() -> Optional[Any]:
    admin_id = session.get("admin_id")
    if not admin_id:
        return None
    with db.session() as conn:
        row = conn.execute("SELECT id, username, session_epoch FROM admin_users WHERE id=?", (admin_id,)).fetchone()
    # Deleted admin or password changed since this session was created.
    if not row or row["session_epoch"] != session.get("epoch", 0):
        session.clear()
        return None
    return row


@admin_bp.before_request
def admin_gate() -> Any:
    """The admin UI only answers on its own nginx server (port), from allowed addresses."""
    if request.headers.get("X-Albert-Admin-Gate") != "1":
        return jsonify({"error": "Not found", "message": "Not found"}), 404
    with db.session() as conn:
        allowed = access.admin_allow(conn)
    if not access.ip_allowed(request.remote_addr, allowed):
        return jsonify({"error": "forbidden", "message": "Address not allowed"}), 403
    return None


def admin_required(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        admin = _current_admin()
        if not admin:
            return jsonify({"error": "unauthorized", "message": "Login required"}), 401
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.headers.get("X-CSRF-Token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf", "")):
                return jsonify({"error": "csrf", "message": "Invalid CSRF token"}), 403
        g.admin = admin
        g.actor = f"admin:{admin['username']}"
        return fn(*args, **kwargs)
    return wrapper


@admin_bp.get("/")
def admin_index() -> Any:
    response = send_from_directory(STATIC_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.get("/static/<path:filename>")
def admin_static(filename: str) -> Any:
    response = send_from_directory(STATIC_DIR, filename)
    response.headers["Cache-Control"] = "no-cache"
    return response


@admin_bp.post("/api/login")
def admin_login() -> Any:
    body = _body()
    ip = request.remote_addr or "?"
    wait = throttle.check(ip)
    if wait > 0:
        return jsonify({"error": "throttled", "message": f"Too many failed logins, retry in {int(wait) + 1}s"}), 429
    with db.session() as conn:
        if admins.count(conn) == 0:
            return jsonify({"error": "no_admins",
                            "message": "No admin user exists yet. Create one on the host: "
                                       "albert-ai-sandbox-manager admin add <username>"}), 409
        user = admins.authenticate(conn, body.get("username", ""), body.get("password", ""))
        if not user:
            throttle.failure(ip)
            db.audit(conn, f"ip:{ip}", "admin.login_failed", str(body.get("username", ""))[:64])
            return jsonify({"error": "invalid_credentials", "message": "Invalid username or password"}), 401
        throttle.success(ip)
        db.audit(conn, f"admin:{user['username']}", "admin.login", None, {"ip": ip})
    session.clear()
    session.permanent = True
    session["admin_id"] = user["id"]
    session["epoch"] = user["session_epoch"]
    session["csrf"] = secrets.token_urlsafe(32)
    return jsonify({"user": {"id": user["id"], "username": user["username"]}, "csrf": session["csrf"],
                    "version": __version__})


@admin_bp.post("/api/logout")
def admin_logout() -> Any:
    session.clear()
    return jsonify({"ok": True})


@admin_bp.get("/api/session")
def admin_session() -> Any:
    admin = _current_admin()
    if not admin:
        with db.session() as conn:
            has_admins = admins.count(conn) > 0
        return jsonify({"error": "unauthorized", "hasAdmins": has_admins, "version": __version__}), 401
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return jsonify({"user": {"id": admin["id"], "username": admin["username"]}, "csrf": session["csrf"],
                    "version": __version__})


# --------------------------------------------------------------------------
# admin API
# --------------------------------------------------------------------------

def _defaults_dict(client: Any) -> Dict[str, Any]:
    effective = clients.sandbox_defaults(client)
    return {
        "cpus": client["default_cpus"],
        "memoryMb": client["default_memory_mb"],
        "diskGb": client["default_disk_gb"],
        "effective": {"cpus": effective["cpus"], "memoryMb": effective["memory_mb"],
                      "diskGb": effective["disk_gb"]},
    }


def _client_dict(conn: Any, client: Any, running: set) -> Dict[str, Any]:
    profile = None
    if client["profile_id"]:
        profile = conn.execute("SELECT id, name FROM profiles WHERE id=?", (client["profile_id"],)).fetchone()
    return {
        "id": client["id"],
        "label": client["label"] or "",
        "createdAt": sandboxes.iso(client["created_at"]),
        "updatedAt": sandboxes.iso(client["updated_at"]),
        "keyHashPrefix": client["key_hash"][:12],
        "defaults": _defaults_dict(client),
        "quota": clients.quotas(client),
        "usage": clients.usage(conn, client["id"], running),
        "allowPersistent": clients.persistent_allowed(client),
        "allowInternet": clients.internet_allowed(client),
        "legacyAccess": clients.legacy_access(client),
        "profileId": client["profile_id"],
        "profile": {"id": profile["id"], "name": profile["name"]} if profile else None,
    }


_CLIENT_FIELD_MAP = {
    "label": "label",
    "quotaCpus": "quota_cpus",
    "quotaMemoryMb": "quota_memory_mb",
    "quotaDiskGb": "quota_disk_gb",
    "quotaSandboxes": "quota_sandboxes",
    "quotaRunning": "quota_running",
    "allowPersistent": "allow_persistent",
    "allowInternet": "allow_internet",
    "legacyAccess": "legacy_access",
    "profileId": "profile_id",
    "defaultCpus": "default_cpus",
    "defaultMemoryMb": "default_memory_mb",
    "defaultDiskGb": "default_disk_gb",
}


def _client_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    return {column: body[key] for key, column in _CLIENT_FIELD_MAP.items() if key in body}


def run_in_background(target: Callable, *args: Any, **kwargs: Any) -> None:
    """Long-running follow-up work of a request (tests replace this with a direct call)."""
    def runner() -> None:
        try:
            target(*args, **kwargs)
        except Exception as exc:  # the state stays visible in the UI (sandbox errors, audit log)
            print(f"[ERROR] background {getattr(target, '__name__', target)}: {exc}", flush=True)
    threading.Thread(target=runner, daemon=True).start()


@admin_bp.get("/api/overview")
@admin_required
def admin_overview() -> Any:
    return jsonify(sandboxes.host_overview())


@admin_bp.get("/api/clients")
@admin_required
def admin_clients() -> Any:
    running = dockerctl.running_names()
    with db.session() as conn:
        return jsonify({"clients": [_client_dict(conn, c, running) for c in clients.list_all(conn)]})


@admin_bp.post("/api/clients")
@admin_required
def admin_create_client() -> Any:
    body = _body()
    fields = _client_fields(body)
    label = fields.pop("label", "") or ""
    if not isinstance(label, str) or not label.strip():
        raise BadRequest("label is required")
    with db.session() as conn:
        client, raw = clients.create(conn, label, **fields)
        db.audit(conn, g.actor, "client.create", label, {"clientId": client["id"]})
        data = _client_dict(conn, client, set())
    return jsonify({"client": data, "apiKey": raw}), 201


@admin_bp.get("/api/clients/<int:client_id>")
@admin_required
def admin_get_client(client_id: int) -> Any:
    running = dockerctl.running_names()
    with db.session() as conn:
        data = _client_dict(conn, clients.get(conn, client_id), running)
    data["sandboxes"] = sandboxes.list_sandboxes(client_id)
    return jsonify({"client": data})


@admin_bp.patch("/api/clients/<int:client_id>")
@admin_required
def admin_update_client(client_id: int) -> Any:
    fields = _client_fields(_body())
    if "label" in fields and (not isinstance(fields["label"], str) or not fields["label"].strip()):
        raise BadRequest("label is required")
    running = dockerctl.running_names()
    with db.session() as conn:
        before = clients.get(conn, client_id)
        client = clients.update(conn, client_id, fields)
        db.audit(conn, g.actor, "client.update", client["label"], {"clientId": client_id, **fields})
        data = _client_dict(conn, client, running)
        has_sandboxes = conn.execute("SELECT 1 FROM sandboxes WHERE api_key_id=? LIMIT 1",
                                     (client_id,)).fetchone() is not None
    # Internet access changed: move/rebuild the client's sandboxes right away.
    if bool(before["allow_internet"]) != bool(client["allow_internet"]) and has_sandboxes:
        run_in_background(sandboxes.apply_network_policy, client_id, actor=g.actor)
        data["networkPolicy"] = "applying"
    return jsonify({"client": data})


@admin_bp.post("/api/clients/<int:client_id>/rotate-key")
@admin_required
def admin_rotate_key(client_id: int) -> Any:
    with db.session() as conn:
        raw = clients.rotate_key(conn, client_id)
        client = clients.get(conn, client_id)
        db.audit(conn, g.actor, "client.rotate_key", client["label"], {"clientId": client_id})
    return jsonify({"apiKey": raw})


@admin_bp.delete("/api/clients/<int:client_id>")
@admin_required
def admin_delete_client(client_id: int) -> Any:
    return jsonify(sandboxes.delete_client(client_id, actor=g.actor))


_PROFILE_FIELD_MAP = {
    "name": "name",
    "description": "description",
    "setupScript": "setup_script",
}


def _profile_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    fields = {column: body[key] for key, column in _PROFILE_FIELD_MAP.items() if key in body}
    pk = body.get("packages")
    if pk is not None:
        if not isinstance(pk, dict):
            raise BadRequest("packages must be an object with apt, pip and npm lists")
        for kind, column in profiles.PACKAGE_COLUMNS.items():
            if kind in pk:
                fields[column] = pk[kind]
    return fields


def _profile_view(conn: Any, row: Any, with_dockerfile: bool = False) -> Dict[str, Any]:
    data = profiles.serialize(conn, row, profiles.base_image_id())
    if with_dockerfile:
        data["dockerfile"] = profiles.dockerfile(row)
    return data


@admin_bp.get("/api/profiles")
@admin_required
def admin_profiles() -> Any:
    base_id = profiles.base_image_id()
    with db.session() as conn:
        return jsonify({"profiles": [profiles.serialize(conn, r, base_id) for r in profiles.list_all(conn)],
                        "baseImage": settings().docker_image})


@admin_bp.post("/api/profiles")
@admin_required
def admin_create_profile() -> Any:
    fields = _profile_fields(_body())
    name = fields.pop("name", "")
    with db.session() as conn:
        row = profiles.create(conn, name if isinstance(name, str) else "", **fields)
        db.audit(conn, g.actor, "profile.create", row["name"], {"profileId": row["id"]})
        return jsonify({"profile": _profile_view(conn, row, with_dockerfile=True)}), 201


@admin_bp.get("/api/profiles/<int:profile_id>")
@admin_required
def admin_get_profile(profile_id: int) -> Any:
    with db.session() as conn:
        profiles.expire_stale(conn)
        return jsonify({"profile": _profile_view(conn, profiles.get(conn, profile_id), with_dockerfile=True)})


@admin_bp.patch("/api/profiles/<int:profile_id>")
@admin_required
def admin_update_profile(profile_id: int) -> Any:
    fields = _profile_fields(_body())
    with db.session() as conn:
        row = profiles.update(conn, profile_id, fields)
        db.audit(conn, g.actor, "profile.update", row["name"],
                 {"profileId": profile_id, "fields": sorted(fields)})
        return jsonify({"profile": _profile_view(conn, row, with_dockerfile=True)})


@admin_bp.delete("/api/profiles/<int:profile_id>")
@admin_required
def admin_delete_profile(profile_id: int) -> Any:
    with db.session() as conn:
        row = profiles.delete(conn, profile_id)
        db.audit(conn, g.actor, "profile.delete", row["name"], {"profileId": profile_id})
    return jsonify({"deleted": profile_id})


@admin_bp.post("/api/profiles/<int:profile_id>/build")
@admin_required
def admin_build_profile(profile_id: int) -> Any:
    profiles.request_build(profile_id, actor=g.actor)
    with db.session() as conn:
        return jsonify({"profile": _profile_view(conn, profiles.get(conn, profile_id), with_dockerfile=True)}), 202


@admin_bp.get("/api/profiles/<int:profile_id>/log")
@admin_required
def admin_profile_log(profile_id: int) -> Any:
    return jsonify(profiles.read_log(profile_id, seq=request.args.get("seq", type=int),
                                     offset=request.args.get("offset", default=0, type=int)))


@admin_bp.get("/api/sandboxes")
@admin_required
def admin_sandboxes() -> Any:
    client_id = request.args.get("client", type=int)
    return jsonify({"sandboxes": sandboxes.list_sandboxes(client_id)})


@admin_bp.get("/api/sandboxes/<name>/stats")
@admin_required
def admin_sandbox_stats(name: str) -> Any:
    sandboxes.describe(name)  # 404 if unknown
    stats = dockerctl.stats([name]).get(name)
    usage = storage.usage(name)
    return jsonify({"stats": stats, "disk": {"usedBytes": usage[0], "sizeBytes": usage[1]} if usage else None})


@admin_bp.post("/api/sandboxes/<name>/<action>")
@admin_required
def admin_sandbox_action(name: str, action: str) -> Any:
    handlers = {"start": sandboxes.start, "stop": sandboxes.stop, "restart": sandboxes.restart,
                "recreate": sandboxes.recreate}
    if action not in handlers:
        raise BadRequest(f"unknown action {action}")
    return jsonify({"sandbox": handlers[action](name, actor=g.actor)})


@admin_bp.patch("/api/sandboxes/<name>")
@admin_required
def admin_update_sandbox(name: str) -> Any:
    body = _body()

    def num(key: str, cast: Callable) -> Any:
        if body.get(key) in (None, ""):
            return None
        try:
            return cast(body[key])
        except (TypeError, ValueError):
            raise BadRequest(f"{key} must be a number")

    cpus, memory_mb, disk_gb = num("cpus", float), num("memoryMb", int), num("diskGb", int)
    # Resources first: if they are refused, nothing else has been changed.
    if any(v is not None for v in (cpus, memory_mb, disk_gb)):
        sandboxes.update_resources(name, cpus=cpus, memory_mb=memory_mb, disk_gb=disk_gb, actor=g.actor)
    if "persistent" in body:
        sandboxes.set_persistent(name, _bool(body["persistent"]), actor=g.actor)
    return jsonify({"sandbox": sandboxes.describe(name)})


@admin_bp.delete("/api/sandboxes/<name>")
@admin_required
def admin_delete_sandbox(name: str) -> Any:
    sandboxes.remove(name, actor=g.actor)
    return jsonify({"deleted": name})


@admin_bp.post("/api/sandboxes/<name>/desktop")
@admin_required
def admin_sandbox_desktop(name: str) -> Any:
    """One-time desktop link for an administrator."""
    sandboxes.describe(name)  # 404 if unknown
    with db.session() as conn:
        link = access.create_desktop_link(conn, name, g.actor)
    return jsonify({"desktop": link}), 201


# --------------------------------------------------------------------------
# settings: HTTPS certificate, admin addresses
# --------------------------------------------------------------------------

def _settings_view() -> Dict[str, Any]:
    data = tlsctl.status()
    data["yourAddress"] = request.remote_addr
    return data


@admin_bp.get("/api/settings")
@admin_required
def admin_settings() -> Any:
    return jsonify({"settings": _settings_view()})


@admin_bp.put("/api/settings/admin-access")
@admin_required
def admin_set_access() -> Any:
    body = _body()
    values = body.get("allow")
    if isinstance(values, str):
        values = values.replace(",", "\n").splitlines()
    if not isinstance(values, list):
        raise BadRequest("allow must be a list of addresses or networks")
    with db.session() as conn:
        previous = access.admin_allow(conn)
        nets = access.set_admin_allow(conn, [str(v) for v in values], request.remote_addr)
        try:
            tlsctl.apply_site()
        except nginxctl.NginxError as exc:
            db.kv_set(conn, access.ADMIN_ALLOW_KEY, previous)
            raise SandboxError(f"nginx refused the change, nothing was changed: {exc}", code="nginx_failed")
        db.audit(conn, g.actor, "settings.admin_access", None, {"allow": nets})
    return jsonify({"settings": _settings_view()})


@admin_bp.post("/api/settings/tls/custom")
@admin_required
def admin_tls_custom() -> Any:
    body = _body()
    tlsctl.install_custom(body.get("certificate"), body.get("chain"), body.get("privateKey"),
                          body.get("host"), actor=g.actor)
    return jsonify({"settings": _settings_view()})


@admin_bp.post("/api/settings/tls/letsencrypt")
@admin_required
def admin_tls_letsencrypt() -> Any:
    body = _body()
    tlsctl.start_letsencrypt(body.get("host"), body.get("email"), body.get("acceptTerms"), actor=g.actor)
    run_in_background(tlsctl.run_letsencrypt, g.actor)
    return jsonify({"settings": _settings_view()}), 202


@admin_bp.delete("/api/settings/tls")
@admin_required
def admin_tls_disable() -> Any:
    tlsctl.disable(actor=g.actor)
    return jsonify({"settings": _settings_view()})


@admin_bp.get("/api/admins")
@admin_required
def admin_list_admins() -> Any:
    with db.session() as conn:
        return jsonify({"admins": [
            {"id": r["id"], "username": r["username"], "createdAt": sandboxes.iso(r["created_at"]),
             "lastLoginAt": sandboxes.iso(r["last_login_at"]), "self": r["id"] == g.admin["id"]}
            for r in admins.list_all(conn)
        ]})


@admin_bp.post("/api/admins")
@admin_required
def admin_create_admin() -> Any:
    body = _body()
    with db.session() as conn:
        row = admins.create(conn, body.get("username", ""), body.get("password", ""))
        db.audit(conn, g.actor, "admin.create", row["username"])
    return jsonify({"admin": {"id": row["id"], "username": row["username"]}}), 201


@admin_bp.post("/api/admins/<int:admin_id>/password")
@admin_required
def admin_set_password(admin_id: int) -> Any:
    body = _body()
    with db.session() as conn:
        if admin_id == g.admin["id"] and not admins.verify_password(conn, admin_id, body.get("currentPassword", "")):
            raise BadRequest("current password is wrong")
        epoch = admins.set_password(conn, admin_id, body.get("password", ""))
        target = admins.get(conn, admin_id)
        db.audit(conn, g.actor, "admin.password", target["username"])
    if admin_id == g.admin["id"]:
        session["epoch"] = epoch  # keep the current session, other sessions are logged out
    return jsonify({"ok": True})


@admin_bp.delete("/api/admins/<int:admin_id>")
@admin_required
def admin_delete_admin(admin_id: int) -> Any:
    with db.session() as conn:
        row = admins.delete(conn, admin_id, acting_admin_id=g.admin["id"])
        db.audit(conn, g.actor, "admin.delete", row["username"])
    return jsonify({"deleted": admin_id})


@admin_bp.get("/api/reconcile")
@admin_required
def admin_reconcile_preview() -> Any:
    return jsonify(reconcile.reconcile(dry_run=True, actor=g.actor))


@admin_bp.post("/api/reconcile")
@admin_required
def admin_reconcile_apply() -> Any:
    return jsonify(reconcile.reconcile(dry_run=False, actor=g.actor))


@admin_bp.get("/api/audit")
@admin_required
def admin_audit() -> Any:
    limit = max(1, min(request.args.get("limit", default=200, type=int), 2000))
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"entries": [
        {"id": r["id"], "ts": sandboxes.iso(r["ts"]), "actor": r["actor"], "action": r["action"],
         "target": r["target"], "details": r["details"]} for r in rows
    ]})


# --------------------------------------------------------------------------
# app factory
# --------------------------------------------------------------------------

class _SessionInterface(SecureCookieSessionInterface):
    """Admin cookie: only for the manager's own paths (behind nginx: /manager/), never
    for the pages of sandboxes; Secure whenever the admin UI is used over HTTPS."""

    def get_cookie_path(self, app: Flask) -> str:
        return (request.script_root or "").rstrip("/") + "/"

    def get_cookie_secure(self, app: Flask) -> bool:
        return bool(app.config.get("SESSION_COOKIE_SECURE")) or request.scheme == "https"


def create_app() -> Flask:
    s = settings()
    db.init_db()
    app = Flask(__name__, static_folder=None)
    app.session_interface = _SessionInterface()
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)  # type: ignore[assignment]
    app.config.update(
        SECRET_KEY=_secret_key(),
        SESSION_COOKIE_NAME="albert_admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=s.public_url.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=s.admin_session_hours),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    app.json.sort_keys = False  # type: ignore[attr-defined]
    app.register_blueprint(client_api)
    app.register_blueprint(admin_bp)
    app.register_blueprint(internal_bp)

    @app.get("/admin")
    def admin_redirect() -> Any:
        return redirect(request.script_root + "/admin/", code=308)

    @app.errorhandler(SandboxError)
    def handle_sandbox_error(exc: SandboxError) -> Any:
        return jsonify(exc.to_dict()), exc.status

    @app.errorhandler(dockerctl.DockerError)
    def handle_docker_error(exc: dockerctl.DockerError) -> Any:
        return jsonify({"error": "docker", "message": str(exc)}), 502

    @app.errorhandler(404)
    def handle_404(_exc: Any) -> Any:
        return jsonify({"error": "Not found", "message": "Not found"}), 404

    @app.errorhandler(405)
    def handle_405(_exc: Any) -> Any:
        return jsonify({"error": "Method not allowed", "message": "Method not allowed"}), 405

    @app.after_request
    def security_headers(response: Any) -> Any:
        if request.path.startswith("/admin"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.path.startswith("/admin/api") or not request.path.startswith("/admin"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


def serve() -> None:
    s = settings()
    app = create_app()
    # Second line of defence after boot (the manager starts right after dockerd):
    # network + firewall rules must be in place for containers docker restarted.
    try:
        from . import netctl
        netctl.ensure_ready()
    except Exception as exc:  # keep serving; create/start refuse without isolation anyway
        print(f"[WARN] network isolation not ready at startup: {exc}", flush=True)
    try:
        from waitress import serve as waitress_serve
    except ImportError:  # pragma: no cover - fallback for dev setups
        app.run(host=s.bind_host, port=s.port, threaded=True)
        return
    # Forwarded headers from nginx are evaluated by ProxyFix (one trusted hop: the
    # service only listens on loopback and nginx sets them on every route).
    # waitress would otherwise drop X-Forwarded-For/-Proto, and every request
    # would look like 127.0.0.1 over HTTP (login throttle per address, admin
    # address list, Secure cookies behind HTTPS).
    waitress_serve(app, host=s.bind_host, port=s.port, threads=int(s.extra.get("MANAGER_THREADS", "16")),
                   ident="albert-sandbox-manager", clear_untrusted_proxy_headers=False)
