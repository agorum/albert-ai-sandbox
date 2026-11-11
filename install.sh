#!/bin/bash
# ALBERT | AI Sandbox manager - install and update.
#
#   sudo bash install.sh [--skip-build] [--force-build] [--system-upgrade]
#
# The script is idempotent and doubles as the update procedure: run it again
# from a newer checkout. Running sandboxes keep running during an update;
# only the manager service restarts briefly. Updates from the former
# Bash/JSON-registry versions are migrated automatically (sandboxes, routes,
# API keys stay valid).
#
#   --skip-build      never rebuild the sandbox Docker image
#   --force-build     always rebuild the sandbox Docker image
#   --system-upgrade  also run "apt-get upgrade" (off by default)
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
say()  { echo -e "${GREEN}[install]${NC} $*"; }
warn() { echo -e "${YELLOW}[install]${NC} $*" >&2; }
die()  { echo -e "${RED}[install]${NC} $*" >&2; exit 1; }

SKIP_BUILD=0; FORCE_BUILD=0; SYSTEM_UPGRADE=0
for arg in "$@"; do
	case "$arg" in
		--skip-build) SKIP_BUILD=1 ;;
		--force-build) FORCE_BUILD=1 ;;
		--system-upgrade) SYSTEM_UPGRADE=1 ;;
		-h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) die "Unknown argument: $arg" ;;
	esac
done

[[ $EUID -eq 0 ]] || die "This script must be run as root"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_DIR="/opt/albert-ai-sandbox-manager"
STORAGE_DIR="/var/lib/albert-sandbox"
NGINX_ALBERT_DIR="/etc/nginx/albert"
NGINX_SITES_AVAILABLE="/etc/nginx/sites-available"
DEFAULT_SITE="/etc/nginx/sites-enabled/default"
CONFD="/etc/nginx/conf.d"
ENV_FILE="${INSTALL_DIR}/config/albert.env"
CLI="${INSTALL_DIR}/scripts/albert-ai-sandbox-manager.sh"
BACKUP_DIR="${INSTALL_DIR}/backups/$(date +%Y%m%d_%H%M%S)"
IMAGE="albert-ai-sandbox:latest"

UPGRADE=0
if [ -d "${INSTALL_DIR}/scripts" ] || systemctl list-unit-files 2>/dev/null | grep -q '^albert-container-manager.service'; then
	UPGRADE=1
fi

echo -e "${GREEN}========================================${NC}"
if [ "$UPGRADE" -eq 1 ]; then
	echo -e "${GREEN}ALBERT Sandbox Manager - Update${NC}"
else
	echo -e "${GREEN}ALBERT Sandbox Manager - Installation${NC}"
fi
echo -e "${GREEN}========================================${NC}"

# ---------------------------------------------------------------------------
# Packages + Docker
# ---------------------------------------------------------------------------
say "Installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
if [ "$SYSTEM_UPGRADE" -eq 1 ]; then
	apt-get upgrade -y
fi
apt-get install -y -qq \
	ca-certificates curl gnupg lsb-release \
	nginx jq git sqlite3 \
	python3 python3-venv python3-pip \
	iproute2 e2fsprogs util-linux tar >/dev/null

if ! command -v docker >/dev/null 2>&1; then
	say "Installing Docker..."
	TMP_DOCKER_SCRIPT="$(mktemp /tmp/get-docker.XXXXXX.sh)"
	curl -fsSL https://get.docker.com -o "${TMP_DOCKER_SCRIPT}"
	sh "${TMP_DOCKER_SCRIPT}"
	rm -f "${TMP_DOCKER_SCRIPT}"
	systemctl enable --now docker
else
	say "Docker is already installed ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'))"
fi

# ---------------------------------------------------------------------------
# Sandbox Docker image - built from this checkout BEFORE anything on the
# running system changes, so a failed build leaves the installation untouched.
# Rebuilt only when docker/ changed (or the image is missing).
# ---------------------------------------------------------------------------
mkdir -p "${INSTALL_DIR}/data"
chmod 700 "${INSTALL_DIR}/data"
DOCKER_HASH="$(cd "${SCRIPT_DIR}/docker" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
HASH_FILE="${INSTALL_DIR}/data/image-build.sha256"
NEED_BUILD=0
if [ "$FORCE_BUILD" -eq 1 ]; then
	NEED_BUILD=1
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
	NEED_BUILD=1
elif [ "$(cat "$HASH_FILE" 2>/dev/null || true)" != "$DOCKER_HASH" ]; then
	NEED_BUILD=1
fi
if [ "$SKIP_BUILD" -eq 1 ]; then
	NEED_BUILD=0
	docker image inspect "$IMAGE" >/dev/null 2>&1 || warn "Image ${IMAGE} missing and --skip-build given: sandboxes cannot be created until it is built."
