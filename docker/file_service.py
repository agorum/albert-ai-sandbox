#!/usr/bin/env python3

import hmac
import mimetypes
import os
import uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_file

UPLOAD_DIR = "/tmp/albert-files"
DEFAULT_PORT = int(os.environ.get("FILE_SERVICE_PORT", "4000"))
# Bind 0.0.0.0 inside the container so Docker's port-publishing can reach
# the service. Host-side isolation is handled one layer up by
# `docker run -p 127.0.0.1:<host>:4000` in albert-ai-sandbox-manager.sh,
# which limits the exposed port to the host loopback. The bearer token
# below is the real authentication layer.
BIND_HOST = os.environ.get("FILE_SERVICE_BIND_HOST", "0.0.0.0")
FILE_SERVICE_TOKEN = os.environ.get("FILE_SERVICE_TOKEN", "").strip()

app = Flask(__name__)


def ensure_upload_dir():
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        os.chmod(UPLOAD_DIR, 0o755)
    except Exception:
        pass


def _extract_bearer():
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(None, 1)[1].strip()


def require_token():
    # Auth is optional: if no FILE_SERVICE_TOKEN is configured, the service
    # runs without authentication (legacy behaviour). When a token is set
    # via the env variable it is enforced on every protected endpoint.
    if not FILE_SERVICE_TOKEN:
        return None
    provided = _extract_bearer()
    if not provided or not hmac.compare_digest(provided, FILE_SERVICE_TOKEN):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401
    return None


def _resolve_safe_path(path: str):
    """Resolve a path and ensure it stays inside UPLOAD_DIR. Returns real_path or None."""
    real_upload = os.path.realpath(UPLOAD_DIR)
    real_path = os.path.realpath(path)
    if real_path != real_upload and not real_path.startswith(real_upload + os.sep):
        return None
    return real_path


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/upload")
def upload_file():
    auth_err = require_token()
    if auth_err:
        return auth_err

    ensure_upload_dir()

    if "file" not in request.files:
        return jsonify({"error": "No file part 'file' in form-data"}), 400

    file = request.files["file"]
    if file.filename is None or file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    ext = Path(file.filename).suffix
    new_name = f"{uuid.uuid4()}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, new_name)

    # Defense-in-depth: ensure dest cannot escape UPLOAD_DIR even if uuid/ext were abused.
    if _resolve_safe_path(dest_path) is None:
        return jsonify({"error": "Invalid destination path"}), 400

    try:
        file.save(dest_path)
        try:
            os.chmod(dest_path, 0o644)
        except Exception:
            pass
        return jsonify({"path": dest_path}), 201
    except Exception as e:
        return jsonify({"error": f"Failed to save file: {e}"}), 500


@app.get("/download")
def download_file():
    auth_err = require_token()
    if auth_err:
        return auth_err

    path = request.args.get("path")
    if not path:
        return jsonify({"error": "Missing query parameter 'path' with full file path"}), 400

    if not os.path.isabs(path):
        return jsonify({"error": "Provided path must be an absolute path"}), 400

    real_path = _resolve_safe_path(path)
    if real_path is None:
        return jsonify({"error": "Access denied: path outside upload directory"}), 403

    if not os.path.exists(real_path) or not os.path.isfile(real_path):
        return jsonify({"error": "File not found"}), 404

    mime, _ = mimetypes.guess_type(real_path)
    try:
        return send_file(
            real_path,
            mimetype=mime or "application/octet-stream",
            as_attachment=False,
            conditional=True,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {e}"}), 500


def main():
    ensure_upload_dir()
    port = int(os.environ.get("FILE_SERVICE_PORT", DEFAULT_PORT))
    app.run(host=BIND_HOST, port=port)


if __name__ == "__main__":
    main()
