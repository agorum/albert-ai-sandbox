"""How long sandboxes live: stop when idle, remove non-persistent ones.

Three values, global (admin UI *Einstellungen*, kv ``lifecycle``; until saved
there ``ALBERT_INACTIVITY_SECONDS`` / ``ALBERT_MAX_AGE_SECONDS`` /
``ALBERT_MAX_LIFETIME_SECONDS``) and per client (columns, NULL = global):

* **inactivity** (10 min): a running sandbox without access and without an
  open connection (desktop, MCP, files) for this long is stopped. Applies to
  all sandboxes; data is kept.
* **max age** (4 h): a non-persistent sandbox older than this is removed as
  soon as it is not in use (no activity within the inactivity time).
* **max lifetime** (24 h): a non-persistent sandbox is removed at the latest
  this long after creation, even while in use (e.g. a forgotten desktop).

Persistent sandboxes are never removed or stopped because of their age; only
the inactivity stop applies to them. ``0`` switches a value off.

When a deadline gets shorter, existing sandboxes get the full new period from
the time of the change: the clock of a sandbox starts at
``max(created_at, changed_at)``, ``changed_at`` being the latest shortening
(global kv ``lifecycle_changed_at`` or the client's ``lifecycle_changed_at``).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Mapping, Optional

from . import db
from .config import settings
from .errors import BadRequest

KEY = "lifecycle"
CHANGED_KEY = "lifecycle_changed_at"
# (API name, client column, settings attribute)
FIELDS = (
    ("inactivitySeconds", "inactivity_seconds", "inactivity_seconds"),
    ("maxAgeSeconds", "max_age_seconds", "max_age_seconds"),
    ("maxLifetimeSeconds", "max_lifetime_seconds", "max_lifetime_seconds"),
)
COLUMNS = tuple(column for _, column, _ in FIELDS)
MIN_SECONDS = 60
MAX_SECONDS = 365 * 86400
IN_USE_FALLBACK = 600  # "in use" window while the inactivity stop is off
WARN_SECONDS = 900     # desktop warning before the max lifetime ends a sandbox


def clean_seconds(name: str, value: Any) -> Optional[int]:
    """None for empty, else whole seconds: 0 (off) or MIN_SECONDS..MAX_SECONDS."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequest(f"{name} must be a number of seconds", code="invalid_lifecycle")
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number of seconds", code="invalid_lifecycle")
    if seconds < 0:
        raise BadRequest(f"{name} must not be negative", code="invalid_lifecycle")
    if 0 < seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        raise BadRequest(f"{name} must be 0 (off) or between {MIN_SECONDS} seconds and 365 days",
                         code="invalid_lifecycle")
    return seconds


def validate(values: Mapping[str, Any]) -> None:
    """The max lifetime cannot be shorter than the max age (both on)."""
    age, lifetime = int(values.get("maxAgeSeconds") or 0), int(values.get("maxLifetimeSeconds") or 0)
    if age and lifetime and lifetime < age:
        raise BadRequest("the maximum lifetime must be at least as long as the time after which unused "
                         "non-persistent sandboxes are removed (or 0 = off)", code="invalid_lifecycle")


