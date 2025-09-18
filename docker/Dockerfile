FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:1
ENV VNC_PORT=5901
ENV NO_VNC_PORT=6081
ENV MCP_HUB_PORT=3000

# System updates and desktop with classic browser packages
RUN apt-get update && apt-get upgrade -y && \
	apt-get install -y \
	xfce4 \
	xfce4-goodies \
	xfce4-terminal \
	xfconf \
	tightvncserver \
	novnc \
	websockify \
	wget \
	curl \
	git \
	nano \
	vim \
	dbus-x11 \
	x11-utils \
	x11-xserver-utils \
	sudo \
	net-tools \
	software-properties-common \
	gnupg \
	lsb-release \
	ca-certificates \
	python3-pip \
	&& apt-get clean

# Install Node.js (Latest LTS) and npm for MCP Hub
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - && \
	apt-get install -y nodejs && \
	npm install -g npm@latest

# Install MCP Hub and MCP Servers
# NOTE: Work around missing runtime dep in mcphub by installing i18next into mcphub's own node_modules
RUN npm install -g @samanhappy/mcphub@latest && \
	( npm i -g --omit=dev i18next@latest i18next-fs-backend@latest || true ) && \
    mkdir -p /app/frontend && \
	ln -s /usr/lib/node_modules/@samanhappy/mcphub/frontend/dist /app/frontend/dist && \
	cd /app && \
	git clone https://github.com/samanhappy/mcphub.git && \
	cp mcphub/package.json . && \
	npm install -g @playwright/mcp@latest

# Fallback: extend Node module resolution to global path
ENV NODE_PATH=/usr/lib/node_modules

# Install additional dependencies for Playwright MCP
RUN npx playwright install --with-deps chromium

# Install Firefox via Mozilla PPA (not Snap)
RUN add-apt-repository -y ppa:mozillateam/ppa && \
	echo 'Package: *' > /etc/apt/preferences.d/mozilla-firefox && \
	echo 'Pin: release o=LP-PPA-mozillateam' >> /etc/apt/preferences.d/mozilla-firefox && \
	echo 'Pin-Priority: 1001' >> /etc/apt/preferences.d/mozilla-firefox && \
	apt-get update && \
	apt-get install -y firefox

# Install Google Chrome (directly from Google)
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - && \
	echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
	apt-get update && \
	apt-get install -y google-chrome-stable

# Chromium as alternative (without Snap)
RUN echo "deb http://archive.ubuntu.com/ubuntu/ focal main universe" >> /etc/apt/sources.list && \
	apt-get update && \
	apt-get install -y chromium-browser || true

# Cleanup
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# Create user without password
RUN useradd -m -s /bin/bash -G sudo ubuntu && \
	echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# VNC setup for XFCE
RUN mkdir -p /home/ubuntu/.vnc && \
	echo "#!/bin/bash" > /home/ubuntu/.vnc/xstartup && \
	echo "unset SESSION_MANAGER" >> /home/ubuntu/.vnc/xstartup && \
	echo "unset DBUS_SESSION_BUS_ADDRESS" >> /home/ubuntu/.vnc/xstartup && \
	echo "startxfce4 &" >> /home/ubuntu/.vnc/xstartup && \
	chmod +x /home/ubuntu/.vnc/xstartup && \
	chown -R ubuntu:ubuntu /home/ubuntu

# Create autostart entry for setting background
RUN mkdir -p /home/ubuntu/.config/autostart && \
	echo '[Desktop Entry]' > /home/ubuntu/.config/autostart/set-background.desktop && \
	echo 'Type=Application' >> /home/ubuntu/.config/autostart/set-background.desktop && \
	echo 'Name=Set Background' >> /home/ubuntu/.config/autostart/set-background.desktop && \
	echo 'Exec=bash -c "sleep 5; for monitor in monitorVNC-0 monitor0 monitor1 monitordisplay1; do xfconf-query -c xfce4-desktop -p /backdrop/screen0/\$monitor/workspace0/last-image -s /usr/share/pixmaps/desktop-background.jpg 2>/dev/null; xfconf-query -c xfce4-desktop -p /backdrop/screen0/\$monitor/workspace0/image-style -s 5 2>/dev/null; xfconf-query -c xfce4-desktop -p /backdrop/screen0/\$monitor/workspace0/image-show -s true 2>/dev/null; done; xfdesktop --reload"' >> /home/ubuntu/.config/autostart/set-background.desktop && \
	echo 'Hidden=false' >> /home/ubuntu/.config/autostart/set-background.desktop && \
	echo 'NoDisplay=false' >> /home/ubuntu/.config/autostart/set-background.desktop && \
	echo 'X-GNOME-Autostart-enabled=true' >> /home/ubuntu/.config/autostart/set-background.desktop && \
	chown -R ubuntu:ubuntu /home/ubuntu/.config

# Configure XFCE to disable problematic plugins
RUN mkdir -p /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml && \
	echo '<?xml version="1.0" encoding="UTF-8"?>' > /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '<channel name="xfce4-panel" version="1.0">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '  <property name="configver" type="int" value="2"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '  <property name="panels" type="array">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    <value type="int" value="1"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    <property name="panel-1" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '      <property name="position" type="string" value="p=6;x=0;y=0"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '      <property name="length" type="uint" value="100"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '      <property name="position-locked" type="bool" value="true"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '      <property name="size" type="uint" value="30"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '      <property name="plugin-ids" type="array">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '        <value type="int" value="1"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '        <value type="int" value="2"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '        <value type="int" value="3"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '        <value type="int" value="4"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '      </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '  </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '  <property name="plugins" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    <property name="plugin-1" type="string" value="applicationsmenu"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    <property name="plugin-2" type="string" value="tasklist"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    <property name="plugin-3" type="string" value="separator"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '    <property name="plugin-4" type="string" value="clock"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '  </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	echo '</channel>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml && \
	chown -R ubuntu:ubuntu /home/ubuntu/.config

