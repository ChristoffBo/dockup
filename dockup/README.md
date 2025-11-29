<img width="1569" height="922" alt="image" src="https://github.com/user-attachments/assets/cb63f08e-37de-450c-a6a6-f562bb7d91d1" />
<img width="796" height="743" alt="image" src="https://github.com/user-attachments/assets/2ce341ec-134e-43a0-abf1-ee9fb7b18534" />
<img width="797" height="845" alt="image" src="https://github.com/user-attachments/assets/42d374f9-377d-4565-9394-d9083fed25b6" />
<img width="1650" height="752" alt="image" src="https://github.com/user-attachments/assets/603cdf7a-9549-499a-89f6-38620f030c65" />
<img width="1651" height="636" alt="image" src="https://github.com/user-attachments/assets/0e9eeed7-c53a-4b9a-8b19-4fc358873df4" />
<img width="1653" height="738" alt="image" src="https://github.com/user-attachments/assets/9ed3fbc4-530f-415c-92d3-03b19b430e14" />

# 🚀 Dockup — Docker Compose Stack Manager with Auto-Update

**Dockup** is a lightweight, self-hosted web UI for managing Docker Compose stacks. It combines the stack management capabilities of **Dockge** with the auto-update detection of **Watchtower**, adding powerful features like health monitoring, notifications, image management, optional password protection, and **peer-to-peer multi-server mode**.

Manage 30+ Docker stacks across multiple servers without touching the terminal. Monitor resources, schedule updates, prune unused images, and keep your homelab running smoothly—all from one beautiful interface.

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

### 🌐 Peer-to-Peer Multi-Server Mode
- **Connect multiple DockUp instances** — Manage all your servers from one dashboard
- **Unified view** — See stacks from all connected servers in a single interface
- **API token authentication** — Secure peer-to-peer connections
- **Server badges** — Visual indicators showing which server each stack runs on
- **Remote control** — Start, stop, and restart stacks on any connected server
- **Independent operation** — Each server runs autonomously with its own schedules
- **No master/agent architecture** — All instances are equal peers

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
- **API token authentication** — Secure peer-to-peer connections between DockUp instances

### ⚙️ Advanced Features
- **Timezone support** — Set your timezone for accurate scheduling
- **Cron scheduling** — Flexible update schedules per stack or globally
- **Health check thresholds** — Configure how many failures before notification
- **WebSocket updates** — Real-time UI updates without page refresh
- **Offline capable** — Works without internet once images are cached
- **Dark theme** — Beautiful dark UI optimized for homelabs

---

## 🔗 Peer Mode: Managing Multiple Servers

Peer mode allows you to connect multiple DockUp instances together for unified multi-server management.

### How Peer Mode Works

**Architecture:**
- **Symmetric P2P** — Any DockUp can connect to any other as a peer
- **No Master/Agent** — All instances are equal, no hierarchy
- **API Token Auth** — Each instance has a unique token for secure connections
- **Independent Operation** — Each server runs its own schedules and updates
- **Live Synchronization** — Dashboard shows real-time status from all connected servers

**What You Can Do:**
- View stacks from all servers in one dashboard
- Start/Stop/Restart stacks on any connected server
- Monitor CPU/RAM/Network across your entire infrastructure
- See health status from all servers
- Filter and search across all connected instances

