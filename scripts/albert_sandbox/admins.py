"""Admin users for the web UI (username + password, hashed with scrypt/pbkdf2)."""
from __future__ import annotations

import re
import secrets
import sqlite3
import time
from typing import List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from .errors import BadRequest, Conflict, NotFound

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
MIN_PASSWORD_LENGTH = 8
# Constant-time dummy check for unknown users (avoid user enumeration by timing).
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


def _validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise BadRequest(f"password must have at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > 256:
        raise BadRequest("password too long")


def list_all(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT id, username, created_at, last_login_at FROM admin_users ORDER BY username").fetchall()


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]


def get(conn: sqlite3.Connection, admin_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, username, created_at, last_login_at, session_epoch FROM admin_users WHERE id=?",
                       (admin_id,)).fetchone()
    if not row:
        raise NotFound("admin user not found")
    return row


def create(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise BadRequest("username: 3-64 characters, letters, digits and . _ @ -")
    _validate_password(password)
    if conn.execute("SELECT 1 FROM admin_users WHERE username=?", (username,)).fetchone():
        raise Conflict(f"admin user '{username}' already exists")
    cur = conn.execute("INSERT INTO admin_users(username, password_hash, created_at) VALUES(?,?,?)",
                       (username, generate_password_hash(password), int(time.time())))
    return get(conn, cur.lastrowid)


def set_password(conn: sqlite3.Connection, admin_id: int, password: str) -> int:
    """Set a new password and invalidate all sessions of that admin. Returns the new session epoch."""
    get(conn, admin_id)
    _validate_password(password)
    conn.execute("UPDATE admin_users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?",
                 (generate_password_hash(password), admin_id))
    return get(conn, admin_id)["session_epoch"]


def delete(conn: sqlite3.Connection, admin_id: int, acting_admin_id: Optional[int] = None) -> sqlite3.Row:
    row = get(conn, admin_id)
    if acting_admin_id is not None and admin_id == acting_admin_id:
        raise BadRequest("you cannot delete your own account")
    if count(conn) <= 1:
        raise BadRequest("the last admin user cannot be deleted")
    conn.execute("DELETE FROM admin_users WHERE id=?", (admin_id,))
    return row


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> Optional[sqlite3.Row]:
    row = conn.execute("SELECT id, username, password_hash FROM admin_users WHERE username=?",
                       ((username or "").strip(),)).fetchone()
    if not row:
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    conn.execute("UPDATE admin_users SET last_login_at=? WHERE id=?", (int(time.time()), row["id"]))
    return get(conn, row["id"])


def verify_password(conn: sqlite3.Connection, admin_id: int, password: str) -> bool:
    row = conn.execute("SELECT password_hash FROM admin_users WHERE id=?", (admin_id,)).fetchone()
    return bool(row) and check_password_hash(row["password_hash"], password or "")


def find_by_name(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT id, username, created_at, last_login_at FROM admin_users WHERE username=?",
                        ((username or "").strip(),)).fetchone()
