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
- **Network isolation**: sandboxes reach the internet, but neither each other nor internal networks nor services of the host. **Internet access can be switched off per client**; internal targets (e.g. an agorum core system) can be opened for all sandboxes or per client.
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

### Before you start
- **Dedicated host**: Debian 12, root access, internet access (packages, Docker, the sandbox image; later also for image updates and software profiles). The installer disables the Debian default nginx site and serves every host name itself, so the host should not serve anything else on ports 80/443.
- **Ports** (host firewall and, in a cloud, the security group):
  | Port | Purpose |
  |---|---|
  | 80 | HTTP: Let's Encrypt challenges, redirect to HTTPS (with HTTPS active it refuses API, MCP and file requests) |
  | 443 | HTTPS: client API, MCP Hub, file service and desktops of the sandboxes |
  | 8443 | Admin UI (`ALBERT_ADMIN_PORT`) – open it only for your admin networks |
- **Subnets**: the sandbox networks `172.30.0.0/16`, `172.31.0.0/16` and the pool for client networks `172.29.0.0/16` must not overlap your networks (`ALBERT_NETWORK_SUBNET`, `ALBERT_OFFLINE_SUBNET`, `ALBERT_CLIENT_SUBNET_POOL`).
- **Internal targets**: sandboxes cannot reach private networks. If they must reach internal systems (e.g. an agorum core server), open them in the admin UI – for all sandboxes under *Einstellungen*, for one client in its settings (see "Internal targets" below).
- **Proxy or load balancer in front**: let nginx on this host terminate TLS (the certificate is managed in the admin UI) and pass on the real client address (nginx `real_ip`); otherwise the admin address list and the login throttle only see the proxy.

### New installation
1. Install:
   ```bash
   git clone <repository-url> albert-ai-sandbox
   cd albert-ai-sandbox
   sudo bash install.sh
   ```
   The installer provisions all required packages, builds the sandbox image, installs the manager under `/opt/albert-ai-sandbox-manager`, writes the nginx site `/etc/nginx/sites-available/albert-sandbox` (main server on 80/443, admin UI on its own port), sets up the systemd units and creates the first admin user `admin`. **Its password is printed only once – store it.**
2. Optional host hardening (ufw, fail2ban, SSH): `sudo bash scripts/setup-tls.sh --fqdn sandbox.example.com --email admin@example.com`.
3. Open the admin UI at `http://<host>:8443/manager/admin/` (or through an SSH tunnel to `127.0.0.1:8443`), sign in and change the password (*Admins*).
4. *Einstellungen*:
   - **HTTPS**: request a Let's Encrypt certificate (the DNS name must point to the host, port 80 must be reachable from the internet) or upload an own certificate (PEM certificate, intermediates, private key without passphrase). Each change is tested with `nginx -t`; if nginx refuses it, the previous configuration stays active (CLI: `albert-ai-sandbox-manager tls status|letsencrypt|custom|disable`). Afterwards the admin UI moves to `https://<host>:8443/manager/admin/`.
   - **Allowed addresses** of the admin UI (your own address must be in the list; `127.0.0.1` for an SSH tunnel always works).
   - **Internal targets for all sandboxes**, if any.
5. *Clients*: create one client per agorum core system. **The API key is shown only once.** Set limits, quotas, internet access, internal targets of this client and software profile as needed. Leave *Alte Anbindung erlauben* off – switch it on only for an ALBERT | AI without the security update (see below).
6. In ALBERT | AI, tool configuration `sandbox_manage`: server URL `https://<host>` and the client's API key.
7. Check: `albert-ai-sandbox-manager selfcheck`; optionally `MANAGER_URL=https://<host>/manager python3 scripts/smoke_test_manager.py --key <API key>` (creates and removes a test sandbox).

