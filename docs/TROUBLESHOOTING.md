# DockUp Troubleshooting Guide

## Can't Access Web UI

### Connection refused on port 5000

**Check if running:**
```bash
docker ps | grep dockup
```

If not running:
```bash
docker logs dockup
docker start dockup
```

**Check port mapping:**
```bash
docker port dockup
```

**Try different addresses:**
```bash
curl http://localhost:5000
curl http://127.0.0.1:5000
curl http://your-server-ip:5000
```

**Check firewall:**
```bash
sudo ufw status
sudo ufw allow 5000
```

### Port 5000 already in use

**Find what's using it:**
```bash
sudo lsof -i :5000
sudo netstat -tlnp | grep 5000
```

**Solutions:**
1. Stop the conflicting service
2. Change DockUp port:
```yaml
ports:
  - "8080:5000"
```

### 502 Bad Gateway (reverse proxy)

**Nginx - add WebSocket support:**
```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

**Check DockUp is accessible:**
```bash
curl http://localhost:5000
```

## Docker Connection Issues

### "Cannot connect to Docker daemon"

**Check socket mount:**
```bash
docker inspect dockup | grep -A 5 Binds
```

**Check socket exists:**
```bash
ls -la /var/run/docker.sock
```

**Fix permissions:**
```bash
sudo chmod 666 /var/run/docker.sock
```

**Check Docker is running:**
```bash
sudo systemctl status docker
sudo systemctl start docker
```

### "docker compose command not found"

**Check version:**
```bash
docker compose version
```

**Install Docker Compose V2:**
```bash
sudo apt-get update
sudo apt-get install docker-compose-plugin
```

## Stack Issues

### "No stacks found"

**Check mount:**
```bash
docker exec dockup ls -la /stacks/
```

**Check structure:**
```
/stacks/
├── stack1/
│   └── compose.yaml
└── stack2/
    └── compose.yaml
```

**Fix permissions:**
```bash
sudo chown -R 1000:1000 /path/to/stacks/
sudo chmod -R 755 /path/to/stacks/
```

### Stack shows "unknown" status

**Check containers:**
```bash
docker ps -a | grep stack-name
```

**Try starting:**
```bash
cd /path/to/stacks/stack-name
docker compose up -d
```

**Check syntax:**
```bash
docker compose -f /path/to/stacks/stack-name/compose.yaml config
```

### Stack won't start

**Check logs:**
```bash
docker logs dockup | grep "stack-name"
```

**Validate compose:**
```bash
cd /path/to/stacks/stack-name
docker compose config
```

**Common issues:**
- Port conflicts
- Missing env vars
- Invalid volume paths
- Missing images

**Test manually:**
```bash
cd /path/to/stacks/stack-name
docker compose up
```

### "Port already in use"

**Find what's using it:**
```bash
sudo lsof -i :80
sudo netstat -tlnp | grep :80
```

**Fix:**
1. Stop conflicting service
2. Change port in compose
3. Use different mapping:
```yaml
ports:
  - "8080:80"
```

## Auto-Update Issues

### Schedules not running

**Check global enabled:**
```bash
curl http://localhost:5000/api/config | grep auto_update_enabled
```

**Check stack enabled:**
```bash
curl http://localhost:5000/api/stack/stack-name | grep auto_update
```

**Check scheduler:**
```bash
docker logs dockup | grep scheduler
```

**Check timezone:**
```bash
curl http://localhost:5000/api/config | grep timezone
```

### Updates running at wrong time

**Timezone mismatch - update it:**
```bash
curl -X POST http://localhost:5000/api/config \
  -H "Content-Type: application/json" \
  -d '{"timezone": "America/New_York"}'

docker restart dockup
```

### "No updates available" but new images exist

**Force check:**
```bash
curl -X POST http://localhost:5000/api/stack/stack-name/check-updates
```

**Pull manually:**
```bash
cd /stacks/stack-name
docker compose pull
docker compose up -d
```

## Authentication Issues

### Can't login

**Reset password:**
```bash
docker exec dockup rm /app/data/password.hash
docker restart dockup
```
Go to http://localhost:5000/setup

### API token not working

**Only works on /api/ping:**
```bash
curl http://localhost:5000/api/ping -H "X-API-Token: your_token"
```

**Regenerate token:**
```bash
curl -X POST http://localhost:5000/api/config/regenerate-token
```

## Notification Issues

### Notifications not sending

**Check enabled:**
```bash
curl http://localhost:5000/api/config | grep notification_enabled
```

**Test notification:**
```bash
curl -X POST http://localhost:5000/api/test-notification
```

**Check logs:**
```bash
docker logs dockup | grep -i notification
```

**Verify URL format:**
```bash
# Gotify
gotify://hostname/token

