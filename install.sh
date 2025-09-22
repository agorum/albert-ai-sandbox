#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Base variables
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_DIR="/opt/albert-ai-sandbox-manager"
NGINX_CONF_DIR="/etc/nginx/sites-available"
NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}ALBERT Sandbox Manager Installation${NC}"
echo -e "${GREEN}========================================${NC}"

# Root check
if [[ $EUID -ne 0 ]]; then
	echo -e "${RED}This script must be run as root${NC}" 
	exit 1
fi

# System update
echo -e "${YELLOW}Updating system...${NC}"
apt-get update
apt-get upgrade -y

# Install dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
apt-get install -y \
	apt-transport-https \
	ca-certificates \
	curl \
	gnupg \
	lsb-release \
	nginx \
	jq \
	git \
	python3 \
	python3-pip \
	python3-venv \
	net-tools

# (Will set up Python virtualenv after copying files)

# Docker installation
echo -e "${YELLOW}Installing Docker...${NC}"
if ! command -v docker &> /dev/null; then
        TMP_DOCKER_SCRIPT="$(mktemp /tmp/get-docker.XXXXXX.sh)"
        curl -fsSL https://get.docker.com -o "${TMP_DOCKER_SCRIPT}"
        sh "${TMP_DOCKER_SCRIPT}"
        rm -f "${TMP_DOCKER_SCRIPT}"
	systemctl enable docker
	systemctl start docker
else
	echo -e "${GREEN}Docker is already installed${NC}"
fi

# Create directory structure
echo -e "${YELLOW}Creating directory structure...${NC}"
mkdir -p ${INSTALL_DIR}/{scripts,docker,config,nginx}

