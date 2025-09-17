#!/bin/bash

# Farben
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}Bereite Installation vor...${NC}"

# Installiere dos2unix falls nicht vorhanden
if ! command -v dos2unix &> /dev/null; then
    echo -e "${YELLOW}Installiere dos2unix...${NC}"
    sudo apt-get update
    sudo apt-get install -y dos2unix
fi

# Konvertiere alle Shell-Scripte
echo -e "${YELLOW}Konvertiere alle Shell-Scripte zu Unix-Format...${NC}"
find . -type f -name "*.sh" -exec dos2unix {} \; 2>/dev/null || {
    find . -type f -name "*.sh" -exec sed -i 's/\r$//' {} \;
}

# Mache alle Shell-Scripte ausführbar
echo -e "${YELLOW}Setze Ausführungsrechte...${NC}"
find . -type f -name "*.sh" -exec chmod +x {} \;

echo -e "${GREEN}Vorbereitung abgeschlossen!${NC}"
echo -e "${GREEN}Führe nun aus: sudo ./install.sh${NC}"
