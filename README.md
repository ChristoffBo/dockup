 # 🚀 Dockup — Docker Compose Stack Manager with Auto-Update

Dockup is a standalone Docker container that combines Dockge-style stack management with Watchtower-style auto-update detection. It provides a full web UI for managing Docker Compose stacks, editing compose files, running commands, checking for image updates, pruning unused images, viewing logs, and scheduling automatic updates. Everything is fully local and self-hosted.

## Features
- Auto-detects all stacks inside /stacks (compose.yaml / docker-compose.yml)
- Automatically imports orphan containers that were started via “docker run”
- Safe orphan import system (does NOT interfere with running containers)
- Start / Stop / Restart / Pull stacks with real-time streamed output
- Edit compose files directly in the browser with YAML validation
- Live container logs per stack
- Live CPU/RAM usage per stack (updates every 10 seconds)
- Update detection using registry APIs + manifest fallback (works on all registries)
- Optional auto-update mode (safe pull + up + health checks)
- Per-stack cron scheduling (off / check / auto)
- Global “Apply to all stacks” scheduler
- Full image manager: list, delete, prune unused
- Automatic prune scheduler
- Apprise + Gotify notifications
- WebSocket real-time updates
- Fully offline-capable once images are pulled

## ⚠️ About Orphan Container Importing
Dockup includes an **auto-importer** that detects any container running WITHOUT a Docker Compose project label. These “orphans” are usually created by:
- `docker run`
- CasaOS
- Portainer
- Old systems left behind

Dockup will:
1. Detect the orphan  
2. Generate a Compose folder for it under `/stacks/imported-<name>`  
3. Create a clean compose.yaml  
4. Tag the original container so it is never imported again  

**Dockup does NOT automatically kill, replace, or adopt the running container.**

### Why?
Because adopting and replacing a live container is dangerous and can cause:
- Broken volumes  
- Broken networks  
- Downtime  
- Data loss  
- Forced container recreation  
- Applications restarting when they should not  

Docker Compose will recreate a container if ANY part of the compose file differs.  


Dockup takes the **safer approach**:
- It imports the orphan  
- Does NOT modify the running container  
- Does NOT recreate anything  
- Lets YOU decide when to switch  
- Prevents accidental downtime  

This guarantees safety for all users.

### How to fully migrate an orphan into Dockup (safe procedure)
1. Dockup will auto-import the orphan and generate a compose folder.  
2. You manually delete the original container:  
   `docker rm -f <containername>`  
3. (Optional) Rename the imported folder/compose file to something cleaner.  
4. Start the stack from Dockup using the UI.

This avoids downtime and ensures 100% safe migration.

## Docker Hub Image
cbothma/dockup:latest

## Example Run Command
docker run -d \
  --name dockup \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /path/to/your/stacks:/stacks \
  -v /path/to/persist/data:/app/data \
  cbothma/dockup:latest

## Mounts
/var/run/docker.sock — Required for Docker control  
/stacks — Folder containing your compose stacks  
/app/data — Persists schedules + UI settings  

## Access
Open the UI at:
http://localhost:5000

Inside the UI you can:
- View all stacks and their resource usage
- Run operations (up / down / restart / pull)
- Edit compose.yaml with validation
- View live logs for each container
- Check updates manually
- Enable auto updates per stack
- Manage and prune images
- Configure notifications
- See real-time output and events

Easy Clean. As it should be.
