# ALBERT | AI Sandbox

ALBERT | AI Sandbox provides isolated, browser-ready compute environments for AI agents such as ALBERT | AI. Each sandbox is created on demand through a REST service, launched as a Docker container, and ships with everything an autonomous agent needs: a full desktop exposed over noVNC, an MCP Hub instance with Playwright for automated browser control, and a shell execution service for running scripts or Python code. When human intervention is required, the same desktop can be opened in any modern browser so credentials or multi-factor prompts can be handled interactively.

## Key Capabilities
- Spin up dedicated desktop sandboxes per API request, preloaded with browser automation and shell tooling.
- Share desktops securely over noVNC so users can supervise or intervene without local software.
- Run MCP Hub with Playwright inside every container to orchestrate browser sessions for the AI agent.
- **Resource limits per sandbox**: CPU cores, RAM and a hard disk limit (default: 1 core, 1 GB RAM, 10 GB disk).
- **Clients with quotas**: every API key is a client with its own per-sandbox limit and an overall quota (cores and RAM used at the same time, total disk, number of sandboxes).
- **Admin web UI** (`/manager/admin/`): manage admin users and clients, see every sandbox with status and disk usage, start/stop/resize/delete sandboxes or whole clients.
- Auto-stop sandboxes after 10 minutes of inactivity, auto-delete non-persistent sandboxes after a maximum age (default 24 hours).
- **Self-cleaning**: a reconcile step removes everything that has no sandbox any more (containers, disk images, volumes, nginx routes) and repairs missing routes and mounts.
- Keep data and lifecycle operations isolated per API key, ensuring one tenant cannot touch another tenant's sandboxes.

## Architecture
```
nginx ── /manager/          → manager service (127.0.0.1:5001): client REST API + admin UI
      ── /<sandbox>/...     → per-sandbox routes (noVNC, MCP Hub, file service) on 127.0.0.1 ports

manager service, CLI, watcher ─→ scripts/albert_sandbox (Python)
                                   ├─ SQLite (data/manager.db): clients, sandboxes, admins, audit log
                                   ├─ docker CLI: containers with --cpus/--memory/--pids-limit
                                   ├─ disk images: /var/lib/albert-sandbox (one ext4 image per sandbox)
                                   └─ nginx routes: /etc/nginx/albert/sandboxes/<name>.conf
```
SQLite is the single source of truth. Docker containers, disk images and nginx routes are derived from it; the watcher reconciles them every minute.

## System Requirements
- Host OS: Debian 12 (bookworm) verified; comparable Debian-based systems should work with equivalent packages.
- Root access to run the installer, manage services, and configure nginx.
- Docker Engine and systemd available on the host (Docker is installed if missing).
- Internet connectivity during installation to fetch Docker components and Python packages.

## Installation and Update
```bash
git clone <repository-url> albert-ai-sandbox
cd albert-ai-sandbox
sudo bash install.sh
```
The installer provisions all required packages, builds the sandbox image, installs the manager under `/opt/albert-ai-sandbox-manager`, configures nginx and the systemd units, and creates the first admin user `admin` with a random password (printed once).

**Updating** works the same way: pull the new version and run `sudo bash install.sh` again. Running sandboxes keep running; only the manager service restarts briefly. The database schema is migrated automatically. Installations of the former Bash version (JSON registry) are migrated as well: existing sandboxes, their data volumes, ports and credentials are taken over, API keys stay valid. The sandbox image is only rebuilt when `docker/` changed (`--skip-build` / `--force-build` override this). Replaced configuration is backed up to `/opt/albert-ai-sandbox-manager/backups/<timestamp>/`.

