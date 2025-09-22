# Albert Sandbox Manager Services

This document captures the host-side services that were verified after installing the sandbox manager with `bash install.sh`. The installation copies the runtime into `/opt/albert-ai-sandbox-manager`, enables the `albert-container-manager.service` systemd unit, and exposes the REST API locally on port `5001` by default.

## Service Configuration
- **Base URL**: `http://127.0.0.1:5001`
- **Environment overrides**: `MANAGER_PORT`, `MANAGER_DB_PATH`, `MANAGER_DATA_DIR`, `MANAGER_ALLOWED_IMAGES`
- **Authentication**: every endpoint except `/health` expects `Authorization: Bearer <PLAINTEXT_API_KEY>`

## Managing API Keys (`api-key-manager.sh`)
API keys gate access to every container operation. The install drops a helper at `/opt/albert-ai-sandbox-manager/scripts/api-key-manager.sh`, which wraps the Python CLI and pins the correct SQLite/database paths.

Create a key (the plaintext value is only printed once):
```bash
sudo /opt/albert-ai-sandbox-manager/scripts/api-key-manager.sh create --label "My team"
```
Sample output:
```text
API Key created:
  label: My team
  key:   55kHw2qOisk_aTJycVpJqcLWUA0kEgQZdeXcwCoHV2w
Store this key now; it will not be shown again.
```

List registered keys (prints label, timestamp, and hash prefix):
```bash
sudo /opt/albert-ai-sandbox-manager/scripts/api-key-manager.sh list
```

Revoke a key and clean up its sandboxes:
```bash
sudo /opt/albert-ai-sandbox-manager/scripts/api-key-manager.sh revoke --key 55kHw2qOisk_aTJycVpJqcLWUA0kEgQZdeXcwCoHV2w
```

All CLI commands honor `MANAGER_DB_PATH` / `MANAGER_DATA_DIR` if you need to target a non-default database.

## REST API Endpoints
All examples below were executed against the running service that was installed and verified during this session. Replace `<API_KEY>` with a plaintext key from the manager above.

### `GET /health`
Checks the Flask service and Docker connectivity (best-effort ping).

```bash
curl -s http://127.0.0.1:5001/health
```
Response observed:
```json
{"status":"ok","docker":"down"}
```
`docker` flips to `up` when the service can reach the Docker socket; if the Python Docker client cannot negotiate (for example when `requests-unixsocket` is unavailable), it reports `down` while the rest of the API continues to function.

### `GET /containers`
Returns all sandboxes owned by the calling API key. Each entry merges scheduler metadata and real-time Docker state (when available).

```bash
curl -s \
  -H "Authorization: Bearer <API_KEY>" \
  http://127.0.0.1:5001/containers
```
Sample response while three sandboxes were running:
```json
{
  "containers": [
    {
      "name": "sbx-2bce0e02",
      "sandboxName": "sbx-2bce0e02",
      "ownerHash": "c030c7356b35f09989ccec9e0b3b04c1f74f20eee9c5ba9b93a2e9b5e27e0455",
      "status": "running",
      "ports": {
        "vnc": "5900",
        "novnc": "6080",
        "mcphub": "7080",
        "filesvc": "7280"
      }
    },
    {
      "name": "sbx-a4b86fd1",
      "sandboxName": "sbx-a4b86fd1",
      "ownerHash": "c030c7356b35f09989ccec9e0b3b04c1f74f20eee9c5ba9b93a2e9b5e27e0455",
      "status": "running",
      "ports": {
        "vnc": "5901",
        "novnc": "6081",
        "mcphub": "7081",
        "filesvc": "7281"
      }
    }
  ]
}
```

### `POST /containers`
Creates a new sandbox using the default `albert-ai-sandbox:latest` image and returns JSON describing the provisioned resources.

```bash
curl -s \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:5001/containers
```
Observed response (HTTP 201):
```json
{
  "container": {
    "name": "sbx-cdf4e078",
    "sandboxName": "sbx-cdf4e078",
    "error": "Could not inspect container: Error while fetching server API version: Not supported URL scheme http+docker",
    "script": {
      "result": "created",
      "name": "sbx-cdf4e078",
      "ownerHash": "c030c7356b35f09989ccec9e0b3b04c1f74f20eee9c5ba9b93a2e9b5e27e0455",
      "ports": {
        "vnc": "5902",
        "novnc": "6082",
        "mcphub": "7082",
        "filesvc": "7282"
      },
      "urls": {
        "desktop": "http://10.0.1.61/sbx-cdf4e078/",
        "filesUpload": "http://10.0.1.61/sbx-cdf4e078/files/upload",
        "filesDownloadPattern": "http://10.0.1.61/sbx-cdf4e078/files/download?path=/tmp/albert-files/<uuid.ext>",
        "mcphub": "http://10.0.1.61/sbx-cdf4e078/mcphub/mcp"
      }
    }
  }
}
```
The `error` field appears when Docker inspection fails; the container is still created and accessible through the URLs that the `script` block reports. Normal deployments with `requests-unixsocket` available return runtime metadata in place of the `error` field.

