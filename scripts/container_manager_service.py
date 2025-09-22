#!/usr/bin/env python3
"""Container Manager Service

Runs on the host (not inside a managed container) and exposes a REST API to
manage per-API-key isolated Docker containers.

Auth: Bearer token with API key. Keys stored hashed (SHA256) in SQLite.

Endpoints (all require Authorization unless stated):
  GET  /health
  POST /containers
  GET  /containers
  GET  /containers/<id>
  POST /containers/<id>/start
  POST /containers/<id>/stop
  POST /containers/<id>/restart
  DELETE /containers/<id>

Request JSON for create (POST /containers):
  {
    "image": "python:3.11-slim",
    "name": "optional-name",   # optional; if omitted auto-generate
    "env": {"FOO": "bar"},    # optional
    "cmd": ["python", "app.py"], # optional command override
    "autoStart": true            # default true
  }

Environment Variables:
  MANAGER_PORT          (default 5001)
  MANAGER_DB_PATH       (default ./data/manager.db)
  MANAGER_DATA_DIR      (default ./data/containers)
  MANAGER_ALLOWED_IMAGES (optional comma separated allow-list)

Data layout:
  data/manager.db (SQLite)
  data/containers/<api_key_prefix>/<container_name_or_id>/

"""
import os
import sqlite3
import hashlib
import time
import json
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from flask import Flask, request, jsonify

THIS_FILE = Path(__file__).resolve()
BASE_DIR = THIS_FILE.parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "manager.db"
DEFAULT_DATA_DIR = BASE_DIR / "data" / "containers"

# Configuration
PORT = int(os.environ.get("MANAGER_PORT", "5001"))
DB_PATH = os.environ.get("MANAGER_DB_PATH", str(DEFAULT_DB_PATH))
DATA_DIR = os.environ.get("MANAGER_DATA_DIR", str(DEFAULT_DATA_DIR))
ALLOWED_IMAGES = [i.strip() for i in os.environ.get("MANAGER_ALLOWED_IMAGES", "").split(",") if i.strip()] or None

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(os.path.dirname(DB_PATH)).mkdir(parents=True, exist_ok=True)
# Migrate legacy scripts/data/manager.db if present
legacy_db = THIS_FILE.parent / "data" / "manager.db"
if not Path(DB_PATH).exists() and legacy_db.exists():
    try:
        os.replace(legacy_db, DB_PATH)
    except Exception:
        pass

app = Flask(__name__)


def docker_info_available() -> bool:
    """Best-effort check whether the Docker daemon is reachable."""
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def inspect_container(name: str) -> Optional[Dict[str, Any]]:
    """Return the raw docker inspect payload for a container name."""
    try:
        proc = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not payload:
        return None
    return payload[0]


