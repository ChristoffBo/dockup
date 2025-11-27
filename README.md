<img width="1569" height="922" alt="image" src="https://github.com/user-attachments/assets/cb63f08e-37de-450c-a6a6-f562bb7d91d1" />
<img width="796" height="743" alt="image" src="https://github.com/user-attachments/assets/2ce341ec-134e-43a0-abf1-ee9fb7b18534" />
<img width="797" height="845" alt="image" src="https://github.com/user-attachments/assets/42d374f9-377d-4565-9394-d9083fed25b6" />
<img width="1650" height="752" alt="image" src="https://github.com/user-attachments/assets/603cdf7a-9549-499a-89f6-38620f030c65" />
<img width="1651" height="636" alt="image" src="https://github.com/user-attachments/assets/0e9eeed7-c53a-4b9a-8b19-4fc358873df4" />
<img width="1653" height="738" alt="image" src="https://github.com/user-attachments/assets/9ed3fbc4-530f-415c-92d3-03b19b430e14" />

# 🚀 Dockup — Docker Compose Stack Manager with Auto-Update

**Dockup** is a lightweight, self-hosted web UI for managing Docker Compose stacks. It combines the stack management capabilities of **Dockge** with the auto-update detection of **Watchtower**, adding powerful features like health monitoring, notifications, image management, and optional password protection.

Manage 30+ Docker stacks without touching the terminal. Monitor resources, schedule updates, prune unused images, and keep your homelab running smoothly—all from one beautiful interface.

---

## ✨ Key Features

### 🐳 Stack Management
- **Auto-discovery** — Scans `/stacks` and detects all compose files automatically
- **Orphan container import** — Converts standalone `docker run` containers into compose stacks
- **Real-time operations** — Start, stop, restart, pull with live terminal output via WebSocket
- **Split YAML editor** — Edit services, networks, volumes, and environment variables separately with validation
- **Clone stacks** — Duplicate existing stacks with one click
- **Port conflict detection** — Warns before starting stacks with conflicting ports
- **Web UI auto-detection** — Automatically finds and links to container web interfaces

### 📊 Monitoring & Stats
- **Host system stats** — CPU, RAM, disk usage, and network throughput
- **Per-container stats** — CPU, memory, and network usage for every container
- **Health checks** — Monitors container health status with failure notifications
- **Live logs** — View real-time logs for any container
- **Update badges** — Visual indicators for stacks with available updates

### 🔄 Auto-Updates
- **Update detection** — Checks Docker registries for new images (DockerHub, LSCR, GHCR, Quay, etc.)
- **Per-stack scheduling** — Set individual cron schedules for each stack
- **Modes**: Off, Check Only (notify), or Auto Update (pull + restart with health checks)
- **Apply to all** — Bulk configure update settings across all stacks
- **Update history** — Track when stacks were last updated and by whom (auto/manual)
- **Health verification** — Waits for containers to be healthy before completing updates

### 🖼️ Image Management
- **List all images** — Alphabetically sorted with size, status, and usage info
- **Prune unused images** — Manual or automatic cleanup with scheduling
- **Delete specific images** — Remove individual images from the UI
- **Storage stats** — See which images are consuming the most space

### 🌐 Network Management
- **View all networks** — List Docker networks with driver types and connected containers
- **Create networks** — Bridge, macvlan, ipvlan with IPAM configuration
- **Delete networks** — Remove unused networks safely

### 🔔 Notifications
- **Gotify integration** — Native support for Gotify notifications
- **Apprise support** — Connect to 80+ notification services (Discord, Slack, Telegram, email, etc.)
- **Customizable alerts** — Choose what to get notified about:
  - Updates available (Check Only mode)
  - Updates completed (Auto Update mode)
  - Errors and failures
  - Health check failures (after N consecutive failures)

### 💾 Backup & Restore
- **One-click backup** — Download all compose files + metadata as a ZIP
- **Restore from backup** — Upload and restore entire stack collections
- **Stack metadata** — Preserves settings, schedules, and custom configurations

### 🔒 Security
- **Optional password protection** — Enable/disable authentication via toggle in settings
- **bcrypt password hashing** — Industry-standard secure password storage
- **Session management** — 31-day persistent sessions
- **First-run setup wizard** — Simple password creation on initial enable
- **Change password** — Update password anytime from settings
- **No forced auth** — Password protection is completely optional

### ⚙️ Advanced Features
- **Timezone support** — Set your timezone for accurate scheduling
- **Cron scheduling** — Flexible update schedules per stack or globally
- **Health check thresholds** — Configure how many failures before notification
- **WebSocket updates** — Real-time UI updates without page refresh
- **Offline capable** — Works without internet once images are cached
- **Dark theme** — Beautiful dark UI optimized for homelabs

---

## 🚨 About Orphan Container Importing

Dockup includes an **auto-importer** that detects containers running **without a Docker Compose project label**. These "orphans" are typically created by:
- `docker run` commands
- CasaOS
- Portainer
- Legacy systems

### How it works:
1. **Detects** the orphan container
2. **Generates** a compose folder under `/stacks/imported-<n>`
3. **Creates** a clean `compose.yaml` with the container's configuration
4. **Tags** the original container so it's never imported again

### ⚠️ Important: Dockup does NOT automatically adopt or replace running containers

**Why?** Because Docker Compose recreates containers if ANY configuration differs, which can cause:
- Broken volumes and networks
- Unexpected downtime
- Data loss
- Application restarts

Dockup takes the **safe approach**:
- Imports the orphan configuration
- Does NOT modify the running container
- Does NOT recreate anything
- Lets YOU decide when to switch

