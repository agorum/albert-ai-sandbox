FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:1
ENV VNC_PORT=5901
ENV NO_VNC_PORT=6081

# System Updates und Desktop mit klassischen Browser-Paketen
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y \
    xfce4 \
    xfce4-goodies \
    xfce4-terminal \
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
    && apt-get clean

# Firefox über Mozilla PPA installieren (nicht Snap)
RUN add-apt-repository -y ppa:mozillateam/ppa && \
    echo 'Package: *' > /etc/apt/preferences.d/mozilla-firefox && \
    echo 'Pin: release o=LP-PPA-mozillateam' >> /etc/apt/preferences.d/mozilla-firefox && \
    echo 'Pin-Priority: 1001' >> /etc/apt/preferences.d/mozilla-firefox && \
    apt-get update && \
    apt-get install -y firefox

# Google Chrome installieren (direkt von Google)
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - && \
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable

# Chromium als Alternative (ohne Snap)
RUN echo "deb http://archive.ubuntu.com/ubuntu/ focal main universe" >> /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y chromium-browser || true

# Aufräumen
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# Benutzer ohne Passwort erstellen
RUN useradd -m -s /bin/bash -G sudo ubuntu && \
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# VNC Setup für XFCE
RUN mkdir -p /home/ubuntu/.vnc && \
    echo "#!/bin/bash" > /home/ubuntu/.vnc/xstartup && \
    echo "unset SESSION_MANAGER" >> /home/ubuntu/.vnc/xstartup && \
    echo "unset DBUS_SESSION_BUS_ADDRESS" >> /home/ubuntu/.vnc/xstartup && \
    echo "startxfce4 &" >> /home/ubuntu/.vnc/xstartup && \
    chmod +x /home/ubuntu/.vnc/xstartup && \
    chown -R ubuntu:ubuntu /home/ubuntu

# Desktop-Verknüpfungen erstellen
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

# Kopiere startup script
COPY startup.sh /startup.sh
RUN chmod +x /startup.sh

EXPOSE ${VNC_PORT}
EXPOSE ${NO_VNC_PORT}

USER root
WORKDIR /home/ubuntu

CMD ["/bin/bash", "/startup.sh"]
