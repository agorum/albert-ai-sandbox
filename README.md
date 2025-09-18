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

- Upload:  `http://<host>/{container_name}/files/upload`  (multipart/form-data field name: `file`)
- Download: `http://<host>/{container_name}/files/download?path=/tmp/albert-files/<uuid.ext>`

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