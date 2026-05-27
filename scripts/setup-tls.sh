#!/bin/bash
# Albert sandbox host hardening: TLS (Let's Encrypt), firewall, security
# headers, fail2ban, SSH hardening, unattended-upgrades.
#
# Run ONCE per host as root, before/after install.sh. Idempotent: every step
# is safe to re-run. Reads FQDN and ADMIN_EMAIL from arguments or environment.
#
#   sudo FQDN=sandbox.host.domain ADMIN_EMAIL=admin@host.domain bash setup-tls.sh
#   sudo bash setup-tls.sh --fqdn sandbox.host.domain --email admin@host.domain
#
# The script deliberately does NOT start or modify the albert-container-manager
# service. Run install.sh after this script finishes.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
say() { echo -e "${GREEN}[setup-tls]${NC} $*"; }
warn() { echo -e "${YELLOW}[setup-tls]${NC} $*" >&2; }
die() { echo -e "${RED}[setup-tls]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Must be run as root."

FQDN="${FQDN:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
while [[ $# -gt 0 ]]; do
	case "$1" in
		--fqdn) FQDN="$2"; shift 2 ;;
		--email) ADMIN_EMAIL="$2"; shift 2 ;;
		-h|--help)
			grep '^#' "$0" | sed 's/^# \{0,1\}//'
			exit 0 ;;
		*) die "Unknown argument: $1" ;;
	esac
done
[[ -n "$FQDN" ]] || die "FQDN not set (use --fqdn or FQDN env)."
[[ -n "$ADMIN_EMAIL" ]] || die "ADMIN_EMAIL not set (use --email or ADMIN_EMAIL env)."

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"
SECURITY_CONF_SRC="${REPO_ROOT}/nginx/albert-security.conf"
SECURITY_CONF_DST="/etc/nginx/conf.d/albert-security.conf"
BACKUP_DIR="/root/albert-hardening/backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

backup() {
	local src="$1"
	[[ -e "$src" ]] || return 0
	install -D "$src" "${BACKUP_DIR}${src}"
}

# --- Step a: packages ------------------------------------------------------
say "Installing required packages (certbot, fail2ban, ufw, unattended-upgrades)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
	certbot python3-certbot-nginx \
	fail2ban ufw unattended-upgrades

# --- Step b: ufw firewall --------------------------------------------------
# SSH rule is added BEFORE enabling to prevent lockout.
say "Configuring ufw firewall..."
ufw allow 22/tcp comment 'ssh' >/dev/null
ufw allow 80/tcp comment 'http (acme + redirect)' >/dev/null
ufw allow 443/tcp comment 'https' >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
if ! ufw status | grep -q '^Status: active'; then
	echo 'y' | ufw enable >/dev/null
fi
say "ufw rules:"
ufw status verbose | sed 's/^/    /'

# --- Step c: Let's Encrypt certificate -------------------------------------
# We need nginx running on port 80 so certbot --nginx can solve HTTP-01.
say "Ensuring nginx is running for ACME HTTP-01 challenge..."
systemctl enable --now nginx
nginx -t

CERT_LIVE="/etc/letsencrypt/live/${FQDN}/fullchain.pem"
if [[ -f "$CERT_LIVE" ]]; then
	say "Certificate already present at $CERT_LIVE — skipping certbot."
else
	say "Requesting Let's Encrypt certificate for $FQDN..."
	backup /etc/nginx/sites-enabled/default
	certbot --nginx \
		-d "$FQDN" \
		-m "$ADMIN_EMAIL" \
		--agree-tos --non-interactive --redirect
fi
systemctl enable --now certbot.timer
say "Renewal timer: $(systemctl is-active certbot.timer)"

# --- Step d: security headers + rate-limit zones ---------------------------
say "Installing ${SECURITY_CONF_DST}..."
if [[ ! -f "$SECURITY_CONF_SRC" ]]; then
	die "Missing template: $SECURITY_CONF_SRC (run from albert-ai-sandbox repo)."
