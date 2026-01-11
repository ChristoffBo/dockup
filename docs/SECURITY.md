# DockUp Security Guide

DockUp has root-level access to Docker. Here's how to lock it down.

## Threat Model

**What DockUp can do:**
- Start/stop/delete any container
- Read all container logs
- Modify compose files
- Access Docker socket (root access)
- Create/delete networks and volumes

**What attackers could do:**
- Deploy malicious containers
- Exfiltrate data
- Modify services
- Install backdoors
- Delete everything

**Bottom line:** Treat DockUp like SSH root access.

## Basic Security

### 1. Strong Password

**Requirements:**
- 12+ characters
- Mix of case, numbers, symbols
- Not a dictionary word
- Not reused

**Generate password:**
```bash
openssl rand -base64 24
```

**Change password:**
```bash
curl -X POST http://localhost:5000/api/auth/change-password \
  -d '{
    "current": "old_password",
    "new_password": "new_password",
    "confirm": "new_password"
  }'
```

### 2. Enable Password Protection

Never disable unless:
- Completely isolated network
- Behind another auth layer
- Testing only

### 3. Rotate API Tokens

**Regenerate monthly:**
```bash
curl -X POST http://localhost:5000/api/config/regenerate-token
```

### 4. Don't Expose to Internet

**Bad:**
```yaml
ports:
  - "5000:5000"  # Accessible from anywhere
```

**Better:**
```yaml
ports:
  - "127.0.0.1:5000:5000"  # Localhost only
```

## Network Security

### Firewall Rules

**UFW:**
```bash
# Allow from specific subnet
sudo ufw allow from 192.168.1.0/24 to any port 5000

# Deny all else
sudo ufw deny 5000
```

**firewalld:**
```bash
sudo firewall-cmd --permanent --new-zone=management
sudo firewall-cmd --permanent --zone=management --add-source=192.168.1.0/24
sudo firewall-cmd --permanent --zone=management --add-port=5000/tcp
sudo firewall-cmd --reload
```

**iptables:**
```bash
sudo iptables -A INPUT -p tcp --dport 5000 -s 192.168.1.0/24 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 5000 -j DROP
sudo iptables-save > /etc/iptables/rules.v4
```

### VPN-Only Access

**Best practice:** Only accessible via VPN.

**Wireguard:**
```bash
wg-quick up wg0

# Firewall
sudo ufw allow from 10.0.0.0/24 to any port 5000
sudo ufw deny 5000
```

**Tailscale:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

