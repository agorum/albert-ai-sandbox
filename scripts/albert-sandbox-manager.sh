#!/bin/bash

source /opt/albert-sandbox-manager/scripts/common.sh
source /opt/albert-sandbox-manager/scripts/port-manager.sh
source /opt/albert-sandbox-manager/scripts/nginx-manager.sh

DOCKER_IMAGE="albert-sandbox:latest"

# Hilfe anzeigen
show_help() {
    echo -e "${GREEN}ALBERT Sandbox Manager${NC}"
    echo -e "${GREEN}=======================${NC}"
    echo "Verwendung: $0 [BEFEHL] [OPTIONEN]"
    echo ""
    echo "Befehle:"
    echo "  create [name]     - Erstellt einen neuen Sandbox Container"
    echo "                      (ohne Name wird kryptischer Name generiert)"
    echo "  remove <name>     - Entfernt einen Container"
    echo "  start <name>      - Startet einen Container"
    echo "  stop <name>       - Stoppt einen Container"
    echo "  restart <name>    - Neustart eines Containers"
    echo "  status [name]     - Zeigt Status (eines) Container(s)"
    echo "  list              - Listet alle Container auf"
    echo "  build             - Baut das Docker Image neu"
    echo "  help              - Zeigt diese Hilfe"
    echo ""
    echo "VNC Passwort: albert"
    echo ""
    echo "Beispiele:"
    echo "  $0 create                  # Erstellt Container mit kryptischem Namen"
    echo "  $0 create mysandbox        # Erstellt Container mit eigenem Namen"
    echo "  $0 status"
    echo "  $0 list"
}

# Docker Image bauen
build_image() {
    echo -e "${YELLOW}Baue Docker Image ...${NC}"
    cd /opt/albert-sandbox-manager/docker
    docker build -t $DOCKER_IMAGE .
    echo -e "${GREEN}Image erfolgreich gebaut${NC}"
}

# Container erstellen
create_container() {
    local name=$1
    
    # Wenn kein Name angegeben, generiere kryptischen Namen
    if [ -z "$name" ]; then
        name=$(generate_cryptic_name)
        echo -e "${BLUE}Generiere kryptischen Container-Namen: ${name}${NC}"
    fi
    
    # Prüfe ob Container bereits existiert
    if docker ps -a --format '{{.Names}}' | grep -q "^${name}$"; then
        echo -e "${RED}Fehler: Container '$name' existiert bereits${NC}"
        return 1
    fi
    
    # Finde freie Ports
    local novnc_port=$(find_free_novnc_port)
    local vnc_port=$(find_free_vnc_port)
    
    if [ -z "$novnc_port" ] || [ -z "$vnc_port" ]; then
        echo -e "${RED}Fehler: Keine freien Ports verfügbar${NC}"
        return 1
    fi
    
    echo -e "${YELLOW}Erstelle Sandbox Container '$name'...${NC}"
    echo -e "${BLUE}  noVNC Port: $novnc_port${NC}"
    echo -e "${BLUE}  VNC Port: $vnc_port${NC}"
    
    # Docker Container erstellen
    docker run -d \
        --name "$name" \
        --restart unless-stopped \
        --cap-add=SYS_ADMIN \
        --security-opt seccomp=unconfined \
        -p ${novnc_port}:6081 \
        -p ${vnc_port}:5901 \
        -e VNC_PORT=5901 \
        -e NO_VNC_PORT=6081 \
        -v ${name}_data:/home/ubuntu \
        --shm-size=2g \
        $DOCKER_IMAGE
    
    if [ $? -eq 0 ]; then
        # In Registry eintragen
        add_to_registry "$name" "$novnc_port" "$vnc_port"
        
        # Nginx konfigurieren
        create_nginx_config "$name" "$novnc_port"
        
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}Sandbox Container erfolgreich erstellt!${NC}"
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}Name: ${name}${NC}"
        echo -e "${GREEN}URL: http://$(hostname -I | awk '{print $1}')/${name}/${NC}"
        echo -e "${YELLOW}VNC Passwort: albert${NC}"
        echo -e "${YELLOW}Wichtig: Notiere dir die URL - der Name ist der Zugriffsschutz!${NC}"
    else
        echo -e "${RED}Fehler beim Erstellen des Containers${NC}"
        return 1
    fi
}

# Container entfernen
remove_container() {
    local name=$1
    
    if [ -z "$name" ]; then
        echo -e "${RED}Fehler: Container-Name erforderlich${NC}"
        return 1
    fi
    
    echo -e "${YELLOW}Entferne Container '$name'...${NC}"
    
    # Container stoppen und entfernen
    docker stop "$name" 2>/dev/null
    docker rm "$name" 2>/dev/null
    
    # Volume entfernen (optional)
    read -p "Daten-Volume ebenfalls löschen? (j/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Jj]$ ]]; then
        docker volume rm "${name}_data" 2>/dev/null
    fi
    
    # Nginx Config entfernen
    remove_nginx_config "$name"
    
    # Aus Registry entfernen
    remove_from_registry "$name"
    
    echo -e "${GREEN}Container '$name' wurde entfernt${NC}"
}

