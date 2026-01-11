#!/bin/bash

# Ultra-simple DockUp installer
# Shows every single command

echo "Starting DockUp installation..."
echo "Current user: $USER"
echo "Current directory: $(pwd)"
echo ""

# Don't exit on errors - show them instead
set +e

echo "[1/12] Checking if running as root..."
if [ "$EUID" -eq 0 ]; then
    echo "ERROR: Don't run as root. Run as normal user."
    echo "The script will ask for sudo password when needed."
    exit 1
fi
echo "OK - Running as normal user"
echo ""

echo "[2/12] Updating package lists..."
sudo apt update
echo "Done"
echo ""

echo "[3/12] Installing Python3 and build tools..."
sudo apt install -y python3 python3-pip python3-venv python3-dev build-essential
echo "Done"
echo ""

echo "[4/12] Installing Docker..."
sudo apt install -y docker.io docker-compose-plugin
echo "Done"
echo ""

echo "[5/12] Installing Git..."
sudo apt install -y git
echo "Done"
echo ""

echo "[6/12] Installing Trivy dependencies..."
sudo apt install -y wget apt-transport-https gnupg lsb-release
echo "Done"
echo ""

echo "[7/12] Adding user to docker group..."
sudo usermod -aG docker $USER
echo "Done - You'll need to log out and back in for this to take effect"
echo ""

echo "[8/12] Cloning DockUp from GitHub..."
if [ -d "/opt/dockup" ]; then
    echo "Warning: /opt/dockup already exists"
    read -p "Delete it and continue? (y/n) " -r
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo rm -rf /opt/dockup
    else
        echo "Cancelled"
        exit 0
    fi
fi

sudo git clone https://github.com/ChristoffBo/dockup.git /opt/dockup
if [ ! -d "/opt/dockup" ]; then
    echo "ERROR: Clone failed. Check internet connection."
    exit 1
fi
sudo chown -R $USER:$USER /opt/dockup
echo "Done"
echo ""

echo "[9/12] Setting up Python virtual environment..."
cd /opt/dockup/dockup || exit 1
python3 -m venv venv
if [ ! -d "venv" ]; then
    echo "ERROR: Virtual environment creation failed"
    exit 1
fi
echo "Done"
echo ""

echo "[10/12] Installing Python packages (this takes 1-2 minutes)..."
source venv/bin/activate

# CRITICAL: Upgrade pip first
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "ERROR: pip install failed"
    exit 1
fi

# Verify critical packages
python3 -c "import psutil, flask, docker" || { echo "ERROR: Critical packages missing"; exit 1; }
echo "Done"
echo ""

echo "[11/12] Creating directories..."
sudo mkdir -p /app/data /app/logs /stacks
sudo chown -R $USER:$USER /app /stacks
echo "Done"
echo ""

echo "[12/12] Creating systemd service..."
read -p "Enter timezone (default: UTC): " TZ
TZ=${TZ:-UTC}

sudo bash -c "cat > /etc/systemd/system/dockup.service" <<EOF
[Unit]
Description=DockUp
After=docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/dockup/dockup
Environment="PATH=/opt/dockup/dockup/venv/bin:/usr/bin"
Environment="PORT=5000"
Environment="TZ=$TZ"
ExecStart=/opt/dockup/dockup/venv/bin/python3 /opt/dockup/dockup/app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dockup
echo "Done"
echo ""

echo "Creating update script..."
cat > /opt/dockup/update.sh <<'EOF'
#!/bin/bash
sudo systemctl stop dockup
cd /opt/dockup
git pull
cd dockup
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl start dockup
sleep 2
sudo systemctl status dockup
EOF
chmod +x /opt/dockup/update.sh
echo "Done"
echo ""

echo "========================================="
echo "Installation Complete!"
echo "========================================="
echo ""
echo "IMPORTANT: Log out and back in, then run:"
echo "  sudo systemctl start dockup"
echo ""
echo "Then open: http://localhost:5000"
echo ""
echo "Commands:"
echo "  sudo systemctl start dockup"
echo "  sudo systemctl stop dockup"
echo "  sudo systemctl status dockup"
echo "  sudo journalctl -u dockup -f"
echo "  /opt/dockup/update.sh"
echo ""
