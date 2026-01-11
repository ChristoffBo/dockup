# DockUp API Documentation

Hey, so someone asked about API documentation for remotely restarting stacks when SSL certificates get renewed. Here's everything you need to know about using DockUp's API.

## Getting Started

DockUp runs on `http://your-server:5000` by default (or whatever port you set with `PORT` env var).

Current version: **1.3.0**

## Authentication - How to Actually Use This Thing

Right now there are two ways to authenticate:

### 1. Web Login (Session-Based)

This is what the web UI uses. You log in once, get a cookie, then use that for subsequent requests.

```bash
# Login first
curl -X POST http://localhost:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your_password"}'

# This gives you a session cookie you can reuse
```

### 2. API Token

Here's the thing - API token auth is only enabled on `/api/ping` right now. I know, not super useful for your SSL renewal use case.

Your API token is in Settings → Instance Configuration, or check the logs when DockUp starts up.

To use it:
```bash
curl http://localhost:5000/api/ping \
  -H "X-API-Token: your_token_here"
```

**For SSL certificate automation, you'll need to use session-based auth** (see examples at the bottom).

If you want to fix this, just add `@require_api_token` decorator to the endpoints you need. Look at line 5676 in app.py for reference.

## The Endpoints You'll Actually Use

### List All Your Stacks

**GET** `/api/stacks`

Just shows you everything that's running.

```bash
curl http://localhost:5000/api/stacks
```

Returns something like:
```json
{
  "stacks": [
    {
      "name": "nginx-proxy",
      "status": "running",
      "containers": 2,
      "services": ["nginx", "letsencrypt"],
      "update_available": false
    }
  ]
}
```

### Get Info About a Specific Stack

**GET** `/api/stack/<stack_name>`

```bash
curl http://localhost:5000/api/stack/nginx-proxy
```

Shows you container status, health checks, all that good stuff.

### The Important One - Stack Operations

**POST** `/api/stack/<stack_name>/operation`

This is what you want for restarting containers when SSL certs renew.

Available operations:
- `up` - Starts the stack (docker compose up -d)
- `down` - Stops and removes containers
- `stop` - Just stops containers, doesn't remove them
- `restart` - Restarts everything
- `pull` - Pulls latest images

Example - restart a stack:
```bash
curl -X POST http://localhost:5000/api/stack/nginx-proxy/operation \
  -H "Content-Type: application/json" \
  -H "Cookie: session=your_session_cookie" \
  -d '{"operation": "restart"}'
```

Example - pull new images and update:
```bash
# First pull
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -H "Content-Type: application/json" \
  -d '{"operation": "pull"}'

# Then bring it up
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -H "Content-Type: application/json" \
  -d '{"operation": "up"}'
```

### Delete a Stack

**DELETE** `/api/stack/<stack_name>`

```bash
# Just delete the stack
curl -X DELETE http://localhost:5000/api/stack/old-stack

# Delete the stack AND its images
curl -X DELETE "http://localhost:5000/api/stack/old-stack?delete_images=true"
```

### View Logs

**GET** `/api/stack/<stack_name>/logs`

```bash
# Get last 100 lines (default)
curl http://localhost:5000/api/stack/nginx-proxy/logs

# Get last 50 lines
curl "http://localhost:5000/api/stack/nginx-proxy/logs?lines=50"
```

## Compose File Management

### Get the Compose File

**GET** `/api/stack/<stack_name>/compose`

Returns the actual compose.yaml content.

### Update Compose File

**POST** `/api/stack/<stack_name>/compose`

```bash
curl -X POST http://localhost:5000/api/stack/nginx-proxy/compose \
  -H "Content-Type: application/json" \
  -d '{"content": "your compose file content here"}'
```

### Validate Before Saving

**POST** `/api/stacks/validate`

Check if your compose file is valid without actually saving it.

```bash
curl -X POST http://localhost:5000/api/stacks/validate \
  -H "Content-Type: application/json" \
  -d '{"content": "version: '\''3.8'\''\nservices:\n  nginx:\n    image: nginx:latest"}'
```

Returns:
```json
{
  "valid": true,
  "services": ["nginx"]
}
```

