#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

container_name="sandbox-VRzsDvRyULCH"

echo -e "${YELLOW}=== Container Diagnose ===${NC}"

# 1. Container Status
echo -e "\n${BLUE}1. Container Status:${NC}"
docker ps -a | grep $container_name

# 2. Stoppe den Container komplett
echo -e "\n${BLUE}2. Stoppe Container komplett:${NC}"
docker stop $container_name
docker update --restart=no $container_name

# 3. Zeige Logs
echo -e "\n${BLUE}3. Container Logs:${NC}"
docker logs --tail 50 $container_name

# 4. Inspiziere Container
echo -e "\n${BLUE}4. Container Restart Policy:${NC}"
docker inspect $container_name | grep -A 5 "RestartPolicy"

# 5. Start im Vordergrund zum Debuggen
echo -e "\n${YELLOW}Möchten Sie den Container interaktiv debuggen? (j/n)${NC}"
read -n 1 -r
echo
if [[ $REPLY =~ ^[Jj]$ ]]; then
    docker run -it --rm \
        -p 6081:6081 \
        -p 5901:5901 \
        --name debug-sandbox \
        albert-sandbox:latest \
        bash
fi