def _normalize_finished_at(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    sentinel = "0001-01-01T00:00:00"
    if value.startswith(sentinel):
        return None
    return value


def serialize_container(name: str) -> Dict[str, Any]:
    """Return a normalized container description using docker CLI inspect."""
    info = inspect_container(name)
    if not info:
        return {
            "name": name,
        }
    config = info.get("Config") or {}
    state = info.get("State") or {}
    resolved_name = (info.get("Name") or "").lstrip("/") or name
    return {
        "id": info.get("Id"),
        "name": resolved_name,
        "image": config.get("Image"),
        "status": state.get("Status"),
        "running": state.get("Running"),
        "startedAt": state.get("StartedAt"),
        "finishedAt": _normalize_finished_at(state.get("FinishedAt")),
    }


def _extract_script_metadata(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result: Dict[str, Any] = {}
    created = payload.get("created")
    if created:
        result["createdAt"] = created
    ports = payload.get("ports")
    if isinstance(ports, dict):
        result["ports"] = ports
    urls = payload.get("urls")
    if isinstance(urls, dict):
        result["urls"] = urls
    status = payload.get("status")
    if status:
        result["status"] = status
    return result

# --- Database helpers ------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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

def init_db():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

init_db()

# --- Auth ------------------------------------------------------------------

def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def extract_bearer() -> Optional[str]:
    auth = request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth.split(None, 1)[1].strip()

def require_api_key():
    key = extract_bearer()
    if not key:
        return None, (jsonify({"error": "Missing or invalid Authorization header"}), 401)
    key_h = hash_key(key)
    conn = get_db()
    try:
        row = conn.execute("SELECT id, key_hash, label FROM api_keys WHERE key_hash=?", (key_h,)).fetchone()
        if not row:
            return None, (jsonify({"error": "Invalid API key"}), 401)
        return dict(row), None
    finally:
        conn.close()

# --- Utility ---------------------------------------------------------------

def allowed_image(image: str) -> bool:
    if ALLOWED_IMAGES is None:
        return True
    return image in ALLOWED_IMAGES

def container_data_dir(api_key_hash: str, name_or_id: str) -> str:
    prefix = api_key_hash[:12]
    path = os.path.join(DATA_DIR, prefix, name_or_id)
    os.makedirs(path, exist_ok=True)
    return path

LABEL_MANAGER = "albert.manager"
LABEL_APIKEY_HASH = "albert.apikey_hash"

# --- External sandbox manager script integration ---------------------------

SANDBOX_SCRIPT = "/opt/albert-ai-sandbox-manager/scripts/albert-ai-sandbox-manager.sh"

def _script_env(api_key_hash: str) -> dict:
    env = os.environ.copy()
    env["ALBERT_JSON"] = "1"  # request JSON output mode
    env["ALBERT_OWNER_KEY_HASH"] = api_key_hash  # for filtering / labeling
    env["ALBERT_NONINTERACTIVE"] = "1"  # avoid interactive prompts (remove)
    env["ALBERT_STATUS_SKIP_STATS"] = "1"  # speed up status endpoints for REST use
    return env

def _run_script(args: List[str], api_key_hash: str, expect_json: bool = True) -> Tuple[int, str, Optional[Any]]:
    """Run the external sandbox manager script.

    Returns (exit_code, raw_stdout, parsed_json or None).
    """
    if not os.path.isfile(SANDBOX_SCRIPT):
        return 127, f"Sandbox script not found at {SANDBOX_SCRIPT}", None
    try:
        proc = subprocess.run([SANDBOX_SCRIPT] + args, capture_output=True, text=True, env=_script_env(api_key_hash), timeout=300)
    except subprocess.TimeoutExpired:
        return 124, "Script timeout", None
    out = proc.stdout.strip()
    if expect_json:
        # Try to locate JSON (strip color codes if any leaked)
        try:
            # Find first '{' or '[' substring
            start = min([p for p in [out.find('{'), out.find('[')] if p >= 0]) if ('{' in out or '[' in out) else 0
            jtxt = out[start:]
            data = json.loads(jtxt)
            return proc.returncode, out, data
        except Exception:
            return proc.returncode, out, None
    return proc.returncode, out, None

def _ensure_registry_mapping(api_key_info: dict, name: str):
    """Ensure the DB has an entry mapping this API key to the Docker container id (by name)."""
    info = inspect_container(name)
    if not info:
        return
    container_id = info.get("Id")
    if not container_id:
        return
    image_ref = (info.get("Config") or {}).get("Image")
    conn = get_db()
    try:
        # Upsert-like: ignore if exists
        existing = conn.execute("SELECT 1 FROM containers WHERE container_id=?", (container_id,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO containers(api_key_id, container_id, name, image, created_at) VALUES(?,?,?,?,?)",
                (api_key_info["id"], container_id, name, image_ref, int(time.time())),
            )
            conn.commit()
    finally:
        conn.close()

# --- Error handlers --------------------------------------------------------

@app.errorhandler(404)
def not_found(e):  # pragma: no cover (simple wrapper)
    return jsonify({"error": "Not found"}), 404

# --- Health & Create -------------------------------------------------------

@app.get("/health")
def health():
    docker_status = "up" if docker_info_available() else "down"
    return jsonify({"status": "ok", "docker": docker_status})

@app.post("/containers")
def create_container():
    auth_info, err = require_api_key()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    requested_name = body.get("name")
    # Delegate to external script (image/env not yet parameterized here)
    args = ["create"]
    if requested_name:
        args.append(requested_name)
    rc, raw, data = _run_script(args, auth_info["key_hash"], expect_json=True)
    if rc != 0 or not isinstance(data, dict):
        return jsonify({"error": "Sandbox create failed", "details": raw, "exitCode": rc}), 500
    sandbox_name = data.get("name") or requested_name
    if not sandbox_name:
        return jsonify({"error": "Sandbox create returned no name", "details": raw}), 500
    _ensure_registry_mapping(auth_info, sandbox_name)
    ser = serialize_container(sandbox_name)
    ser.update(_extract_script_metadata(data))
    ser["sandboxName"] = sandbox_name
    return jsonify({"container": ser}), 201


# (Legacy _get_owned_container helper removed after script delegation refactor)

@app.get("/containers")
def list_containers():
    auth_info, err = require_api_key()
    if err:
        return err
    rc, raw, data = _run_script(["list"], auth_info["key_hash"], expect_json=True)
    if rc != 0 or not isinstance(data, list):
        return jsonify({"error": "List failed", "details": raw, "exitCode": rc}), 500
    # Attach docker IDs where possible
    enriched = []
    for entry in data:
        name = entry.get("name")
        details = serialize_container(name) if name else {}
        details.update(_extract_script_metadata(entry))
        if name:
            details.setdefault("name", name)
            details["sandboxName"] = name
        enriched.append(details)
    return jsonify({"containers": enriched})

@app.get("/containers/<cid>")
def get_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    # Use script status (single) to reflect registry & ports
    rc, raw, data = _run_script(["status", cid], auth_info["key_hash"], expect_json=True)
    if rc != 0 or not isinstance(data, (dict, list)):
        return jsonify({"error": "Status failed", "details": raw, "exitCode": rc}), 404
    if isinstance(data, list):
        # script may return list when filtering; pick matching name
        match = next((d for d in data if d.get("name") == cid), None)
    else:
        match = data
    if not match:
        return jsonify({"error": "Container not found"}), 404
    if isinstance(match, dict) and match.get("error"):
        if match.get("error") == "not_found":
            return jsonify({"error": "Container not found"}), 404
        return jsonify({"error": "Status failed", "details": match}), 500
    # Enrich with docker info
    ser = serialize_container(cid)
    ser.update(_extract_script_metadata(match))
    ser["sandboxName"] = cid
    return jsonify({"container": ser})

@app.post("/containers/<cid>/start")
def start_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    rc, raw, data = _run_script(["start", cid], auth_info["key_hash"], expect_json=True)
    if rc != 0:
        return jsonify({"error": "Start failed", "details": raw, "exitCode": rc}), 500
    return get_container(cid)

@app.post("/containers/<cid>/stop")
def stop_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    rc, raw, data = _run_script(["stop", cid], auth_info["key_hash"], expect_json=False)
    if rc != 0:
        return jsonify({"error": "Stop failed", "details": raw, "exitCode": rc}), 500
    return get_container(cid)

@app.post("/containers/<cid>/restart")
def restart_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    rc, raw, data = _run_script(["restart", cid], auth_info["key_hash"], expect_json=False)
    if rc != 0:
        return jsonify({"error": "Restart failed", "details": raw, "exitCode": rc}), 500
    return get_container(cid)

@app.delete("/containers/<cid>")
def delete_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    rc, raw, _ = _run_script(["remove", cid], auth_info["key_hash"], expect_json=False)
    if rc != 0:
        return jsonify({"error": "Remove failed", "details": raw, "exitCode": rc}), 500
    # DB cleanup (best-effort)
    conn = get_db()
    try:
        conn.execute("DELETE FROM containers WHERE api_key_id=? AND name=?", (auth_info["id"], cid))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"deleted": cid})

# --- Main ------------------------------------------------------------------

def main():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
