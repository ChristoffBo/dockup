# DockUp Installation Guide

## Quick Start

### Docker Compose (Recommended)

```yaml
version: '3.8'

services:
  dockup:
    image: your-registry/dockup:latest
    container_name: dockup
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      # Docker socket - REQUIRED
      - /var/run/docker.sock:/var/run/docker.sock
      
      # Persistent data - REQUIRED
      - ./dockup-data:/app/data
      
      # Your stacks directory - REQUIRED
      - /path/to/your/stacks:/stacks
      
      # Logs - optional
      - ./dockup-logs:/app/logs
    
    environment:
      - TZ=America/New_York
    
    mem_limit: 512m
    cpus: 1.0
```

Then:
```bash
docker compose up -d
```

### Docker Run

```bash
docker run -d \
  --name dockup \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v ./dockup-data:/app/data \
  -v /path/to/your/stacks:/stacks \
  -v ./dockup-logs:/app/logs \
  -e TZ=America/New_York \
  your-registry/dockup:latest
```

## First Time Setup

1. **Start DockUp:**
   ```bash
   docker compose up -d
   ```

2. **Check logs:**
   ```bash
   docker logs dockup
   ```
   
   You should see:
   ```
   DockUp 1.3.0 starting
   ✓ Docker connected successfully
   ✓ Trivy database exists
   ✓ Configuration loaded
   Starting Dockup on port 5000...
   ```

3. **Open web interface:**
   ```
   http://localhost:5000
   ```

4. **Set password:**
   - First login prompts for password
   - Set strong password (12+ characters)

5. **Configure instance:**
   - Settings → Set instance name
   - Settings → Set timezone
   - Settings → Configure notifications (optional)

## Volume Requirements

### Docker Socket (REQUIRED)
```yaml
- /var/run/docker.sock:/var/run/docker.sock
```
Without this, DockUp can't control Docker.

### Data Directory (REQUIRED)
```yaml
- ./dockup-data:/app/data
```
Stores configuration, schedules, metadata. **Backup this directory.**

### Stacks Directory (REQUIRED)
```yaml
- /path/to/your/stacks:/stacks
```
Where your compose files live.

Structure:
```
/stacks/
├── nginx-proxy/
│   └── compose.yaml
├── plex/
│   └── compose.yaml
└── traefik/
    └── compose.yaml
```

### Logs Directory (Optional)
```yaml
- ./dockup-logs:/app/logs
```
Application logs, rotates daily, keeps 7 days.

## Environment Variables

**PORT** (default: 5000)
```yaml
environment:
  - PORT=8080
```

**TZ** (default: UTC)
```yaml
environment:
  - TZ=Africa/Johannesburg
```

Common timezones:
- `America/New_York`
- `America/Los_Angeles`
- `Europe/London`
- `Africa/Johannesburg`
- `Asia/Tokyo`

## Stack Directory Structure

DockUp expects:

```
/stacks/
├── stack-name-1/
│   ├── compose.yaml          # or docker-compose.yml
│   └── .env                   # optional
├── stack-name-2/
│   ├── compose.yaml
│   └── .dockup/               # DockUp metadata
│       └── metadata.json
└── stack-name-3/
    └── compose.yaml
```

**Rules:**
- Each stack in its own directory
- Compose file named `compose.yaml` or `docker-compose.yml`
- Stack name = directory name
- No spaces in directory names

## Migrating Existing Stacks

If you already have stacks running:

1. **Move compose files:**
   ```bash
   mkdir -p /path/to/stacks/myapp
   cp /old/location/docker-compose.yml /path/to/stacks/myapp/compose.yaml
   ```

2. **Or symlink:**
   ```bash
   ln -s /existing/location/myapp /path/to/stacks/myapp
   ```

3. **Start DockUp** - it auto-detects running containers

4. **Verify** - check web UI

## Reverse Proxy Setup

### Nginx

```nginx
server {
    listen 80;
    server_name dockup.yourdomain.com;
    
    location / {
        proxy_pass http://localhost:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        
        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### Caddy

```
dockup.yourdomain.com {
    reverse_proxy localhost:5000
}
```

### Traefik

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.dockup.rule=Host(`dockup.yourdomain.com`)"
  - "traefik.http.routers.dockup.entrypoints=websecure"
  - "traefik.http.services.dockup.loadbalancer.server.port=5000"
```

## Resource Requirements

**Minimum:**
- CPU: 0.5 cores
- RAM: 256 MB
- Disk: 500 MB

**Recommended:**
- CPU: 1 core
- RAM: 512 MB
- Disk: 2 GB (for Trivy database)

## Trivy Database

First startup downloads Trivy vulnerability database (2-5 minutes):

```
Trivy database not found, downloading...
```

This is normal. Subsequent starts are instant.

Update manually:
```bash
docker exec dockup trivy image --download-db-only
```

## Updating DockUp

1. **Pull new image:**
   ```bash
   docker pull your-registry/dockup:latest
   ```

2. **Stop container:**
   ```bash
   docker stop dockup
   docker rm dockup
   ```

3. **Start new:**
   ```bash
   docker compose up -d
   ```

Your data is safe in the `/app/data` volume.

## Common Installation Issues

### "Cannot connect to Docker daemon"

**Check socket:**
```bash
ls -la /var/run/docker.sock
docker inspect dockup | grep -A 5 Binds
```

**Fix permissions:**
```bash
sudo chmod 666 /var/run/docker.sock
```

### "Port 5000 already in use"

**Find what's using it:**
```bash
sudo lsof -i :5000
```

**Change port:**
```yaml
ports:
  - "8080:5000"
```

### "No stacks found"

**Check mount:**
```bash
docker exec dockup ls -la /stacks
```

**Fix permissions:**
```bash
sudo chown -R 1000:1000 /path/to/stacks/
```

### Trivy download fails

**Check internet:**
```bash
docker exec dockup ping -c 3 8.8.8.8
```

**Manual download:**
```bash
docker exec dockup trivy image --download-db-only
```

## Backup

**What to backup:**
```bash
/path/to/dockup-data/
/path/to/stacks/
docker-compose.yml
```

**Automated backup:**
```bash
#!/bin/bash
tar -czf dockup-backup-$(date +%Y%m%d).tar.gz \
  dockup-data/ \
  /path/to/stacks/
```

Run daily via cron:
```bash
0 3 * * * /path/to/backup-script.sh
```

## Verify Installation

```bash
# Check version
curl http://localhost:5000/api/version

# Check stacks
curl http://localhost:5000/api/stacks

# Test Docker
docker exec dockup docker ps
```