Optional body fields:
- `name`: request a friendly sandbox name; auto-generated when omitted.
- `autoStart`: defaults to `true`.
- `env` / `cmd`: reserved for future use (currently ignored by the wrapper script).

### `GET /containers/{name}`
Fetches detailed status for a specific sandbox.

```bash
curl -s \
  -H "Authorization: Bearer <API_KEY>" \
  http://127.0.0.1:5001/containers/sbx-cdf4e078
```
Sample output while the sandbox was running:
```json
{
  "container": {
    "name": "sbx-cdf4e078",
    "sandboxName": "sbx-cdf4e078",
    "error": "Could not inspect: Error while fetching server API version: Not supported URL scheme http+docker",
    "script": {
      "status": "running",
      "created": "2025-09-22T15:02:01+02:00",
      "resources": "CPU: 0.03% | RAM: 432.5MiB / 7.753GiB",
      "ports": {
        "vnc": "5902",
        "novnc": "6082",
        "mcphub": "7082",
        "filesvc": "7282"
      },
      "urls": {
        "desktop": "http://10.0.1.61/sbx-cdf4e078/",
        "files": "http://10.0.1.61/sbx-cdf4e078/files/",
        "mcphub": "http://10.0.1.61/sbx-cdf4e078/mcphub/mcp"
      }
    }
  }
}
```
A `404` is returned if the sandbox cannot be located via the manager wrapper.

### `POST /containers/{name}/stop`
Gracefully stops the sandbox and echoes updated status metadata.

```bash
curl -s \
  -X POST \
  -H "Authorization: Bearer <API_KEY>" \
  http://127.0.0.1:5001/containers/sbx-cdf4e078/stop
```
Response:
```json
{
  "container": {
    "name": "sbx-cdf4e078",
    "sandboxName": "sbx-cdf4e078",
    "error": "Could not inspect: Error while fetching server API version: Not supported URL scheme http+docker",
    "script": {
      "status": "stopped",
      "ports": {
        "vnc": "5902",
        "novnc": "6082",
        "mcphub": "7082",
        "filesvc": "7282"
      },
      "urls": {
        "desktop": "http://10.0.1.61/sbx-cdf4e078/",
        "files": "http://10.0.1.61/sbx-cdf4e078/files/",
        "mcphub": "http://10.0.1.61/sbx-cdf4e078/mcphub/mcp"
      }
    }
  }
}
```

### `POST /containers/{name}/start`
Restarts a stopped sandbox.

```bash
curl -s \
  -X POST \
  -H "Authorization: Bearer <API_KEY>" \
  http://127.0.0.1:5001/containers/sbx-cdf4e078/start
```
Response mirrors the stop payload with `status: "running"` and fresh resource metrics.

### `POST /containers/{name}/restart`
Convenience action that issues a stop followed by start.

```bash
curl -s \
  -X POST \
  -H "Authorization: Bearer <API_KEY>" \
  http://127.0.0.1:5001/containers/sbx-cdf4e078/restart
```
Response: same shape as `start`, with resource usage reflecting the new run.

### `DELETE /containers/{name}`
Destroys the sandbox and its associated reverse-proxy routing.

```bash
curl -s \
  -X DELETE \
  -H "Authorization: Bearer <API_KEY>" \
  http://127.0.0.1:5001/containers/sbx-cdf4e078
```
Response:
```json
{"deleted":"sbx-cdf4e078"}
```

## Under-the-hood CLI (`albert-ai-sandbox-manager.sh`)
The REST API delegates to `/opt/albert-ai-sandbox-manager/scripts/albert-ai-sandbox-manager.sh`. The script accepts commands such as `create`, `list`, `status`, `start`, `stop`, `restart`, `remove`, and `build`, and it surfaces JSON when `ALBERT_JSON=1` (set automatically by the REST layer). Direct CLI usage is helpful for debugging:

```bash
sudo ALBERT_JSON=1 /opt/albert-ai-sandbox-manager/scripts/albert-ai-sandbox-manager.sh list --api-key-hash $(python3 -c 'import hashlib;print(hashlib.sha256(b"<API_KEY>").hexdigest())')
```

## Validation Performed
- Installation via `bash install.sh`
- Confirmed `albert-container-manager.service` active on port 5001
- Created API key with `api-key-manager.sh`
- Exercised `GET /health`, `GET /containers`, `POST /containers`, `GET /containers/{name}`
- Verified lifecycle actions: stop, start, restart, delete
- Observed Docker inspection warnings; container lifecycle succeeded regardless