fi
if [ "$NEED_BUILD" -eq 1 ]; then
	say "Building Docker image ${IMAGE} (takes a while; running sandboxes are not affected)..."
	if ! docker build -t "$IMAGE" "${SCRIPT_DIR}/docker"; then
		die "Docker image build failed - nothing was changed on this system. Fix the build (network access?) and run install.sh again, or use --skip-build to keep the current image."
	fi
	echo "$DOCKER_HASH" > "$HASH_FILE"
else
	say "Docker image ${IMAGE} is up to date"
fi

# ---------------------------------------------------------------------------
# Stop manager + watcher during the update (sandboxes keep running)
# ---------------------------------------------------------------------------
if [ "$UPGRADE" -eq 1 ]; then
	say "Pausing manager service and watcher (sandboxes keep running)..."
	systemctl stop albert-inactivity-watcher.timer 2>/dev/null || true
	systemctl stop albert-inactivity-watcher.service 2>/dev/null || true
	systemctl stop albert-container-manager.service 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
say "Copying files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"/{scripts,docker,config,data,nginx,backups} "$BACKUP_DIR"
chmod 700 "${INSTALL_DIR}/data" "${INSTALL_DIR}/backups"
# Files of the former Bash implementation (replaced by scripts/albert_sandbox).
for legacy in common.sh port-manager.sh nginx-manager.sh manager_db.py api_key_manager.py; do
	rm -f "${INSTALL_DIR}/scripts/${legacy}"
