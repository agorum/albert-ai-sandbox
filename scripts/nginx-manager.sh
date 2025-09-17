#!/bin/bash

source /opt/albert-ai-sandbox-manager/scripts/common.sh

# Create nginx configuration for container
create_nginx_config() {
	local container_name=$1
	local novnc_port=$2
    
	cat > "${NGINX_CONF_DIR}/albert-${container_name}.conf" << EOCONF

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
EOCONF

	# Add include to main configuration - BUT ONLY IF NOT ALREADY PRESENT
	if ! grep -q "include ${NGINX_CONF_DIR}/albert-\*.conf;" /etc/nginx/sites-enabled/default; then
		# Add include after server_name
		sed -i '/server_name _;/a\\n\t# Albert Sandbox Configs\n\tinclude /etc/nginx/sites-available/albert-*.conf;' /etc/nginx/sites-enabled/default
		echo -e "${GREEN}✓ Nginx include added${NC}"
	else
		echo -e "${YELLOW}ℹ Nginx include already present${NC}"
	fi
	
	# Reload nginx
	nginx -t && systemctl reload nginx
}

# Remove nginx configuration for container
remove_nginx_config() {
	local container_name=$1
	
	rm -f "${NGINX_CONF_DIR}/albert-${container_name}.conf"
	nginx -t && systemctl reload nginx
}

# Clean up duplicate includes
cleanup_nginx_includes() {
	local config_file="/etc/nginx/sites-enabled/default"
	
	# Count how often the include appears
	local count=$(grep -c "include ${NGINX_CONF_DIR}/albert-\*.conf;" "$config_file" 2>/dev/null || echo 0)
	
	if [ "$count" -gt 1 ]; then
		echo -e "${YELLOW}Cleaning up duplicate nginx includes (found: $count)...${NC}"
		
		# Create backup
		cp "$config_file" "${config_file}.backup.$(date +%Y%m%d_%H%M%S)"
		
		# Remove all includes and add one back
		sed -i '/# Albert Sandbox Configs/d' "$config_file"
		sed -i '/include .*albert-\*.conf;/d' "$config_file"
		
		# Add one include back
		sed -i '/server_name _;/a\\n\t# Albert Sandbox Configs\n\tinclude /etc/nginx/sites-available/albert-*.conf;' "$config_file"
		
		echo -e "${GREEN}✓ Nginx includes cleaned up${NC}"
	fi
}
