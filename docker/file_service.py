#!/usr/bin/env python3

import os
import uuid
import mimetypes
from pathlib import Path
from flask import Flask, request, jsonify, send_file

UPLOAD_DIR = "/tmp/albert-files"
DEFAULT_PORT = int(os.environ.get("FILE_SERVICE_PORT", "4000"))

app = Flask(__name__)


def ensure_upload_dir():
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        # Make it world-writable as container may run different users
        os.chmod(UPLOAD_DIR, 0o777)
    except Exception:
        # Ignore chmod failures on some FS
        pass


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/upload")
def upload_file():
    ensure_upload_dir()

    if "file" not in request.files:
        return jsonify({"error": "No file part 'file' in form-data"}), 400

    file = request.files["file"]
    if file.filename is None or file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    ext = Path(file.filename).suffix  # includes leading dot or empty string
    new_name = f"{uuid.uuid4()}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, new_name)

    try:
        file.save(dest_path)
        # relax permissions; directory already 777
        try:
            os.chmod(dest_path, 0o666)
        except Exception:
            pass
        return jsonify({"path": dest_path}), 201
    except Exception as e:
        return jsonify({"error": f"Failed to save file: {e}"}), 500


@app.get("/download")
def download_file():
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "Missing query parameter 'path' with full file path"}), 400

    # Expect absolute path per requirement
    if not os.path.isabs(path):
        return jsonify({"error": "Provided path must be an absolute path"}), 400

    if not os.path.exists(path) or not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    # Try to guess a content-type
    mime, _ = mimetypes.guess_type(path)
    try:
        return send_file(path, mimetype=mime or "application/octet-stream", as_attachment=False, conditional=True)
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {e}"}), 500


def main():
    ensure_upload_dir()
    port = int(os.environ.get("FILE_SERVICE_PORT", DEFAULT_PORT))
    # Bind to all interfaces
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
