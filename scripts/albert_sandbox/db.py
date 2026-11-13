"""SQLite access, schema and schema migrations.

SQLite is the single source of truth for clients (API keys), sandboxes,
admin users and the audit log. Every connection enables WAL mode and a busy
timeout so the REST service, the CLI and the watcher can work concurrently.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .config import settings

SCHEMA_VERSION = 5

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_hash TEXT UNIQUE NOT NULL,
  label TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS containers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  api_key_id INTEGER NOT NULL,
  container_id TEXT UNIQUE NOT NULL,
  name TEXT,
  image TEXT,
  created_at INTEGER NOT NULL,
  FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);
"""

V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
  container_id TEXT,
  image TEXT,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  persistent INTEGER NOT NULL DEFAULT 0,
  novnc_port INTEGER,
  vnc_port INTEGER,
  mcphub_port INTEGER,
  filesvc_port INTEGER,
  vnc_password TEXT,
  filesvc_token TEXT,
  cpus REAL,
  memory_mb INTEGER,
  disk_gb INTEGER,
  storage TEXT NOT NULL DEFAULT 'image',
  disk_used_bytes INTEGER,
  rootfs_used_bytes INTEGER,
  usage_updated_at INTEGER,
  last_activity_at INTEGER,
  last_started_at INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sandboxes_client ON sandboxes(api_key_id);
CREATE TABLE IF NOT EXISTS admin_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

API_KEY_V2_COLUMNS = {
    "default_cpus": "REAL",
    "default_memory_mb": "INTEGER",
    "default_disk_gb": "INTEGER",
    "quota_cpus": "REAL",
    "quota_memory_mb": "INTEGER",
    "quota_disk_gb": "INTEGER",
    "quota_sandboxes": "INTEGER",
    "updated_at": "INTEGER",
}

# v3: sessions of an admin are invalidated by bumping session_epoch
ADMIN_V3_COLUMNS = {
    "session_epoch": "INTEGER NOT NULL DEFAULT 0",
}

# v4: limit of sandboxes running at the same time; persistent sandboxes only
# for clients that are allowed to have them. New clients start without that
# permission (column default 0); clients that existed before the upgrade keep
# the previous behaviour (set to 1 once, when the column is added).
API_KEY_V4_COLUMNS = {
    "quota_running": ("INTEGER", None),
    "allow_persistent": ("INTEGER NOT NULL DEFAULT 0", "UPDATE api_keys SET allow_persistent=1"),
}

# v5: sandbox profiles (extra software on top of the shipped image) and
# internet access per client. Clients keep internet access (previous
# behaviour) and the standard image (profile_id NULL).
V5_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL COLLATE NOCASE,
  description TEXT NOT NULL DEFAULT '',
  apt_packages TEXT NOT NULL DEFAULT '',
  pip_packages TEXT NOT NULL DEFAULT '',
  npm_packages TEXT NOT NULL DEFAULT '',
  setup_script TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  image TEXT,
  image_id TEXT,
  image_config_hash TEXT,
  image_base_id TEXT,
  image_size_bytes INTEGER,
  built_at INTEGER,
  build_seq INTEGER NOT NULL DEFAULT 0,
  build_state TEXT NOT NULL DEFAULT 'none',
  build_started_at INTEGER,
  build_finished_at INTEGER,
  build_heartbeat_at INTEGER,
  build_error TEXT,
  build_actor TEXT
);
"""

API_KEY_V5_COLUMNS = {
    "profile_id": "INTEGER REFERENCES profiles(id)",
    "allow_internet": "INTEGER NOT NULL DEFAULT 1",
}


def connect() -> sqlite3.Connection:
    path = settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    """Create/upgrade the schema. Safe to call on every start."""
    with session() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(BASE_SCHEMA)
        conn.executescript(V2_SCHEMA)
        conn.executescript(V5_SCHEMA)
        existing = _columns(conn, "api_keys")
        for column, ctype in API_KEY_V2_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ctype}")
        for column, (ctype, backfill) in API_KEY_V4_COLUMNS.items():
            if column not in existing:
                with transaction(conn):
                    conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ctype}")
                    if backfill:
                        conn.execute(backfill)
        for column, ctype in API_KEY_V5_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ctype}")
        existing = _columns(conn, "admin_users")
        for column, ctype in ADMIN_V3_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE admin_users ADD COLUMN {column} {ctype}")
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def audit(conn: sqlite3.Connection, actor: str, action: str, target: Optional[str] = None,
          details: Optional[Dict[str, Any]] = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, target, details) VALUES(?,?,?,?,?)",
        (int(time.time()), actor, action, target, json.dumps(details) if details else None),
    )
    # Keep the log bounded (roughly the last 20k entries).
    conn.execute(
        "DELETE FROM audit_log WHERE id < (SELECT COALESCE(MAX(id), 0) - 20000 FROM audit_log)"
    )
