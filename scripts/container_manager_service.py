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
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
import docker
from docker.errors import DockerException, NotFound, APIError

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

# Lazy singleton docker client (avoid import-time failure if Docker not ready or
# requests-unixsocket not yet installed). We create it on first use with retries.
_docker_client = None  # type: Optional[docker.DockerClient]

def get_docker_client(retries: int = 5, delay: float = 1.5) -> docker.DockerClient:
    """Return a cached docker client, creating it lazily with retries.

    We intentionally create the client only when first needed so that:
      * Service can start even if Docker daemon is still warming up
      * Missing dependency issues (e.g. requests-unixsocket) can be fixed and
        service restarted without crashing import
    """
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            # docker.from_env() respects DOCKER_HOST / sockets automatically.
            c = docker.from_env()
            # Light connectivity check; will raise if daemon not reachable
            c.ping()
            _docker_client = c
            return c
        except Exception as ex:  # broad: want to retry for any failure
            last_exc = ex
            time.sleep(delay)
    # Exhausted retries
    assert last_exc is not None
    raise last_exc

def docker_unavailable_response(ex: Exception):
    return jsonify({"error": f"Docker unavailable: {ex.__class__.__name__}: {ex}"}), 503

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

def serialize_container(c, api_key_id: int) -> Dict[str, Any]:
    c.reload()
    state = c.attrs.get("State", {})
    return {
        "id": c.id,
        "name": c.name,
        "image": c.image.tags[0] if c.image.tags else c.image.short_id,
        "status": state.get("Status"),
        "running": state.get("Running"),
        "exitCode": state.get("ExitCode"),
        "startedAt": state.get("StartedAt"),
        "finishedAt": state.get("FinishedAt"),
        "apiKeyId": api_key_id,
        "labels": c.labels,
    }

LABEL_MANAGER = "albert.manager"
LABEL_APIKEY_HASH = "albert.apikey_hash"

# --- Error handlers --------------------------------------------------------

@app.errorhandler(404)
def not_found(e):  # pragma: no cover (simple wrapper)
    return jsonify({"error": "Not found"}), 404

# --- Health ----------------------------------------------------------------

@app.get("/health")
def health():
    # Provide docker readiness info (non-fatal if unreachable)
    docker_status = "down"
    try:
        get_docker_client(retries=1, delay=0)
        docker_status = "up"
    except Exception:
        pass
    return jsonify({"status": "ok", "docker": docker_status})

# --- Container operations --------------------------------------------------

