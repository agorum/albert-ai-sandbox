#!/bin/bash
set -e

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Basis-Variablen
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_DIR="/opt/albert-sandbox-manager"
NGINX_CONF_DIR="/etc/nginx/sites-available"
NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}ALBERT Sandbox Manager Installation${NC}"
echo -e "${GREEN}========================================${NC}"

# Root-Check
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}Dieses Script muss als root ausgeführt werden${NC}" 
   exit 1
fi

# Mache alle Shell-Scripte ausführbar
echo -e "${YELLOW}Setze Ausführungsrechte...${NC}"
find "${SCRIPT_DIR}" -type f -name "*.sh" -exec chmod +x {} \;

# System Update
echo -e "${YELLOW}Aktualisiere System...${NC}"
apt-get update
apt-get upgrade -y

# Installiere Abhängigkeiten
echo -e "${YELLOW}Installiere Abhängigkeiten...${NC}"
apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    nginx \
    jq \
    git \
    python3 \
    python3-pip \
    net-tools \
    dos2unix

# Docker Installation
echo -e "${YELLOW}Installiere Docker...${NC}"
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
    systemctl enable docker
    systemctl start docker
else
    echo -e "${GREEN}Docker ist bereits installiert${NC}"
fi

# Erstelle Verzeichnisstruktur
echo -e "${YELLOW}Erstelle Verzeichnisstruktur...${NC}"
mkdir -p ${INSTALL_DIR}/{scripts,docker,config,nginx}