# Container starten
start_container() {
    local name=$1
    
    if [ -z "$name" ]; then
        echo -e "${RED}Fehler: Container-Name erforderlich${NC}"
        return 1
    fi
    
    echo -e "${YELLOW}Starte Container '$name'...${NC}"
    docker start "$name"
    
    if [ $? -eq 0 ]; then
        local info=$(get_container_info "$name")
        echo -e "${GREEN}Container '$name' gestartet${NC}"
        echo -e "${GREEN}URL: http://$(hostname -I | awk '{print $1}')/${name}/${NC}"
    else
        echo -e "${RED}Fehler beim Starten des Containers${NC}"
        return 1
    fi
}

# Container stoppen
stop_container() {
    local name=$1
    
    if [ -z "$name" ]; then
        echo -e "${RED}Fehler: Container-Name erforderlich${NC}"
        return 1
    fi
    
    echo -e "${YELLOW}Stoppe Container '$name'...${NC}"
    docker stop "$name"
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}Container '$name' gestoppt${NC}"
    else
        echo -e "${RED}Fehler beim Stoppen des Containers${NC}"
        return 1
    fi
}

# Container neustarten
restart_container() {
    local name=$1
    
    if [ -z "$name" ]; then
        echo -e "${RED}Fehler: Container-Name erforderlich${NC}"
        return 1
    fi
    
    stop_container "$name"
    start_container "$name"
}

# Status anzeigen
show_status() {
    local name=$1
    
    if [ -z "$name" ]; then
        # Zeige alle Container
        echo -e "${GREEN}Status aller Sandbox Container:${NC}"
        echo -e "${GREEN}=================================${NC}"
        echo -e "${BLUE}Desktop: KDE Plasma${NC}"
        echo "------------------------------"
        
        for container_name in $(get_all_containers); do
            show_single_status "$container_name"
            echo "------------------------------"
        done
    else
        show_single_status "$name"
    fi
}

# Einzelnen Container-Status anzeigen
show_single_status() {
    local name=$1
    local info=$(get_container_info "$name")
    
    if [ -z "$info" ]; then
        echo -e "${RED}Container '$name' nicht in Registry gefunden${NC}"
        return 1
    fi
    
    local port=$(echo "$info" | jq -r '.port')
    local vnc_port=$(echo "$info" | jq -r '.vnc_port')
    local created=$(echo "$info" | jq -r '.created')
    
    echo -e "${BLUE}Container: ${NC}$name"
    echo -e "${BLUE}Erstellt: ${NC}$created"
    echo -e "${BLUE}Desktop: ${NC}KDE Plasma"
    echo -e "${BLUE}noVNC Port: ${NC}$port"
    echo -e "${BLUE}VNC Port: ${NC}$vnc_port"
    
    # Docker Status
    if docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
        echo -e "${BLUE}Docker Status: ${GREEN}Läuft${NC}"
        local stats=$(docker stats --no-stream --format "CPU: {{.CPUPerc}} | RAM: {{.MemUsage}}" "$name" 2>/dev/null)
        echo -e "${BLUE}Ressourcen: ${NC}$stats"
    else
        echo -e "${BLUE}Docker Status: ${RED}Gestoppt${NC}"
    fi
    
    echo -e "${BLUE}URL: ${NC}http://$(hostname -I | awk '{print $1}')/${name}/"
}

# Container auflisten
list_containers() {
    echo -e "${GREEN}ALBERT Sandbox Container:${NC}"
    echo -e "${GREEN}========================================${NC}"
    
    printf "%-30s %-10s %-10s %-10s\n" "NAME" "STATUS" "NOVNC-PORT" "VNC-PORT"
    printf "%-30s %-10s %-10s %-10s\n" "----" "------" "----------" "--------"
    
    for container_name in $(get_all_containers); do
        local info=$(get_container_info "$container_name")
        local port=$(echo "$info" | jq -r '.port')
        local vnc_port=$(echo "$info" | jq -r '.vnc_port')
        
        if docker ps --format '{{.Names}}' | grep -q "^${container_name}$"; then
            local status="${GREEN}Läuft${NC}"
        else
            local status="${RED}Gestoppt${NC}"
        fi
        
        printf "%-30s %-20b %-10s %-10s\n" "$container_name" "$status" "$port" "$vnc_port"
    done
    
    echo ""
    echo -e "${BLUE}Desktop: KDE Plasma | VNC-Passwort: albert${NC}"
    echo ""
    echo -e "${BLUE}Zugriffs-URLs:${NC}"
    for container_name in $(get_all_containers); do
        echo "  http://$(hostname -I | awk '{print $1}')/${container_name}/"
    done
}

# Hauptprogramm
case "$1" in
    create)
        create_container "$2"
        ;;
    remove|delete)
        remove_container "$2"
        ;;
    start)
        start_container "$2"
        ;;
    stop)
        stop_container "$2"
        ;;
    restart)
        restart_container "$2"
        ;;
    status)
        show_status "$2"
        ;;
    list)
        list_containers
        ;;
    build)
        build_image
        ;;
    help|--help|-h|"")
        show_help
        ;;
    *)
        echo -e "${RED}Unbekannter Befehl: $1${NC}"
        show_help
        exit 1
        ;;
esac