@app.post("/containers")
def create_container():
    auth_info, err = require_api_key()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    image = body.get("image")
    if not image:
        return jsonify({"error": "Field 'image' is required"}), 400
    if not allowed_image(image):
        return jsonify({"error": "Image not allowed"}), 403
    name = body.get("name") or f"ct-{uuid.uuid4().hex[:8]}"
    env = body.get("env") or {}
    if not isinstance(env, dict):
        return jsonify({"error": "Field 'env' must be an object"}), 400
    cmd = body.get("cmd")
    auto_start = body.get("autoStart", True)

    # Ensure docker client
    try:
        client = get_docker_client()
    except Exception as ex:
        return docker_unavailable_response(ex)

    # Pull image lazily
    try:
        client.images.pull(image)
    except DockerException as ex:
        return jsonify({"error": f"Failed to pull image: {ex}"}), 400

    key_hash = auth_info["key_hash"]
    labels = {LABEL_MANAGER: "1", LABEL_APIKEY_HASH: key_hash}

    # Create container
    try:
        c = client.containers.create(
            image=image,
            name=name,
            command=cmd,
            environment=env,
            labels=labels,
            detach=True,
        )
    except APIError as ex:
        return jsonify({"error": f"Docker API error: {ex.explanation}"}), 400
    except DockerException as ex:
        return jsonify({"error": f"Failed to create container: {ex}"}), 500

    # Persist mapping
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO containers(api_key_id, container_id, name, image, created_at) VALUES(?,?,?,?,?)",
            (auth_info["id"], c.id, name, image, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    # Data directory
    container_data_dir(key_hash, name)

    if auto_start:
        try:
            c.start()
        except DockerException as ex:
            return jsonify({"error": f"Container created but failed to start: {ex}", "id": c.id}), 500

    return jsonify({"container": serialize_container(c, auth_info["id"])}) , 201


def _get_owned_container(api_key_hash: str, api_key_id: int, cid: str):
    # Accept either DB numeric id or docker id/name
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT container_id, name FROM containers WHERE api_key_id=? AND (container_id=? OR name=?)",
            (api_key_id, cid, cid),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None, (jsonify({"error": "Container not found"}), 404)
    docker_id = row["container_id"]
    try:
        client = get_docker_client()
    except Exception as ex:
        return None, docker_unavailable_response(ex)
    try:
        c = client.containers.get(docker_id)
    except NotFound:
        return None, (jsonify({"error": "Container missing (stale entry)"}), 404)
    # Verify label ownership
    if c.labels.get(LABEL_APIKEY_HASH) != api_key_hash:
        return None, (jsonify({"error": "Ownership mismatch"}), 403)
    return c, None

@app.get("/containers")
def list_containers():
    auth_info, err = require_api_key()
    if err:
        return err
    try:
        client = get_docker_client()
    except Exception as ex:
        return docker_unavailable_response(ex)

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT container_id FROM containers WHERE api_key_id=?",
            (auth_info["id"],)
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        try:
            c = client.containers.get(r["container_id"])  # may raise
            result.append(serialize_container(c, auth_info["id"]))
        except NotFound:
            # stale entry; skip (could also clean DB lazily)
            pass
    return jsonify({"containers": result})

@app.get("/containers/<cid>")
def get_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    c, err = _get_owned_container(auth_info["key_hash"], auth_info["id"], cid)
    if err:
        return err
    return jsonify({"container": serialize_container(c, auth_info["id"])})

@app.post("/containers/<cid>/start")
def start_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    c, err = _get_owned_container(auth_info["key_hash"], auth_info["id"], cid)
    if err:
        return err
    try:
        c.start()
    except DockerException as ex:
        return jsonify({"error": f"Failed to start: {ex}"}), 500
    return jsonify({"container": serialize_container(c, auth_info["id"])})

@app.post("/containers/<cid>/stop")
def stop_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    c, err = _get_owned_container(auth_info["key_hash"], auth_info["id"], cid)
    if err:
        return err
    timeout = int(request.args.get("timeout", "10"))
    try:
        c.stop(timeout=timeout)
    except DockerException as ex:
        return jsonify({"error": f"Failed to stop: {ex}"}), 500
    return jsonify({"container": serialize_container(c, auth_info["id"])})

@app.post("/containers/<cid>/restart")
def restart_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    c, err = _get_owned_container(auth_info["key_hash"], auth_info["id"], cid)
    if err:
        return err
    try:
        c.restart()
    except DockerException as ex:
        return jsonify({"error": f"Failed to restart: {ex}"}), 500
    return jsonify({"container": serialize_container(c, auth_info["id"])})

@app.delete("/containers/<cid>")
def delete_container(cid: str):
    auth_info, err = require_api_key()
    if err:
        return err
    c, err = _get_owned_container(auth_info["key_hash"], auth_info["id"], cid)
    if err:
        return err
    name = c.name
    try:
        if c.status == "running":
            c.stop(timeout=10)
        c.remove(v=True, force=True)
    except DockerException as ex:
        return jsonify({"error": f"Failed to remove: {ex}"}), 500

    # DB cleanup
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM containers WHERE api_key_id=? AND (container_id=? OR name=?)",
            (auth_info["id"], c.id, cid),
        )
        conn.commit()
    finally:
        conn.close()

    # Data dir cleanup
    ddir = os.path.join(DATA_DIR, auth_info["key_hash"][:12], name)
    if os.path.isdir(ddir):
        try:
            import shutil
            shutil.rmtree(ddir)
        except Exception:
            pass

    return jsonify({"deleted": c.id})

# --- Main ------------------------------------------------------------------

def main():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
