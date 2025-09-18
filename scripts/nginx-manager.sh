#!/bin/bash

set -euo pipefail

# Load shared functions/vars
source /opt/albert-ai-sandbox-manager/scripts/common.sh

# Writes per-container nginx config with noVNC and optional MCP Hub proxy
create_nginx_config() {
    local container_name="$1"
    local novnc_port="$2"
    local mcphub_port="${3:-}"
    local filesvc_port="${4:-}"

    local cfg="${NGINX_CONF_DIR}/albert-${container_name}.conf"

    cat > "$cfg" <<EOF
# Auto-redirect to noVNC with correct websocket path
location = /${container_name}/ {
    return 301 /${container_name}/vnc.html?path=${container_name}/websockify&password=albert&autoconnect=true&resize=scale;
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

# Internal: ensure exactly one include line exists after server_name _;
_ensure_single_include_line() {
    local file_path="$1"
    local include_line="$2"

    # Remove any existing markers/includes
    sed -i '/# Albert Sandbox Configs/d' "$file_path"
    sed -i '/# ALBERT Sandbox Configs/d' "$file_path"
    sed -i '/include .*albert-\*.conf;/d' "$file_path"

    # Insert once after server_name _; using awk to avoid sed escape quirks
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

# Tidies duplicate include lines in default site
cleanup_nginx_includes() {
    local config_file="${NGINX_ENABLED_DIR}/default"
    # Generic regex pattern to match any albert-*.conf include lines
    local pattern='include .*albert-\*.conf;'

    local count=$(grep -c "$pattern" "$config_file" 2>/dev/null || echo 0)
    if [ "$count" -gt 1 ]; then
        echo -e "${YELLOW}Cleaning up duplicate nginx includes (found: $count)...${NC}"
        cp "$config_file" "${config_file}.backup.$(date +%Y%m%d_%H%M%S)"
        # Remove all our include markers/lines
        sed -i '/# Albert Sandbox Configs/d' "$config_file"
        sed -i '/# ALBERT Sandbox Configs/d' "$config_file"
        sed -i "/$pattern/d" "$config_file"
        # Add a single include line back using helper
        _ensure_single_include_line "$config_file" "include ${NGINX_CONF_DIR}/albert-*.conf;"
        echo -e "${GREEN}✓ Nginx includes cleaned up${NC}"
    fi
}