Or if it's broken:
```json
{
  "valid": false,
  "error": "Invalid YAML syntax at line 5"
}
```

## Auto-Updates and Scheduling

### Get Current Schedule

**GET** `/api/stack/<stack_name>/schedule`

Shows you what's configured for auto-updates.

### Set Up Auto-Updates

**POST** `/api/stack/<stack_name>/schedule`

```bash
curl -X POST http://localhost:5000/api/stack/nginx-proxy/schedule \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "schedule": "0 3 * * *",
    "timezone": "America/New_York"
  }'
```

Some useful cron schedules:
- `0 3 * * *` - Every day at 3 AM
- `0 */6 * * *` - Every 6 hours
- `0 2 * * 0` - Sundays at 2 AM
- `0 1 1 * *` - First of the month at 1 AM

### Manually Check for Updates

**POST** `/api/stack/<stack_name>/check-updates`

```bash
curl -X POST http://localhost:5000/api/stack/nginx-proxy/check-updates
```

## Images and Cleanup

### List All Images

**GET** `/api/images`

Shows all Docker images on the system.

### Delete an Image

**DELETE** `/api/images/<image_id>`

```bash
curl -X DELETE http://localhost:5000/api/images/sha256:abc123def456
```

### Clean Up Unused Images

**POST** `/api/images/prune`

Removes all dangling/unused images. Frees up disk space.

```bash
curl -X POST http://localhost:5000/api/images/prune
```

Returns:
```json
{
  "success": true,
  "space_reclaimed": 1024000000,
  "images_deleted": 5
}
```

## System Stats and Monitoring

### Host System Stats

**GET** `/api/host/stats`

CPU, memory, disk usage - all the good stuff.

```bash
curl http://localhost:5000/api/host/stats
```

Returns:
```json
{
  "cpu_percent": 45.2,
  "memory": {
    "total": 16000000000,
    "used": 8000000000,
    "percent": 50.0
  },
  "disk": {
    "total": 500000000000,
    "used": 250000000000,
    "percent": 50.0
  },
  "containers": {
    "running": 25,
    "stopped": 5,
    "total": 30
  }
}
```

### Docker Hub Rate Limits

**GET** `/api/docker-hub/rate-limit`

Check how many pulls you have left before Docker Hub rate limits you.

### Check Disk Usage

**GET** `/api/appdata/sizes`

Shows how much disk space each stack is using.

### Version Info

**GET** `/api/version`

```bash
curl http://localhost:5000/api/version
```

Returns:
```json
{
  "dockup_version": "1.3.0",
  "docker_version": "24.0.7",
  "compose_version": "2.23.0",
  "python_version": "3.11.6"
}
```

### Health Check

**GET** `/api/ping`

Just checks if DockUp is alive. This one actually supports API tokens.

```bash
curl http://localhost:5000/api/ping \
  -H "X-API-Token: your_token"
```

## Security Scanning

### Scan a Container

**POST** `/api/container/<container_id>/scan`

Runs Trivy security scan on the container image.

```bash
curl -X POST http://localhost:5000/api/container/abc123def456/scan
```

### Check Scan Results

**GET** `/api/container/<container_id>/scan-status`

```bash
curl http://localhost:5000/api/container/abc123def456/scan-status
```

Returns:
```json
{
  "status": "completed",
  "vulnerabilities": {
    "CRITICAL": 2,
    "HIGH": 5,
    "MEDIUM": 12,
    "LOW": 8
  },
  "scan_time": "2025-01-11T10:30:00Z"
}
```

## Backup and Restore

### Create Backup

**POST** `/api/backup/create`

Backs up all your stacks and configuration.

```bash
curl -X POST http://localhost:5000/api/backup/create
```

Returns the backup filename. Then you can download it from `/static/backups/`.

### Restore from Backup

**POST** `/api/backup/restore`

```bash
curl -X POST http://localhost:5000/api/backup/restore \
  -H "Content-Type: application/json" \
  -d '{"backup_file": "backup_20250111_103000.tar.gz"}'
```

## Network Management

### List Networks

