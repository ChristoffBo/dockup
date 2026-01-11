# DockUp API Documentation

Complete API reference for remotely managing Docker stacks.

## Getting Started

DockUp runs on `http://your-server:5000` by default (or whatever port you set with `PORT` env var).

Current version: **1.3.0**

## Authentication

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

API token auth is only enabled on `/api/ping` right now.

Your API token is in Settings → Instance Configuration, or check the logs when DockUp starts up.

To use it:
```bash
curl http://localhost:5000/api/ping \
  -H "X-API-Token: your_token_here"
```

**For SSL certificate automation, you'll need to use session-based auth** (see examples at the bottom).

## The Endpoints You'll Actually Use

### List All Your Stacks

**GET** `/api/stacks`

```bash
curl http://localhost:5000/api/stacks
```

Returns:
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

### Stack Operations

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
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -d '{"operation": "pull"}'

curl -X POST http://localhost:5000/api/stack/myapp/operation \
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

Or if broken:
```json
{
  "valid": false,
  "error": "Invalid YAML syntax at line 5"
}
```

## Auto-Updates and Scheduling

### Get Current Schedule

**GET** `/api/stack/<stack_name>/schedule`

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

### Delete an Image

**DELETE** `/api/images/<image_id>`

```bash
curl -X DELETE http://localhost:5000/api/images/sha256:abc123def456
```

### Clean Up Unused Images

**POST** `/api/images/prune`

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

## System Stats

### Host System Stats

**GET** `/api/host/stats`

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

### Check Disk Usage

**GET** `/api/appdata/sizes`

### Version Info

**GET** `/api/version`

```bash
curl http://localhost:5000/api/version
```

### Health Check

**GET** `/api/ping`

```bash
curl http://localhost:5000/api/ping \
  -H "X-API-Token: your_token"
```

## Security Scanning

### Scan a Container

**POST** `/api/container/<container_id>/scan`

```bash
curl -X POST http://localhost:5000/api/container/abc123def456/scan
```

### Check Scan Results

**GET** `/api/container/<container_id>/scan-status`

```bash
curl http://localhost:5000/api/container/abc123def456/scan-status
```

## Backup and Restore

### Create Backup

**POST** `/api/backup/create`

```bash
curl -X POST http://localhost:5000/api/backup/create
```

### Restore Backup

**POST** `/api/backup/restore`

```bash
curl -X POST http://localhost:5000/api/backup/restore \
  -H "Content-Type: application/json" \
  -d '{"backup_file": "backup_20250111_103000.tar.gz"}'
```

## Network Management

### List Networks

**GET** `/api/networks`

### Create Network

**POST** `/api/network`

```bash
curl -X POST http://localhost:5000/api/network \
  -H "Content-Type: application/json" \
  -d '{
    "name": "myapp-network",
    "driver": "bridge"
  }'
```

### Delete Network

**DELETE** `/api/network/<network_id>`

## Configuration

### Get Current Config

**GET** `/api/config`

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

```bash
curl -X POST http://localhost:5000/api/config/regenerate-token
```

## Templates

### List Available Templates

**GET** `/api/templates/list`

### Refresh Templates

**POST** `/api/templates/refresh`

### Get Specific Template

**GET** `/api/templates/<template_id>`

```bash
curl http://localhost:5000/api/templates/nginx-proxy-manager
```

### Get Categories

**GET** `/api/templates/categories`

## Peer Management

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

### Get Peer's Stacks

**GET** `/api/peer/<peer_id>/stacks`

### Control Peer's Stack

**POST** `/api/peer/<peer_id>/stack/<stack_name>/operation`

## SSL Certificate Automation Example

Save this as `/etc/letsencrypt/renewal-hooks/deploy/dockup-restart.sh`:

```bash
#!/bin/bash

DOCKUP_URL="http://localhost:5000"
DOCKUP_PASSWORD="your_password_here"
LOG_FILE="/var/log/dockup-certbot.log"

STACKS_TO_RESTART=(
    "nginx-proxy"
    "traefik"
)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

if ! curl -s -f "$DOCKUP_URL/api/ping" > /dev/null 2>&1; then
    log "ERROR: Cannot reach DockUp"
    exit 1
fi

log "SSL certificate renewed - restarting stacks..."

SESSION_COOKIE=$(curl -s -c - -X POST "$DOCKUP_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"password\": \"$DOCKUP_PASSWORD\"}" \
    | grep session | awk '{print $7}')

if [ -z "$SESSION_COOKIE" ]; then
    log "ERROR: Login failed"
    exit 1
fi

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

Make executable:
```bash
chmod +x /etc/letsencrypt/renewal-hooks/deploy/dockup-restart.sh
```

Test it:
```bash
/etc/letsencrypt/renewal-hooks/deploy/dockup-restart.sh
certbot renew --force-renewal
```

## WebSocket

Connect to `/ws` for real-time updates:

```javascript
const ws = new WebSocket('ws://localhost:5000/ws');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === 'operation_output') {
    console.log(`${data.stack}: ${data.line}`);
  }
};
```

Message types:
- `operation_output` - Live output from stack operations
- `stats_update` - Container stats
- `health_update` - Health status changes
- `scan_complete` - Security scan finished

## Common Errors

**401 Unauthorized**
```json
{"error": "Authentication required"}
```

**400 Bad Request**
```json
{"error": "Invalid operation: unknown"}
```

**404 Not Found**
```json
{"error": "Stack not found"}
```

**500 Internal Server Error**
```json
{"error": "Failed to execute operation: connection timeout"}
```

## Common Use Cases

### Automated Certificate Renewal
```bash
curl -X POST http://localhost:5000/api/stack/nginx-proxy/operation \
  -H "Cookie: session=$SESSION" \
  -d '{"operation": "restart"}'
```

### Scheduled Maintenance
```bash
# Stop services
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -d '{"operation": "stop"}'

sleep 300

# Start services
curl -X POST http://localhost:5000/api/stack/myapp/operation \
  -d '{"operation": "up"}'
```

### Health Monitoring
```bash
while true; do
  STATUS=$(curl -s http://localhost:5000/api/stack/myapp | jq -r '.health_status')
  if [ "$STATUS" != "healthy" ]; then
    curl -X POST http://localhost:5000/api/stack/myapp/operation \
      -d '{"operation": "restart"}'
  fi
  sleep 300
done
```

### Bulk Updates
```bash
for stack in $(curl -s http://localhost:5000/api/stacks | jq -r '.stacks[].name'); do
  curl -X POST "http://localhost:5000/api/stack/$stack/operation" -d '{"operation": "pull"}'
  curl -X POST "http://localhost:5000/api/stack/$stack/operation" -d '{"operation": "up"}'
  sleep 10
done
```

## Quick Reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/stacks` | GET | List all stacks |
| `/api/stack/<n>/operation` | POST | Start/stop/restart |
| `/api/stack/<n>/logs` | GET | View logs |
| `/api/config` | GET/POST | Settings |
| `/api/host/stats` | GET | System resources |
| `/api/ping` | GET | Health check |
