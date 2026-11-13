# ALBERT | AI Sandbox

ALBERT | AI Sandbox provides isolated, browser-ready compute environments for AI agents such as ALBERT | AI. Each sandbox is created on demand through a REST service, launched as a Docker container, and ships with everything an autonomous agent needs: a full desktop exposed over noVNC, an MCP Hub instance with Playwright for automated browser control, and a shell execution service for running scripts or Python code. When human intervention is required, the same desktop can be opened in any modern browser so credentials or multi-factor prompts can be handled interactively.

## Key Capabilities
- Spin up dedicated desktop sandboxes per API request, preloaded with browser automation and shell tooling.
- Share desktops securely over noVNC so users can supervise or intervene without local software.
- Run MCP Hub with Playwright inside every container to orchestrate browser sessions for the AI agent.
- **Resource limits per sandbox**: CPU cores, RAM and a hard disk limit (default: 1 core, 2 GB RAM, 10 GB disk).
- **Clients with quotas**: every API key is a client with its own per-sandbox limit and an overall quota (cores and RAM used at the same time, total disk, number of sandboxes).
- **Admin web UI** (own port, default `:8443/manager/admin/`): manage admin users and clients, see every sandbox with status and disk usage, start/stop/resize/delete sandboxes or whole clients, set up HTTPS (Let's Encrypt or own certificate) and limit the admin UI to certain addresses.
- **Access only with the API key**: MCP Hub, file service and desktop of a sandbox are checked by nginx through the manager on every request; sandboxes are bound to the ALBERT user that created them; the desktop opens with one-time links.
- Auto-stop sandboxes after 10 minutes of inactivity, auto-delete non-persistent sandboxes after a maximum age (default 24 hours).
- **Self-cleaning**: a reconcile step removes everything that has no sandbox any more (containers, disk images, volumes, nginx routes) and repairs missing routes and mounts.
- **Network isolation**: sandboxes reach the internet, but neither each other nor internal networks nor services of the host. **Internet access can be switched off per client.**
- **Sandbox profiles**: own images per client with extra software (apt, pip, npm packages, optional setup commands), built in the admin UI on top of the shipped image.
- **Hardened containers** without extra privileges; a one-click **rebuild** brings existing sandboxes to the current image and hardening while keeping their data.
- Keep data and lifecycle operations isolated per API key, ensuring one tenant cannot touch another tenant's sandboxes.

## Architecture
```
nginx :80/:443 ── /manager/       → manager service (127.0.0.1:5001): client REST API
               ── /<sandbox>/...  → per-sandbox routes; every request checked by the manager first
                                    (auth_request): MCP Hub, file service (API key + user),
                                    desktop (one-time link → cookie); noVNC files served by nginx
nginx :8443    ── /manager/admin/ → admin UI (own origin, optional address list)

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

**HTTPS** is set up in the admin UI under *Einstellungen*: request a Let's Encrypt certificate (host name must point to the host, port 80 reachable) or upload an own certificate (PEM certificate, intermediates, private key without passphrase). Each change is tested with `nginx -t`; if nginx refuses it, the previous configuration stays active. With HTTPS active the host accepts API, MCP and file requests only over HTTPS (`https_required` over HTTP). CLI: `albert-ai-sandbox-manager tls status|letsencrypt|custom|disable`.

Optional host hardening (firewall, fail2ban, SSH, Let's Encrypt through the manager): `sudo bash scripts/setup-tls.sh --fqdn sandbox.example.com --email admin@example.com` (after `install.sh`).

The installer writes the nginx site `/etc/nginx/sites-available/albert-sandbox` (main server on 80/443, admin UI on its own port) and disables the Debian default site; a certificate set up by an older `setup-tls.sh` is taken over.

## Configuration
`/opt/albert-ai-sandbox-manager/config/albert.env` (KEY=VALUE, never overwritten by updates) is read by the manager service, the watcher and the CLI. Restart the manager after changes (`systemctl restart albert-container-manager`).

| Variable | Default | Meaning |
|---|---|---|
| `ALBERT_PUBLIC_URL` | `http://<primary IP>` | Base URL used in API responses while HTTPS is off (with HTTPS: `https://<host name of the certificate>`) |
| `ALBERT_ADMIN_PORT` | `8443` | Port of the admin UI (own origin; HTTPS once a certificate is active) |
| `ALBERT_DESKTOP_LINK_SECONDS` | `120` | Validity of a one-time desktop link |
| `ALBERT_DESKTOP_SESSION_HOURS` | `8` | Validity of the desktop session (cookie) a link opens |
| `ALBERT_DEFAULT_CPUS` | `1` | Global per-sandbox CPU limit (if the client has none) |
| `ALBERT_DEFAULT_MEMORY_MB` | `2048` | Global per-sandbox RAM limit (an idle desktop needs ~450 MB, one browser page ~900 MB) |
| `ALBERT_DEFAULT_DISK_GB` | `10` | Global per-sandbox disk size |
| `ALBERT_INACTIVITY_SECONDS` | `600` | Stop sandboxes after inactivity (data is kept, `0` = off) |
| `ALBERT_MAX_AGE_SECONDS` | `86400` | Delete non-persistent sandboxes after this age (`0` = off) |
| `ALBERT_PORT_RANGE` | `20000-29999` | Host ports for sandbox services (bound to 127.0.0.1) |
| `ALBERT_STORAGE_DIR` | `/var/lib/albert-sandbox` | Disk images and mount points |
| `ALBERT_NETWORK_SUBNET` | `172.30.0.0/16` | Subnet of the isolated sandbox network (must not overlap your LAN) |
| `ALBERT_OFFLINE_SUBNET` | `172.31.0.0/16` | Subnet of the network for clients without internet access (created on first use; must not overlap your LAN) |
| `ALBERT_OFFLINE_DNS` | `127.0.0.254` | DNS server given to sandboxes without internet access – an address nothing answers on, so name lookups fail at once |
| `ALBERT_PROFILE_BUILD_TIMEOUT` | `3600` | Maximum duration of a profile image build in seconds |
| `ALBERT_BLOCK_NETS` | private ranges, CGNAT, link-local, `198.18.0.0/15` | Destinations sandboxes must not reach |
| `ALBERT_ALLOW_NETS` | – | Exceptions from `ALBERT_BLOCK_NETS` (e.g. an internal test system) |
| `ALBERT_BLOCK_HOST_ACCESS` | `1` | Block connections from sandboxes to services on the host (ssh, nginx, …) |

## Clients, Limits and Quotas
A client is an API key plus settings:

- **Limit per sandbox** (CPU cores, RAM, disk): what each sandbox of the client gets. Empty values fall back to the global defaults above. Through the API a sandbox may request *less*, never more. Admins can change the limits of a single sandbox later (CPU/RAM live, disk can only grow).
- **Overall quota** (empty = unlimited):
  - CPU cores and RAM count the limits of the client's **running** sandboxes, i.e. what is used at the same time. Starting or creating a sandbox beyond the quota is refused with HTTP 409 `quota_exceeded`.
  - Disk counts the configured size of **all** sandboxes of the client, running or stopped.
  - Maximum number of sandboxes (running or stopped).
  - Maximum number of sandboxes **running at the same time** ("davon laufend"). Creating or starting one more is refused with HTTP 409 `running_limit_exceeded`; the message names the running sandboxes so the agent can stop one first. `0` = the client may not run sandboxes. Lowering the limit stops nothing; it applies to the next start.
- **Persistent sandboxes** ("Persistente Sandboxen erlauben"): only clients with this permission may create persistent sandboxes or switch one to persistent; otherwise the request is refused with HTTP 403 `persistent_not_allowed`. New clients start without it. Clients that existed before the update keep it, so nothing changes for them until an admin switches it off. Withdrawing the permission leaves existing persistent sandboxes as they are (they can still be switched off, not on again).
- **Software profile** ("Software-Profil", see below): which image new sandboxes of the client are created from. Default: the shipped image.
- **Internet access** ("Internetzugriff", default on): without it the client's sandboxes cannot open any connection to the outside and cannot resolve names; only the internal exceptions in `ALBERT_ALLOW_NETS` stay reachable. ALBERT | AI still reaches the sandbox (desktop, MCP Hub, file service), because those connections come in through nginx. Switching the setting applies at once to all existing sandboxes of the client: they are moved into the other network and rebuilt (data, ports and credentials are kept; running ones are briefly unreachable). The client API reports the setting (`internetAccess` in `/quota`, `internet` in the sandbox object) so the agent knows it cannot download anything.

Manage clients in the admin UI or on the host:
```bash
albert-ai-sandbox-manager client create --label "Customer A" --default-cpus 2 --default-memory-mb 2048 \
    --quota-cpus 10 --quota-memory-mb 20480 --quota-disk-gb 200 --quota-running 2 --allow-persistent no
albert-ai-sandbox-manager client list
albert-ai-sandbox-manager client set 3 --quota-cpus none      # none = unlimited / global default
albert-ai-sandbox-manager client set 3 --profile "Office" --internet no
albert-ai-sandbox-manager client rotate 3                       # new API key, sandboxes stay
albert-ai-sandbox-manager client delete 3                       # deletes all its sandboxes too
```
The plaintext key is shown once; only its SHA-256 hash is stored. `scripts/api-key-manager.sh create|list|revoke` still works as before.

### How the limits are enforced
- CPU and RAM: Docker cgroup limits (`--cpus`, `--memory` without extra swap) plus a process limit (`--pids-limit`).
- Disk: every sandbox gets its own sparse ext4 image file of the configured size, loop-mounted on the host and mounted into the container as `/home/ubuntu` and `/tmp/albert-files` (uploads). When it is full, writes inside the sandbox fail; the host disk cannot fill up. Files deleted inside the sandbox give space back to the host. The container's own layer (system directories) is measured and shown in the admin UI.
- Sandboxes migrated from the former version keep their Docker volume and have no disk limit (shown as "ohne Limit"); CPU/RAM limits can be set for them in the admin UI.

## Sandbox Profiles
A profile adds software to the shipped sandbox image, e.g. for customers whose sandboxes have no internet access and therefore cannot install anything at runtime. It consists of:

- **OS packages** (apt), **Python packages** (pip) and **global Node packages** (npm), one per line or separated by spaces, optionally with a version (`tesseract-ocr-deu=4.1.1-2`, `openpyxl==3.1.5`, `@mermaid-js/mermaid-cli@11.4.2`). Names are validated, so nothing can be injected through them.
- optional **setup commands** for anything that is not a package (e.g. adding a vendor's APT repository). They run as root at the end of the build inside the build container – never on the host – with `bash -euo pipefail`.

"Bauen" in the admin UI (Profile) generates a Dockerfile `FROM albert-ai-sandbox:latest` and builds `albert-ai-sandbox-profile-<id>:<n>`; the output is shown live. Builds run one after another as separate jobs (`systemd-run`), so they continue when the manager restarts. A failed build keeps the previous image in use. The host needs internet access (package mirrors) while building; the sandboxes do not.

Assign a profile to a client ("Software-Profil"); its new sandboxes are created from the profile image. A client whose profile was never built gets HTTP 503 `profile_not_ready`. Existing sandboxes are marked outdated after a profile change or a new build and are switched with "Neu aufbauen" / `recreate --outdated` (data is kept).

When `install.sh` rebuilds the shipped image, it rebuilds all profiles built on the previous version automatically. Profile images no longer used by a profile or a container are removed by the cleanup. Profiles that are still assigned to a client cannot be deleted.

```bash
albert-ai-sandbox-manager profile create --name "Office" --apt "libreoffice tesseract-ocr-deu" --pip "pandas"
albert-ai-sandbox-manager profile build Office          # shows the build output; --background to detach
albert-ai-sandbox-manager profile list | show <id|name> | log <id|name> | set | delete
```

## Network Isolation and Hardening
Sandboxes run code an AI agent controls, so they are isolated from everything but the internet:

- **Own network without inter-container traffic**: all sandboxes live in the Docker network `albert-sandbox` (`enable_icc=false`). A sandbox cannot connect to another sandbox — not to its MCP Hub, file service or desktop.
- **No internal networks**: iptables rules (chain `ALBERT-SANDBOX` in `DOCKER-USER`) drop connections from sandboxes to `ALBERT_BLOCK_NETS` — RFC 1918 networks, CGNAT, link-local including cloud metadata (169.254.169.254). Needed internal targets can be allowed with `ALBERT_ALLOW_NETS`.
- **No host services**: chain `ALBERT-SANDBOX-IN` (in `INPUT`) drops connections from sandboxes to the host itself (ssh, nginx, …). nginx still reaches the sandboxes, so desktop, MCP Hub and file service work as before.
- **Clients without internet access**: their sandboxes live in the second network `albert-sandbox-offline` (`ALBERT_OFFLINE_SUBNET`, created on first use). The firewall drops every connection from it except to `ALBERT_ALLOW_NETS`. The containers also get a DNS server nothing answers on (`ALBERT_OFFLINE_DNS`): Docker's embedded DNS resolves names from the host's network namespace and would otherwise bypass the firewall.
- The rules are applied at boot before Docker starts (`albert-sandbox-firewall.service`), before every create/start, and checked by the watcher every minute; missing or outdated rules are restored automatically. A sandbox is never created or started without them. Status: `albert-ai-sandbox-manager firewall status`, also shown in the admin UI (Übersicht → Netz-Isolation).
- **Container hardening**: no `CAP_SYS_ADMIN`, Docker's default seccomp profile, no `NET_RAW`/`MKNOD`, a real init process (`--init`, fast clean stops), process limit. Chrome runs with `--no-sandbox`, so it needs no extra privileges.
- **Existing sandboxes**: containers in the old shared network are moved into the isolated network automatically (live, without restart). Hardening, the internal tokens and a new image need a rebuild: `albert-ai-sandbox-manager recreate --outdated`, or "Neu aufbauen" in the admin UI. The rebuild keeps home directory, uploads, ports, URLs and limits and renews VNC password and internal tokens (values that ever showed up in chats or logs become worthless); if it fails, the previous image is restored.

## Access to Sandboxes
Sandboxes run code an AI controls; whoever reaches a sandbox's shell reaches its data. Therefore:

- **API key for everything**: MCP Hub and file service need `Authorization: Bearer <API key of the client>` like the REST API. nginx asks the manager before every request (`auth_request`) and forwards it with the sandbox's internal token; MCP Hub and file service check that token as well. Rotating or deleting the key in the admin UI closes MCP and files at once.
- **User binding**: ALBERT | AI sends `X-Albert-User` (pseudonymous id of the user) with every request. A sandbox belongs to the user that created it; other users of the same client neither see nor reach it.
- **Desktop with one-time links**: `POST /containers/<name>/desktop` returns a link that works once within two minutes and sets a desktop cookie for that sandbox only. The plain URL, a used or an expired link shows "link expired". The VNC password travels only in the URL fragment (never logged).
- **No secrets in API answers**: VNC password and internal tokens are no longer returned.
- **Admin UI on its own origin** (own port): pages served from sandboxes can never act in it; the admin cookie is limited to `/manager/`; nginx strips cookies before forwarding to a container; files from a sandbox are delivered with `Content-Security-Policy: sandbox`; the noVNC client files come from the host, not from the container.
- **Legacy access** ("Alte Anbindung erlauben", per client): keeps ALBERT | AI versions without this update working (MCP with the fixed key, files without key, plain desktop URL, no user binding). On for clients that existed before the update, off for new clients. The overview warns while a client still has it. Outdated sandboxes are marked "veraltet" in the admin UI and listed after every update by `install.sh`.

## Sandbox Image
`docker/Dockerfile` pins everything installed on top of Ubuntu (Node.js major line, MCP Hub, Playwright MCP, bash-mcp, Flask) via build arguments at the top of the file. MCP servers are installed at build time — nothing is downloaded when a sandbox starts. OS packages, Google Chrome and Firefox (from Mozilla's APT repository) are taken from their vendors with security updates. To update a component, change its version argument, run `install.sh` (rebuilds when `docker/` changed), test, then rebuild existing sandboxes with `recreate --outdated`.

## Admin Web UI
Open `https://<host>:8443/manager/admin/` (`/manager/admin/` on the main port redirects there) and sign in. Pages:
- **Übersicht**: host resources, allocated CPU/RAM of running sandboxes, disk usage.
- **Clients**: create (the API key is shown once), edit per-sandbox limits, quotas, running limit, the persistence permission, software profile, internet access and legacy access, rotate the key, delete a client with all its sandboxes; per client all sandboxes with status, user, limits, disk usage, last activity and actions (start, stop, restart, change limits, live values, delete). The sandbox name opens the desktop with a one-time link.
- **Sandboxen**: all sandboxes across clients.
- **Profile**: sandbox profiles with their packages, build status, build log and the clients using them.
- **Admins**: add admin users, change passwords, delete admins.
- **Wartung**: preview and run the cleanup (reconcile), audit log of all changes.
- **Einstellungen**: HTTPS certificate (Let's Encrypt or own certificate, expiry, renewal) and the addresses allowed to open the admin UI (empty = all; loopback for an SSH tunnel always works; your own address must be in the list).

Admin users on the host: `albert-ai-sandbox-manager admin add|list|passwd|remove <username>`.

## Manage Sandboxes from the Command Line
`albert-ai-sandbox-manager` (in `/usr/local/bin`) keeps the commands of the former Bash manager. With `--api-key <PLAINTEXT>` or `--api-key-hash <HASH>` it acts as that client; without a key it works in operator mode on all sandboxes (`create` always needs a key).
- `create [name] [--persistent] [--cpus N] [--memory-mb N] [--disk-gb N]`
- `list` / `status [name]`, `start`, `stop`, `restart`, `remove <name>` (always deletes the data)
- `resources <name> --cpus N --memory-mb N --disk-gb N`, `persistent <name> on|off`
- `recreate <name>` / `recreate --outdated` – rebuild with current image and hardening, data is kept
- `firewall apply|status|remove` – network isolation
- `profile list|show|create|set|delete|build|log|rebuild-outdated` – sandbox profiles
- `desktop <name>` – one-time link to the desktop
- `tls status|letsencrypt|custom|disable` – HTTPS certificate; `admin-access show|set <addr…>|clear` – addresses allowed for the admin UI; `site apply|show` – nginx site
- `client set <id> --legacy-access yes|no` – former access without API key / user binding
- `reconcile [--dry-run]` – clean up leftovers, `selfcheck`, `build` – rebuild the image

Add `--json` for machine-readable output.

## Manage Sandboxes over REST
Integrators talk to the manager through nginx (`https://<host>/manager/`) or locally (`http://127.0.0.1:5001`) with Bearer authentication. See [SERVICES.md](SERVICES.md) for all endpoints.
```bash
curl -X POST -H "Authorization: Bearer $ALBERT_API_KEY" -H "X-Albert-User: $USER_PSEUDONYM" \
     -H "Content-Type: application/json" \
     -d '{"name": "agent-lab-01", "resources": {"cpus": 1, "memoryMb": 2048}}' \
     https://<host>/manager/containers
```
Responses include the MCP Hub endpoint, the file service URLs, limits and usage; `POST /containers/<name>/desktop` returns a one-time desktop link.

## File Transfer Services
Each sandbox exposes a lightweight file bridge so agents can move artifacts in and out of the container. The service sits behind nginx and is namespaced per sandbox.

```bash
H=(-H "Authorization: Bearer $ALBERT_API_KEY" -H "X-Albert-User: $USER_PSEUDONYM")
curl "${H[@]}" -X POST -F "file=@model-output.json" https://<host>/<sandbox>/files/upload
curl "${H[@]}" "https://<host>/<sandbox>/files/download?path=/tmp/albert-files/<uuid.ext>" --output ./downloaded.json
```
Uploads land in `/tmp/albert-files` inside the sandbox (owned by the desktop user, so the AI writes there without sudo) and count towards the sandbox disk limit. Without the client's API key (and the right user) the file service answers `401`/`403`, except for clients with legacy access.

## Container Lifecycle & Automatic Cleanup
The watcher (`albert-inactivity-watcher.timer`, every minute):

| Step | Default | Effect |
|---|---|---|
| Inactivity stop | 10 minutes | Stops idle sandboxes (data is kept) |
| Max age removal | 24 hours | Deletes non-persistent sandboxes **including data** |
| Reconcile | always | Removes containers, disk images, volumes and nginx routes without a sandbox; finishes interrupted removals; rewrites missing routes; mounts disks |

Every removal deletes the container, its disk image (or legacy volume), its nginx route and its database row. If a step fails, the sandbox stays in state `removing` and the next watcher run finishes the job. A failed creation is rolled back completely.

Persistent sandboxes are excluded from max-age removal (`"persistent": true` at creation, `PATCH /containers/<name>/persistent`, CLI `--persistent`, or in the admin UI) — only for clients that are allowed to have them.

Opening the desktop of a stopped sandbox shows a short explanation page (HTTP 503, reloads every 30 seconds) instead of "502 Bad Gateway"; an expired or used desktop link shows "link expired" (HTTP 401).

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
Removes all sandboxes including their data, profile images, the manager, its systemd units and nginx routes. Docker, nginx, packages and the sandbox image are kept.

## Enterprise Integration
A ready-to-use integration of ALBERT | AI Sandbox is available inside ALBERT | AI from agorum Software GmbH. Learn more at https://www.agorum.com.