### Safe migration procedure:
1. Dockup auto-imports the orphan and creates a compose folder
2. You manually stop and remove the original container:
   ```bash
   docker stop <container_name>
   docker rm <container_name>
   ```
3. (Optional) Rename the imported folder to something cleaner
4. Start the stack from Dockup UI

This guarantees **zero downtime** and **100% safe migration**.

---

## 📦 Installation

### Docker Hub
```
cbothma/dockup:latest
```

### Quick Start
```bash
docker run -d \
  --name dockup \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /path/to/your/stacks:/stacks \
  -v dockup_data:/app/data \
  --restart unless-stopped \
  cbothma/dockup:latest
```

### Docker Compose
```yaml
version: '3.8'
services:
  dockup:
    image: cbothma/dockup:latest
    container_name: dockup
    ports:
      - "5000:5000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /path/to/your/stacks:/stacks
      - dockup_data:/app/data
    restart: unless-stopped
```

### Volume Mounts
| Mount | Purpose |
|-------|---------|
| `/var/run/docker.sock` | **Required** — Docker API access |
| `/stacks` | Your Docker Compose stacks directory |
| `/app/data` | Persists schedules, settings, and metadata |

---

## 🎯 Usage

### Access the UI
Open your browser to:
```
http://localhost:5000
```

### First-Time Setup (Optional)
If you want password protection:
1. Go to **Settings**
2. Enable **"Password Protection"**
3. Click **Save Settings**
4. You'll be redirected to set up your password
5. Enter password twice to confirm
6. Done! You're auto-logged in

To disable password protection, just uncheck the toggle and save.

### Managing Stacks
**Dashboard** shows all your stacks with:
- Status indicators (running, stopped, inactive)
- Resource usage (CPU, RAM, network)
- Update availability badges
- Quick action buttons

**Operations:**
- **Up** — Start the stack
- **Down** — Stop and remove containers
- **Restart** — Restart all containers
- **Pull** — Pull latest images
- **Logs** — View real-time container logs
- **Edit** — Modify compose file with split editor
- **Clone** — Duplicate stack configuration
- **Delete** — Remove stack (optionally delete images)

### Update Modes
Each stack can have its own update mode:

| Mode | Behavior |
|------|----------|
| **Off** | No automatic checks or updates |
| **Check Only** | Checks for updates, sends notification, no changes made |
| **Auto Update** | Pulls new images, restarts stack, verifies health |

Set schedules using cron expressions or use the friendly helper:
- `0 2 * * *` — Daily at 2 AM
- `0 3 * * 0` — Weekly on Sunday at 3 AM
- `0 4 1 * *` — Monthly on the 1st at 4 AM

### Image Management
**Images** tab shows all Docker images:
- Alphabetically sorted
- Shows size, status (in use / unused), and containers
- One-click delete for unused images
- Manual or scheduled automatic pruning

### Network Management
**Networks** tab lets you:
- View all Docker networks
- Create custom networks (bridge, macvlan, ipvlan)
- Configure IPAM (subnets, gateways, IP ranges)
- Delete unused networks

### Notifications
Configure in **Settings**:
1. **Gotify**: Enter URL + token
2. **Apprise**: Add service URLs (Discord, Slack, Telegram, etc.)
3. Choose what to get notified about
4. Test notifications with the "Test" button

### Backup & Restore
**Settings** → **Backup All Stacks**:
- Downloads ZIP with all compose files and metadata
- Includes stack schedules and settings
- Restore by uploading the ZIP file

---

## ⚙️ Configuration

All settings are managed via the web UI:

### General Settings
- **Timezone** — Set your local timezone for accurate scheduling
- **Default Cron** — Default schedule for new stacks

### Notifications
- **Gotify URL & Token** — Native Gotify support
- **Apprise URLs** — Add multiple notification services
- **Notification Toggles**:
  - Notify on update checks
  - Notify on completed updates
  - Notify on errors
  - Notify on health failures
- **Health Check Threshold** — Number of consecutive failures before alerting

### Auto-Prune
- **Enable Auto-Prune** — Automatically clean unused images
- **Prune Schedule** — Set cron for automatic cleanup

### Password Protection
- **Enable Password Protection** — Require login to access Dockup
- **Change Password** — Update your password anytime
- Completely optional — toggle on/off as needed

---

## 🔧 Technical Details

### Supported Registries
Dockup can detect updates from:
- Docker Hub (`docker.io`)
- Linux Server (`lscr.io`)
- GitHub Container Registry (`ghcr.io`)
- Quay (`quay.io`)
- Google Container Registry (`gcr.io`)
- Any standard Docker Registry v2

### Health Checks
Dockup monitors container health and:
- Tracks consecutive failures
- Sends notification after threshold reached
- Notifies when stack recovers
- Shows health status in UI with badges

### Stack Metadata
Each stack stores metadata in `.dockup-meta.json`:
- Ever started flag (tracks inactive vs stopped)
- Update history
- Custom settings
- Web UI URL

### Safety Features
- **No destructive defaults** — Everything requires confirmation
- **Health verification** — Updates wait for containers to be healthy
- **Safe orphan import** — Never modifies running containers
- **Port conflict detection** — Warns before starting conflicting services

---

## 📜 License

MIT License — Use it, modify it, share it.

---

## 🙏 Acknowledgments

Inspired by:
- **Dockge** — For clean stack management UI
- **Watchtower** — For reliable auto-update methodology
- **Portainer** — For comprehensive Docker management

Built to be lightweight, focused, and **actually useful** for homelabs.

---

**Easy. Clean. As it should be.** 🚀