**What Stays Local:**
- Editing compose files (must edit on that server's UI)
- Viewing container logs (must view on that server's UI)
- Configuring schedules (each server manages its own)
- Creating/deleting stacks (must do on target server)
- Backups (backs up local stacks only)

### Setting Up Peer Connections

**Step 1: Install DockUp on Each Server**

Install DockUp on every server you want to connect:

```bash
docker run -d \
  --name dockup \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /path/to/stacks:/stacks \
  -v dockup_data:/app/data \
  --restart unless-stopped \
  cbothma/dockup:latest
```

**Step 2: Set Instance Names**

On each server:
1. Open DockUp: `http://server-ip:5000`
2. Go to **Settings** → **Remote DockUp Instances**
3. Enter an **Instance Name** (e.g., "Main Server", "Media Server")
4. The name auto-saves when you click away from the field

**Step 3: Copy API Token**

On the server you want to connect **TO**:
1. In **Settings** → **Remote DockUp Instances** → **This Instance**
2. Click the **eye icon** (👁) to reveal the full API token
3. Click the **copy button** (📋) to copy to clipboard

**Step 4: Add Peer Connection**

On the server you're managing **FROM**:
1. Go to **Settings** → **Remote DockUp Instances** → **Connected Peers**
2. Click **"Add Remote Instance"**
3. Fill in the form:
   - **Instance ID**: Unique identifier (e.g., `media-server`) — lowercase, no spaces
   - **Display Name**: Friendly name shown on dashboard (e.g., "Media Server")
   - **URL**: Full URL to remote DockUp (e.g., `http://192.168.1.100:5000`)
   - **API Token**: Paste the full token from Step 3
   - **Enabled**: Keep checked
4. Click **"Add Peer"**
5. Click **"Test"** to verify — should show "Connected to [Instance Name]"

**Step 5: View Combined Dashboard**

Go to **Dashboard** — you'll now see:
- **Local stacks** — No badge, all operations available
- **Remote stacks** — Blue badge showing server name (e.g., `plex [🖥 Media Server]`)

### What Works with Remote Stacks

| Operation | Local Stack | Remote Stack | Notes |
|-----------|-------------|--------------|-------|
| **View Status** | ✅ | ✅ | Real-time status from each server |
| **Start** | ✅ | ✅ | Proxied to remote server |
| **Stop** | ✅ | ✅ | Proxied to remote server |
| **Restart** | ✅ | ✅ | Proxied to remote server |
| **CPU/RAM/Network Stats** | ✅ | ✅ | Live stats from each server |
| **Health Status** | ✅ | ✅ | Health monitoring across servers |
| **Edit Compose** | ✅ | ❌ Disabled | Must edit on that server's UI |
| **View Logs** | ✅ | ❌ Disabled | Must view on that server's UI |
| **Configure Schedule** | ✅ | ❌ Disabled | Each server manages its own |
| **Check/Auto Update** | ✅ | ❌ Disabled | Each server manages its own |
| **Delete Stack** | ✅ | ❌ Disabled | Must delete on that server's UI |
| **Clone Stack** | ✅ | ❌ Disabled | Must clone on that server's UI |

### Understanding Server Badges

**Dashboard View:**

```
Running Stacks:
┌─────────────────────────────────┐
│ 🗂 plex  [🖥 Media Server]      │ ← Remote stack (blue badge)
│ Running • Healthy               │
│ [Stop] [Restart]                │ ← Start/Stop work
└─────────────────────────────────┘

┌─────────────────────────────────┐
│ 🗂 sonarr                        │ ← Local stack (no badge)
│ Running • Healthy               │
│ [Stop] [Restart] [Edit] [Logs]  │ ← All buttons work
└─────────────────────────────────┘
```

- **No badge** = Local stack (all operations available)
- **Blue badge** = Remote stack (basic operations only)
- **Greyed buttons** = Disabled for remote (hover for tooltip)

### Best Practices

**1. Use One Server as Your "Main Dashboard":**
- Connect all other servers to it as peers
- Bookmark this server's URL for daily use
- View your entire infrastructure from one place

**2. Keep Browser Tabs for Each Server:**
- Tab 1: Main dashboard (unified view + basic control)
- Tab 2: Server A UI (for editing, logs, advanced config)
- Tab 3: Server B UI (for editing, logs, advanced config)

**3. Configure Auto-Updates Locally:**
- Each DockUp manages its own stack schedules
- Updates run independently on each server
- Main dashboard shows update status across all servers

**4. Use Peer Mode For:**
- Quick infrastructure overview
- Monitoring health across all servers
- Starting/stopping stacks remotely
- Checking which stacks need updates
- Filtering by tags across all servers

**5. Don't Use Peer Mode For:**
- Editing compose files (go to that server's UI)
- Viewing detailed logs (go to that server's UI)
- Configuring schedules (each server manages its own)
- Creating new stacks (create on target server)

### Backups in Peer Mode

**Important:** Backups are **local only**

When you click "Backup All Stacks":
- ✅ Backs up stacks on the current server only
- ❌ Does NOT backup remote peer stacks
- ❌ Does NOT backup peer connections

**To backup everything:**
1. Go to Server A → **Backup All Stacks** → Download `backup-serverA.zip`
2. Go to Server B → **Backup All Stacks** → Download `backup-serverB.zip`
3. Now you have complete backups of all servers

### Troubleshooting Peer Connections

**Test Shows 401 Error:**
- Make sure you copied the **full** API token (click eye icon first, then copy)
- Verify the remote DockUp URL is correct and accessible
- Check firewall rules allow connections between servers

**Peer Disappears After Refresh:**
- Update to latest DockUp version (early bug, now fixed)
- Peers persist in `/app/data/config.json`

**Can't See Remote Stacks:**
- Check peer is enabled in Settings → Connected Peers
- Click Test to verify connection
- Check remote DockUp is running and accessible

**Wrong Buttons Showing:**
- Make sure both servers are running latest DockUp version
- Backend must return `status` field for proper button display

### Security Notes

- **API tokens are secrets** — Treat them like passwords
- Each DockUp instance has a unique token
- Tokens stored in `/app/data/config.json`
- Click Regenerate to get new token (breaks existing connections)
- Connections are **not encrypted** — Use VPN/Wireguard for internet-exposed connections

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
| `/app/data` | Persists schedules, settings, metadata, and peer connections |

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
- Server badges (in peer mode)

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
- **Note:** Only backs up stacks on the current server (not remote peers)

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

### Remote DockUp Instances (Peer Mode)
- **Instance Name** — Name to identify this DockUp instance
- **API Token** — Display, copy, and regenerate your instance's API token
- **Connected Peers** — Add, edit, test, and remove peer connections
- View and manage all connected DockUp instances

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
- Works across all connected peer instances

### Stack Metadata
Each stack stores metadata in `.dockup-meta.json`:
- Ever started flag (tracks inactive vs stopped)
- Update history
- Custom settings
- Web UI URL
- Tagging

### Peer Communication
- **Protocol**: HTTP REST API with JSON
- **Authentication**: API token in `X-API-Token` header
- **Session Caching**: Connections cached for performance
- **Endpoints**: `/api/stacks/all`, `/api/peer/<id>/stack/<name>/operation`
- **Status Field**: Backend returns `status` ('running'|'stopped'|'partial'|'inactive')

### Safety Features
- **No destructive defaults** — Everything requires confirmation
- **Health verification** — Updates wait for containers to be healthy
- **Safe orphan import** — Never modifies running containers
- **Port conflict detection** — Warns before starting conflicting services
- **Read-only remote operations** — Can't accidentally modify remote compose files

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

**Easy. Clean. Multi-Server. As it should be.** 🚀