**GET** `/api/networks`

Shows all Docker networks.

### Create Network

**POST** `/api/network`

```bash
curl -X POST http://localhost:5000/api/network \
  -H "Content-Type: application/json" \
  -d '{
    "name": "myapp-network",
    "driver": "bridge",
    "internal": false,
    "attachable": true
  }'
```

### Delete Network

**DELETE** `/api/network/<network_id>`

```bash
curl -X DELETE http://localhost:5000/api/network/net123
```

## Configuration

### Get Current Config

**GET** `/api/config`

Returns your DockUp settings (without passwords).

### Update Config

**POST** `/api/config`

```bash
curl -X POST http://localhost:5000/api/config \
  -H "Content-Type: application/json" \
  -d '{
    "instance_name": "Production DockUp",
    "timezone": "America/New_York",
    "notification_enabled": true,
    "notification_urls": ["gotify://server/token"]
  }'
```

### Regenerate API Token

**POST** `/api/config/regenerate-token`

Creates a new API token (invalidates the old one).

```bash
curl -X POST http://localhost:5000/api/config/regenerate-token
```

## Templates

### List Available Templates

**GET** `/api/templates/list`

Shows all the pre-configured compose templates (if templates are enabled).

### Refresh Templates

**POST** `/api/templates/refresh`

Pulls latest templates from LinuxServer.io, CasaOS, etc.

### Get Specific Template

**GET** `/api/templates/<template_id>`

```bash
curl http://localhost:5000/api/templates/nginx-proxy-manager
```

### Get Categories

**GET** `/api/templates/categories`

Lists all template categories (Network, Media, Security, etc.) with counts.

## Peer Management

If you're running multiple DockUp instances and want them to talk to each other.

### List Peers

**GET** `/api/peers`

### Add Peer

**POST** `/api/peers`

```bash
curl -X POST http://localhost:5000/api/peers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production Server",
    "url": "http://192.168.1.100:5000",
    "api_token": "peer_token_here",
    "enabled": true
  }'
```

### Update Peer

**PUT** `/api/peers/<peer_id>`

### Delete Peer

**DELETE** `/api/peers/<peer_id>`

### Test Connection

**GET** `/api/peers/<peer_id>/test`

Checks if you can reach the peer instance.

### Get Peer's Stacks

**GET** `/api/peer/<peer_id>/stacks`

Lists stacks running on a remote peer.

### Control Peer's Stack

**POST** `/api/peer/<peer_id>/stack/<stack_name>/operation`

Same as the local stack operation endpoint, but on a remote instance.

## SSL Certificate Automation - Your Actual Use Case

Here's how to set this up with Let's Encrypt/Certbot.

### The Script

Save this as `/etc/letsencrypt/renewal-hooks/deploy/dockup-restart.sh`:

```bash
#!/bin/bash
#
# DockUp SSL Certificate Renewal Hook
#

DOCKUP_URL="http://localhost:5000"
DOCKUP_PASSWORD="your_password_here"
LOG_FILE="/var/log/dockup-certbot.log"

# Stacks to restart after cert renewal
STACKS_TO_RESTART=(
    "nginx-proxy"
    "traefik"
)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Check if DockUp is up
if ! curl -s -f "$DOCKUP_URL/api/ping" > /dev/null 2>&1; then
    log "ERROR: Cannot reach DockUp"
    exit 1
fi

log "SSL certificate renewed - restarting stacks..."

# Login
SESSION_COOKIE=$(curl -s -c - -X POST "$DOCKUP_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$DOCKUP_PASSWORD\"}" \
    | grep session | awk '{print $7}')

if [ -z "$SESSION_COOKIE" ]; then
    log "ERROR: Login failed"
    exit 1
fi

# Restart each stack
for STACK in "${STACKS_TO_RESTART[@]}"; do
    log "Restarting $STACK..."
    
    RESPONSE=$(curl -s -X POST "$DOCKUP_URL/api/stack/$STACK/operation" \
        -H "Content-Type: application/json" \
        -H "Cookie: session=$SESSION_COOKIE" \
        -d '{"operation": "restart"}')
    
    if echo "$RESPONSE" | grep -q '"success":true'; then
        log "✓ $STACK restarted"
    else
        log "✗ $STACK failed: $RESPONSE"
    fi
    
    sleep 5
done

log "Done"
exit 0
```