Optional host hardening (TLS via Let's Encrypt, firewall, fail2ban, SSH): `sudo bash scripts/setup-tls.sh --fqdn sandbox.example.com --email admin@example.com`.

## Configuration
`/opt/albert-ai-sandbox-manager/config/albert.env` (KEY=VALUE, never overwritten by updates) is read by the manager service, the watcher and the CLI. Restart the manager after changes (`systemctl restart albert-container-manager`).

| Variable | Default | Meaning |
|---|---|---|
| `ALBERT_PUBLIC_URL` | `http://<primary IP>` | Base URL used in API responses (set by `setup-tls.sh`) |
| `ALBERT_DEFAULT_CPUS` | `1` | Global per-sandbox CPU limit (if the client has none) |
| `ALBERT_DEFAULT_MEMORY_MB` | `1024` | Global per-sandbox RAM limit |
| `ALBERT_DEFAULT_DISK_GB` | `10` | Global per-sandbox disk size |
| `ALBERT_INACTIVITY_SECONDS` | `600` | Stop sandboxes after inactivity (data is kept, `0` = off) |
| `ALBERT_MAX_AGE_SECONDS` | `86400` | Delete non-persistent sandboxes after this age (`0` = off) |
| `ALBERT_PORT_RANGE` | `20000-29999` | Host ports for sandbox services (bound to 127.0.0.1) |
| `ALBERT_STORAGE_DIR` | `/var/lib/albert-sandbox` | Disk images and mount points |

## Clients, Limits and Quotas
A client is an API key plus settings:

- **Limit per sandbox** (CPU cores, RAM, disk): what each sandbox of the client gets. Empty values fall back to the global defaults above. Through the API a sandbox may request *less*, never more. Admins can change the limits of a single sandbox later (CPU/RAM live, disk can only grow).
- **Overall quota** (empty = unlimited):
  - CPU cores and RAM count the limits of the client's **running** sandboxes, i.e. what is used at the same time. Starting or creating a sandbox beyond the quota is refused with HTTP 409 `quota_exceeded`.
  - Disk counts the configured size of **all** sandboxes of the client, running or stopped.
  - Maximum number of sandboxes.

Manage clients in the admin UI or on the host:
```bash
albert-ai-sandbox-manager client create --label "Customer A" --default-cpus 2 --default-memory-mb 2048 \
    --quota-cpus 10 --quota-memory-mb 20480 --quota-disk-gb 200
albert-ai-sandbox-manager client list
albert-ai-sandbox-manager client set 3 --quota-cpus none      # none = unlimited / global default
albert-ai-sandbox-manager client rotate 3                       # new API key, sandboxes stay
albert-ai-sandbox-manager client delete 3                       # deletes all its sandboxes too
```
The plaintext key is shown once; only its SHA-256 hash is stored. `scripts/api-key-manager.sh create|list|revoke` still works as before.

### How the limits are enforced
- CPU and RAM: Docker cgroup limits (`--cpus`, `--memory` without extra swap) plus a process limit (`--pids-limit`).
- Disk: every sandbox gets its own sparse ext4 image file of the configured size, loop-mounted on the host and mounted into the container as `/home/ubuntu` and `/tmp/albert-files` (uploads). When it is full, writes inside the sandbox fail; the host disk cannot fill up. Files deleted inside the sandbox give space back to the host. The container's own layer (system directories) is measured and shown in the admin UI.
- Sandboxes migrated from the former version keep their Docker volume and have no disk limit (shown as "ohne Limit"); CPU/RAM limits can be set for them in the admin UI.

## Admin Web UI
Open `https://<host>/manager/admin/` and sign in. Pages:
- **Übersicht**: host resources, allocated CPU/RAM of running sandboxes, disk usage.
- **Clients**: create (the API key is shown once), edit per-sandbox limits and quotas, rotate the key, delete a client with all its sandboxes; per client all sandboxes with status, limits, disk usage, last activity and actions (start, stop, restart, change limits, live values, delete).
- **Sandboxen**: all sandboxes across clients.
- **Admins**: add admin users, change passwords, delete admins.
- **Wartung**: preview and run the cleanup (reconcile), audit log of all changes.

Admin users on the host: `albert-ai-sandbox-manager admin add|list|passwd|remove <username>`.

## Manage Sandboxes from the Command Line
`albert-ai-sandbox-manager` (in `/usr/local/bin`) keeps the commands of the former Bash manager. With `--api-key <PLAINTEXT>` or `--api-key-hash <HASH>` it acts as that client; without a key it works in operator mode on all sandboxes (`create` always needs a key).
- `create [name] [--persistent] [--cpus N] [--memory-mb N] [--disk-gb N]`
- `list` / `status [name]`, `start`, `stop`, `restart`, `remove <name>` (always deletes the data)
- `resources <name> --cpus N --memory-mb N --disk-gb N`, `persistent <name> on|off`
- `reconcile [--dry-run]` – clean up leftovers, `selfcheck`, `build` – rebuild the image

Add `--json` for machine-readable output.

## Manage Sandboxes over REST
Integrators talk to the manager through nginx (`https://<host>/manager/`) or locally (`http://127.0.0.1:5001`) with Bearer authentication. See [SERVICES.md](SERVICES.md) for all endpoints.
```bash
curl -X POST -H "Authorization: Bearer $ALBERT_API_KEY" -H "Content-Type: application/json" \
     -d '{"name": "agent-lab-01", "resources": {"cpus": 1, "memoryMb": 1024}}' \
     https://<host>/manager/containers
```
Responses include the noVNC desktop URL, the MCP Hub endpoint, the file service URLs, limits and usage.

## File Transfer Services
Each sandbox exposes a lightweight file bridge so agents can move artifacts in and out of the container. The service sits behind nginx and is namespaced per sandbox.

```bash
curl -X POST -F "file=@model-output.json" http://<host>/<sandbox>/files/upload
curl "http://<host>/<sandbox>/files/download?path=/tmp/albert-files/<uuid.ext>" --output ./downloaded.json
```
Uploads land in `/tmp/albert-files` inside the sandbox and count towards the sandbox disk limit. Note: the file service does not enforce the per-sandbox `fileServiceToken` yet; access is protected by the unguessable sandbox name only.

## Container Lifecycle & Automatic Cleanup
The watcher (`albert-inactivity-watcher.timer`, every minute):

| Step | Default | Effect |
|---|---|---|
| Inactivity stop | 10 minutes | Stops idle sandboxes (data is kept) |
| Max age removal | 24 hours | Deletes non-persistent sandboxes **including data** |
| Reconcile | always | Removes containers, disk images, volumes and nginx routes without a sandbox; finishes interrupted removals; rewrites missing routes; mounts disks |

Every removal deletes the container, its disk image (or legacy volume), its nginx route and its database row. If a step fails, the sandbox stays in state `removing` and the next watcher run finishes the job. A failed creation is rolled back completely.

Persistent sandboxes are excluded from max-age removal (`"persistent": true` at creation, `PATCH /containers/<name>/persistent`, CLI `--persistent`, or in the admin UI).

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
The tests use an in-memory fake for Docker and the disk images and run without root.

## Uninstall
```bash
sudo bash uninstall.sh
```
Removes all sandboxes including their data, the manager, its systemd units and nginx routes. Docker, nginx, packages and the sandbox image are kept.

## Enterprise Integration
A ready-to-use integration of ALBERT | AI Sandbox is available inside ALBERT | AI from agorum Software GmbH. Learn more at https://www.agorum.com.
