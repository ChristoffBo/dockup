# DockUp Native Installation Guide

Running DockUp directly on your system without Docker.

## Automated Install Script

Copy this entire script and save it as `install-dockup.sh`:

```bash
#!/bin/bash

echo "========================================="
echo "DockUp Native Installation"
echo "========================================="
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "ERROR: Don't run as root. Script will ask for sudo when needed."
    exit 1
fi

echo "[1/12] Updating system..."
sudo apt update || exit 1

echo "[2/12] Installing system dependencies..."
sudo apt install -y python3 python3-pip python3-venv python3-dev build-essential \
    docker.io docker-compose-plugin git wget apt-transport-https gnupg lsb-release || exit 1

echo "[3/12] Adding user to docker group..."
sudo usermod -aG docker $USER

echo "[4/12] Installing Trivy (optional, can skip if fails)..."
if ! command -v trivy &> /dev/null; then
    wget -qO - https://aquasecurity.github.io/trivy-repo/deb/public.key | sudo gpg --dearmor -o /usr/share/keyrings/trivy.gpg
    echo "deb [signed-by=/usr/share/keyrings/trivy.gpg] https://aquasecurity.github.io/trivy-repo/deb $(lsb_release -sc) main" | sudo tee -a /etc/apt/sources.list.d/trivy.list
    sudo apt update
    sudo apt install -y trivy || echo "Warning: Trivy install failed, continuing anyway"
fi

echo "[5/12] Cloning DockUp..."
if [ -d "/opt/dockup" ]; then
    echo "DockUp already exists. Remove it? (y/n)"
    read -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo rm -rf /opt/dockup
    else
        exit 0
    fi
fi

sudo git clone https://github.com/ChristoffBo/dockup.git /opt/dockup || exit 1
sudo chown -R $USER:$USER /opt/dockup

echo "[6/12] Creating Python virtual environment..."
cd /opt/dockup/dockup || exit 1
python3 -m venv venv || exit 1

echo "[7/12] Installing Python dependencies..."
source venv/bin/activate

# CRITICAL: Upgrade pip first
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt || exit 1

# Verify critical packages
python3 -c "import psutil, flask, docker" || exit 1

echo "[8/12] Creating directories..."
sudo mkdir -p /app/data /app/logs /stacks
sudo chown -R $USER:$USER /app /stacks

echo "[9/12] Downloading Trivy database (optional)..."
if command -v trivy &> /dev/null; then
    trivy image --download-db-only || echo "Trivy DB download failed, will retry on first scan"
fi

echo "[10/12] Setting timezone..."
read -p "Enter timezone (e.g., Africa/Johannesburg) [UTC]: " TZ
TZ=${TZ:-UTC}

echo "[11/12] Creating systemd service..."
sudo tee /etc/systemd/system/dockup.service > /dev/null <<EOF
[Unit]
Description=DockUp - Docker Stack Manager
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=/opt/dockup/dockup
Environment="PATH=/opt/dockup/dockup/venv/bin:/usr/local/bin:/usr/bin"
Environment="PORT=5000"
Environment="TZ=$TZ"
ExecStart=/opt/dockup/dockup/venv/bin/python3 /opt/dockup/dockup/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dockup

echo "[12/12] Creating update script..."
cat > /opt/dockup/update.sh <<'UPDATEEOF'
#!/bin/bash
echo "Stopping DockUp..."
sudo systemctl stop dockup

echo "Backing up data..."
sudo cp -r /app/data /app/data.backup.$(date +%Y%m%d)

echo "Pulling latest changes..."
cd /opt/dockup
git pull

echo "Updating dependencies..."
cd dockup
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Starting DockUp..."
sudo systemctl start dockup

echo "Waiting for startup..."
sleep 3

echo "Status:"
sudo systemctl status dockup --no-pager

echo ""
echo "If startup failed, restore backup:"
echo "  sudo systemctl stop dockup"
echo "  sudo rm -rf /app/data"
echo "  sudo mv /app/data.backup.YYYYMMDD /app/data"
echo "  sudo systemctl start dockup"
UPDATEEOF

chmod +x /opt/dockup/update.sh

echo ""
echo "========================================="
echo "Installation Complete!"
echo "========================================="
echo ""
echo "IMPORTANT: Log out and back in for docker group to take effect"
echo ""
echo "After logging back in:"
echo "  sudo systemctl start dockup"
echo "  sudo systemctl status dockup"
echo ""
echo "Then open: http://localhost:5000"
echo ""
echo "Commands:"
echo "  Start:     sudo systemctl start dockup"
echo "  Stop:      sudo systemctl stop dockup"
echo "  Restart:   sudo systemctl restart dockup"
echo "  Status:    sudo systemctl status dockup"
echo "  Logs:      sudo journalctl -u dockup -f"
echo "  Update:    /opt/dockup/update.sh"
echo "  Uninstall: /opt/dockup/uninstall.sh"
echo ""
```