# Copy all files
echo -e "${YELLOW}Copying files...${NC}"
cp -r ${SCRIPT_DIR}/scripts/* ${INSTALL_DIR}/scripts/ 2>/dev/null || {
	echo -e "${YELLOW}Scripts directory not found, skipping...${NC}"
}
cp -r ${SCRIPT_DIR}/docker/* ${INSTALL_DIR}/docker/ 2>/dev/null || {
	echo -e "${YELLOW}Docker directory not found, skipping...${NC}"
}
cp -r ${SCRIPT_DIR}/config/* ${INSTALL_DIR}/config/ 2>/dev/null || true
cp -r ${SCRIPT_DIR}/requirements.txt ${INSTALL_DIR}/ 2>/dev/null || true

# Python dependencies for manager service (use dedicated venv to avoid PEP 668 issues)
echo -e "${YELLOW}Setting up Python virtual environment...${NC}"
VENV_DIR="${INSTALL_DIR}/venv"
if [ ! -d "${VENV_DIR}" ]; then
	python3 -m venv "${VENV_DIR}" 2>/dev/null || {
		echo -e "${YELLOW}First venv attempt failed, ensuring python3-venv & ensurepip...${NC}";
		apt-get install -y python3-venv >/dev/null 2>&1 || true
		python3 -m ensurepip --upgrade 2>/dev/null || true
		python3 -m venv "${VENV_DIR}" 2>/dev/null || {
			echo -e "${RED}Virtualenv creation failed twice – falling back to system Python with --break-system-packages${NC}";
			USE_SYSTEM_PIP=1
		};
	}
fi

if [ -z "${USE_SYSTEM_PIP:-}" ]; then
	# Inside venv
	"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel --quiet || true
	echo -e "${YELLOW}Installing Python requirements in venv...${NC}"
	if [ -f "${INSTALL_DIR}/requirements.txt" ]; then
		"${VENV_DIR}/bin/pip" install --no-cache-dir -r "${INSTALL_DIR}/requirements.txt" || {
			echo -e "${RED}Failed to install Python requirements in venv${NC}"; exit 1; }
	else
		echo -e "${YELLOW}requirements.txt not found, skipping Python deps (manager service may fail)${NC}"
	fi
else
	echo -e "${YELLOW}Using system Python (no venv). Installing requirements with --break-system-packages (PEP 668 override).${NC}"
	if [ -f "${INSTALL_DIR}/requirements.txt" ]; then
		pip3 install --no-cache-dir --break-system-packages -r "${INSTALL_DIR}/requirements.txt" || {
			echo -e "${RED}Failed to install Python requirements system-wide${NC}"; exit 1; }
	fi
fi

# Set permissions
echo -e "${YELLOW}Setting permissions...${NC}"
chmod +x ${INSTALL_DIR}/scripts/*.sh 2>/dev/null || true
chmod +x ${INSTALL_DIR}/docker/startup.sh 2>/dev/null || true
chmod +x ${INSTALL_DIR}/docker/*.sh 2>/dev/null || true

# Initialize registry
if [ ! -f "${INSTALL_DIR}/config/container-registry.json" ]; then
	echo "[]" > ${INSTALL_DIR}/config/container-registry.json
fi

# Check if Dockerfile exists
if [ ! -f "${INSTALL_DIR}/docker/Dockerfile" ]; then
	echo -e "${RED}Error: Dockerfile not found!${NC}"
	echo -e "${YELLOW}Please ensure all files are copied correctly.${NC}"
	exit 1
fi

# Build Docker image
echo -e "${YELLOW}Building Docker image...${NC}"
cd ${INSTALL_DIR}/docker
docker build -t albert-ai-sandbox:latest . || {
	echo -e "${RED}Error building Docker image!${NC}"
	echo -e "${YELLOW}Please check Docker installation and Dockerfile.${NC}"
	exit 1
}

# Configure nginx (idempotent include handling)
echo -e "${YELLOW}Configuring nginx...${NC}"

DEFAULT_SITE="${NGINX_ENABLED_DIR}/default"
INCLUDE_LINE="include ${NGINX_CONF_DIR}/albert-*.conf;"

# Helper to ensure exactly one include line after 'server_name _;' using awk (robust vs sed newlines)
ensure_single_include_line() {
	local file_path="$1"
	local include_line="$2"

	# Backup original once per run
	cp "$file_path" "${file_path}.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true

	# Remove all old markers and include lines (also catches stray variants)
	sed -i '/# Albert Sandbox Configs/d' "$file_path"
	sed -i '/# ALBERT Sandbox Configs/d' "$file_path"
	sed -i '/include .*albert-\*.conf;/d' "$file_path"

	# Insert a single include block after the first server_name _; line
	awk -v inc_line="$include_line" '
		BEGIN { inserted=0 }
		{
			print $0
			if (!inserted && $0 ~ /server_name[[:space:]]+_;/) {
				print "\t# Albert Sandbox Configs"
				print "\t" inc_line
				inserted=1
			}
		}
	' "$file_path" > "${file_path}.tmp" && mv "${file_path}.tmp" "$file_path"
}

if [ -f "${DEFAULT_SITE}" ]; then
	echo -e "${YELLOW}Reconciling nginx default site includes...${NC}"
	include_count=$(grep -c "include .*albert-\\*.conf;" "${DEFAULT_SITE}" 2>/dev/null || echo 0)

	if [ "${include_count}" -ne 1 ]; then
		ensure_single_include_line "${DEFAULT_SITE}" "${INCLUDE_LINE}"
		echo -e "${GREEN}✓ Nginx include normalized to a single line${NC}"
	else
		echo -e "${GREEN}✓ Nginx include already present (1)${NC}"
	fi
fi

# Start/restart nginx
systemctl enable nginx
nginx -t && systemctl reload nginx || {
	echo -e "${YELLOW}Nginx could not be reloaded, trying restart...${NC}"
	systemctl restart nginx
}

# Create symlink for easy access
echo -e "${YELLOW}Creating symlink for global access...${NC}"
ln -sf ${INSTALL_DIR}/scripts/albert-ai-sandbox-manager.sh /usr/local/bin/albert-ai-sandbox-manager
ln -sf ${INSTALL_DIR}/scripts/api-key-manager.sh /usr/local/bin/albert-api-key-manager 2>/dev/null || true

# ---------------------------------------------------------------------------
# Container Manager Service (systemd setup)
# ---------------------------------------------------------------------------
MANAGER_SERVICE_FILE="/etc/systemd/system/albert-container-manager.service"
echo -e "${YELLOW}Installing/Updating manager systemd unit...${NC}"
cat > "$MANAGER_SERVICE_FILE" <<'EOF'
[Unit]
Description=ALBERT Container Manager REST Service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/albert-ai-sandbox-manager
ExecStartPre=/usr/bin/bash -c 'for i in {1..15}; do docker info >/dev/null 2>&1 && exit 0; echo "[manager] waiting for docker ($i)"; sleep 2; done; echo "Docker daemon not ready after wait" >&2; exit 1'
ExecStart=/usr/bin/env bash -c '[ -x /opt/albert-ai-sandbox-manager/venv/bin/python ] && exec /opt/albert-ai-sandbox-manager/venv/bin/python /opt/albert-ai-sandbox-manager/scripts/container_manager_service.py || exec python3 /opt/albert-ai-sandbox-manager/scripts/container_manager_service.py'
Restart=on-failure
RestartSec=5
Environment=MANAGER_PORT=5001
Environment=MANAGER_DB_PATH=/opt/albert-ai-sandbox-manager/data/manager.db
Environment=MANAGER_DATA_DIR=/opt/albert-ai-sandbox-manager/data/containers
# Optional: restrict privileges a bit (comment out if causing issues)
# NoNewPrivileges=true
# ProtectSystem=full
# ProtectHome=true
# PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable albert-container-manager.service
systemctl restart albert-container-manager.service || echo -e "${YELLOW}Warning: Manager service failed to (re)start; check logs with: journalctl -u albert-container-manager -e${NC}"

# ---------------------------------------------------------------------------
# Nginx routing for manager service under /manager/
# ---------------------------------------------------------------------------
MANAGER_NGX_CONF="${NGINX_CONF_DIR}/albert-manager.conf"
if [ ! -f "$MANAGER_NGX_CONF" ]; then
cat > "$MANAGER_NGX_CONF" <<'EOF'
# Reverse proxy for ALBERT Container Manager REST API
location /manager/ {
	proxy_pass http://localhost:5001/;
	proxy_http_version 1.1;
	proxy_set_header Upgrade $http_upgrade;
	proxy_set_header Connection "upgrade";
	proxy_set_header Host $host;
	proxy_set_header X-Real-IP $remote_addr;
	proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
	proxy_set_header X-Forwarded-Proto $scheme;
	proxy_read_timeout 86400;
	proxy_buffering off;
	proxy_request_buffering off;
	proxy_cache off;
}
EOF
	echo -e "${GREEN}✓ Nginx manager route installed (/manager/)${NC}"
else
	echo -e "${YELLOW}Nginx manager route already exists, skipping${NC}"
fi

# Ensure include line exists (reuse logic already applied earlier)
if systemctl is-active --quiet nginx; then
	nginx -t && systemctl reload nginx || systemctl restart nginx || true
fi

# Verify installation
echo -e "${YELLOW}Verifying installation...${NC}"
if [ -f "/usr/local/bin/albert-ai-sandbox-manager" ] && [ -x "/usr/local/bin/albert-ai-sandbox-manager" ]; then
	echo -e "${GREEN}✓ albert-ai-sandbox-manager successfully installed${NC}"
else
	echo -e "${RED}✗ albert-ai-sandbox-manager installation failed${NC}"
	exit 1
fi

if docker images | grep -q "albert-ai-sandbox"; then
	echo -e "${GREEN}✓ Docker image successfully built${NC}"
else
	echo -e "${RED}✗ Docker image not found${NC}"
	exit 1
fi

if systemctl is-active --quiet nginx; then
	echo -e "${GREEN}✓ Nginx is running${NC}"
else
	echo -e "${RED}✗ Nginx is not running${NC}"
fi

# Check final nginx configuration
echo -e "${YELLOW}Checking final nginx configuration...${NC}"
include_count=$(grep -c "include.*albert-\*.conf;" /etc/nginx/sites-enabled/default 2>/dev/null || echo 0)
if [ "$include_count" -eq 1 ]; then
	echo -e "${GREEN}✓ Nginx include correctly configured (1 include found)${NC}"
elif [ "$include_count" -gt 1 ]; then
	echo -e "${RED}⚠ Warning: Multiple includes found ($include_count)${NC}"
	echo -e "${YELLOW}  Run: nano /etc/nginx/sites-enabled/default${NC}"
	echo -e "${YELLOW}  and remove duplicate include lines${NC}"
else
	echo -e "${YELLOW}⚠ No nginx include found${NC}"
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Installation completed!${NC}"
echo -e "${GREEN}========================================${NC}"
