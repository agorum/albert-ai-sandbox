#!/usr/bin/env python3
"""API Key management CLI for the container manager service.

Usage examples:
  python scripts/api_key_manager.py create --label "Team A"
  python scripts/api_key_manager.py list
  python scripts/api_key_manager.py revoke --key <PLAINTEXT_KEY>

The plaintext key is ONLY shown at creation time. Store it securely.
Keys are stored hashed (SHA256) in the SQLite DB used by the service.

Environment variables:
  MANAGER_DB_PATH (default ./data/manager.db)
  MANAGER_DATA_DIR (default ./data/containers)
"""
import os
import sys
import argparse
import sqlite3
import hashlib
import time
import secrets
import shutil
from pathlib import Path

DB_PATH = os.environ.get("MANAGER_DB_PATH", "./data/manager.db")
DATA_DIR = os.environ.get("MANAGER_DATA_DIR", "./data/containers")

SCHEMA = """
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

def hash_key(k: str) -> str:
    import hashlib
    return hashlib.sha256(k.encode('utf-8')).hexdigest()

def get_db():
    Path(os.path.dirname(DB_PATH)).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

init_db()


def cmd_create(args):
    label = args.label
    # Generate secure random key
    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_key(raw_key)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO api_keys(key_hash, label, created_at) VALUES(?,?,?)",
            (key_hash, label, int(time.time()))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        print("Failed to insert key (hash collision?)", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()
    print("API Key created:")
    print(f"  label: {label}")
    print(f"  key:   {raw_key}")
    print("Store this key now; it will not be shown again.")


def cmd_list(_args):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, key_hash, label, created_at FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("No API keys.")
        return
    for r in rows:
        print(f"id={r['id']} label={r['label'] or ''} created={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['created_at']))} key_hash_prefix={r['key_hash'][:12]}")


def _docker_client():
    import docker
    return docker.from_env()


def cmd_revoke(args):
    raw_key = args.key
    key_hash = hash_key(raw_key)
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone()
        if not row:
            print("API key not found", file=sys.stderr)
            sys.exit(2)
        api_key_id = row['id']
        # Fetch containers to remove
        containers = conn.execute(
            "SELECT container_id, name FROM containers WHERE api_key_id=?",
            (api_key_id,)
        ).fetchall()
    finally:
        conn.close()

    # Remove containers via Docker
    if containers:
        print(f"Stopping/removing {len(containers)} containers...")
        dclient = _docker_client()
        for c in containers:
            cid = c['container_id']
            try:
                cont = dclient.containers.get(cid)
                try:
                    cont.stop(timeout=10)
                except Exception:
                    pass
                try:
                    cont.remove(v=True, force=True)
                    print(f" removed {cid[:12]}")
                except Exception as e:
                    print(f" failed removing {cid[:12]}: {e}")
            except Exception:
                print(f" container {cid[:12]} missing; skipping")

    # Delete DB entries (api_key cascade removes containers row)
    conn = get_db()
    try:
        conn.execute("DELETE FROM api_keys WHERE key_hash=?", (key_hash,))
        conn.commit()
    finally:
        conn.close()

    # Delete data directory
    ddir = os.path.join(DATA_DIR, key_hash[:12])
    if os.path.isdir(ddir):
        try:
            shutil.rmtree(ddir)
            print("Deleted data directory")
        except Exception as e:
            print(f"Failed to delete data directory: {e}")

    print("API key revoked.")


def build_parser():
    p = argparse.ArgumentParser(description="Manage API keys for container service")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Create a new API key")
    c.add_argument("--label", help="Label/description", default="")
    c.set_defaults(func=cmd_create)

    l = sub.add_parser("list", help="List API keys")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("revoke", help="Revoke an API key (and remove owned containers)")
    r.add_argument("--key", required=True, help="Plaintext API key to revoke")
    r.set_defaults(func=cmd_revoke)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)

if __name__ == "__main__":
    main()