# Disable XFCE session management and power manager to prevent error dialogs
RUN mkdir -p /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml && \
	echo '<?xml version="1.0" encoding="UTF-8"?>' > /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '<channel name="xfce4-session" version="1.0">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '  <property name="startup" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '    <property name="screensaver" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '      <property name="enabled" type="bool" value="false"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '    </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '  </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '  <property name="general" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '    <property name="FailsafeSessionName" type="string" value="Failsafe"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '    <property name="SessionName" type="string" value="Default"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '    <property name="SaveOnExit" type="bool" value="false"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '  </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '</channel>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml && \
	echo '<?xml version="1.0" encoding="UTF-8"?>' > /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	echo '<channel name="xfce4-power-manager" version="1.0">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	echo '  <property name="xfce4-power-manager" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	echo '    <property name="power-button-action" type="uint" value="3"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	echo '    <property name="show-tray-icon" type="bool" value="false"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	echo '  </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	echo '</channel>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml && \
	chown -R ubuntu:ubuntu /home/ubuntu/.config

# Copy and configure desktop background
COPY desktop-background.jpg /usr/share/pixmaps/desktop-background.jpg
RUN chmod 644 /usr/share/pixmaps/desktop-background.jpg

# Copy MCP Hub configuration
COPY mcp_settings.json /app/mcp_settings.json
RUN mkdir -p /app && \
	chown -R ubuntu:ubuntu /app && \
	mkdir -p /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml && \
	echo '<?xml version="1.0" encoding="UTF-8"?>' > /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '<channel name="xfce4-desktop" version="1.0">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '  <property name="backdrop" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '    <property name="screen0" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '      <property name="monitorVNC-0" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        <property name="workspace0" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="color-style" type="int" value="0"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-style" type="int" value="5"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="last-image" type="string" value="/usr/share/pixmaps/desktop-background.jpg"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-show" type="bool" value="true"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        <property name="workspace1" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="color-style" type="int" value="0"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-style" type="int" value="5"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="last-image" type="string" value="/usr/share/pixmaps/desktop-background.jpg"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-show" type="bool" value="true"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        <property name="workspace2" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="color-style" type="int" value="0"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-style" type="int" value="5"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="last-image" type="string" value="/usr/share/pixmaps/desktop-background.jpg"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-show" type="bool" value="true"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        <property name="workspace3" type="empty">' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="color-style" type="int" value="0"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-style" type="int" value="5"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="last-image" type="string" value="/usr/share/pixmaps/desktop-background.jpg"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '          <property name="image-show" type="bool" value="true"/>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '        </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '      </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '    </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '  </property>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	echo '</channel>' >> /home/ubuntu/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml && \
	chown -R ubuntu:ubuntu /home/ubuntu/.config

# Create desktop shortcuts
RUN mkdir -p /home/ubuntu/Desktop && \
	echo "[Desktop Entry]" > /home/ubuntu/Desktop/firefox.desktop && \
	echo "Version=1.0" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Type=Application" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Name=Firefox" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Comment=Browse the Web" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Exec=firefox %u" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Icon=firefox" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Terminal=false" >> /home/ubuntu/Desktop/firefox.desktop && \
	echo "Categories=Network;WebBrowser;" >> /home/ubuntu/Desktop/firefox.desktop && \
	chmod +x /home/ubuntu/Desktop/firefox.desktop && \
	\
	echo "[Desktop Entry]" > /home/ubuntu/Desktop/chrome.desktop && \
	echo "Version=1.0" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Type=Application" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Name=Google Chrome" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Comment=Browse the Web" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Exec=google-chrome --no-sandbox %u" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Icon=google-chrome" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Terminal=false" >> /home/ubuntu/Desktop/chrome.desktop && \
	echo "Categories=Network;WebBrowser;" >> /home/ubuntu/Desktop/chrome.desktop && \
	chmod +x /home/ubuntu/Desktop/chrome.desktop && \
	\
	echo "[Desktop Entry]" > /home/ubuntu/Desktop/terminal.desktop && \
	echo "Version=1.0" >> /home/ubuntu/Desktop/terminal.desktop && \
	echo "Type=Application" >> /home/ubuntu/Desktop/terminal.desktop && \
	echo "Name=Terminal" >> /home/ubuntu/Desktop/terminal.desktop && \
	echo "Comment=Terminal" >> /home/ubuntu/Desktop/terminal.desktop && \
	echo "Exec=xfce4-terminal" >> /home/ubuntu/Desktop/terminal.desktop && \
	echo "Icon=utilities-terminal" >> /home/ubuntu/Desktop/terminal.desktop && \
	echo "Terminal=false" >> /home/ubuntu/Desktop/terminal.desktop && \
	chmod +x /home/ubuntu/Desktop/terminal.desktop && \
	chown -R ubuntu:ubuntu /home/ubuntu/Desktop

# noVNC Setup
RUN ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# Copy startup script
COPY startup.sh /startup.sh
RUN chmod +x /startup.sh

EXPOSE ${VNC_PORT}
EXPOSE ${NO_VNC_PORT}
EXPOSE ${MCP_HUB_PORT}

USER root
WORKDIR /home/ubuntu

CMD ["/bin/bash", "/startup.sh"]
