#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Base variables
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_DIR="/opt/albert-sandbox-manager"
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

# Make all shell scripts executable
echo -e "${YELLOW}Setting execution permissions...${NC}"
find "${SCRIPT_DIR}" -type f -name "*.sh" -exec chmod +x {} \;

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
	net-tools \
	dos2unix

# Docker installation
echo -e "${YELLOW}Installing Docker...${NC}"
if ! command -v docker &> /dev/null; then
	curl -fsSL https://get.docker.com -o get-docker.sh
	sh get-docker.sh
	rm get-docker.sh
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

# Convert all copied shell scripts to Unix line endings again
echo -e "${YELLOW}Converting line endings of installed scripts...${NC}"
find "${INSTALL_DIR}" -type f -name "*.sh" -exec dos2unix {} \; 2>/dev/null || {
	find "${INSTALL_DIR}" -type f -name "*.sh" -exec sed -i 's/\r$//' {} \;
}

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
docker build -t albert-sandbox:latest . || {
	echo -e "${RED}Error building Docker image!${NC}"
	echo -e "${YELLOW}Please check Docker installation and Dockerfile.${NC}"
	exit 1
}

# Configure nginx
echo -e "${YELLOW}Configuring nginx...${NC}"

# IMPORTANT: Clean up duplicate includes BEFORE setup
echo -e "${YELLOW}Cleaning nginx configuration...${NC}"
if [ -f "/etc/nginx/sites-enabled/default" ]; then
	# Create backup
	#cp /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/default.backup.$(date +%Y%m%d_%H%M%S)
	
	# Remove ALL old ALBERT-Sandbox includes
	sed -i '/# ALBERT Sandbox Configs/d' /etc/nginx/sites-enabled/default
	sed -i '/include .*albert-\*.conf;/d' /etc/nginx/sites-enabled/default
	
	# Add ONE new include
	sed -i '/server_name _;/a\\n\t# ALBERT Sandbox Configs\n\tinclude /etc/nginx/sites-available/albert-*.conf;' /etc/nginx/sites-enabled/default
	
	echo -e "${GREEN}✓ Nginx configuration cleaned${NC}"
fi

# Start/restart nginx
systemctl enable nginx
nginx -t && systemctl reload nginx || {
	echo -e "${YELLOW}Nginx could not be reloaded, trying restart...${NC}"
	systemctl restart nginx
}

# Create symlink for easy access
echo -e "${YELLOW}Creating symlink for global access...${NC}"
ln -sf ${INSTALL_DIR}/scripts/albert-sandbox-manager.sh /usr/local/bin/albert-sandbox-manager

# Verify installation
echo -e "${YELLOW}Verifying installation...${NC}"
if [ -f "/usr/local/bin/albert-sandbox-manager" ] && [ -x "/usr/local/bin/albert-sandbox-manager" ]; then
	echo -e "${GREEN}✓ albert-sandbox-manager successfully installed${NC}"
else
	echo -e "${RED}✗ albert-sandbox-manager installation failed${NC}"
	exit 1
fi

if docker images | grep -q "albert-sandbox"; then
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
echo ""
echo "Usage:"
echo "  albert-sandbox-manager create        - New sandbox with cryptic name"
echo "  albert-sandbox-manager create <name> - New sandbox with custom name"
echo "  albert-sandbox-manager list          - List containers"
echo "  albert-sandbox-manager status        - Show status"
echo "  albert-sandbox-manager help          - Show help"
echo ""
echo -e "${YELLOW}Info: VNC password for all containers: albert${NC}"
echo ""
echo "Example:"
echo "  albert-sandbox-manager create"
echo ""
echo -e "${YELLOW}Create first test sandbox? (y/n)${NC}"
read -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
	albert-sandbox-manager create test-sandbox || {
		echo -e "${RED}Error creating test sandbox${NC}"
		echo -e "${YELLOW}Try: albert-sandbox-manager create${NC}"
	}
fi