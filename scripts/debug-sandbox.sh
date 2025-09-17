#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}Albert Sandbox Quick Fix${NC}"

# Container name
read -p "Container Name: " container_name

echo -e "\n${YELLOW}1. Stoppe Container...${NC}"
docker stop "$container_name" 2>/dev/null

echo -e "${YELLOW}2. Starte Container neu...${NC}"
docker start "$container_name"

# Warte auf Start
sleep 5

echo -e "${YELLOW}3. Prüfe VNC/noVNC Prozesse...${NC}"
docker exec "$container_name" bash -c "
    # Kill alte Prozesse
    pkill -f vnc || true
    pkill -f websockify || true
    sleep 2
    
    # Starte VNC neu
    su - ubuntu -c 'tightvncserver -kill :1' 2>/dev/null || true
    sleep 1
    su - ubuntu -c 'rm -rf /tmp/.X* /tmp/.x*'
    su - ubuntu -c 'tightvncserver :1 -geometry 1920x1080 -depth 24 -SecurityTypes None'
    sleep 2
    
    # Starte websockify neu
    websockify --web=/usr/share/novnc/ 6081 localhost:5901 &
    sleep 2
    
    # Prüfe ob alles läuft
    ps aux | grep -E '(vnc|websockify)'
"

echo -e "\n${YELLOW}4. Nginx neu laden...${NC}"
nginx -t && systemctl reload nginx

# Hole Port aus Registry
port=$(cat /opt/albert-sandbox-manager/config/container-registry.json | jq -r ".[] | select(.name == \"$container_name\") | .port")

echo -e "\n${GREEN}Container sollte jetzt erreichbar sein unter:${NC}"
echo -e "${GREEN}http://$(hostname -I | awk '{print $1}')/${container_name}/${NC}"
echo -e "${GREEN}Direkt: http://$(hostname -I | awk '{print $1}'):${port}${NC}"