# Discord
discord://webhook_id/webhook_token
```

**Test connectivity:**
```bash
docker exec dockup ping -c 3 gotify.example.com
docker exec dockup curl -I https://gotify.example.com
```

## Performance Issues

### High CPU usage

**Check stats:**
```bash
docker stats dockup
```

**Common causes:**
1. Security scans running
2. Too many stacks (50+)
3. Frequent polling

### High memory usage

**Check memory:**
```bash
docker stats dockup
```

**Reduce memory:**
```bash
# Disable templates
curl -X POST http://localhost:5000/api/config \
  -d '{"templates_enabled": false}'

docker restart dockup
```

**Set limits:**
```yaml
services:
  dockup:
    mem_limit: 512m
```

## Security Scanning Issues

### Trivy database download fails

**Check internet:**
```bash
docker exec dockup ping -c 3 8.8.8.8
docker exec dockup curl -I https://github.com
```

**Manual download:**
```bash
docker exec dockup trivy image --download-db-only
```

**Check disk space:**
```bash
docker exec dockup df -h
```

### Scans timing out

Large images take 10+ minutes. This is normal.

**Check status:**
```bash
curl http://localhost:5000/api/container/container_id/scan-status
```

## Template Issues

### Templates not loading

**Check enabled:**
```bash
curl http://localhost:5000/api/templates/list | grep enabled
```

**Refresh manually:**
```bash
curl -X POST http://localhost:5000/api/templates/refresh
```

**Check logs:**
```bash
docker logs dockup | grep -i template
```

**Clear cache:**
```bash
docker exec dockup rm /app/data/templates_cache.json
docker restart dockup
```

## Peer Management Issues

### Can't connect to peer

**Test connectivity:**
```bash
curl http://localhost:5000/api/peers/peer_id/test
```

**Check reachable:**
```bash
docker exec dockup curl http://peer-ip:5000/api/ping
```

**Verify token:**
Get token from peer's Settings, update in your config.

### Peer authentication fails

**Get peer's token:**
```bash
ssh peer-server
docker exec dockup cat /app/data/config.json | grep api_token
```

**Update:**
```bash
curl -X PUT http://localhost:5000/api/peers/peer_id \
  -d '{"api_token": "correct_token"}'
```

## Data Corruption

### Config file corrupted

**Backup broken file:**
```bash
docker exec dockup cp /app/data/config.json /app/data/config.json.broken
```

**Delete and restart:**
```bash
docker exec dockup rm /app/data/config.json
docker restart dockup
```

### Full reset

**Backup data:**
```bash
docker cp dockup:/app/data ./dockup-data-backup
```

**Clear all:**
```bash
docker exec dockup rm -rf /app/data/*
docker restart dockup
```

## Log Issues

### Logs not rotating

**Check logs:**
```bash
docker exec dockup ls -la /app/logs/
```

**Manual rotate:**
```bash
docker exec dockup mv /app/logs/dockup.log /app/logs/dockup.log.1
docker restart dockup
```

### Can't access logs

**From container:**
```bash
docker logs dockup
docker logs -f dockup
docker logs --tail 100 dockup
```

**Inside container:**
```bash
docker exec dockup tail -f /app/logs/dockup.log
```

**Copy out:**
```bash
docker cp dockup:/app/logs/ ./dockup-logs/
```

## Emergency Recovery

### Complete DockUp failure

**Your stacks are fine** - DockUp just manages them.

**Access stacks directly:**
```bash
cd /path/to/stacks/stack-name
docker compose up -d
docker compose down
docker compose logs
```

**Rebuild DockUp:**
```bash
docker stop dockup
docker rm dockup
docker compose up -d
```

**Data locations:**
- `/path/to/dockup-data/` - Config, passwords
- `/path/to/stacks/` - Compose files

## Diagnostic Commands

**System info:**
```bash
curl http://localhost:5000/api/version
```

**Stack list:**
```bash
curl http://localhost:5000/api/stacks
```

**Config:**
```bash
curl http://localhost:5000/api/config
```

**Host stats:**
```bash
curl http://localhost:5000/api/host/stats
```

**Logs:**
```bash
docker logs dockup | tail -100
```
