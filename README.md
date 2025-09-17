# ALBERT Sandbox Manager

Ein Docker-basiertes System zur Verwaltung isolierter Desktop-Umgebungen mit Browser-Zugriff über noVNC.

## Features

- 🖥️ Ubuntu 22.04 Desktop-Umgebung
- 🌐 Browser-Zugriff ohne Client-Software (noVNC)
- 🔒 Sicherheit durch kryptische Container-Namen (kein Passwort nötig)
- 🚀 Firefox und Chromium vorinstalliert
- 💾 Persistente Daten über Docker Volumes
- 🔧 Einfache Verwaltung über CLI

## Installation

```bash
git clone <repository-url> albert-sandbox-manager
cd albert-sandbox-manager
bash prepare.sh
./install.sh
```

## Aufruf
```bash
cd /opt/albert-sandbox-manager

albert-sandbox-manager create        - Neuen Sandbox mit kryptischem Namen
albert-sandbox-manager create <name> - Neuen Sandbox mit eigenem Namen
albert-sandbox-manager list          - Container auflisten
albert-sandbox-manager status        - Status anzeigen
albert-sandbox-manager help          - Hilfe anzeigen
```