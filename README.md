# 🚀 Dockup — Docker Compose Stack Manager with Auto-Update

Dockup is a standalone Docker container that combines Dockge-style stack management with Tugtainer/Watchtower-style auto-update detection. It provides a full web UI for managing Docker Compose stacks, editing compose files, running commands, checking for image updates, pruning unused images, viewing logs, and scheduling automatic updates. Everything is fully local and self-hosted.

## Features
- Auto-detects all stacks inside /stacks (compose.yaml / docker-compose.yml)
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

## Docker Hub Image
**cbothma/dockup:latest**

## Example Run Command
docker run -d \
  --name dockup \
  cbothma/dockup:latest \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /path/to/your/stacks:/stacks \
  -v /path/to/persist/data:/app/data

## Mounts
/var/run/docker.sock  — Required for Docker control  
/stacks               — Folder containing your compose stacks  
/app/data             — Persists schedules + UI settings  

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

Dockup replaces Dockge + Tugtainer + Watchtower with one fast, modern, fully local tool.
