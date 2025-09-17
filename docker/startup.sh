#!/bin/bash

echo "Starting VNC server with 1280x720 resolution..."
rm -rf /tmp/.X*-lock /tmp/.X11-unix

# Setze automatisch das Passwort "albert"
su - ubuntu -c "mkdir -p ~/.vnc"
su - ubuntu -c "echo 'albert' | vncpasswd -f > ~/.vnc/passwd"
su - ubuntu -c "chmod 600 ~/.vnc/passwd"

# Starte VNC mit 1280x720 Auflösung
su - ubuntu -c 'tightvncserver :1 -geometry 1280x720 -depth 24 -rfbauth ~/.vnc/passwd' &

echo "Waiting for VNC to start..."
sleep 5

echo "Starting noVNC..."
websockify --web=/usr/share/novnc/ 6081 localhost:5901 &

echo "Services started. Keeping container alive..."
tail -f /dev/null