done
rm -rf "${INSTALL_DIR}/scripts/albert_sandbox"
cp -r "${SCRIPT_DIR}/scripts/." "${INSTALL_DIR}/scripts/"
cp -r "${SCRIPT_DIR}/docker/." "${INSTALL_DIR}/docker/"
cp -r "${SCRIPT_DIR}/nginx/." "${INSTALL_DIR}/nginx/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
find "${INSTALL_DIR}/scripts" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "${INSTALL_DIR}"/scripts/*.sh "${INSTALL_DIR}"/scripts/*.py "${INSTALL_DIR}/docker/startup.sh"

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
say "Setting up Python virtual environment..."
VENV_DIR="${INSTALL_DIR}/venv"
[ -x "${VENV_DIR}/bin/python" ] || python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet --no-cache-dir -r "${INSTALL_DIR}/requirements.txt"

# ---------------------------------------------------------------------------
# Configuration file (operators edit this; never overwritten)
# ---------------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
	say "Creating ${ENV_FILE}..."
	cat > "$ENV_FILE" <<'EOF'
# ALBERT | AI Sandbox manager configuration (KEY=VALUE).
# Read by the manager service, the watcher and the CLI. Restart the manager
# after changes: systemctl restart albert-container-manager
#
# Public base URL used in API responses (set by scripts/setup-tls.sh):
#ALBERT_PUBLIC_URL=https://sandbox.example.com
#
# Global per-sandbox defaults (used when a client has no own limit):
#ALBERT_DEFAULT_CPUS=1
#ALBERT_DEFAULT_MEMORY_MB=1024
#ALBERT_DEFAULT_DISK_GB=10
#
# Housekeeping: stop after inactivity, remove non-persistent sandboxes after max age (0 = off)
#ALBERT_INACTIVITY_SECONDS=600
#ALBERT_MAX_AGE_SECONDS=86400
#
# Host port pool for sandbox services (bound to 127.0.0.1 only)
#ALBERT_PORT_RANGE=20000-29999
EOF
	chmod 640 "$ENV_FILE"
fi
# Carry over settings that older versions kept in systemd units/drop-ins.
set_env_if_missing() {
	local key="$1" value="$2"
	[ -n "$value" ] || return 0
	grep -qE "^${key}=" "$ENV_FILE" && return 0
	echo "${key}=${value}" >> "$ENV_FILE"
	say "Migrated ${key}=${value} into ${ENV_FILE}"
}
OLD_DROPIN="/etc/systemd/system/albert-container-manager.service.d/public-url.conf"
if [ -f "$OLD_DROPIN" ]; then
	set_env_if_missing ALBERT_PUBLIC_URL "$(sed -n 's/^Environment=ALBERT_PUBLIC_URL=//p' "$OLD_DROPIN" | tail -n1)"
fi
OLD_WATCHER_UNIT="/etc/systemd/system/albert-inactivity-watcher.service"
if [ -f "$OLD_WATCHER_UNIT" ]; then
	for key in ALBERT_INACTIVITY_SECONDS ALBERT_MAX_AGE_SECONDS; do
		value="$(systemctl show albert-inactivity-watcher.service -p Environment --value 2>/dev/null \
			| tr ' ' '\n' | sed -n "s/^${key}=//p" | tail -n1)"
		# Only carry over values that differ from the defaults.
		case "${key}:${value}" in
			ALBERT_INACTIVITY_SECONDS:600|ALBERT_MAX_AGE_SECONDS:86400|*:) ;;
			*) set_env_if_missing "$key" "$value" ;;
		esac
	done
fi

# ---------------------------------------------------------------------------
# Storage for sandbox disk images
# ---------------------------------------------------------------------------
mkdir -p "${STORAGE_DIR}"/{disks,mnt,skel}
chmod 700 "${STORAGE_DIR}" "${STORAGE_DIR}"/{disks,mnt,skel}

# ---------------------------------------------------------------------------
# Database migration (schema + import of the legacy JSON registry)
# ---------------------------------------------------------------------------
say "Migrating database..."
"$CLI" migrate

# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------
say "Configuring nginx..."
mkdir -p "$BACKUP_DIR/nginx"
cp -a "$DEFAULT_SITE" "$BACKUP_DIR/nginx/default" 2>/dev/null || true
cp -a "${NGINX_SITES_AVAILABLE}"/albert-*.conf "$BACKUP_DIR/nginx/" 2>/dev/null || true
cp -a "${CONFD}"/albert-*.conf "$BACKUP_DIR/nginx/" 2>/dev/null || true

mkdir -p "${NGINX_ALBERT_DIR}/sandboxes"
install -m 0644 "${INSTALL_DIR}/nginx/albert-ratelimit.conf" "${CONFD}/albert-ratelimit.conf"
if [ -f "${CONFD}/albert-security.conf" ]; then
	# Installed by setup-tls.sh; refresh it (rate-limit zones moved to albert-ratelimit.conf).
	install -m 0644 "${INSTALL_DIR}/nginx/albert-security.conf" "${CONFD}/albert-security.conf"
fi
install -m 0644 "${INSTALL_DIR}/nginx/manager.conf" "${NGINX_ALBERT_DIR}/manager.conf"
cat > "${NGINX_ALBERT_DIR}/sandboxes.conf" <<EOF
# Managed by albert-ai-sandbox install.sh - one file per sandbox, written by the manager.
include ${NGINX_ALBERT_DIR}/sandboxes/*.conf;
EOF

# Write routes for all sandboxes (incl. migrated ones) before switching over.
ALBERT_NGINX_RELOAD=0 "$CLI" reconcile || warn "reconcile reported errors (see above)"

INCLUDE_LINE="include ${NGINX_ALBERT_DIR}/*.conf;"
if [ -f "$DEFAULT_SITE" ]; then
	# Drop old include lines (incl. the former sites-available/albert-*.conf glob)
	# and add exactly one include + upload limit per relevant server block.
	sed -i '/# Albert Sandbox Configs/d; /# ALBERT Sandbox Configs/d' "$DEFAULT_SITE"
	sed -i '/include .*albert-\*\.conf;/d; /include .*\/etc\/nginx\/albert\/\*\.conf;/d' "$DEFAULT_SITE"
	sed -i '/^[[:space:]]*client_max_body_size[[:space:]]\+0;/d' "$DEFAULT_SITE"
	awk -v inc_line="$INCLUDE_LINE" '
		{
			print $0
			if ($0 ~ /server_name[[:space:]]+_;/ || $0 ~ /server_name[[:space:]].*managed by Certbot/) {
				print "\t# ALBERT Sandbox Configs"
				print "\t" inc_line
				print "\tclient_max_body_size 0;"
			}
		}
	' "$DEFAULT_SITE" > "${DEFAULT_SITE}.tmp" && mv "${DEFAULT_SITE}.tmp" "$DEFAULT_SITE"
else
	warn "${DEFAULT_SITE} not found - add '${INCLUDE_LINE}' to your server block manually."
fi
# Files of the former layout (per-sandbox routes, global /mcphub route, manager route).
rm -f "${NGINX_SITES_AVAILABLE}"/albert-*.conf

systemctl enable nginx >/dev/null 2>&1 || true
if nginx -t 2>/dev/null; then
	systemctl reload nginx 2>/dev/null || systemctl restart nginx
	say "nginx reloaded"
else
	warn "nginx configuration test failed - restoring previous configuration"
	nginx -t || true
	cp -a "$BACKUP_DIR/nginx/default" "$DEFAULT_SITE" 2>/dev/null || true
	for f in "$BACKUP_DIR"/nginx/albert-*.conf; do
		[ -e "$f" ] || continue
		case "$(basename "$f")" in
			albert-ratelimit.conf|albert-security.conf) cp -a "$f" "${CONFD}/" ;;
			*) cp -a "$f" "${NGINX_SITES_AVAILABLE}/" ;;
		esac
	done
	[ -f "$BACKUP_DIR/nginx/albert-ratelimit.conf" ] || rm -f "${CONFD}/albert-ratelimit.conf"
	nginx -t && systemctl reload nginx || true
	die "nginx update failed, previous configuration restored (backup: $BACKUP_DIR/nginx)"
fi

# ---------------------------------------------------------------------------
# CLI symlinks
# ---------------------------------------------------------------------------
ln -sf "${INSTALL_DIR}/scripts/albert-ai-sandbox-manager.sh" /usr/local/bin/albert-ai-sandbox-manager
ln -sf "${INSTALL_DIR}/scripts/api-key-manager.sh" /usr/local/bin/albert-api-key-manager

# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------
say "Installing systemd units..."
cat > /etc/systemd/system/albert-container-manager.service <<EOF
[Unit]
Description=ALBERT Sandbox Manager (REST API + admin UI)
After=network-online.target docker.service albert-sandbox-disks.service
Wants=network-online.target albert-sandbox-disks.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
# Listen on loopback only; clients reach the API through nginx (/manager/).
Environment=MANAGER_BIND_HOST=127.0.0.1
Environment=MANAGER_PORT=5001
Environment=MANAGER_DB_PATH=${INSTALL_DIR}/data/manager.db
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/scripts/container_manager_service.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/albert-inactivity-watcher.service <<EOF
[Unit]
Description=ALBERT Sandbox housekeeping (idle stop, expiry, cleanup)
After=docker.service nginx.service albert-sandbox-disks.service

[Service]
Type=oneshot
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/scripts/inactivity_watcher.py
EOF

cat > /etc/systemd/system/albert-inactivity-watcher.timer <<'EOF'
[Unit]
Description=Run ALBERT sandbox housekeeping every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/albert-sandbox-disks.service <<EOF
[Unit]
Description=Mount ALBERT sandbox disk images
DefaultDependencies=no
After=local-fs.target
Before=docker.service
RequiresMountsFor=${STORAGE_DIR} ${INSTALL_DIR}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${CLI} mount-disks

[Install]
WantedBy=multi-user.target docker.service
EOF

# Settings now live in ${ENV_FILE}; the old drop-in would override it.
rm -f "$OLD_DROPIN"
rmdir /etc/systemd/system/albert-container-manager.service.d 2>/dev/null || true

systemctl daemon-reload
systemctl enable albert-sandbox-disks.service >/dev/null 2>&1
systemctl start albert-sandbox-disks.service
systemctl enable albert-container-manager.service >/dev/null 2>&1
systemctl restart albert-container-manager.service
systemctl enable --now albert-inactivity-watcher.timer >/dev/null 2>&1

# ---------------------------------------------------------------------------
# First admin user for the web UI
# ---------------------------------------------------------------------------
ADMIN_COUNT="$("$CLI" selfcheck --json | "${VENV_DIR}/bin/python" -c 'import json,sys;print(json.load(sys.stdin)["admins"])')"
if [ "$ADMIN_COUNT" = "0" ]; then
	say "Creating first admin user 'admin' for the web UI..."
	"$CLI" admin add admin --generate
	warn "Store this password now - it is not shown again. Change it in the web UI (Admins)."
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
say "Verifying..."
ok=1
for i in $(seq 1 20); do
	code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/admin/api/session || true)"
	[ "$code" = "401" ] || [ "$code" = "200" ] && break
	sleep 1
done
if [ "${code:-}" = "401" ] || [ "${code:-}" = "200" ]; then
	say "✓ manager service answers on 127.0.0.1:5001"
else
	warn "✗ manager service not reachable (journalctl -u albert-container-manager -e)"; ok=0
fi
systemctl is-active --quiet albert-inactivity-watcher.timer && say "✓ watcher timer active" || { warn "✗ watcher timer inactive"; ok=0; }
docker image inspect "$IMAGE" >/dev/null 2>&1 && say "✓ Docker image ${IMAGE} present" || { warn "✗ Docker image missing"; ok=0; }
systemctl is-active --quiet nginx && say "✓ nginx running" || { warn "✗ nginx not running"; ok=0; }

PUBLIC_URL="$("$CLI" selfcheck --json | "${VENV_DIR}/bin/python" -c 'import json,sys;print(json.load(sys.stdin)["publicUrl"])')"
echo -e "${GREEN}========================================${NC}"
if [ "$ok" -eq 1 ]; then
	echo -e "${GREEN}Done. Admin UI: ${PUBLIC_URL}/manager/admin/${NC}"
else
	echo -e "${YELLOW}Done with warnings. Admin UI: ${PUBLIC_URL}/manager/admin/${NC}"
fi
echo -e "${GREEN}Backups of replaced configuration: ${BACKUP_DIR}${NC}"
echo -e "${GREEN}========================================${NC}"
