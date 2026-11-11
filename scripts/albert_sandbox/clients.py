"""Clients (tenants), identified by an API key, with a per-sandbox limit and
overall quotas.

Per-sandbox limit (``default_*`` columns): CPU, RAM and disk every sandbox of
the client gets. Unset values fall back to the global defaults
(ALBERT_DEFAULT_CPUS / _MEMORY_MB / _DISK_GB; 1 core, 2 GB RAM, 10 GB disk).
Through the client API a sandbox may request *less*, never more; admins can
raise the limits of a single sandbox afterwards.

Quota semantics:
  * CPU and RAM quotas count the limits of sandboxes that are *running* (or
    being created) - "used at the same time".
  * The disk quota counts the configured disk size of *all* sandboxes of the
    client, running or stopped, because stopped sandboxes keep their data.
  * The sandbox quota counts all sandboxes of the client.
  * ``None`` means unlimited. Legacy sandboxes without limits count as 0.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from .config import settings
from .errors import BadRequest, NotFound, QuotaExceeded

QUOTA_FIELDS = ("quota_cpus", "quota_memory_mb", "quota_disk_gb", "quota_sandboxes")
DEFAULT_FIELDS = ("default_cpus", "default_memory_mb", "default_disk_gb")
EDITABLE_FIELDS = ("label",) + QUOTA_FIELDS + DEFAULT_FIELDS


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return secrets.token_urlsafe(32)


def get(conn: sqlite3.Connection, client_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM api_keys WHERE id=?", (client_id,)).fetchone()
    if not row:
        raise NotFound(f"client {client_id} not found")
    return row


def find_by_key(conn: sqlite3.Connection, plaintext_or_hash: str) -> Optional[sqlite3.Row]:
    candidate = plaintext_or_hash.strip()
    if len(candidate) == 64 and all(c in "0123456789abcdefABCDEF" for c in candidate):
        key_hash = candidate.lower()
        row = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM api_keys WHERE key_hash=?", (hash_key(candidate),)).fetchone()


def list_all(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM api_keys ORDER BY created_at, id").fetchall()


def _clean_number(field: str, value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{field} must be a number")
    if number < 0:
        raise BadRequest(f"{field} must not be negative")
    if field.endswith("_cpus"):
        return round(number, 2)
    return int(number)


def validate_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            continue
        if key == "label":
            if value is not None and not isinstance(value, str):
                raise BadRequest("label must be a string")
            label = (value or "").strip()
            if len(label) > 200:
                raise BadRequest("label too long (max 200 characters)")
            clean[key] = label
        else:
            clean[key] = _clean_number(key, value)
    for key in DEFAULT_FIELDS:
        if clean.get(key) == 0:
            raise BadRequest(f"{key} must be greater than 0 (leave empty for the global default)")
    # The per-sandbox limit must be something a sandbox can actually get.
    from .sandboxes import validate_resources  # local import: sandboxes imports this module
    validate_resources(clean.get("default_cpus"), clean.get("default_memory_mb"), clean.get("default_disk_gb"))
    return clean


def create(conn: sqlite3.Connection, label: str = "", **fields: Any) -> Tuple[sqlite3.Row, str]:
    clean = validate_fields({"label": label, **fields})
    raw = generate_key()
    now = int(time.time())
    columns = ["key_hash", "created_at", "updated_at"] + list(clean.keys())
    values = [hash_key(raw), now, now] + list(clean.values())
    placeholders = ",".join("?" for _ in columns)
    cur = conn.execute(f"INSERT INTO api_keys({','.join(columns)}) VALUES({placeholders})", values)
    return get(conn, cur.lastrowid), raw


def update(conn: sqlite3.Connection, client_id: int, fields: Dict[str, Any]) -> sqlite3.Row:
    get(conn, client_id)
    clean = validate_fields(fields)
    if clean:
        assignments = ", ".join(f"{k}=?" for k in clean)
        conn.execute(f"UPDATE api_keys SET {assignments}, updated_at=? WHERE id=?",
                     [*clean.values(), int(time.time()), client_id])
    return get(conn, client_id)


def rotate_key(conn: sqlite3.Connection, client_id: int) -> str:
    get(conn, client_id)
    raw = generate_key()
    conn.execute("UPDATE api_keys SET key_hash=?, updated_at=? WHERE id=?",
                 (hash_key(raw), int(time.time()), client_id))
    return raw


def delete_record(conn: sqlite3.Connection, client_id: int) -> None:
    remaining = conn.execute("SELECT COUNT(*) FROM sandboxes WHERE api_key_id=?", (client_id,)).fetchone()[0]
    if remaining:
        raise BadRequest(f"client still owns {remaining} sandbox(es)")
    conn.execute("DELETE FROM containers WHERE api_key_id=?", (client_id,))
    conn.execute("DELETE FROM api_keys WHERE id=?", (client_id,))


def sandbox_defaults(client: sqlite3.Row) -> Dict[str, Any]:
    s = settings()
    return {
        "cpus": client["default_cpus"] or s.default_cpus,
        "memory_mb": client["default_memory_mb"] or s.default_memory_mb,
        "disk_gb": client["default_disk_gb"] or s.default_disk_gb,
    }


def quotas(client: sqlite3.Row) -> Dict[str, Any]:
    return {
        "cpus": client["quota_cpus"],
        "memoryMb": client["quota_memory_mb"],
        "diskGb": client["quota_disk_gb"],
        "sandboxes": client["quota_sandboxes"],
    }


def usage(conn: sqlite3.Connection, client_id: int, running: Iterable[str],
          exclude: Optional[str] = None) -> Dict[str, Any]:
    """Current consumption of a client against its quotas."""
    running_set = set(running)
    totals = {"cpus": 0.0, "memoryMb": 0, "diskGb": 0, "sandboxes": 0, "running": 0}
    for row in conn.execute(
        "SELECT name, state, cpus, memory_mb, disk_gb FROM sandboxes WHERE api_key_id=?", (client_id,)
    ):
        if exclude and row["name"] == exclude:
            continue
        totals["sandboxes"] += 1
        totals["diskGb"] += row["disk_gb"] or 0
        active = row["name"] in running_set or row["state"] == "creating"
        if active:
            totals["running"] += 1
            totals["cpus"] += row["cpus"] or 0
            totals["memoryMb"] += row["memory_mb"] or 0
    totals["cpus"] = round(totals["cpus"], 2)
    return totals


def check_quota(conn: sqlite3.Connection, client: sqlite3.Row, running: Iterable[str], *,
                add_cpus: float = 0, add_memory_mb: int = 0, add_disk_gb: int = 0,
                add_sandboxes: int = 0, exclude: Optional[str] = None) -> None:
    current = usage(conn, client["id"], running, exclude=exclude)
    limits = quotas(client)
    checks = [
        ("cpus", current["cpus"] + add_cpus, limits["cpus"], add_cpus, "CPU cores (running sandboxes)"),
        ("memoryMb", current["memoryMb"] + add_memory_mb, limits["memoryMb"], add_memory_mb,
         "RAM in MB (running sandboxes)"),
        ("diskGb", current["diskGb"] + add_disk_gb, limits["diskGb"], add_disk_gb, "disk in GB (all sandboxes)"),
        ("sandboxes", current["sandboxes"] + add_sandboxes, limits["sandboxes"], add_sandboxes, "sandboxes"),
    ]
    for key, wanted, limit, added, label in checks:
        if limit is None or not added:
            continue
        if wanted > limit + 1e-9:
            raise QuotaExceeded(
                f"Quota exceeded for {label}: {wanted:g} requested in total, limit is {limit:g}",
                details={"resource": key, "limit": limit, "used": current[key], "requested": added},
            )
