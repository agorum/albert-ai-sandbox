# ALBERT | AI Sandbox

ALBERT | AI Sandbox provides isolated, browser-ready compute environments for AI agents such as ALBERT | AI. Each sandbox is created on demand through a REST service, launched as a Docker container, and ships with everything an autonomous agent needs: a full desktop exposed over noVNC, an MC Hub instance with Playwright for automated browser control, and a shell execution service for running scripts or Python code. When human intervention is required, the same desktop can be opened in any modern browser so credentials or multi-factor prompts can be handled interactively.

## Key Capabilities
- Spin up dedicated desktop sandboxes per API request, preloaded with browser automation and shell tooling.
- Share desktops securely over noVNC so users can supervise or intervene without local software.
- Run MCHub with Playwright inside every container to orchestrate browser sessions for the AI agent.
- Execute shell commands, scripts, or notebooks inside the container through the embedded shell service.
- Auto-stop containers after 10 minutes of inactivity to conserve capacity.
- Keep data and lifecycle operations isolated per API key, ensuring one tenant cannot touch another tenant's sandboxes.

## System Requirements
- Host OS: Debian 12 (bookworm) verified; comparable Debian-based systems should work with equivalent packages.
- Root access to run the installer, manage services, and configure nginx.
- Docker Engine and systemd available on the host.
- Internet connectivity during installation to fetch Docker components and Python packages.

## Installation
1. Sign in to a Debian 12 host with root privileges.
2. Clone the repository and run the installer:
   ```bash
   git clone <repository-url> albert-ai-sandbox
   cd albert-ai-sandbox
   sudo bash install.sh
   ```
3. The installer provisions all required packages, builds the desktop container image, registers the manager service, and places helper scripts under `/opt/albert-ai-sandbox-manager` with convenient symlinks in `/usr/local/bin`.
4. After installation the `albert-container-manager` systemd service runs on port `5001`, ready to accept API calls.

## Provision API Keys
API keys gate access to everything: container creation, lifecycle control, and file exchange. Use the bundled helper from the installation directory:
```bash
sudo /opt/albert-ai-sandbox-manager/scripts/api-key-manager.sh create --label "My Agent"
```
- `create` prints the plaintext key once; store it securely because only the hash is kept on the host.
- `list` shows existing keys with labels and hash prefixes, helping you audit active tenants.
- `revoke` removes a key and cleans up any sandboxes it owns.

Every container is tagged with the SHA-256 hash of the API key that created it. Command-line and REST actions must present the corresponding plaintext key; operations against other tenants are rejected.

## Manage Sandboxes from the Command Line
The `albert-ai-sandbox-manager` script (installed into `/usr/local/bin`) wraps the REST workflow so you can manage sandboxes locally without writing integration code. Supply either `--api-key <PLAINTEXT>` or a precomputed hash via `--api-key-hash`.

Supported actions include:
- `create [name]` – Provision a sandbox with an optional friendly name; the script returns URLs for the desktop, MC Hub, and file service.
- `list` / `status [name]` – Inspect all sandboxes or a single sandbox for the current API key, including uptime and exposed ports.
- `start`, `stop`, `restart` – Control the lifecycle of an existing sandbox.
- `remove <name>` – Delete the sandbox and its persisted data.
- `build` – Rebuild the Docker image after you modify assets under `/opt/albert-ai-sandbox-manager/docker`.

Example:
```bash
albert-ai-sandbox-manager create --api-key "$ALBERT_API_KEY" --json
albert-ai-sandbox-manager list --api-key "$ALBERT_API_KEY"
```
JSON mode is convenient for programmatic consumption when wiring the manager into CI/CD or orchestration pipelines.

## Manage Sandboxes over REST
Integrators can talk directly to the manager service (default `http://127.0.0.1:5001`) using Bearer authentication with the plaintext API key.

List existing sandboxes:
```bash
curl -H "Authorization: Bearer $ALBERT_API_KEY" \
     http://127.0.0.1:5001/containers
```
Create a sandbox (auto-starts and returns connection details):
```bash
curl -X POST \
     -H "Authorization: Bearer $ALBERT_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"name": "agent-lab-01"}' \
     http://127.0.0.1:5001/containers
```
Stop a sandbox when you are done:
```bash
curl -X POST \
     -H "Authorization: Bearer $ALBERT_API_KEY" \
     http://127.0.0.1:5001/containers/agent-lab-01/stop
```
Additional endpoints let you restart, delete, or retrieve detailed status for a single sandbox. All responses include the noVNC desktop URL, MC Hub endpoint, shell gateway, and idle-timeout metadata so you can embed the sandbox into your own control plane.

## File Transfer Services
Each sandbox exposes a lightweight file bridge so agents can move artifacts in and out of the container. The service sits behind nginx and is automatically namespaced per sandbox.

- Uploads accept multipart form data and return the absolute path inside the container. Agents can then reference that path from shell or Python sessions.
- Downloads stream files back to the caller when provided with a valid path.

Example requests (replace `<sandbox>` with the sandbox name returned during creation):
```bash
curl -X POST \
     -H "Authorization: Bearer $ALBERT_API_KEY" \
     -F "file=@model-output.json" \
     http://<host>/<sandbox>/files/upload

curl -H "Authorization: Bearer $ALBERT_API_KEY" \
     "http://<host>/<sandbox>/files/download?path=/tmp/albert-files/<uuid.ext>" \
     --output ./downloaded.json
```
Uploads and downloads are guarded by the owning API key, so tenants only see their own data.

## Automatic Desktop Access
Every sandbox boots into a full desktop session with the ALBERT toolchain already configured. The installer sets up nginx and noVNC to relay the desktop through `http://<host>/<sandbox>/`, making it easy for operators to open the environment in a browser, complete authentication steps, or monitor an agent live. When activity stops for 10 minutes the container is shut down automatically; the next `start` call resumes the environment.

## Uninstall
To remove the sandbox manager, disable the systemd service, clean up nginx entries, and delete installed files:
```bash
sudo bash uninstall.sh
```
The script leaves Docker images and shared OS packages untouched so you can decide whether to keep them for future installations.

## Enterprise Integration
A ready-to-use integration of ALBERT | AI Sandbox is available inside ALBERT | AI from agorum core. Learn more at https://www.agorum.com.
