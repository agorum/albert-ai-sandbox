#!/usr/bin/env python3
"""Simple smoke test for container manager service.
Requires the service to be running locally.

Steps:
 1. Create API key via CLI (user must copy key and supply manually OR pass via env SMOKE_KEY)
 2. Create container
 3. List containers
 4. Get status
 5. Restart
 6. Stop
 7. Delete

This script focuses on exercising endpoints; adjust for your environment.
"""
import os
import sys
import json
import time
import argparse
import subprocess
from urllib import request as urlrequest
from urllib.error import HTTPError

SERVICE_URL = os.environ.get("MANAGER_URL", "http://localhost:5001")


def http(method, path, token=None, data=None):
    url = SERVICE_URL + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urlrequest.Request(url, data=body, method=method, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="Plaintext API key (if omitted, tries SMOKE_KEY env)")
    ap.add_argument("--image", default="hello-world", help="Image to run (default hello-world)")
    args = ap.parse_args()
    key = args.key or os.environ.get("SMOKE_KEY")
    if not key:
        print("Provide --key or set SMOKE_KEY env with a valid API key", file=sys.stderr)
        sys.exit(2)

    # Create container
    print("Creating container...")
    code, data = http("POST", "/containers", key, {"image": args.image, "autoStart": True})
    print(code, data)
    if code != 201:
        print("Create failed")
        return 1
    cid = data['container']['id']

    print("Listing containers...")
    code, data = http("GET", "/containers", key)
    print(code, data)

    print("Status...")
    code, data = http("GET", f"/containers/{cid}", key)
    print(code, data)

    print("Restart...")
    code, data = http("POST", f"/containers/{cid}/restart", key)
    print(code, data)

    print("Stop...")
    code, data = http("POST", f"/containers/{cid}/stop", key)
    print(code, data)

    print("Delete...")
    code, data = http("DELETE", f"/containers/{cid}", key)
    print(code, data)

    print("Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
