#!/bin/bash
# ALBERT | AI Sandbox manager - uninstall.
#
#   sudo bash uninstall.sh [--yes]
#
# Removes ALL sandboxes including their data (containers, disk images,
# legacy volumes), sandbox profile images, the manager, its systemd units,
# nginx routes and the installation directory. Docker, nginx, Debian packages, the sandbox Docker
# image and /etc/nginx/conf.d/albert-security.conf (setup-tls.sh hardening)
# are left in place.
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
say() { echo -e "${YELLOW}[uninstall]${NC} $*"; }

INSTALL_DIR="/opt/albert-ai-sandbox-manager"
STORAGE_DIR="/var/lib/albert-sandbox"
NGINX_ALBERT_DIR="/etc/nginx/albert"
DEFAULT_SITE="/etc/nginx/sites-enabled/default"

[[ $EUID -eq 0 ]] || { echo -e "${RED}This script must be run as root${NC}"; exit 1; }

if [ "${1:-}" != "--yes" ]; then
	count=$(docker ps -aq --filter label=albert.manager=1 2>/dev/null | wc -l)
	echo -e "${RED}This deletes the ALBERT sandbox manager and ALL ${count} sandboxes including their data.${NC}"
	read -r -p "Type 'uninstall' to continue: " answer
	[ "$answer" = "uninstall" ] || { echo "Aborted."; exit 1; }
fi

say "Stopping services..."
for unit in albert-inactivity-watcher.timer albert-inactivity-watcher.service albert-container-manager.service; do
	systemctl disable --now "$unit" >/dev/null 2>&1 || true
done

if command -v docker >/dev/null 2>&1; then
	say "Removing sandbox containers..."
	for c in $(docker ps -aq --filter label=albert.manager=1); do
		docker rm -f -v "$c" >/dev/null 2>&1 || true
	done
	for v in $(docker volume ls -q | grep -E '^sbx-.*_data$' || true); do
		docker volume rm -f "$v" >/dev/null 2>&1 || true
	done
fi

say "Removing sandbox network isolation..."
if [ -x "${INSTALL_DIR}/scripts/albert-ai-sandbox-manager.sh" ]; then
	"${INSTALL_DIR}/scripts/albert-ai-sandbox-manager.sh" firewall remove >/dev/null 2>&1 || true
fi
systemctl disable albert-sandbox-firewall.service >/dev/null 2>&1 || true
if command -v docker >/dev/null 2>&1; then
	docker network rm albert-sandbox >/dev/null 2>&1 || true
	docker network rm albert-sandbox-offline >/dev/null 2>&1 || true
	say "Removing sandbox profile images..."
	for img in $(docker image ls -q --filter label=albert.profile | sort -u); do
		docker image rm -f "$img" >/dev/null 2>&1 || true
	done
fi

say "Removing sandbox disks..."
if [ -d "${STORAGE_DIR}/mnt" ]; then
	for m in "${STORAGE_DIR}"/mnt/*; do
		[ -d "$m" ] || continue
		mountpoint -q "$m" && { umount "$m" 2>/dev/null || umount -l "$m"; }
		chattr -i "$m" 2>/dev/null || true
	done
fi
systemctl disable albert-sandbox-disks.service >/dev/null 2>&1 || true
rm -rf "$STORAGE_DIR"

say "Removing nginx configuration..."
if [ -f "$DEFAULT_SITE" ]; then
	sed -i '/# Albert Sandbox Configs/d; /# ALBERT Sandbox Configs/d' "$DEFAULT_SITE"
	sed -i '/include .*albert-\*\.conf;/d; /include .*\/etc\/nginx\/albert\/\*\.conf;/d' "$DEFAULT_SITE"
fi
rm -rf "$NGINX_ALBERT_DIR"
rm -f /etc/nginx/sites-available/albert-*.conf /etc/nginx/conf.d/albert-ratelimit.conf
if systemctl is-active --quiet nginx; then
	nginx -t >/dev/null 2>&1 && systemctl reload nginx || systemctl restart nginx || true
fi

say "Removing systemd units, symlinks and ${INSTALL_DIR}..."
rm -f /etc/systemd/system/albert-container-manager.service \
	/etc/systemd/system/albert-inactivity-watcher.service \
	/etc/systemd/system/albert-inactivity-watcher.timer \
	/etc/systemd/system/albert-sandbox-disks.service \
	/etc/systemd/system/albert-sandbox-firewall.service
rm -rf /etc/systemd/system/albert-container-manager.service.d
systemctl daemon-reload
rm -f /usr/local/bin/albert-ai-sandbox-manager /usr/local/bin/albert-api-key-manager
rm -rf "$INSTALL_DIR"

echo -e "${GREEN}Uninstall complete.${NC} Docker image 'albert-ai-sandbox:latest' and system packages were kept."
