# ALBERT Sandbox Manager

A Docker-based system for managing isolated desktop environments with browser access via noVNC.

## Features

- 🖥️ Ubuntu 22.04 desktop environment
- 🌐 Browser access without client software (noVNC)
- 🔒 Security through cryptic container names (no password needed)
- 🚀 Firefox and Chromium pre-installed
- 💾 Persistent data via Docker volumes
- 🔧 Easy management via CLI

## Installation

```bash
git clone <repository-url> albert-sandbox-manager
cd albert-sandbox-manager
bash install.sh
```

## Usage
```bash
cd /opt/albert-sandbox-manager
./albert-sandbox-manager
```