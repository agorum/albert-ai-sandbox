#!/bin/bash

echo "Starting VNC server with 1280x720 resolution..."
rm -rf /tmp/.X*-lock /tmp/.X11-unix

# Automatically set password "albert"
su - ubuntu -c "mkdir -p ~/.vnc"
su - ubuntu -c "echo 'albert' | vncpasswd -f > ~/.vnc/passwd"
su - ubuntu -c "chmod 600 ~/.vnc/passwd"

# Start VNC with 1280x720 resolution
su - ubuntu -c 'tightvncserver :1 -geometry 1280x720 -depth 24 -rfbauth ~/.vnc/passwd' &

echo "Waiting for VNC to start..."
sleep 5

echo "Starting noVNC..."
websockify --web=/usr/share/novnc/ 6081 localhost:5901 &

echo "Starting MCP Hub..."
cd /app && MCP_HUB_ADMIN_PASSWORD=albert PORT=3000 mcphub &

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

echo "Services started. Keeping container alive..."
tail -f /dev/null
