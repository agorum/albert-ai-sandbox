# Albert Sandbox Manager – REST API

The manager service (`albert-container-manager.service`) listens on `127.0.0.1:5001` and is published by nginx under `/manager/` (e.g. `https://<host>/manager/containers`). Payloads keep the field names of the former versions; new fields were only added.

## Client API
Every endpoint expects `Authorization: Bearer <PLAINTEXT_API_KEY>`. Sandboxes of other clients are reported as `404`.

Errors use the shape `{"error": "<code>", "message": "<text>", ...}`:

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | invalid input (e.g. non-numeric resources) |
| 401 | – | missing/invalid API key |
| 404 | `not_found` | unknown sandbox (or owned by another client) |
| 409 | `quota_exceeded` | client quota (CPU/RAM of running sandboxes, disk, count) would be exceeded; details: `resource`, `limit`, `used`, `requested` |
| 409 | `sandbox_limit_exceeded` | requested resources above the client's per-sandbox limit |
| 409 | `conflict` | sandbox is being created/removed |
| 500 | `create_failed`, `mcphub_timeout`, `image_missing`, `remove_incomplete` | operation failed (creations are rolled back; incomplete removals are finished by the watcher) |
| 502 | `docker` | Docker CLI error |

### Sandbox object
```json
{
  "id": "4f2c…",
  "name": "sbx-agent-lab-01-3f9a0c1d2e4b5a67",
  "sandboxName": "sbx-agent-lab-01-3f9a0c1d2e4b5a67",
  "image": "albert-ai-sandbox:latest",
  "status": "running",
  "running": true,
  "startedAt": "2026-11-11T15:02:01.123Z",
  "finishedAt": null,
  "createdAt": "2026-11-11T16:02:01+01:00",
  "persistent": false,
  "ports": {"novnc": "20000", "vnc": "", "mcphub": "20001", "filesvc": "20002"},
  "urls": {
    "desktop": "https://<host>/<name>/",
    "mcphub": "https://<host>/<name>/mcphub/mcp",
    "files": "https://<host>/<name>/files/",
    "filesUpload": "https://<host>/<name>/files/upload",
    "filesDownloadPattern": "https://<host>/<name>/files/download?path=/tmp/albert-files/<uuid.ext>"
  },
  "resources": {"cpus": 1.0, "memoryMb": 2048, "diskGb": 10, "storage": "image"},
  "usage": {"diskUsedBytes": 52428800, "rootfsUsedBytes": 1048576, "updatedAt": "2026-11-11T16:10:00+01:00"},
  "lastActivityAt": "2026-11-11T16:09:12+01:00",
  "clientId": 3
}
```
`status` is `running`, `stopped`, `creating` or `removing`. `resources.storage` is `image` (hard disk limit) or `volume` (sandbox migrated from the former version, no disk limit, `diskGb` = null). The VNC port is no longer published (`"vnc": ""`), except for migrated sandboxes.

### `GET /health`
`{"status": "ok", "docker": "up"}`

### `GET /quota` (new)
```json
{
  "quota": {"cpus": 10, "memoryMb": 20480, "diskGb": 200, "sandboxes": null},
  "usage": {"cpus": 2.0, "memoryMb": 2048, "diskGb": 30, "sandboxes": 3, "running": 2},
  "defaults": {"cpus": null, "memoryMb": null, "diskGb": null,
               "effective": {"cpus": 1.0, "memoryMb": 2048, "diskGb": 10}}
}
```
`null` quota = unlimited; `defaults.effective` is the per-sandbox limit that applies.

### `POST /containers`
Body (all optional):
```json
{"name": "agent-lab-01", "persistent": false, "resources": {"cpus": 1, "memoryMb": 2048, "diskGb": 10}}
```
`name` is a hint; the final name gets a random suffix. Missing resources default to the client's per-sandbox limit; larger values are refused (`sandbox_limit_exceeded`). Response `201 {"container": {…sandbox object…, "vncPassword": "…", "fileServiceToken": "…", "result": "created"}}`. The secrets are only returned here. The call returns when MCP Hub inside the sandbox answers (up to ~2 minutes).

### `GET /containers`
`{"containers": [ …sandbox objects… ]}`

### `GET /containers/{name}`
`{"container": { … }}`

### `POST /containers/{name}/start` · `/stop` · `/restart`
`{"container": { … }}`. Start checks the CPU/RAM quota and waits for MCP Hub.

### `PATCH|POST /containers/{name}/persistent`
Body `{"persistent": true}` → `{"name": "…", "persistent": true}`

### `DELETE /containers/{name}`
Removes container, disk image, nginx route and database entry → `{"deleted": "…"}`

## Admin API
Used by the web UI under `/manager/admin/`; JSON under `/manager/admin/api/`. Session cookie after `POST api/login {"username","password"}` (returns a CSRF token); every non-GET request needs the header `X-CSRF-Token`. Failed logins are throttled per IP.

| Method | Path | Purpose |
|---|---|---|
| GET | `api/session` | current admin + CSRF token (401 if not logged in, `hasAdmins`) |
| POST | `api/login`, `api/logout` | session |
| GET | `api/overview` | host resources, totals, defaults |
| GET/POST | `api/clients` | list (with usage) / create (returns `apiKey` once) |
| GET/PATCH/DELETE | `api/clients/{id}` | detail incl. sandboxes / change label, `defaultCpus`, `defaultMemoryMb`, `defaultDiskGb`, `quotaCpus`, `quotaMemoryMb`, `quotaDiskGb`, `quotaSandboxes` (null = unset) / delete with all sandboxes |
| POST | `api/clients/{id}/rotate-key` | new API key, sandboxes stay |
| GET | `api/sandboxes[?client=id]` | all sandboxes |
| PATCH/DELETE | `api/sandboxes/{name}` | `persistent`, `cpus`, `memoryMb`, `diskGb` (disk can only grow) / delete |
| POST | `api/sandboxes/{name}/start|stop|restart` | lifecycle |
| GET | `api/sandboxes/{name}/stats` | live docker stats + disk usage |
| GET/POST | `api/admins`, `api/admins/{id}/password`, DELETE `api/admins/{id}` | admin users |
| GET/POST | `api/reconcile` | cleanup preview / run |
| GET | `api/audit?limit=200` | audit log |

## CLI
`albert-ai-sandbox-manager help` lists all commands; see README. The REST service no longer shells out to the CLI; both use the same Python package (`scripts/albert_sandbox`).