# Kopiere alle Dateien
echo -e "${YELLOW}Kopiere Dateien...${NC}"
cp -r ${SCRIPT_DIR}/scripts/* ${INSTALL_DIR}/scripts/ 2>/dev/null || {
    echo -e "${YELLOW}Scripts-Verzeichnis nicht gefunden, überspringe...${NC}"
}
cp -r ${SCRIPT_DIR}/docker/* ${INSTALL_DIR}/docker/ 2>/dev/null || {
    echo -e "${YELLOW}Docker-Verzeichnis nicht gefunden, überspringe...${NC}"
}
cp -r ${SCRIPT_DIR}/config/* ${INSTALL_DIR}/config/ 2>/dev/null || true

# Konvertiere nochmals alle kopierten Shell-Scripte zu Unix-Zeilenenden
echo -e "${YELLOW}Konvertiere Zeilenenden der installierten Scripte...${NC}"
find "${INSTALL_DIR}" -type f -name "*.sh" -exec dos2unix {} \; 2>/dev/null || {
    find "${INSTALL_DIR}" -type f -name "*.sh" -exec sed -i 's/\r$//' {} \;
}

# Rechte setzen
echo -e "${YELLOW}Setze Berechtigungen...${NC}"
chmod +x ${INSTALL_DIR}/scripts/*.sh 2>/dev/null || true
chmod +x ${INSTALL_DIR}/docker/startup.sh 2>/dev/null || true
chmod +x ${INSTALL_DIR}/docker/*.sh 2>/dev/null || true

# Registry initialisieren
if [ ! -f "${INSTALL_DIR}/config/container-registry.json" ]; then
    echo "[]" > ${INSTALL_DIR}/config/container-registry.json
fi

# Prüfe ob Dockerfile existiert
if [ ! -f "${INSTALL_DIR}/docker/Dockerfile" ]; then
    echo -e "${RED}Fehler: Dockerfile nicht gefunden!${NC}"
    echo -e "${YELLOW}Bitte stellen Sie sicher, dass alle Dateien korrekt kopiert wurden.${NC}"
    exit 1
fi

# Docker Image bauen
echo -e "${YELLOW}Baue Docker Image...${NC}"
cd ${INSTALL_DIR}/docker
docker build -t albert-sandbox:latest . || {
    echo -e "${RED}Fehler beim Bauen des Docker Images!${NC}"
    echo -e "${YELLOW}Bitte überprüfen Sie die Docker-Installation und die Dockerfile.${NC}"
    exit 1
}

# Nginx konfigurieren
echo -e "${YELLOW}Konfiguriere Nginx...${NC}"

# WICHTIG: Bereinige doppelte Includes VOR dem Setup
echo -e "${YELLOW}Bereinige Nginx Konfiguration...${NC}"
if [ -f "/etc/nginx/sites-enabled/default" ]; then
    # Backup erstellen
    #cp /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/default.backup.$(date +%Y%m%d_%H%M%S)
    
    # Entferne ALLE alten ALBERT-Sandbox Includes
    sed -i '/# ALBERT Sandbox Configs/d' /etc/nginx/sites-enabled/default
    sed -i '/include .*albert-\*.conf;/d' /etc/nginx/sites-enabled/default
    
    # Füge EINEN neuen Include hinzu
    sed -i '/server_name _;/a\\n\t# ALBERT Sandbox Configs\n\tinclude /etc/nginx/sites-available/albert-*.conf;' /etc/nginx/sites-enabled/default
    
    echo -e "${GREEN}✓ Nginx Konfiguration bereinigt${NC}"
fi

# Nginx starten/neustarten
systemctl enable nginx
nginx -t && systemctl reload nginx || {
    echo -e "${YELLOW}Nginx konnte nicht neu geladen werden, versuche Neustart...${NC}"
    systemctl restart nginx
}

# Symlink für einfachen Zugriff
echo -e "${YELLOW}Erstelle Symlink für globalen Zugriff...${NC}"
ln -sf ${INSTALL_DIR}/scripts/albert-sandbox-manager.sh /usr/local/bin/albert-sandbox-manager

# Überprüfe Installation
echo -e "${YELLOW}Überprüfe Installation...${NC}"
if [ -f "/usr/local/bin/albert-sandbox-manager" ] && [ -x "/usr/local/bin/albert-sandbox-manager" ]; then
    echo -e "${GREEN}✓ albert-sandbox-manager erfolgreich installiert${NC}"
else
    echo -e "${RED}✗ albert-sandbox-manager Installation fehlgeschlagen${NC}"
    exit 1
fi

if docker images | grep -q "albert-sandbox"; then
    echo -e "${GREEN}✓ Docker Image erfolgreich gebaut${NC}"
else
    echo -e "${RED}✗ Docker Image nicht gefunden${NC}"
    exit 1
fi

if systemctl is-active --quiet nginx; then
    echo -e "${GREEN}✓ Nginx läuft${NC}"
else
    echo -e "${RED}✗ Nginx läuft nicht${NC}"
fi

# Finale Nginx-Konfiguration prüfen
echo -e "${YELLOW}Prüfe finale Nginx-Konfiguration...${NC}"
include_count=$(grep -c "include.*albert-\*.conf;" /etc/nginx/sites-enabled/default 2>/dev/null || echo 0)
if [ "$include_count" -eq 1 ]; then
    echo -e "${GREEN}✓ Nginx Include korrekt konfiguriert (1 Include gefunden)${NC}"
elif [ "$include_count" -gt 1 ]; then
    echo -e "${RED}⚠ Warnung: Mehrere Includes gefunden ($include_count)${NC}"
    echo -e "${YELLOW}  Führe aus: nano /etc/nginx/sites-enabled/default${NC}"
    echo -e "${YELLOW}  und entferne doppelte Include-Zeilen${NC}"
else
    echo -e "${YELLOW}⚠ Kein Nginx Include gefunden${NC}"
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Installation abgeschlossen!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Verwendung:"
echo "  albert-sandbox-manager create        - Neuen Sandbox mit kryptischem Namen"
echo "  albert-sandbox-manager create <name> - Neuen Sandbox mit eigenem Namen"
echo "  albert-sandbox-manager list          - Container auflisten"
echo "  albert-sandbox-manager status        - Status anzeigen"
echo "  albert-sandbox-manager help          - Hilfe anzeigen"
echo ""
echo -e "${YELLOW}Info: VNC-Passwort für alle Container: albert${NC}"
echo ""
echo "Beispiel:"
echo "  albert-sandbox-manager create"
echo ""
echo -e "${YELLOW}Erste Test-Sandbox erstellen? (j/n)${NC}"
read -n 1 -r
echo
if [[ $REPLY =~ ^[Jj]$ ]]; then
    albert-sandbox-manager create test-sandbox || {
        echo -e "${RED}Fehler beim Erstellen der Test-Sandbox${NC}"
        echo -e "${YELLOW}Versuchen Sie: albert-sandbox-manager create${NC}"
    }
fi