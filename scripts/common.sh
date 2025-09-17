#!/bin/bash

# Gemeinsame Variablen und Funktionen
REGISTRY_FILE="/opt/albert-sandbox-manager/config/container-registry.json"
NGINX_CONF_DIR="/etc/nginx/sites-available"
NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
BASE_PORT=6080
MAX_PORT=6180

# Farben
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Registry initialisieren wenn nicht vorhanden
init_registry() {
    if [ ! -f "$REGISTRY_FILE" ]; then
        echo "[]" > "$REGISTRY_FILE"
    fi
}

# Container in Registry hinzufügen
add_to_registry() {
    local name=$1
    local port=$2
    local vnc_port=$3
    
    init_registry
    
    local entry=$(jq -n \
        --arg name "$name" \
        --arg port "$port" \
        --arg vnc_port "$vnc_port" \
        --arg created "$(date -Iseconds)" \
        '{name: $name, port: $port, vnc_port: $vnc_port, created: $created}')
    
    jq ". += [$entry]" "$REGISTRY_FILE" > "${REGISTRY_FILE}.tmp" && mv "${REGISTRY_FILE}.tmp" "$REGISTRY_FILE"
}

# Container aus Registry entfernen
remove_from_registry() {
    local name=$1
    jq "map(select(.name != \"$name\"))" "$REGISTRY_FILE" > "${REGISTRY_FILE}.tmp" && mv "${REGISTRY_FILE}.tmp" "$REGISTRY_FILE"
}

# Container-Info aus Registry holen
get_container_info() {
    local name=$1
    jq -r ".[] | select(.name == \"$name\")" "$REGISTRY_FILE"
}

# Alle Container aus Registry holen
get_all_containers() {
    init_registry
    jq -r '.[] | .name' "$REGISTRY_FILE"
}

# Generiere kryptischen Namen
generate_cryptic_name() {
    local prefix=${1:-"sandbox"}
    echo "${prefix}-$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 12 | head -n 1)"
}