### Updating an existing host
1. **Coming from a version without network isolation**: clarify first whether sandboxes need internal targets (see above) – the isolation blocks them otherwise; set them right after the update (admin UI or `ALBERT_ALLOW_NETS`). It is the one point where an update can break something.
2. Pull the new version and run `sudo bash install.sh` again. Running sandboxes keep running; only the manager service restarts briefly. The sandbox image is rebuilt when `docker/` changed (takes a few minutes; `--skip-build` / `--force-build` override this). The database schema is migrated automatically; installations of the former Bash version (JSON registry) are migrated as well: existing sandboxes, their data volumes and ports are taken over, API keys stay valid. A certificate set up by an older `setup-tls.sh` is taken over. Replaced configuration is backed up to `/opt/albert-ai-sandbox-manager/backups/<timestamp>/`.
3. **Existing clients keep the former access** (*Alte Anbindung erlauben* is switched on for them), so ALBERT | AI versions without the security update keep working.
4. The admin UI moves to port 8443: open the port (restricted to admin networks); `/manager/admin/` on the main port redirects there.
5. **Rebuild the existing sandboxes** (preferably outside working hours): `albert-ai-sandbox-manager recreate --outdated`, or "Neu aufbauen" per sandbox in the admin UI. Until then they are marked *veraltet* but keep working. The rebuild keeps home directory, uploads, ports and limits, renews VNC password and internal tokens (open desktops have to be opened again) and briefly interrupts running sandboxes. Sandboxes migrated from the Bash version keep their Docker volume (no disk limit). Everything installed outside the home directory (e.g. with `apt` or a system-wide `pip`) is gone after a rebuild – use a software profile for software every sandbox of a client needs. **Since the move to Ubuntu 24.04** (Python 3.12): Python packages a user installed into the home directory (`pip install --user`, `~/.local/lib/python3.10`) are not found any more and have to be installed again.
6. Set up HTTPS in the admin UI (if not done yet). **Then switch the server URL in every connected ALBERT | AI to `https://`** – over HTTP the host refuses API, MCP and file requests (`https_required`).
7. Once an ALBERT | AI has the security update, switch off *Alte Anbindung erlauben* for its client – **but only after its users have used their sandboxes once** (see "Sandboxes from before the update" below).

### ALBERT | AI and sandbox server versions
Sandbox server and ALBERT | AI can be updated in any order:

| Sandbox server | ALBERT | Result |
|---|---|---|
| new | without security update | Works while the client has *Alte Anbindung erlauben* (MCP with the former fixed key, files without key, plain desktop URL, no user binding). Existing clients have it after the update; **new clients do not** – switch it on when creating a client for such an ALBERT. |
| old | with security update | Works: ALBERT recognises the old server (no `api` in `GET /manager/health`) and uses the former interface – without the new protection. |
| new | with security update | Full protection once *Alte Anbindung erlauben* is off: API key and user binding for MCP and files, one-time desktop links, no secrets in API answers. |

**Sandboxes from before the update** belong to no user yet. The first ALBERT user who addresses one after the ALBERT update takes it over (ALBERT does this once with the names it knew before) – but only **while the client still has *Alte Anbindung erlauben***. Sandboxes not taken over by then can no longer be reached through ALBERT afterwards; an administrator can only delete them. Non-persistent sandboxes are removed after 24 hours anyway; for persistent ones, switch the former access off only after their users have used them once.

