# Albert Sandbox Manager – REST API

The manager service (`albert-container-manager.service`) listens on `127.0.0.1:5001` and is published by nginx under `/manager/` (e.g. `https://<host>/manager/containers`). Payloads keep the field names of the former versions; new fields were only added (and the secrets were removed, see below).

## Client API
Every endpoint expects `Authorization: Bearer <PLAINTEXT_API_KEY>`. Sandboxes of other clients are reported as `404`.

**User binding:** every request (except `/health`) carries `X-Albert-User: <pseudonymous id of the ALBERT user>` (1–128 characters `A-Z a-z 0-9 . _ : @ + = / -`; e.g. a hash of the user's UUID, no names). `POST /containers` binds the new sandbox to that user; list, details, start/stop/restart, persistent, desktop and delete only see the sandboxes of that user (others: `404`). The header is set by ALBERT itself, never by the model. Clients with *legacy access* (admin UI: "Alte Anbindung erlauben"; on for clients that existed before this version, off for new ones) may omit it; a sandbox without user is then taken over by the first user that addresses it with the header.

**MCP Hub and file service** (`urls.mcphub`, `urls.files…`) need the same `Authorization: Bearer <API key>` and `X-Albert-User` as the API. nginx asks the manager before every request and forwards it with the sandbox's internal token; `401` = missing/invalid key, `403` = sandbox of another client or user. Legacy clients keep the former access (MCP with the fixed key `albert`, files without key).

**HTTPS:** once a certificate is active (admin UI, Einstellungen) the host answers API, MCP and file requests only over HTTPS; over HTTP they get `403 {"error": "https_required"}`. Use the `https://` base URL and verify the certificate.

Errors use the shape `{"error": "<code>", "message": "<text>", ...}`:

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | invalid input (e.g. non-numeric resources) |
| 400 | `user_required`, `invalid_user` | `X-Albert-User` missing (clients without legacy access) or invalid |
| 401 | – | missing/invalid API key |
| 403 | `persistent_not_allowed` | the client may not have persistent sandboxes (`POST /containers` with `"persistent": true`, or switching a sandbox to persistent) |
| 404 | `not_found` | unknown sandbox (or owned by another client) |
| 503 | `profile_not_ready` | the software profile assigned to the client has not been built yet (an admin has to build it) |
| 409 | `running_limit_exceeded` | the client already runs as many sandboxes as allowed (create or start); details: `limit`, `used`, `runningSandboxes` (only the asking user's own) – stop one of them first |
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
  "clientId": 3,
  "user": "u-7f3a…",
  "internet": true,
  "profile": {"id": 2, "name": "Office", "description": "",
              "packages": {"apt": ["libreoffice"], "pip": ["pandas"], "npm": []}},
  "persistentAllowed": false
}
```
`status` is `running`, `stopped`, `creating`, `removing`, `rebuilding` or `error` (a rebuild failed; the sandbox and its data are kept until an admin acts). `outdated` lists why the container should be rebuilt (`image`, `hardening`, `network`, `auth` = container from before the internal tokens of MCP Hub and file service); empty when up to date. `user` is the ALBERT user the sandbox is bound to (`null` = no binding). `urls.desktop` without a one-time link only works for legacy clients; use `POST /containers/{name}/desktop`. `persistentAllowed` tells whether the owning client may switch the sandbox to persistent. `internet` is false for clients without internet access (no outgoing connections, no name resolution). `profile` is the client's software profile (`null` = standard image); `outdated` contains `image` while the sandbox still runs an older image than the client's current one and `network` while its network does not match the internet setting. `resources.storage` is `image` (hard disk limit) or `volume` (sandbox migrated from the former version, no disk limit, `diskGb` = null). The VNC port is no longer published (`"vnc": ""`), except for migrated sandboxes.

### `GET /health`
`{"status": "ok", "docker": "up", "api": 3, "features": ["quota", "profiles", "user-binding", "service-auth", "desktop-link", "no-secrets"], "legacyAccess": false, "https": true}` (API key needed, `X-Albert-User` not). `api`/`features` tell this server from older ones (no `api` field: former interface, MCP with the fixed key, no user binding). Every client API response also carries the header `X-Albert-Sandbox-Api: 3`. `legacyAccess`: the client still has the former access; `https`: HTTPS is active (API only over HTTPS).

### `GET /quota` (new)
```json
{
  "quota": {"cpus": 10, "memoryMb": 20480, "diskGb": 200, "sandboxes": null, "running": 2},
  "usage": {"cpus": 2.0, "memoryMb": 2048, "diskGb": 30, "sandboxes": 3, "running": 2, "persistent": 0,
            "runningNames": ["sbx-a-…", "sbx-b-…"]},
  "defaults": {"cpus": null, "memoryMb": null, "diskGb": null,
               "effective": {"cpus": 1.0, "memoryMb": 2048, "diskGb": 10}},
  "persistentAllowed": false,
  "internetAccess": true,
  "profile": null
}
```
`null` quota = unlimited; quota and usage totals cover the whole client (all users), `usage.runningNames` only lists the asking user's sandboxes (`X-Albert-User`); `quota.running` limits the sandboxes running at the same time; `defaults.effective` is the per-sandbox limit that applies; `persistentAllowed` tells whether persistent sandboxes may be created; `internetAccess` false means the sandboxes cannot reach the internet (install nothing at runtime, use the software of `profile`).

### `POST /containers`
Body (all optional):
```json
{"name": "agent-lab-01", "persistent": false, "resources": {"cpus": 1, "memoryMb": 2048, "diskGb": 10}}
```
`name` is a hint; the final name gets a random suffix. Missing resources default to the client's per-sandbox limit; larger values are refused (`sandbox_limit_exceeded`). `"persistent": true` needs the client's permission (`persistent_not_allowed` otherwise); a new sandbox counts against the running limit (`running_limit_exceeded`). Response `201 {"container": {…sandbox object…, "result": "created"}}`. VNC password and internal tokens are no longer returned (former fields `vncPassword`, `fileServiceToken`): nginx uses them, clients authenticate with their API key. The call returns when MCP Hub inside the sandbox answers (up to ~2 minutes).

### `GET /containers`
`{"containers": [ …sandbox objects… ]}`

### `GET /containers/{name}`
`{"container": { … }}`

### `POST /containers/{name}/start` · `/stop` · `/restart`
`{"container": { … }}`. Start checks the running limit and the CPU/RAM quota and waits for MCP Hub.

### `PATCH|POST /containers/{name}/persistent`
Body `{"persistent": true}` → `{"name": "…", "persistent": true}`. Switching on needs the client's permission (403 `persistent_not_allowed`), switching off always works.

### `POST /containers/{name}/desktop` (new)
`201 {"desktop": {"url": "https://<host>/<name>/?ticket=…", "expiresAt": "…", "validSeconds": 120, "singleUse": true}}`. A one-time link to the desktop: valid once and only for `validSeconds` (`ALBERT_DESKTOP_LINK_SECONDS`). Opening it sets a desktop cookie for this sandbox only (`ALBERT_DESKTOP_SESSION_HOURS`, default 8 h); afterwards the link is worthless, so it may appear in a chat. Fetch a new link every time the user wants to open the desktop. Without a valid link or cookie the desktop shows "link expired".

### `DELETE /containers/{name}`
Removes container, disk image, nginx route and database entry → `{"deleted": "…"}`

## Admin API
Used by the web UI under `/manager/admin/` on its **own port** (`ALBERT_ADMIN_PORT`, default 8443; an own origin, so pages of sandboxes can never act in it), optionally limited to a list of addresses (loopback = SSH tunnel always allowed). On the main port `/manager/admin` redirects there. JSON under `/manager/admin/api/`. Session cookie (path `/manager/`) after `POST api/login {"username","password"}` (returns a CSRF token); every non-GET request needs the header `X-CSRF-Token`. Failed logins are throttled per IP.

| Method | Path | Purpose |
|---|---|---|
| GET | `api/session` | current admin + CSRF token (401 if not logged in, `hasAdmins`) |
| POST | `api/login`, `api/logout` | session |
| GET | `api/overview` | host resources, totals, defaults, network isolation status |
| GET/POST | `api/clients` | list (with usage) / create (returns `apiKey` once) |
| GET/PATCH/DELETE | `api/clients/{id}` | detail incl. sandboxes / change label, `defaultCpus`, `defaultMemoryMb`, `defaultDiskGb`, `quotaCpus`, `quotaMemoryMb`, `quotaDiskGb`, `quotaSandboxes`, `quotaRunning` (null = unset), `allowPersistent` (bool), `allowInternet` (bool; a change moves and rebuilds the client's sandboxes in the background, response field `networkPolicy: "applying"`), `profileId` (null = standard image), `legacyAccess` (bool, former access without API key / user binding), `allowNets` (list of IPv4 addresses/networks the client's sandboxes may reach in addition to the global ones, `[]` = none, `400 invalid_net`; the first own target gives the client an own network: `netSubnet`, `network`, existing sandboxes are moved without rebuild, `networkPolicy: "applying"`) / delete with all sandboxes |
| POST | `api/clients/{id}/rotate-key` | new API key, sandboxes stay |
| GET/POST | `api/profiles` | list (status, image, clients) / create `{name, description, packages: {apt, pip, npm}, setupScript}` |
| GET/PATCH/DELETE | `api/profiles/{id}` | detail incl. generated `dockerfile` / change (same fields) / delete (400 `profile_in_use` while assigned) |
| POST | `api/profiles/{id}/build` | start a build (202; 409 while one runs) |
| GET | `api/profiles/{id}/log?seq=&offset=` | build output from `offset` (`text`, next `offset`, `running`) |
| GET | `api/sandboxes[?client=id]` | all sandboxes |
| PATCH/DELETE | `api/sandboxes/{name}` | `persistent`, `cpus`, `memoryMb`, `diskGb` (disk can only grow) / delete |
| POST | `api/sandboxes/{name}/start|stop|restart|recreate` | lifecycle; `recreate` rebuilds the container with the current image/hardening/network, keeping data and ports; VNC password and internal tokens are renewed |
| POST | `api/sandboxes/{name}/desktop` | one-time desktop link for the admin |
| GET | `api/settings` | HTTPS state (certificate, job), admin URL/port, allowed admin addresses, own address, internal targets (`allowNets`: `nets`, `source` = `settings` or `config` for `ALBERT_ALLOW_NETS`, `configNets`, `clients` with own targets), `blockedNets` |
| PUT | `api/settings/allow-nets` | `{"allowNets": ["10.0.5.20", "10.0.6.0/24"]}` – internal targets of all sandboxes, applied to the firewall at once (`[]` = none); `{"reset": true}` = back to `ALBERT_ALLOW_NETS`; `400 invalid_net`, `firewall_failed` (nothing changed) |
| PUT | `api/settings/admin-access` | `{"allow": ["203.0.113.7", "10.0.0.0/16"]}` (or a newline separated string; empty = all); refused (`lockout`) if the own address is missing |
| POST | `api/settings/tls/custom` | `{"host", "certificate", "chain", "privateKey"}` (PEM): checked (key matches, name, expiry), then active |
| POST | `api/settings/tls/letsencrypt` | `{"host", "email", "acceptTerms": true}` → 202, certificate is requested in the background (`job` in `api/settings`) |
| DELETE | `api/settings/tls` | back to HTTP |
| GET | `api/sandboxes/{name}/stats` | live docker stats + disk usage |
| GET/POST | `api/admins`, `api/admins/{id}/password`, DELETE `api/admins/{id}` | admin users |
| GET/POST | `api/reconcile` | cleanup preview / run |
| GET | `api/audit?limit=200` | audit log |

## Internal endpoints (nginx only)
`GET /internal/auth/{mcp|files|desktop|desktop-page}/{name}` (auth_request; 204/401/403) and `GET /internal/desktop/{name}/entry` (desktop entry: one-time link → cookie → noVNC page, settings in the URL fragment). They answer only requests that carry the header nginx sets on its internal routes; `/manager/internal/` is blocked.

## CLI
`albert-ai-sandbox-manager help` lists all commands; see README. The REST service no longer shells out to the CLI; both use the same Python package (`scripts/albert_sandbox`).
