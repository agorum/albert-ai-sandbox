#!/bin/bash

source /opt/albert-ai-sandbox-manager/scripts/common.sh

# Find a free port for noVNC
find_free_novnc_port() {
	local port=$BASE_PORT
	while [ $port -le $MAX_PORT ]; do
		if ! netstat -tuln | grep -q ":$port "; then
			if ! jq -e ".[] | select(.port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
				echo $port
				return 0
			fi
		fi
		((port++))
	done
	return 1
}

# Find a free port for VNC
find_free_vnc_port() {
	local port=5900
	while [ $port -le 5999 ]; do
		if ! netstat -tuln | grep -q ":$port "; then
			if ! jq -e ".[] | select(.vnc_port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
				echo $port
				return 0
			fi
		fi
		((port++))
	done
	return 1
}

# Find a free port for MCP Hub
find_free_mcphub_port() {
	local port=$((BASE_PORT + 1000))  # Start 1000 ports higher than BASE_PORT
	while [ $port -le $MAX_PORT ]; do
		if ! netstat -tuln | grep -q ":$port "; then
			if ! jq -e ".[] | select(.mcphub_port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
				if ! jq -e ".[] | select(.port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
					echo $port
					return 0
				fi
			fi
		fi
		((port++))
	done
	return 1
}

# Find a free port for File service
find_free_filesvc_port() {
	local port=$((BASE_PORT + 1200))  # Start 1200 ports higher than BASE_PORT
	while [ $port -le $MAX_PORT ]; do
		if ! netstat -tuln | grep -q ":$port "; then
			# ensure not used by any sandbox as generic .port either
			if ! jq -e ".[] | select(.filesvc_port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
				if ! jq -e ".[] | select(.port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
					if ! jq -e ".[] | select(.mcphub_port == \"$port\")" "$REGISTRY_FILE" > /dev/null 2>&1; then
						echo $port
						return 0
					fi
				fi
			fi
		fi
		((port++))
	done
	return 1
}