### Things to keep in mind
- Certificates: Let's Encrypt renews itself (certbot timer); an own certificate must be replaced before it expires (the admin UI warns 14 days ahead).
- Back up `/opt/albert-ai-sandbox-manager/data/manager.db` (clients, sandbox owners, profiles, admin users) – it is the single source of truth.
- Sizing (measured on a host with 2 cores and 8 GB RAM): an idle sandbox needs about 440 MB RAM, a browser page about 900 MB more; creating one takes about 14 s (6 at once: about 20 s each). The RAM limit of a sandbox is a ceiling, not a reservation. The **memory guard** keeps the host from promising too much: the RAM limits of all running sandboxes may add up to `ALBERT_MEMORY_OVERCOMMIT` (default 2) × (host RAM − `ALBERT_MEMORY_RESERVE_MB`, default 1 GB) – on 8 GB that is about 13.8 GB, i.e. 6 sandboxes of 2 GB. Beyond that, and whenever less than the reserve is free, creating or starting a sandbox is refused with `503 host_full` (the message names the user's own running sandboxes, so the agent can stop one). The overview of the admin UI shows the promised RAM. Raise the factor for hosts whose sandboxes mostly idle; if memory runs out anyway, the kernel ends processes in sandboxes first (manager and nginx are protected).
- Rate limits of the client API (`/manager/`): `ALBERT_API_RATE_PER_KEY` (default 20 requests per second per API key – all users of one ALBERT | AI share it) and `ALBERT_API_RATE_PER_IP` (default 40 per address, against floods with wrong keys); bursts of 5 seconds' worth are allowed, then `429 rate_limited` with `Retry-After: 1`. MCP Hub, file service and desktop are not counted. Measured: the manager answers about 70 list/status requests per second on 2 cores. After changing a limit run `albert-ai-sandbox-manager site apply`.
- Clients created later start without the former access; an ALBERT without the security update gets `400 user_required` / `401` until it is switched on.

## Configuration
`/opt/albert-ai-sandbox-manager/config/albert.env` (KEY=VALUE, never overwritten by updates) is read by the manager service, the watcher and the CLI. Restart the manager after changes (`systemctl restart albert-container-manager`).

| Variable | Default | Meaning |
|---|---|---|
| `ALBERT_PUBLIC_URL` | `http://<primary IP>` | Base URL used in API responses while HTTPS is off (with HTTPS: `https://<host name of the certificate>`) |
| `ALBERT_ADMIN_PORT` | `8443` | Port of the admin UI (own origin; HTTPS once a certificate is active) |
| `ALBERT_API_RATE_PER_KEY` / `ALBERT_API_RATE_PER_IP` | `20` / `40` | Requests per second to the client API per API key / per address (bursts of 5 seconds' worth; `0` = no limit; apply with `site apply`) |
| `ALBERT_MEMORY_OVERCOMMIT` | `2` | Memory guard: RAM limits of running sandboxes may add up to this factor × (host RAM − reserve); `0` = off |
| `ALBERT_MEMORY_RESERVE_MB` | `1024` | Memory guard: RAM kept for the host; no create/start while less than this is free |
| `ALBERT_DESKTOP_LINK_SECONDS` | `120` | Validity of a one-time desktop link |
| `ALBERT_DESKTOP_SESSION_HOURS` | `8` | Validity of the desktop session (cookie) a link opens |
| `ALBERT_NGINX_SITE_FILE` / `ALBERT_NGINX_SITE_LINK` | `/etc/nginx/sites-available/albert-sandbox` / `/etc/nginx/sites-enabled/albert-sandbox` | nginx site written by the manager (main server, HTTPS, admin port) |
| `ALBERT_NOVNC_DIR` | `<install dir>/novnc` | noVNC client files served by nginx (copied from the sandbox image by `install.sh`) |
| `ALBERT_ACME_DIR` | `/var/www/albert-acme` | Webroot for Let's Encrypt HTTP-01 challenges |
| `ALBERT_TLS_DIR` | `/etc/albert-sandbox/tls` | Uploaded certificates and keys (root only) |
| `ALBERT_LETSENCRYPT_DIR` / `ALBERT_CERTBOT` | `/etc/letsencrypt` / `certbot` | Let's Encrypt directory and certbot binary |
| `ALBERT_DEFAULT_CPUS` | `1` | Global per-sandbox CPU limit (if the client has none) |
| `ALBERT_DEFAULT_MEMORY_MB` | `2048` | Global per-sandbox RAM limit (an idle desktop needs ~450 MB, one browser page ~900 MB) |
| `ALBERT_DEFAULT_DISK_GB` | `10` | Global per-sandbox disk size |
| `ALBERT_INACTIVITY_SECONDS` | `600` | Stop sandboxes after inactivity (data is kept, `0` = off) |
| `ALBERT_MAX_AGE_SECONDS` | `86400` | Delete non-persistent sandboxes after this age (`0` = off) |
| `ALBERT_PORT_RANGE` | `20000-29999` | Host ports for sandbox services (bound to 127.0.0.1) |
| `ALBERT_STORAGE_DIR` | `/var/lib/albert-sandbox` | Disk images and mount points |
| `ALBERT_NETWORK_SUBNET` | `172.30.0.0/16` | Subnet of the isolated sandbox network (must not overlap your LAN) |
| `ALBERT_OFFLINE_SUBNET` | `172.31.0.0/16` | Subnet of the network for clients without internet access (created on first use; must not overlap your LAN) |
| `ALBERT_CLIENT_SUBNET_POOL` | `172.29.0.0/16` | Subnets of the own networks of clients with own internal targets (must not overlap your LAN) |
| `ALBERT_CLIENT_SUBNET_PREFIX` | `24` | Size of one client network (`/24` = 253 sandboxes, 256 clients in the default pool) |
| `ALBERT_OFFLINE_DNS` | `127.0.0.254` | DNS server given to sandboxes without internet access – an address nothing answers on, so name lookups fail at once |
| `ALBERT_PROFILE_BUILD_TIMEOUT` | `3600` | Maximum duration of a profile image build in seconds |
| `ALBERT_BLOCK_NETS` | private ranges, CGNAT, link-local, `198.18.0.0/15` | Destinations sandboxes must not reach |
| `ALBERT_ALLOW_NETS` | – | Internal targets of all sandboxes (exceptions from `ALBERT_BLOCK_NETS`) – only until they are saved in the admin UI (*Einstellungen*), which then applies instead |
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
- **Internet access** ("Internetzugriff", default on): without it the client's sandboxes cannot open any connection to the outside and cannot resolve names; only the internal targets (global and the client's own) stay reachable. ALBERT | AI still reaches the sandbox (desktop, MCP Hub, file service), because those connections come in through nginx. Switching the setting applies at once to all existing sandboxes of the client: they are moved into the other network and rebuilt (data, ports and credentials are kept; running ones are briefly unreachable). The client API reports the setting (`internetAccess` in `/quota`, `internet` in the sandbox object) so the agent knows it cannot download anything.
- **Internal targets of the client** ("Interne Ziele dieses Clients"): IPv4 addresses or networks its sandboxes may reach in addition to the targets for all sandboxes, see below.

Manage clients in the admin UI or on the host:
```bash
albert-ai-sandbox-manager client create --label "Customer A" --default-cpus 2 --default-memory-mb 2048 \
    --quota-cpus 10 --quota-memory-mb 20480 --quota-disk-gb 200 --quota-running 2 --allow-persistent no
albert-ai-sandbox-manager client list
albert-ai-sandbox-manager client set 3 --quota-cpus none      # none = unlimited / global default
albert-ai-sandbox-manager client set 3 --profile "Office" --internet no
albert-ai-sandbox-manager client set 3 --allow-nets 10.0.5.20,10.0.6.0/24   # none = no own targets
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
- **No internal networks**: iptables rules (chain `ALBERT-SANDBOX` in `DOCKER-USER`) drop connections from sandboxes to `ALBERT_BLOCK_NETS` — RFC 1918 networks, CGNAT, link-local including cloud metadata (169.254.169.254). Needed internal targets can be allowed (see below).
- **No host services**: chain `ALBERT-SANDBOX-IN` (in `INPUT`) drops connections from sandboxes to the host itself (ssh, nginx, …). nginx still reaches the sandboxes, so desktop, MCP Hub and file service work as before.
- **Clients without internet access**: their sandboxes live in the second network `albert-sandbox-offline` (`ALBERT_OFFLINE_SUBNET`, created on first use). The firewall drops every connection from it except to the internal targets. The containers also get a DNS server nothing answers on (`ALBERT_OFFLINE_DNS`): Docker's embedded DNS resolves names from the host's network namespace and would otherwise bypass the firewall.
- The rules are applied at boot before Docker starts (`albert-sandbox-firewall.service`), before every create/start, when an internal target changes, and checked by the watcher every minute; missing or outdated rules are restored automatically. A sandbox is never created or started without them. Status: `albert-ai-sandbox-manager firewall status`, also shown in the admin UI (Übersicht → Netz-Isolation).
- **Container hardening**: no `CAP_SYS_ADMIN`, Docker's default seccomp profile, no `NET_RAW`/`MKNOD`, a real init process (`--init`, fast clean stops), process limit. Chrome runs with `--no-sandbox`, so it needs no extra privileges.
- **Existing sandboxes**: containers in the old shared network are moved into the isolated network automatically (live, without restart). Hardening, the internal tokens and a new image need a rebuild: `albert-ai-sandbox-manager recreate --outdated`, or "Neu aufbauen" in the admin UI. The rebuild keeps home directory, uploads, ports, URLs and limits and renews VNC password and internal tokens (values that ever showed up in chats or logs become worthless); if it fails, the previous image is restored.

### Internal targets
By default no sandbox reaches an internal network. Exceptions:

- **For all sandboxes**: admin UI → *Einstellungen* → "Interne Ziele für alle Sandboxen", or `albert-ai-sandbox-manager allow-nets set 10.0.5.20 10.0.6.0/24`. Until something is saved there, `ALBERT_ALLOW_NETS` from `albert.env` applies; `allow-nets reset` (or "Wert aus albert.env verwenden") goes back to it.
- **Per client**, in addition: client settings → "Interne Ziele dieses Clients", or `client set <id> --allow-nets …`. Typical: the agorum core system of exactly this customer.

Entries are IPv4 addresses or networks (CIDR); `0.0.0.0/0` and the sandbox networks themselves are refused. They apply at once, also to running sandboxes and also to clients without internet access (for them they are the only reachable destinations).

The firewall separates sandboxes by subnet (container addresses change on every start), so a client gets **an own network** the first time it gets own targets: `albert-sandbox-c<id>` with a subnet from `ALBERT_CLIENT_SUBNET_POOL` (`172.29.0.0/16`, one `/24` each), isolated like the shared networks. Its existing sandboxes are moved into it at once without a rebuild (running ones lose their connections for a moment). The client keeps the network when its own targets are removed again; it is removed when the client is deleted. A subnet of the pool the rules do not know gets no connection at all, and if the database cannot be read the rules allow no internal target (the watcher repairs that within a minute).

## Access to Sandboxes
Sandboxes run code an AI controls; whoever reaches a sandbox's shell reaches its data. Therefore:

- **API key for everything**: MCP Hub and file service need `Authorization: Bearer <API key of the client>` like the REST API. nginx asks the manager before every request (`auth_request`) and forwards it with the sandbox's internal token; MCP Hub and file service check that token as well. Rotating or deleting the key in the admin UI closes MCP and files at once.
- **User binding**: ALBERT | AI sends `X-Albert-User` (pseudonymous id of the user) with every request. A sandbox belongs to the user that created it; other users of the same client neither see nor reach it.
- **Desktop with one-time links**: `POST /containers/<name>/desktop` returns a link that works once within two minutes and sets a desktop cookie for that sandbox only. The plain URL, a used or an expired link shows "link expired". The VNC password travels only in the URL fragment (never logged).
- **No secrets in API answers**: VNC password and internal tokens are no longer returned.
- **Admin UI on its own origin** (own port): pages served from sandboxes can never act in it; the admin cookie is limited to `/manager/`; nginx strips cookies before forwarding to a container; files from a sandbox are delivered with `Content-Security-Policy: sandbox`; the noVNC client files come from the host, not from the container.
- **Legacy access** ("Alte Anbindung erlauben", per client): keeps ALBERT | AI versions without this update working (MCP with the fixed key, files without key, plain desktop URL, no user binding). On for clients that existed before the update, off for new clients. The overview warns while a client still has it. Outdated sandboxes are marked "veraltet" in the admin UI and listed after every update by `install.sh`.

## Sandbox Image
The sandbox image is based on **Ubuntu 24.04** (desktop user `ubuntu`, UID 1000, passwordless `sudo`; `pip` installs system-wide as on 22.04 via `/etc/pip.conf`, the file service runs in its own virtual environment). `docker/Dockerfile` pins everything installed on top of Ubuntu (Node.js major line, MCP Hub, Playwright MCP, bash-mcp, Flask) via build arguments at the top of the file. MCP servers are installed at build time — nothing is downloaded when a sandbox starts. OS packages, Google Chrome and Firefox (from Mozilla's APT repository) are taken from their vendors with security updates. To update a component, change its version argument, run `install.sh` (rebuilds when `docker/` changed), test, then rebuild existing sandboxes with `recreate --outdated`.

## Admin Web UI
Open `https://<host>:8443/manager/admin/` (`/manager/admin/` on the main port redirects there) and sign in. Pages:
- **Übersicht**: host resources, allocated CPU/RAM of running sandboxes, disk usage.
- **Clients**: create (the API key is shown once), edit per-sandbox limits, quotas, running limit, the persistence permission, software profile, internet access, internal targets and legacy access, rotate the key, delete a client with all its sandboxes; per client all sandboxes with status, user, limits, disk usage, last activity and actions (start, stop, restart, change limits, live values, delete). The sandbox name opens the desktop with a one-time link.
- **Sandboxen**: all sandboxes across clients.
- **Profile**: sandbox profiles with their packages, build status, build log and the clients using them.
- **Admins**: add admin users, change passwords, delete admins.
- **Wartung**: preview and run the cleanup (reconcile), audit log of all changes.
- **Einstellungen**: HTTPS certificate (Let's Encrypt or own certificate, expiry, renewal), the addresses allowed to open the admin UI (empty = all; loopback for an SSH tunnel always works; your own address must be in the list) and the internal targets of all sandboxes.

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
- `allow-nets show|set <net…>|reset` – internal targets of all sandboxes; `client set <id> --allow-nets <net,…>|none` – of one client
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
