#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Base variables
INSTALL_DIR="/opt/albert-ai-sandbox-manager"
NGINX_CONF_DIR="/etc/nginx/sites-available"
NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
MANAGER_SERVICE_FILE="/etc/systemd/system/albert-container-manager.service"
DEFAULT_SITE="${NGINX_ENABLED_DIR}/default"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}ALBERT Sandbox Manager Uninstallation${NC}"
echo -e "${GREEN}========================================${NC}"

# Root check
if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}This script must be run as root${NC}"
        exit 1
fi

# Helper to clean nginx include lines
cleanup_nginx_include() {
        local target_file="$1"

        if [ ! -f "$target_file" ]; then
                return 0
        fi

        if grep -q "albert-\\*.conf" "$target_file" 2>/dev/null || \
           grep -q "Albert Sandbox Configs" "$target_file" 2>/dev/null; then
                echo -e "${YELLOW}Cleaning nginx includes in ${target_file}...${NC}"
                sed -i '/# Albert Sandbox Configs/d' "$target_file"
                sed -i '/# ALBERT Sandbox Configs/d' "$target_file"
                sed -i '/include[[:space:]]\+.*albert-.*\\.conf;[[:space:]]*$/d' "$target_file"
        fi
}

# Stop and disable systemd service if present
if systemctl list-unit-files 2>/dev/null | grep -q '^albert-container-manager.service'; then
        echo -e "${YELLOW}Stopping albert-container-manager.service...${NC}"
        systemctl stop albert-container-manager.service 2>/dev/null || true
        echo -e "${YELLOW}Disabling albert-container-manager.service...${NC}"
        systemctl disable albert-container-manager.service 2>/dev/null || true
fi

# Remove systemd unit file
if [ -f "$MANAGER_SERVICE_FILE" ]; then
        echo -e "${YELLOW}Removing systemd unit file...${NC}"
        rm -f "$MANAGER_SERVICE_FILE"
        systemctl daemon-reload 2>/dev/null || true
fi

# Remove nginx manager configuration
if [ -f "${NGINX_CONF_DIR}/albert-manager.conf" ]; then
        echo -e "${YELLOW}Removing nginx manager configuration...${NC}"
        rm -f "${NGINX_CONF_DIR}/albert-manager.conf"
fi

# Clean nginx include lines
cleanup_nginx_include "$DEFAULT_SITE"
cleanup_nginx_include "${NGINX_CONF_DIR}/default"

# Remove nginx backup files created during installation
find "$NGINX_ENABLED_DIR" -maxdepth 1 -type f -name 'default.backup.*' -print -delete 2>/dev/null || true

# Reload nginx if running
if systemctl is-active --quiet nginx; then
        echo -e "${YELLOW}Reloading nginx...${NC}"
        nginx -t && systemctl reload nginx || systemctl restart nginx || true
fi

# Remove symlinks
if [ -L "/usr/local/bin/albert-ai-sandbox-manager" ]; then
        echo -e "${YELLOW}Removing albert-ai-sandbox-manager symlink...${NC}"
        rm -f /usr/local/bin/albert-ai-sandbox-manager
fi
if [ -L "/usr/local/bin/albert-api-key-manager" ]; then
        echo -e "${YELLOW}Removing albert-api-key-manager symlink...${NC}"
        rm -f /usr/local/bin/albert-api-key-manager
fi

# Remove installation directory
if [ -d "$INSTALL_DIR" ]; then
        echo -e "${YELLOW}Removing installation directory ${INSTALL_DIR}...${NC}"
        rm -rf "$INSTALL_DIR"
fi

# Reminder about Docker images and packages
echo -e "${YELLOW}Note:${NC} Debian packages and Docker images installed during setup remain untouched."

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Uninstallation completed!${NC}"
echo -e "${GREEN}========================================${NC}"