sudo ufw allow from 100.0.0.0/8 to any port 5000
sudo ufw deny 5000
```

## Reverse Proxy + Authentication

### Nginx + HTTP Basic Auth

```nginx
server {
    listen 443 ssl http2;
    server_name dockup.example.com;
    
    ssl_certificate /etc/letsencrypt/live/dockup.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dockup.example.com/privkey.pem;
    
    auth_basic "DockUp Access";
    auth_basic_user_file /etc/nginx/.htpasswd;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        
        # WebSocket
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

**Create htpasswd:**
```bash
sudo apt-get install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd admin
```

### Caddy + Basic Auth

```
dockup.example.com {
    basicauth {
        admin $2a$14$hashed_password
    }
    
    reverse_proxy localhost:5000
}
```

**Generate hash:**
```bash
caddy hash-password
```

## HTTPS/TLS

### Let's Encrypt

```bash
sudo apt-get install certbot python3-certbot-nginx
sudo certbot --nginx -d dockup.example.com
sudo systemctl enable certbot.timer
```

### Caddy (Auto HTTPS)

```
dockup.example.com {
    reverse_proxy localhost:5000
}
```

Caddy handles HTTPS automatically.

### Traefik (Auto HTTPS)

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.dockup.rule=Host(`dockup.example.com`)"
  - "traefik.http.routers.dockup.entrypoints=websecure"
  - "traefik.http.routers.dockup.tls.certresolver=letsencrypt"
```

## Access Control

### IP Whitelisting

**Nginx:**
```nginx
location / {
    allow 192.168.1.0/24;
    allow 10.0.0.0/8;
    deny all;
    
    proxy_pass http://localhost:5000;
}
```

**Caddy:**
```
dockup.example.com {
    @allowed {
        remote_ip 192.168.1.0/24 10.0.0.0/8
    }
    handle @allowed {
        reverse_proxy localhost:5000
    }
    respond 403
}
```

## Container Security

### Resource Limits

```yaml
services:
  dockup:
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
```

### Security Scanning

**Scan DockUp image:**
```bash
trivy image your-registry/dockup:latest
```

**Regular scans:**
```bash
# Cron: 0 2 * * 0
docker pull your-registry/dockup:latest
trivy image your-registry/dockup:latest
```

## Monitoring and Logging

### Watch Logs

```bash
# Failed logins
docker logs dockup | grep "Invalid password"

# API calls
docker logs dockup | grep "POST"

# Stack operations
docker logs dockup | grep "operation"
```

### Centralized Logging

```yaml
services:
  dockup:
    logging:
      driver: syslog
      options:
        syslog-address: "tcp://logserver:514"
        tag: "dockup"
```

### Alert on Suspicious Activity

```bash
# Watch for failed logins
docker logs -f dockup | grep --line-buffered "Invalid password" | while read line; do
  curl https://gotify.example.com/message?token=TOKEN \
    -F "title=DockUp Alert" \
    -F "message=$line"
done
```

## Data Protection

### Backup Encryption

```bash
# Create backup
curl -X POST http://localhost:5000/api/backup/create

# Encrypt
gpg --encrypt --recipient you@example.com backup.tar.gz
rm backup.tar.gz
```

### Automated Encrypted Backups

```bash
#!/bin/bash
BACKUP=$(curl -s -X POST http://localhost:5000/api/backup/create | jq -r '.backup_file')
gpg --encrypt --recipient you@example.com "$BACKUP"
rm "$BACKUP"
```

## Hardening Checklist

**Basic:**
- [ ] Strong password (12+ characters)
- [ ] Password protection enabled
- [ ] HTTPS with valid certificate
- [ ] Firewall rules restricting access
- [ ] Regular backups

**Intermediate:**
- [ ] Reverse proxy with authentication
- [ ] VPN-only access
- [ ] IP whitelisting
- [ ] API token rotation
- [ ] Resource limits
- [ ] Monitoring and alerting

**Advanced:**
- [ ] Client certificate auth
- [ ] Geographic restrictions
- [ ] Centralized logging
- [ ] Encrypted backups
- [ ] Disaster recovery tested

## Security Incident Response

**If compromised:**

1. **Immediately:**
   ```bash
   docker stop dockup
   ```

2. **Check logs:**
   ```bash
   docker logs dockup > dockup-logs.txt
   grep "Invalid password" dockup-logs.txt
   grep "POST" dockup-logs.txt
   ```

3. **Check changes:**
   ```bash
   diff -r /stacks/ /backup/stacks/
   docker ps -a
   docker images
   ```

4. **Rotate credentials:**
   - Change password
   - Regenerate API token
   - Update peer tokens

5. **Restore from backup:**
   ```bash
   curl -X POST http://localhost:5000/api/backup/restore \
     -d '{"backup_file": "backup_before_incident.tar.gz"}'
   ```

6. **Investigate:**
   - Check firewall logs
   - Review access logs
   - Scan containers

## Regular Security Tasks

**Daily:**
- Monitor logs for anomalies
- Check failed login attempts

**Weekly:**
- Review stack changes
- Check resource usage

**Monthly:**
- Rotate API tokens
- Review firewall rules
- Test backups

**Quarterly:**
- Review access controls
- Security scan DockUp image
- Test disaster recovery

**Annually:**
- Change password
- Full security audit

## Security Notes

**DockUp is for trusted internal use.**

Not multi-tenant. Not internet-facing. Assumes legitimate Docker access.

**For internet-facing:**
Use VPN + strong auth + monitoring.

**Best practice:**
Don't expose publicly at all.
