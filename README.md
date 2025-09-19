# ALBERT Sandbox Manager

A Docker-based system for managing isolated desktop environments with browser access via noVNC.

## Features

- 🖥️ Ubuntu 22.04 desktop environment
- 🌐 Browser access without client software (noVNC)
- 🔒 Security through cryptic container names (no password needed)
- 🚀 Firefox and Chromium pre-installed
- 💾 Persistent data via Docker volumes
- 🔧 Easy management via CLI
- 📦 Simple file service inside each container (upload/download via REST)

## Installation

```bash
git clone <repository-url> albert-ai-sandbox-manager
cd albert-ai-sandbox-manager
bash install.sh
```

## Usage
```bash
cd /opt/albert-ai-sandbox-manager
./albert-ai-sandbox-manager
```

## File service (upload/download)

Each sandbox also exposes a small REST file service via nginx under the container path:


On upload, the file is stored in `/tmp/albert-files` inside the container using a new UUID and the original file extension. The response contains the full absolute path, e.g.:

```json
{ "path": "/tmp/albert-files/6d6d2d64-7e3a-4b33-9b46-2c5b0f205f3e.pdf" }
```

Download returns the file contents as a stream. If the file path is invalid or missing, you'll receive an error JSON with appropriate status codes.

### Quick test from Windows PowerShell

Replace placeholders: `$host` (your server IP), `$name` (your container name), and the path returned by upload.

```powershell
# Upload a file
$host = "192.168.1.10"
$name = "<your-container-name>"
$file = "C:\\path\\to\\document.pdf"
$response = Invoke-RestMethod -Method Post -Uri "http://$host/$name/files/upload" -Form @{ file = Get-Item $file }
$response

# Download it back
$path = $response.path
Invoke-WebRequest -Uri "http://$host/$name/files/download?path=$([uri]::EscapeDataString($path))" -OutFile "C:\\temp\\downloaded.pdf"
```

## Container Manager REST Service (Host)

In addition to the per-sandbox file service, a host-level management service allows
secure lifecycle control of Docker containers via an authenticated REST API.

### Capabilities

1. Create a new container
2. Stop a container
3. Start a container
4. Restart a container
5. Query container status
6. List containers (owned by the API key)
7. Delete a container (including its data directory)

All operations are isolated per API key. An API key can only manage the containers it created.

### Security Model

- Clients authenticate using a Bearer token: `Authorization: Bearer <API_KEY>`
- Only the SHA256 hash of the key is stored in SQLite (`api_keys.key_hash`)
- Containers are labeled with ownership metadata:
	- `albert.manager=1`
	- `albert.apikey_hash=<sha256>`
- A container request fails with 403 if ownership doesn't match.

### Directory Layout

```
data/
	manager.db                 # SQLite database
	containers/
		<keyhash-prefix>/
			<container-name>/ ...  # Container-specific data directory (created on demand)
```

`<keyhash-prefix>` is the first 12 characters of the API key hash.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MANAGER_PORT` | `5001` | Port for the service |
| `MANAGER_DB_PATH` | `./data/manager.db` | Path to SQLite DB |
| `MANAGER_DATA_DIR` | `./data/containers` | Base directory for per-container data |
| `MANAGER_ALLOWED_IMAGES` | (empty = allow all) | Comma-separated allow-list of images |

### Install Python Dependencies

```powershell
pip install -r requirements.txt
```

### Start the Service (Host)

```powershell
python scripts/container_manager_service.py
```

Service health check:

```powershell
Invoke-RestMethod -Uri "http://localhost:5001/health"
```

### API Key Management (CLI)

Create a key (prints plaintext once):

```powershell
python scripts/api_key_manager.py create --label "Team A"
```

List keys:

```powershell
python scripts/api_key_manager.py list
```

Revoke a key (removes owned containers & data):

```powershell
python scripts/api_key_manager.py revoke --key <PLAINTEXT_KEY>
```

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health (no auth) |
| POST | `/containers` | Create (body requires `image`) |
| GET | `/containers` | List owned containers |
| GET | `/containers/<id>` | Inspect container (name or ID) |
| POST | `/containers/<id>/start` | Start container |
| POST | `/containers/<id>/stop` | Stop (optional `?timeout=10`) |
| POST | `/containers/<id>/restart` | Restart |
| DELETE | `/containers/<id>` | Delete (stops first if running) |

Create request JSON fields:

```json
{
	"image": "python:3.11-slim",      // required
	"name": "optional-name",          // optional (auto-generated if omitted)
	"env": {"FOO": "bar"},            // optional
	"cmd": ["python", "app.py"],       // optional
	"autoStart": true                   // default true
}
```

### Example PowerShell Usage

```powershell
$key = "<PLAINTEXT_API_KEY>"
$headers = @{ Authorization = "Bearer $key"; 'Content-Type' = 'application/json' }

# Create a container
$body = @{ image = "hello-world"; autoStart = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:5001/containers -Headers $headers -Body $body

# List containers
Invoke-RestMethod -Uri http://localhost:5001/containers -Headers $headers

# Stop container (replace <id>)
Invoke-RestMethod -Method Post -Uri http://localhost:5001/containers/<id>/stop -Headers $headers

# Delete container
Invoke-RestMethod -Method Delete -Uri http://localhost:5001/containers/<id> -Headers $headers
```

### cURL Examples (Linux/macOS style)

```bash
API_KEY=...; BASE=http://localhost:5001

curl -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
	-d '{"image":"hello-world"}' $BASE/containers

curl -H "Authorization: Bearer $API_KEY" $BASE/containers
```

### Error Handling

| Status | Reason |
|--------|--------|
| 400 | Missing fields, malformed JSON, bad image pull |
| 401 | Missing/invalid API key |
| 403 | Image not allowed or ownership violation |
| 404 | Container not found |
| 500 | Docker/internal errors |

### Smoke Test Script

You can run a scripted lifecycle test once you have a key:

```powershell
python scripts\smoke_test_manager.py --key <PLAINTEXT_API_KEY>
```

### Notes / Future Hardening Ideas

- Rate limiting (per key) via a proxy or Flask limiter
- Image allow-list enforcement (already supported via env var)
- Resource constraints (CPU/mem) at create time
- Key hashing could include salt/stretching (current: plain SHA256)

---
If you encounter issues or have feature requests, please open an issue or PR.