def _shorter(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """Did a removal deadline get shorter (or switched on)?"""
    for key in ("maxAgeSeconds", "maxLifetimeSeconds"):
        before, after = int(old.get(key) or 0), int(new.get(key) or 0)
        if after and (not before or after < before):
            return True
    return False


def config_values() -> Dict[str, int]:
    s = settings()
    return {api: int(getattr(s, attr)) for api, _, attr in FIELDS}


def global_values(conn: sqlite3.Connection) -> Dict[str, Any]:
    stored = db.kv_get(conn, KEY)
    if isinstance(stored, dict) and all(isinstance(stored.get(api), int) for api, _, _ in FIELDS):
        values: Dict[str, Any] = {api: stored[api] for api, _, _ in FIELDS}
        values["source"] = "settings"
    else:
        values = {**config_values(), "source": "config"}
    values["changedAt"] = int(db.kv_get(conn, CHANGED_KEY, 0) or 0)
    return values


def set_global(conn: sqlite3.Connection, fields: Mapping[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """Save the global values (missing ones keep their current value)."""
    now = int(now or time.time())
    current = global_values(conn)
    new = {api: current[api] for api, _, _ in FIELDS}
    for api, _, _ in FIELDS:
        if api in fields:
            value = clean_seconds(api, fields[api])
            new[api] = 0 if value is None else value
    validate(new)
    if _shorter(current, new):
        db.kv_set(conn, CHANGED_KEY, now)
    db.kv_set(conn, KEY, new)
    return global_values(conn)


def reset_global(conn: sqlite3.Connection, now: Optional[int] = None) -> Dict[str, Any]:
    """Back to the values of albert.env."""
    current = global_values(conn)
    if _shorter(current, config_values()):
        db.kv_set(conn, CHANGED_KEY, int(now or time.time()))
    conn.execute("DELETE FROM kv WHERE key=?", (KEY,))
    return global_values(conn)


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def overrides(client: Any) -> Dict[str, Optional[int]]:
    """Own values of a client (None = global value)."""
    return {api: _get(client, column) for api, column, _ in FIELDS}


def effective(client: Any, glob: Mapping[str, Any]) -> Dict[str, Any]:
    """Values that apply to a client's sandboxes, with their source."""
    own = overrides(client) if client is not None else {api: None for api, _, _ in FIELDS}
    result: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for api, _, _ in FIELDS:
        if own[api] is not None:
            result[api], sources[api] = int(own[api]), "client"
        else:
            result[api], sources[api] = int(glob[api]), "global"
    result["sources"] = sources
    result["changedAt"] = max(int(glob.get("changedAt") or 0), int(_get(client, "lifecycle_changed_at") or 0)
                              if client is not None else 0)
    return result


def client_change(before: Any, after_values: Mapping[str, Any], glob: Mapping[str, Any]) -> bool:
    """Validate the client's new values; True if a removal deadline got shorter."""
    new_eff = effective(after_values, glob)
    validate(new_eff)
    return _shorter(effective(before, glob), new_eff) if before is not None else False


def deadlines(row: Any, eff: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    """Removal deadlines of a non-persistent sandbox (unix time; None = none).

    ``idle``: removed from then on as soon as it is not in use; ``latest``:
    removed then even while in use. Persistent sandboxes have none."""
    if _get(row, "persistent"):
        return {"idle": None, "latest": None}
    start = max(int(_get(row, "created_at") or 0), int(eff.get("changedAt") or 0))
    age, lifetime = int(eff.get("maxAgeSeconds") or 0), int(eff.get("maxLifetimeSeconds") or 0)
    return {"idle": start + age if age else None, "latest": start + lifetime if lifetime else None}


def in_use(row: Any, eff: Mapping[str, Any], running: bool, now: float) -> bool:
    """Running and used within the inactivity time (an open connection counts as use)."""
    if not running:
        return False
    window = int(eff.get("inactivitySeconds") or 0) or IN_USE_FALLBACK
    last = max(int(_get(row, "last_activity_at") or 0), int(_get(row, "last_started_at") or 0),
               int(_get(row, "created_at") or 0))
    return now - last < window


def client_view(client: Any, glob: Mapping[str, Any]) -> Dict[str, Any]:
    """Admin view of a client: own values (None = global) and the values that apply."""
    eff = effective(client, glob)
    return {"own": overrides(client), "effective": {api: eff[api] for api, _, _ in FIELDS},
            "sources": eff["sources"]}


def api_view(eff: Mapping[str, Any]) -> Dict[str, int]:
    """What a client (ALBERT) learns about the rules (seconds, 0 = off)."""
    return {"stopAfterIdleSeconds": int(eff["inactivitySeconds"]),
            "deleteNonPersistentAfterSeconds": int(eff["maxAgeSeconds"]),
            "maxLifetimeSeconds": int(eff["maxLifetimeSeconds"])}
