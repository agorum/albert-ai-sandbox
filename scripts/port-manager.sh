#!/bin/bash

source /opt/albert-sandbox-manager/scripts/common.sh

# Finde freien Port für noVNC
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

# Finde freien Port für VNC
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
