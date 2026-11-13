#!/bin/bash

echo "Starting VNC server with 1280x720 resolution..."
rm -rf /tmp/.X*-lock /tmp/.X11-unix

# VNC password: prefer env VNC_PASSWORD; otherwise generate a random one.
# tightvnc truncates passwords at 8 chars, so we cap/generate accordingly.
if [ -z "${VNC_PASSWORD}" ]; then
	VNC_PASSWORD="$(openssl rand -base64 9 | tr -dc 'A-Za-z0-9' | head -c 8)"
	echo "Generated random VNC password (length 8)."
fi
VNC_PASSWORD_TRUNC="$(printf '%s' "${VNC_PASSWORD}" | cut -c1-8)"
export VNC_PASSWORD_TRUNC

su - ubuntu -c "mkdir -p ~/.vnc"
su - ubuntu -c "printf '%s' \"${VNC_PASSWORD_TRUNC}\" | vncpasswd -f > ~/.vnc/passwd"
su - ubuntu -c "chmod 600 ~/.vnc/passwd"

# Internal tokens, set by the manager: the file service and MCP Hub only accept
# requests that nginx forwards after checking the client's API key.
if [ -z "${FILE_SERVICE_TOKEN}" ]; then
	echo "Warning: FILE_SERVICE_TOKEN not set; the file service runs without authentication." >&2
fi
export FILE_SERVICE_TOKEN
if [ -n "${MCPHUB_BEARER_TOKEN:-}" ]; then
	# MCP Hub migrates routing.bearerAuthKey into its (hashed) key list on start;
	# starting from the key alone keeps the active key equal to this token.
	python3 - <<'PY'
import json
import os

path = "/app/mcp_settings.json"
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
routing = data.setdefault("systemConfig", {}).setdefault("routing", {})
routing["enableBearerAuth"] = True
routing["bearerAuthKey"] = os.environ["MCPHUB_BEARER_TOKEN"]
data.pop("bearerKeys", None)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
PY
	unset MCPHUB_BEARER_TOKEN
else
	echo "Warning: MCPHUB_BEARER_TOKEN not set; MCP Hub keeps the key of the image." >&2
fi

# Start VNC with 1280x720 resolution
su - ubuntu -c 'tightvncserver :1 -geometry 1280x720 -depth 24 -rfbauth ~/.vnc/passwd' &

echo "Waiting for VNC to start..."
sleep 5

echo "Starting noVNC..."
websockify --web=/usr/share/novnc/ 6081 localhost:5901 &

echo init upload directory
# Belongs to the desktop user: the AI writes results there without sudo.
mkdir -p /tmp/albert-files
chown ubuntu:ubuntu /tmp/albert-files
chmod 775 /tmp/albert-files

echo "Starting MCP Hub..."
mkdir /tmp/playwright-mcp-output
chmod 777 /tmp/playwright-mcp-output
su - ubuntu -c 'export DISPLAY=:1.0 && cd /app && MCP_HUB_ADMIN_PASSWORD=albert PORT=3000 mcphub &'
#cd /app && MCP_HUB_ADMIN_PASSWORD=albert PORT=3000 mcphub &

echo "Waiting for XFCE to initialize..."
sleep 5

# Create a script to set the background that runs when XFCE is fully loaded
su - ubuntu -c 'cat > /tmp/set_background.sh << '"'"'EOF'"'"'
#!/bin/bash
export DISPLAY=:1
sleep 10

# Wait for xfconf to be available
while ! pgrep -x "xfconfd" > /dev/null; do
    sleep 1
done

# Try different monitor configurations
for monitor in monitorVNC-0 monitor0 monitor1 monitordisplay1; do
    echo "Trying monitor: $monitor"
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/$monitor/workspace0/last-image -s /usr/share/pixmaps/desktop-background.jpg 2>/dev/null
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/$monitor/workspace0/image-style -s 5 2>/dev/null
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/$monitor/workspace0/image-show -s true 2>/dev/null
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/$monitor/workspace1/last-image -s /usr/share/pixmaps/desktop-background.jpg 2>/dev/null
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/$monitor/workspace1/image-style -s 5 2>/dev/null
    xfconf-query -c xfce4-desktop -p /backdrop/screen0/$monitor/workspace1/image-show -s true 2>/dev/null
done

# Also try without monitor specification (fallback)
xfconf-query -c xfce4-desktop -p /backdrop/screen0/workspace0/last-image -s /usr/share/pixmaps/desktop-background.jpg 2>/dev/null
xfconf-query -c xfce4-desktop -p /backdrop/screen0/workspace0/image-style -s 5 2>/dev/null
xfconf-query -c xfce4-desktop -p /backdrop/screen0/workspace0/image-show -s true 2>/dev/null

# Force desktop refresh
sleep 2
killall xfdesktop 2>/dev/null
sleep 1
xfdesktop --reload 2>/dev/null &

echo "Background set successfully"
EOF'

su - ubuntu -c 'chmod +x /tmp/set_background.sh'
su - ubuntu -c '/tmp/set_background.sh' &

echo "Starting File Service..."
export FILE_SERVICE_PORT=${FILE_SERVICE_PORT:-4000}
python3 /app/file_service.py &

echo "Services started. Keeping container alive..."
tail -f /dev/null