**To install:**
```bash
# Save script
nano install-dockup.sh
# Paste script, Ctrl+X, Y, Enter

# Make executable
chmod +x install-dockup.sh

# Run
./install-dockup.sh

# Log out and back in
exit

# After logging back in
sudo systemctl start dockup
sudo systemctl status dockup

# Open browser
# http://localhost:5000
```

## Manual Installation

If the script fails, install manually:

```bash
# 1. Install dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv python3-dev build-essential \
    docker.io docker-compose-plugin git

# 2. Add to docker group
sudo usermod -aG docker $USER
# Log out and back in here

# 3. Clone DockUp
sudo git clone https://github.com/ChristoffBo/dockup.git /opt/dockup
sudo chown -R $USER:$USER /opt/dockup

# 4. Setup Python
cd /opt/dockup/dockup
python3 -m venv venv
source venv/bin/activate

# 5. Install Python packages (IMPORTANT: upgrade pip first)
pip install --upgrade pip
pip install -r requirements.txt

# Verify
python3 -c "import psutil, flask, docker"

# 6. Create directories
sudo mkdir -p /app/data /app/logs /stacks
sudo chown -R $USER:$USER /app /stacks

# 7. Test run manually first
python3 app.py
# Should show: "Starting Dockup on port 5000..."
# Press Ctrl+C to stop

# 8. Create systemd service
sudo nano /etc/systemd/system/dockup.service
```

Paste this (replace YOUR_USERNAME):
```ini
[Unit]
Description=DockUp - Docker Stack Manager
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=YOUR_USERNAME
Group=YOUR_USERNAME
WorkingDirectory=/opt/dockup/dockup
Environment="PATH=/opt/dockup/dockup/venv/bin:/usr/local/bin:/usr/bin"
Environment="PORT=5000"
Environment="TZ=Africa/Johannesburg"
ExecStart=/opt/dockup/dockup/venv/bin/python3 /opt/dockup/dockup/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# 9. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable dockup
sudo systemctl start dockup
sudo systemctl status dockup
```

## Updating DockUp

### Simple Update

```bash
/opt/dockup/update.sh
```

### Manual Update

```bash
# Stop service
sudo systemctl stop dockup

# Backup data (optional but recommended)
sudo cp -r /app/data /app/data.backup

# Pull updates
cd /opt/dockup
git pull

# Update dependencies
cd dockup
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Restart
sudo systemctl start dockup
sudo systemctl status dockup
```

### Update to Specific Version

```bash
cd /opt/dockup
git fetch --tags
git tag  # List available versions
git checkout v1.3.0  # Or whatever version
cd dockup
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart dockup
```

### Rollback After Bad Update

```bash
sudo systemctl stop dockup

# Restore data
sudo rm -rf /app/data
sudo mv /app/data.backup /app/data

# Go back to previous version
cd /opt/dockup
git log --oneline  # See commits
git checkout HEAD~1  # Go back one commit

cd dockup
source venv/bin/activate
pip install -r requirements.txt

sudo systemctl start dockup
```

## Uninstalling DockUp

### Option 1: Automated Uninstall Script

Create `/opt/dockup/uninstall.sh`:

```bash
#!/bin/bash

echo "========================================="
echo "DockUp Uninstall"
echo "========================================="
echo ""
echo "This will remove:"
echo "  - DockUp application (/opt/dockup)"
echo "  - Systemd service"
echo "  - User from docker group (optional)"
echo ""
echo "This will NOT remove:"
echo "  - Your data (/app/data)"
echo "  - Your stacks (/stacks)"
echo "  - Docker itself"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 0
fi

echo "Stopping and disabling service..."
sudo systemctl stop dockup
sudo systemctl disable dockup
sudo rm /etc/systemd/system/dockup.service
sudo systemctl daemon-reload

echo "Removing DockUp..."
sudo rm -rf /opt/dockup

echo ""
read -p "Remove user from docker group? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sudo deluser $USER docker
    echo "Log out and back in for group change to take effect"
fi

echo ""
read -p "Remove data (/app/data, /app/logs)? THIS DELETES YOUR CONFIG! (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sudo rm -rf /app
    echo "Data removed"
else
    echo "Data preserved in /app/"
fi

echo ""
echo "DockUp uninstalled."
echo "Your stacks are still in /stacks/"
echo ""
```

Make executable and run:
```bash
chmod +x /opt/dockup/uninstall.sh
/opt/dockup/uninstall.sh
```

### Option 2: Manual Uninstall

```bash
# Stop and remove service
sudo systemctl stop dockup
sudo systemctl disable dockup
sudo rm /etc/systemd/system/dockup.service
sudo systemctl daemon-reload

# Remove DockUp
sudo rm -rf /opt/dockup

# Optional: Remove from docker group
sudo deluser $USER docker

# Optional: Remove data (THIS DELETES YOUR CONFIG!)
sudo rm -rf /app

# Your stacks are still in /stacks/ if you want to keep them
```

## Service Management

```bash
# Start
sudo systemctl start dockup

# Stop
sudo systemctl stop dockup

# Restart
sudo systemctl restart dockup

# Status
sudo systemctl status dockup

# Logs (live)
sudo journalctl -u dockup -f

# Logs (last 100 lines)
sudo journalctl -u dockup -n 100

# Enable auto-start on boot
sudo systemctl enable dockup

# Disable auto-start
sudo systemctl disable dockup
```

## Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u dockup -n 50

# Run manually to see error
cd /opt/dockup/dockup
source venv/bin/activate
python3 app.py
```

### "ModuleNotFoundError: No module named 'psutil'"

```bash
cd /opt/dockup/dockup
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### "Permission denied" accessing Docker

```bash
# Check if in docker group
groups

# If "docker" not shown, you need to log out and back in
exit
# Then log back in

# Verify
docker ps
```

### Port 5000 already in use

```bash
# Check what's using it
sudo lsof -i :5000

# Change DockUp port
sudo nano /etc/systemd/system/dockup.service
# Change PORT=5000 to PORT=8080

sudo systemctl daemon-reload
sudo systemctl restart dockup

# Access at http://localhost:8080
```

### Can't access web UI

```bash
# Is it running?
sudo systemctl status dockup

# Is it listening?
sudo netstat -tlnp | grep 5000

# Can you reach it?
curl http://localhost:5000/api/ping

# Check firewall
sudo ufw status
sudo ufw allow 5000
```

## Directory Structure

```
/opt/dockup/
├── dockup/              # Application
│   ├── app.py
│   ├── requirements.txt
│   ├── templates/
│   ├── static/
│   └── venv/            # Virtual environment
├── docs/
├── update.sh            # Update script
└── uninstall.sh         # Uninstall script

/app/
├── data/                # Config, passwords
│   ├── config.json
│   ├── password.hash
│   └── ...
└── logs/                # Application logs
    └── dockup.log

/stacks/                 # Your Docker stacks
├── nginx-proxy/
│   └── compose.yaml
└── ...

/etc/systemd/system/
└── dockup.service       # Systemd service
```

## Advantages of Native Install

**Pros:**
- Easy updates (just git pull)
- Direct code access
- No Docker overhead
- Can modify code live
- Simpler debugging

**Cons:**
- Manual dependency management
- No isolation
- Python version dependencies

## Switching to Docker

If you want to switch back to Docker:

```bash
# 1. Stop native
sudo systemctl stop dockup
sudo systemctl disable dockup

# 2. Your data is in /app/data - mount it in Docker
docker run -d \
  --name dockup \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /app/data:/app/data \
  -v /stacks:/stacks \
  your-registry/dockup:latest
```

Your config, passwords, and stacks are preserved.
