"""Who may use a sandbox: API key, user binding, desktop links, admin addresses.

Client API
    Every request carries the client's API key. With ``X-Albert-User`` (a
    pseudonymous id of the ALBERT user, set by ALBERT, never by the model) a
    sandbox is bound to the user that created it; other users of the same
    client neither see nor reach it.

Sandbox services (MCP Hub, file service, desktop)
    nginx asks the manager before every request (``auth_request`` ->
    ``/internal/auth/<kind>/<name>``, see ``authorize``):

    * ``mcp`` / ``files``: the request must carry the API key of the client
      owning the sandbox and, for sandboxes bound to a user, the same
      ``X-Albert-User``. nginx then replaces the key by the sandbox's internal
      token, which the container checks as well.
    * ``desktop``: the browser must hold the desktop cookie of the sandbox. It
      is set when a one-time desktop link (``create_desktop_link``) is opened.

Legacy access
    Clients with ``legacy_access`` keep working with ALBERT versions from
    before this update: MCP with the former fixed key, files without a key,
    the plain desktop URL, requests without ``X-Albert-User``. Sandboxes they
    created without a user are taken over by the first user that addresses
    them with ``X-Albert-User`` (migration).

Admin UI
    Only reachable through its own nginx server (port), optionally limited to
    a list of addresses; the loopback address (SSH tunnel) is always allowed.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import sqlite3
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import clients, db
from .config import settings
from .errors import BadRequest, NotFound

USER_HEADER = "X-Albert-User"
USER_RE = re.compile(r"^[A-Za-z0-9._:@+=/-]{1,128}$")
DESKTOP_COOKIE = "albert_desktop"
KINDS = ("mcp", "files", "desktop", "desktop-page")
ADMIN_ALLOW_KEY = "admin_allow"

# Results of ``authorize`` (HTTP status for nginx auth_request).
ALLOW, DENY_AUTH, DENY_FORBIDDEN = 204, 401, 403


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# user binding (client API)
# --------------------------------------------------------------------------

def clean_user(value: Optional[str]) -> Optional[str]:
    """The X-Albert-User value, or None if absent. Invalid values are refused."""
    text = (value or "").strip()
    if not text:
        return None
    if not USER_RE.match(text):
        raise BadRequest(f"{USER_HEADER} must be 1-128 characters of A-Z a-z 0-9 . _ : @ + = / -",
                         code="invalid_user")
    return text


def require_user(client: sqlite3.Row, user: Optional[str]) -> None:
    """Clients without legacy access must name the ALBERT user on every request."""
    if user is None and not clients.legacy_access(client):
        raise BadRequest(f"The {USER_HEADER} header is required for this client (pseudonymous id of "
                         "the ALBERT user the sandbox belongs to).", code="user_required")


def check_sandbox(conn: sqlite3.Connection, name: str, client: sqlite3.Row, user: Optional[str],
                  actor: str) -> sqlite3.Row:
    """The sandbox if this client/user may use it; NotFound otherwise.

    Sandboxes without a user (created before the user binding) are taken over
    by the first user of a legacy client that addresses them.
    """
    row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
    if not row or row["api_key_id"] != client["id"]:
        raise NotFound(f"Container '{name}' not found", code="not_found")
    if user is None:
        return row  # require_user() already refused this for clients without legacy access
    if row["owner"] == user:
        return row
    if row["owner"] is None and clients.legacy_access(client):
        conn.execute("UPDATE sandboxes SET owner=? WHERE name=? AND owner IS NULL", (user, name))
        db.audit(conn, actor, "sandbox.claim", name, {"user": user})
        return conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
    raise NotFound(f"Container '{name}' not found", code="not_found")


# --------------------------------------------------------------------------
# nginx auth_request
# --------------------------------------------------------------------------

def bearer(authorization: Optional[str]) -> Optional[str]:
    text = (authorization or "").strip()
    if not text.lower().startswith("bearer "):
        return None
    token = text.split(None, 1)[1].strip()
    return token or None


def authorize(kind: str, name: str, authorization: Optional[str] = None, user_header: Optional[str] = None,
              desktop_cookie: Optional[str] = None, running: Optional[Iterable[str]] = None) -> Tuple[int, str]:
    """Decide a request to a sandbox service. Returns (status, reason).

    ``running`` (names of running containers) is only needed for
    ``desktop-page``: a stopped sandbox gets 403 (nginx shows "stopped").
    """
    if kind not in KINDS:
        return DENY_FORBIDDEN, "unknown service"
    with db.session() as conn:
        row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
        if not row:
            return DENY_FORBIDDEN, "unknown sandbox"
        client = conn.execute("SELECT * FROM api_keys WHERE id=?", (row["api_key_id"],)).fetchone()
        if not client:
            return DENY_FORBIDDEN, "sandbox without client"
        legacy = clients.legacy_access(client)
        if kind in ("desktop", "desktop-page"):
            ok = (desktop_cookie and session_valid(conn, name, desktop_cookie)) or legacy
            if not ok:
                return DENY_AUTH, "desktop link required"
            if kind == "desktop-page" and running is not None and name not in set(running):
                return DENY_FORBIDDEN, "stopped"
            return ALLOW, "desktop"
        key = bearer(authorization)
        key_client = conn.execute("SELECT id FROM api_keys WHERE key_hash=?",
                                  (clients.hash_key(key),)).fetchone() if key else None
        if not key_client:
            # Former ALBERT versions: MCP with the fixed key, files without any key.
            return (ALLOW, "legacy") if legacy else (DENY_AUTH, "API key required")
        if key_client["id"] != row["api_key_id"]:
            return DENY_FORBIDDEN, "sandbox of another client"
        try:
            user = clean_user(user_header)
        except BadRequest:
            return DENY_FORBIDDEN, "invalid user"
        if user is None:
            return (ALLOW, "client") if legacy else (DENY_FORBIDDEN, "user required")
        if row["owner"] == user or (row["owner"] is None and legacy):
            return ALLOW, "owner"
        return DENY_FORBIDDEN, "sandbox of another user"


# --------------------------------------------------------------------------
# desktop: one-time links and desktop sessions (cookie)
# --------------------------------------------------------------------------

def create_desktop_link(conn: sqlite3.Connection, name: str, actor: str,
                        base_url: Optional[str] = None) -> Dict[str, Any]:
    """A link that opens the desktop once, valid for a short time."""
    s = settings()
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + s.desktop_ticket_seconds
    purge_expired(conn, now)
    conn.execute("INSERT INTO desktop_tickets(token_hash, sandbox, created_at, expires_at, actor) "
                 "VALUES(?,?,?,?,?)", (_hash(token), name, now, expires, actor))
    db.audit(conn, actor, "sandbox.desktop_link", name)
    base = (base_url or public_url()).rstrip("/")
    from .sandboxes import iso  # local import: sandboxes imports this module
    return {"url": f"{base}/{name}/?ticket={urllib.parse.quote(token)}", "expiresAt": iso(expires),
            "validSeconds": s.desktop_ticket_seconds, "singleUse": True}


def redeem_ticket(conn: sqlite3.Connection, name: str, ticket: str) -> Optional[Tuple[str, int]]:
    """Exchange a one-time link for a desktop session. Returns (cookie value, max age)."""
    if not ticket:
        return None
    now = int(time.time())
    deleted = conn.execute("DELETE FROM desktop_tickets WHERE token_hash=? AND sandbox=? AND expires_at>=?",
                           (_hash(ticket), name, now)).rowcount
    if deleted != 1:
        return None
    max_age = settings().desktop_session_hours * 3600
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO desktop_sessions(token_hash, sandbox, created_at, expires_at) VALUES(?,?,?,?)",
                 (_hash(token), name, now, now + max_age))
    return token, max_age


def session_valid(conn: sqlite3.Connection, name: str, cookie_value: str) -> bool:
    if not cookie_value:
        return False
    return conn.execute("SELECT 1 FROM desktop_sessions WHERE token_hash=? AND sandbox=? AND expires_at>=?",
                        (_hash(cookie_value), name, int(time.time()))).fetchone() is not None


def revoke_desktop(conn: sqlite3.Connection, name: str) -> None:
    """End all desktop sessions and links of a sandbox (removed or rebuilt with new credentials)."""
    conn.execute("DELETE FROM desktop_tickets WHERE sandbox=?", (name,))
    conn.execute("DELETE FROM desktop_sessions WHERE sandbox=?", (name,))


def purge_expired(conn: sqlite3.Connection, now: Optional[int] = None) -> int:
    now = now or int(time.time())
    removed = conn.execute("DELETE FROM desktop_tickets WHERE expires_at<?", (now,)).rowcount
    removed += conn.execute("DELETE FROM desktop_sessions WHERE expires_at<?", (now,)).rowcount
    return removed


def desktop_location(name: str, vnc_password: str) -> str:
    """noVNC page of a sandbox. The settings sit in the URL fragment, which the
    browser never sends to a server (no password in logs or referrers)."""
    fragment = urllib.parse.urlencode({"path": f"{name}/websockify", "password": vnc_password or "",
                                       "autoconnect": "true", "resize": "scale"}, safe="/")
    return f"/{name}/vnc.html#{fragment}"


# --------------------------------------------------------------------------
# public URL (HTTPS once a certificate is active)
# --------------------------------------------------------------------------

_url_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def public_url() -> str:
    """Base URL of this host for links in API answers."""
    now = time.monotonic()
    if _url_cache["value"] is not None and now - _url_cache["at"] < 5:
        return _url_cache["value"]
    value = settings().public_url
    try:
        with db.session() as conn:
            tls = db.kv_get(conn, "tls", {}) or {}
        if tls.get("active") and tls.get("fqdn"):
            value = f"https://{tls['fqdn']}"
    except sqlite3.Error:
        pass
    _url_cache.update(at=now, value=value)
    return value


def reset_url_cache() -> None:
    _url_cache.update(at=0.0, value=None)


# --------------------------------------------------------------------------
# admin UI: allowed addresses
# --------------------------------------------------------------------------

def parse_networks(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for raw in values:
        text = (raw or "").strip()
        if not text or text.startswith("#"):
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise BadRequest(f"'{text}' is not an IP address or network (e.g. 203.0.113.7 or 10.0.0.0/16)",
                             code="invalid_address")
        if str(net) not in result:
            result.append(str(net))
    if len(result) > 200:
        raise BadRequest("at most 200 addresses or networks")
    return result


def admin_allow(conn: sqlite3.Connection) -> List[str]:
    value = db.kv_get(conn, ADMIN_ALLOW_KEY, []) or []
    return [str(v) for v in value if isinstance(v, str)]


def ip_allowed(ip: Optional[str], allowed: List[str]) -> bool:
    """Empty list: everybody. The loopback address (SSH tunnel) is always allowed."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return not allowed
    if addr.is_loopback or not allowed:
        return True
    for entry in allowed:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def set_admin_allow(conn: sqlite3.Connection, values: Iterable[str], current_ip: Optional[str]) -> List[str]:
    """Store the allowed addresses; refuses lists that would lock out the current admin."""
    nets = parse_networks(values)
    if nets and not ip_allowed(current_ip, nets):
        raise BadRequest(f"Your own address {current_ip} is not in the list - you would lock yourself out. "
                         "Add it (or its network) first.", code="lockout")
    db.kv_set(conn, ADMIN_ALLOW_KEY, nets)
    return nets