Make it executable:
```bash
chmod +x /etc/letsencrypt/renewal-hooks/deploy/dockup-restart.sh
```

### Testing It

```bash
# Test the script manually
/etc/letsencrypt/renewal-hooks/deploy/dockup-restart.sh

# Or force a cert renewal to test
certbot renew --force-renewal
```

Check the log:
```bash
tail -f /var/log/dockup-certbot.log
```

### Alternative - Simple One-Liner

If you just need to restart one stack:

```bash
# Add to crontab or your renewal script
curl -s -c /tmp/cookie -X POST http://localhost:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your_password"}' && \
curl -X POST http://localhost:5000/api/stack/nginx-proxy/operation \
  -H "Content-Type: application/json" \
  -H "Cookie: $(grep session /tmp/cookie | awk '{print $7}')" \
  -d '{"operation": "restart"}'
```

## WebSocket for Real-Time Updates

If you want live updates (like watching logs in real-time), connect to the WebSocket at `/ws`.

```javascript
const ws = new WebSocket('ws://localhost:5000/ws');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === 'operation_output') {
    console.log(`${data.stack}: ${data.line}`);
  }
};
```

Message types you'll get:
- `operation_output` - Live output from stack operations
- `stats_update` - Container stats
- `health_update` - Health status changes
- `scan_complete` - Security scan finished

## Common Errors

**401 Unauthorized**
```json
{"error": "Authentication required"}
```
You forgot to login or your session expired.

**400 Bad Request**
```json
{"error": "Invalid operation: unknown"}
```
Typo in the operation name. Use: up, down, stop, restart, or pull.

**404 Not Found**
```json
{"error": "Stack not found"}
```
Stack doesn't exist. Check the name.

**500 Internal Server Error**
```json
{"error": "Failed to execute operation: connection timeout"}
```
Something went wrong on the server. Check the logs: `docker logs dockup`

## Tips and Tricks

### Quick Health Check Script

```bash
#!/bin/bash
# Monitor all stacks and auto-restart unhealthy ones

while true; do
  for stack in $(curl -s http://localhost:5000/api/stacks | jq -r '.stacks[].name'); do
    status=$(curl -s http://localhost:5000/api/stack/$stack | jq -r '.health_status')
    
    if [ "$status" != "healthy" ]; then
      echo "⚠️  $stack is $status - restarting..."
      curl -X POST http://localhost:5000/api/stack/$stack/operation \
        -d '{"operation": "restart"}'
    fi
  done
  sleep 300  # Check every 5 minutes
done
```

### Bulk Update All Stacks

```bash
#!/bin/bash
# Update everything during maintenance window

for stack in $(curl -s http://localhost:5000/api/stacks | jq -r '.stacks[].name'); do
  echo "Updating $stack..."
  
  curl -X POST "http://localhost:5000/api/stack/$stack/operation" \
    -d '{"operation": "pull"}' && \
  curl -X POST "http://localhost:5000/api/stack/$stack/operation" \
    -d '{"operation": "up"}'
  
  sleep 10
done
```

### Scheduled Maintenance

```bash
#!/bin/bash
# Run this in crontab: 0 3 * * 0 (Sunday 3 AM)

# Stop services
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -d '{"operation": "stop"}'

# Do your maintenance stuff
sleep 300

# Start services back up
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -d '{"operation": "up"}'
```

## Security Notes

1. **Don't expose DockUp directly to the internet** - Use a reverse proxy with auth
2. **Strong passwords** - Seriously, use a password manager
3. **Rotate API tokens** - Use the regenerate endpoint occasionally
4. **HTTPS only** - Always use TLS in production
5. **Firewall rules** - Limit access to trusted IPs
6. **Check logs** - `docker logs dockup` to watch for suspicious activity

## That's It



The code is solid but if you're exposing this to the internet, please add rate limiting and better auth. Right now it's designed for internal homelab use.
