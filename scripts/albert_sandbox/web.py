"""Flask application: client REST API (Bearer API key) and admin UI/API.

Client API (unchanged paths and payloads, plus additions):
  GET    /health
  GET    /quota                         (new) quota, usage and defaults
  POST   /containers                    body: name, persistent, resources{cpus,memoryMb,diskGb}
  GET    /containers
  GET    /containers/<name>
  POST   /containers/<name>/start|stop|restart
  PATCH  /containers/<name>/persistent  (POST also accepted)
  DELETE /containers/<name>

Admin UI: /admin/ (behind nginx: /manager/admin/), JSON API under /admin/api/.
Authentication with admin users (session cookie + CSRF header).
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

from flask import Blueprint, Flask, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, admins, clients, db, dockerctl, reconcile, sandboxes, storage
from .config import settings
from .errors import BadRequest, SandboxError

STATIC_DIR = Path(__file__).resolve().parent / "static" / "admin"

client_api = Blueprint("client_api", __name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


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
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        client, err = _client_auth()
        if err:
            return err
        g.client = client
        return fn(*args, **kwargs)
    return wrapper


@client_api.get("/health")
@client_required
def health() -> Any:
    return jsonify({"status": "ok", "docker": "up" if dockerctl.available() else "down"})


@client_api.get("/quota")
@client_required
def client_quota() -> Any:
    with db.session() as conn:
        client = clients.get(conn, g.client["id"])
        usage = clients.usage(conn, client["id"], dockerctl.running_names())
    return jsonify({"quota": clients.quotas(client), "usage": usage,
                    "defaults": _defaults_dict(client)})


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
                              actor=f"client:{g.client['id']}")
    return jsonify({"container": result}), 201


@client_api.get("/containers")
@client_required
def list_containers() -> Any:
    items = sandboxes.list_sandboxes(g.client["id"])
    for item in items:
        item.pop("clientLabel", None)
    return jsonify({"containers": items})


@client_api.get("/containers/<name>")
@client_required
def get_container(name: str) -> Any:
    return jsonify({"container": sandboxes.describe(name, g.client["id"])})


@client_api.post("/containers/<name>/start")
@client_required
def start_container(name: str) -> Any:
    return jsonify({"container": sandboxes.start(name, g.client["id"], actor=f"client:{g.client['id']}")})


@client_api.post("/containers/<name>/stop")
@client_required
def stop_container(name: str) -> Any:
    return jsonify({"container": sandboxes.stop(name, g.client["id"], actor=f"client:{g.client['id']}")})


@client_api.post("/containers/<name>/restart")
@client_required
def restart_container(name: str) -> Any:
    return jsonify({"container": sandboxes.restart(name, g.client["id"], actor=f"client:{g.client['id']}")})


@client_api.route("/containers/<name>/persistent", methods=["PATCH", "POST"])
@client_required
def persistent_container(name: str) -> Any:
    body = _body()
    return jsonify(sandboxes.set_persistent(name, _bool(body.get("persistent"), True), g.client["id"],
                                            actor=f"client:{g.client['id']}"))


@client_api.delete("/containers/<name>")
@client_required
def delete_container(name: str) -> Any:
    sandboxes.remove(name, g.client["id"], actor=f"client:{g.client['id']}")
    return jsonify({"deleted": name})


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
    return {
        "id": client["id"],
        "label": client["label"] or "",
        "createdAt": sandboxes.iso(client["created_at"]),
        "updatedAt": sandboxes.iso(client["updated_at"]),
        "keyHashPrefix": client["key_hash"][:12],
        "defaults": _defaults_dict(client),
        "quota": clients.quotas(client),
        "usage": clients.usage(conn, client["id"], running),
    }


_CLIENT_FIELD_MAP = {
    "label": "label",
    "quotaCpus": "quota_cpus",
    "quotaMemoryMb": "quota_memory_mb",
    "quotaDiskGb": "quota_disk_gb",
    "quotaSandboxes": "quota_sandboxes",
    "defaultCpus": "default_cpus",
    "defaultMemoryMb": "default_memory_mb",
    "defaultDiskGb": "default_disk_gb",
}


def _client_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    return {column: body[key] for key, column in _CLIENT_FIELD_MAP.items() if key in body}


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
        client = clients.update(conn, client_id, fields)
        db.audit(conn, g.actor, "client.update", client["label"], {"clientId": client_id, **fields})
        return jsonify({"client": _client_dict(conn, client, running)})


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

def create_app() -> Flask:
    s = settings()
    db.init_db()
    app = Flask(__name__, static_folder=None)
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
    try:
        from waitress import serve as waitress_serve
    except ImportError:  # pragma: no cover - fallback for dev setups
        app.run(host=s.bind_host, port=s.port, threaded=True)
        return
    # Forwarded headers from nginx are evaluated by ProxyFix (one trusted hop).
    waitress_serve(app, host=s.bind_host, port=s.port, threads=int(s.extra.get("MANAGER_THREADS", "16")),
                   ident="albert-sandbox-manager")
