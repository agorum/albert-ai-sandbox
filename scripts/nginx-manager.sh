#!/bin/bash

# NOTE: Do NOT enable global 'set -euo pipefail' here because this script is sourced
# by the main manager. Strict modes in a sourced context propagate and caused silent
# exits before JSON error emission. Individual commands are explicitly checked instead.

# Load shared functions/vars
source /opt/albert-ai-sandbox-manager/scripts/common.sh

# Writes per-container nginx config with noVNC and optional MCP Hub proxy
create_nginx_config() {
    local container_name="$1"
    local novnc_port="$2"
    local mcphub_port="${3:-}"
    local filesvc_port="${4:-}"
    local vnc_password="${5:-}"

    local cfg="${NGINX_CONF_DIR}/albert-${container_name}.conf"

    # URL-encode the VNC password for safe inclusion in the redirect query string.
    local vnc_password_enc
    if [ -n "$vnc_password" ] && command -v python3 >/dev/null 2>&1; then
        vnc_password_enc=$(VNC_PW="$vnc_password" python3 -c 'import os,urllib.parse;print(urllib.parse.quote(os.environ["VNC_PW"],safe=""))')
    else
        vnc_password_enc="$vnc_password"
    fi

    cat > "$cfg" <<EOF
# Auto-redirect to noVNC with correct websocket path
location = /${container_name}/ {
    return 301 /${container_name}/vnc.html?path=${container_name}/websockify&password=${vnc_password_enc}&autoconnect=true&resize=scale;
}

# Main proxy for noVNC interface
location /${container_name}/ {
    proxy_pass http://localhost:${novnc_port}/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
}

# Websocket proxy for VNC connection
location /${container_name}/websockify {
    proxy_pass http://localhost:${novnc_port}/websockify;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
}
EOF

    if [ -n "${mcphub_port}" ]; then
        cat >> "$cfg" <<EOF

# MCP Hub API endpoints
location /${container_name}/mcphub/mcp {
    proxy_pass http://localhost:${mcphub_port}/mcp;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
}

location /${container_name}/mcphub/sse {
    proxy_pass http://localhost:${mcphub_port}/sse;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
}
EOF
    fi

    if [ -n "${filesvc_port}" ]; then
        cat >> "$cfg" <<EOF

# File service (upload/download)
location /${container_name}/files/ {
    proxy_pass http://localhost:${filesvc_port}/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    client_max_body_size 0;
    proxy_read_timeout 86400;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
}
EOF
    fi

    _ensure_include_and_reload
}

# Writes a single global MCP Hub reverse proxy config
create_global_mcphub_config() {
    local mcphub_port="$1"
    local cfg="${NGINX_CONF_DIR}/albert-mcphub-global.conf"

    cat > "$cfg" <<EOF
# Global MCP Hub API endpoints
location /mcphub/mcp {
    proxy_pass http://localhost:${mcphub_port}/mcp;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
}

location /mcphub/sse {
    proxy_pass http://localhost:${mcphub_port}/sse;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
}
EOF

    _ensure_include_and_reload
}

# Removes the per-container nginx config and reloads
remove_nginx_config() {
    local container_name="$1"
    rm -f "${NGINX_CONF_DIR}/albert-${container_name}.conf"
    nginx -t && systemctl reload nginx
}

# Internal helper: ensure includes present once and reload nginx
_ensure_include_and_reload() {
    local default_site="${NGINX_ENABLED_DIR}/default"
    local include_line="include ${NGINX_CONF_DIR}/albert-*.conf;"

    # Proactively clean up duplicate include lines first and ensure exactly one include
    cleanup_nginx_includes
    _ensure_single_include_line "$default_site" "$include_line"

    # Validate and reload
    nginx -t && systemctl reload nginx
}

# Internal: ensure the albert include line is present in every relevant
# server block (default '_' block and any certbot-managed TLS block). This
# mirrors the logic in install.sh so per-container nginx writes stay in sync.
_ensure_single_include_line() {
    local file_path="$1"
    local include_line="$2"

    # Remove any existing markers/includes
    sed -i '/# Albert Sandbox Configs/d' "$file_path"
    sed -i '/# ALBERT Sandbox Configs/d' "$file_path"
    sed -i '/include .*albert-\*.conf;/d' "$file_path"

    # Insert after every matching server_name line.
    awk -v inc_line="$include_line" '
        {
            print $0
            if ($0 ~ /server_name[[:space:]]+_;/ || $0 ~ /server_name[[:space:]].*managed by Certbot/) {
                print "\t# Albert Sandbox Configs"
                print "\t" inc_line
            }
        }
    ' "$file_path" > "${file_path}.tmp" && mv "${file_path}.tmp" "$file_path"
}

# Tidies include lines in the default site. After every operation we expect
# exactly one include per target server block (1 for plain HTTP, 2 once TLS
# is enabled). Only rewrite if the count diverges from that target. Warnings
# are written to stderr so they do not contaminate JSON stdout consumed by
# container_manager_service.
cleanup_nginx_includes() {
    local config_file="${NGINX_ENABLED_DIR}/default"
    local pattern='include .*albert-\*.conf;'

    local count
    count=$(grep -c "$pattern" "$config_file" 2>/dev/null || echo 0)
    local target
    target=$(grep -cE 'server_name[[:space:]]+_;|server_name[[:space:]].*managed by Certbot' "$config_file" 2>/dev/null || echo 0)

    if [ "$target" -eq 0 ]; then
        return 0
    fi
    if [ "$count" -eq "$target" ]; then
        return 0
    fi
    echo "Reconciling nginx includes (have=$count, want=$target)..." >&2
    # Never place backups inside sites-enabled: nginx globs that directory.
    local backup_dir="/opt/albert-ai-sandbox-manager/nginx/backups"
    mkdir -p "$backup_dir"
    cp "$config_file" "$backup_dir/default.backup.$(date +%Y%m%d_%H%M%S)"
    _ensure_single_include_line "$config_file" "include ${NGINX_CONF_DIR}/albert-*.conf;"
}