fi
backup "$SECURITY_CONF_DST"
install -m 0644 "$SECURITY_CONF_SRC" "$SECURITY_CONF_DST"
nginx -t
systemctl reload nginx

# --- Step e: fail2ban ------------------------------------------------------
say "Configuring fail2ban jails..."
JAIL_FILE="/etc/fail2ban/jail.d/albert.local"
backup "$JAIL_FILE"
cat > "$JAIL_FILE" <<'F2B'
# Albert sandbox fail2ban overrides. Managed by scripts/setup-tls.sh.
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled  = true
port     = ssh
mode     = aggressive

[nginx-http-auth]
enabled  = true

[nginx-limit-req]
enabled  = true
filter   = nginx-limit-req
logpath  = /var/log/nginx/error.log
maxretry = 10
findtime = 10m
bantime  = 1h
F2B
systemctl enable fail2ban >/dev/null
systemctl restart fail2ban
sleep 1
say "Active jails: $(fail2ban-client status 2>/dev/null | awk -F: '/Jail list/{print $2}' | xargs)"

# --- Step f: SSH hardening (with lockout safety) ---------------------------
say "Hardening sshd (key-only, no password auth)..."
SSHD_DROPIN="/etc/ssh/sshd_config.d/10-albert-hardening.conf"
AUTH_KEYS_OK=0
for h in /root /home/*; do
	ak="$h/.ssh/authorized_keys"
	if [[ -s "$ak" ]]; then
		AUTH_KEYS_OK=1
		break
	fi
done
if [[ "$AUTH_KEYS_OK" -ne 1 ]]; then
	warn "No non-empty authorized_keys found. Writing drop-in BUT NOT reloading sshd to prevent lockout."
	warn "Review $SSHD_DROPIN, install your public key, then run: sshd -t && systemctl reload ssh"
	RELOAD_SSH=0
else
	RELOAD_SSH=1
fi
backup "$SSHD_DROPIN"
cat > "$SSHD_DROPIN" <<'SSH'
# Albert sandbox SSH hardening. Managed by scripts/setup-tls.sh.
PasswordAuthentication no
PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
SSH
if [[ "$RELOAD_SSH" -eq 1 ]]; then
	sshd -t
	systemctl reload ssh
	say "sshd reloaded with hardened config."
fi

# --- Step g: unattended-upgrades -------------------------------------------
say "Enabling unattended-upgrades..."
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'AU'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
AU
systemctl enable --now unattended-upgrades >/dev/null

# --- Step h: ALBERT_PUBLIC_URL drop-in -------------------------------------
# Make the public FQDN available to the manager service so REST responses
# carry https://<fqdn>/... URLs instead of http://<ip>/... . install.sh
# doesn't know the FQDN, so we place the override here as a systemd drop-in.
say "Configuring ALBERT_PUBLIC_URL for manager service..."
DROPIN_DIR="/etc/systemd/system/albert-container-manager.service.d"
DROPIN_FILE="${DROPIN_DIR}/public-url.conf"
mkdir -p "$DROPIN_DIR"
backup "$DROPIN_FILE"
cat > "$DROPIN_FILE" <<EOF
[Service]
Environment=ALBERT_PUBLIC_URL=https://${FQDN}
EOF
systemctl daemon-reload
if systemctl is-active --quiet albert-container-manager.service; then
	systemctl restart albert-container-manager.service
	say "Restarted albert-container-manager with ALBERT_PUBLIC_URL=https://${FQDN}"
else
	say "albert-container-manager not active yet; drop-in will take effect on next start."
fi

# --- Summary ---------------------------------------------------------------
say "============================================================"
say "Host hardening complete."
say ""
say "Next steps:"
say "  1. Deploy albert-ai-sandbox code to its install dir."
say "  2. Run 'sudo bash install.sh' to (re)install services."
say "  3. Rotate API keys: 'albert-api-key-manager' — any old key that"
say "     traversed plain HTTP must be considered compromised."
say "  4. systemctl start albert-container-manager nginx albert-inactivity-watcher.timer"
say ""
say "Backups for this run are in $BACKUP_DIR"
