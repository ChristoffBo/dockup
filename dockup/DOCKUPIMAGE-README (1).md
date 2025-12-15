# DockUp Image Builder & Dependency Updater

## What It Does

This script automates the process of updating DockUp's dependencies and rebuilding the Docker image.

**The script will:**
1. ✅ Backup ALL files to `Old/backup-VERSION-TIMESTAMP/`
2. ✅ Check for dependency updates:
   - Python base image (python:3.14-slim)
   - Trivy scanner version
   - All Python packages in Dockerfile
3. ✅ Update `Dockerfile` with new versions
4. ✅ Bump version in `app.py` (line 8) ONLY if updates found
5. ✅ Rebuild and restart the DockUp container
6. ✅ Create `UPDATE-SUMMARY.txt` with what changed

**The script will NEVER:**
- ❌ Delete anything from `Old/` folder (only you can do that)
- ❌ Stop your container if the build fails
- ❌ Update anything if no updates are found

---

## Requirements

- Python 3 installed on the host
- Docker installed and running
- Script must be run from the DockUp build directory (`/DATA/Compose/dockup/`)
- `curl` installed (should be default on most systems)

---

## Installation

1. Copy `dockupimage.sh` to your DockUp directory:
   ```bash
   cp dockupimage.sh /DATA/Compose/dockup/
   cd /DATA/Compose/dockup/
   ```

2. Make it executable:
   ```bash
   chmod +x dockupimage.sh
   ```

---

## Usage

### Basic Usage

Simply run the script from your DockUp directory:

```bash
cd /DATA/Compose/dockup/
./dockupimage.sh
```

### What Happens

**If updates are found:**
```
═══════════════════════════════════════════════════════════
  DockUp Image Builder & Dependency Updater
═══════════════════════════════════════════════════════════

ℹ Current version: 1.1.5

ℹ Creating backup: backup-1.1.5-20241215-145230
✓ Backup created: /DATA/Compose/dockup/Old/backup-1.1.5-20241215-145230

ℹ Checking for dependency updates...

Updates found:
Python Packages:
  - flask: 3.1.2 → 3.1.3
  - requests: 2.32.3 → 2.32.4
  - docker: 7.1.0 → 7.1.5

Trivy Scanner:
  - Trivy: v0.58.1 → v0.59.0

ℹ Updating dependency files...
✓ Python packages updated in Dockerfile
✓ Trivy version updated in Dockerfile
✓ Version bumped in app.py: 1.1.5 → 1.1.6

⚠ Ready to rebuild DockUp with version 1.1.6
Build and restart DockUp? (y/n): y

ℹ Stopping and removing existing container...
ℹ Building new DockUp image...
✓ Build successful!
ℹ Starting new DockUp container...
✓ Container started successfully!
✓ DockUp is running!

✓ Summary saved to UPDATE-SUMMARY.txt
✓ Update complete! Check UPDATE-SUMMARY.txt for details.
ℹ Don't forget to push to Docker Hub when ready!
```

**If no updates found:**
```
═══════════════════════════════════════════════════════════
  DockUp Image Builder & Dependency Updater
═══════════════════════════════════════════════════════════

ℹ Current version: 1.1.5

ℹ Creating backup: backup-1.1.5-20241215-145230
✓ Backup created: /DATA/Compose/dockup/Old/backup-1.1.5-20241215-145230

ℹ Checking for dependency updates...

✓ All dependencies are up to date!
ℹ No updates needed.
```

---

## After Running

### Check the Summary

The script creates `UPDATE-SUMMARY.txt` with details:

```
DockUp Update Summary
=====================
Date: 2024-12-15 14:52:30
Backup: /DATA/Compose/dockup/Old/backup-1.1.5-20241215-145230

Version Change: 1.1.5 → 1.1.6

Dependencies Updated:
Python Packages:
  - flask: 3.1.2 → 3.1.3
  - requests: 2.32.3 → 2.32.4
  - docker: 7.1.0 → 7.1.5

Trivy Scanner:
  - Trivy: v0.58.1 → v0.59.0

Build Status: SUCCESS
Container Status: RUNNING

Ready to push to Docker Hub:
  docker push cbothma/dockup:latest
  docker tag cbothma/dockup:latest cbothma/dockup:1.1.6
  docker push cbothma/dockup:1.1.6
```

### Push to Docker Hub

When you're ready to push the new version to Docker Hub:

```bash
docker push cbothma/dockup:latest
docker tag cbothma/dockup:latest cbothma/dockup:1.1.6
docker push cbothma/dockup:1.1.6
```

---

## Safety Features

### Backups Are Always Created

Every time you run the script, it backs up ALL files to:
```
Old/backup-1.1.5-20241215-145230/
```

This includes:
- app.py
- Dockerfile
- static/
- templates/
- Build and load.txt
- Everything else in the directory

**Backups are NEVER deleted by the script.** Only you can clean them up.

### Build Failures Are Safe

If the Docker build fails:
- ✅ Old container keeps running
- ✅ All files are backed up in `Old/`
- ✅ You can restore from backup

### Restore from Backup

If something breaks, restore from the backup:

```bash
# Find your backup
ls -la Old/

# Restore all files
cp -r Old/backup-1.1.5-20241215-145230/* .

# Rebuild
docker stop dockup && docker rm dockup
docker build -t dockup:latest .
docker run -d \
  --name dockup \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /DATA/Compose:/stacks \
  -v dockup_data:/app/data \
  --restart unless-stopped \
  dockup:latest
```

---

## How Often to Run

**Recommended:** Monthly or quarterly

Dependency updates include:
- Security patches
- Bug fixes
- Performance improvements

You don't need to run this constantly. Once a month or when you remember is fine.

---

## What Gets Updated

### Dependencies Checked (in Dockerfile)

**Base Image:**
- `python:3.14-slim` (or whatever version you're using)
- Script checks Docker Hub for newer Python slim images

**Security Scanner:**
- Trivy vulnerability scanner
- Script checks GitHub releases for newer versions

**Python Packages:**
- flask
- flask-sock
- docker (docker-py)
- pyyaml
- apscheduler
- apprise
- croniter
- requests
- pytz
- psutil
- bcrypt

**Important:** The script checks PyPI for each package individually and only updates if newer versions exist.

---

## Troubleshooting

### Script says "Required files not found"
- Make sure you're running from `/DATA/Compose/dockup/`
- Check that `app.py` and `Dockerfile` exist

### Build fails
- Check Docker logs: `docker logs dockup`
- Restore from backup (see above)
- Check `UPDATE-SUMMARY.txt` for what changed

### Container won't start
- Check logs: `docker logs dockup`
- Verify volumes are mounted correctly
- Restore from backup

### Python packages conflict
- Restore from backup
- Check Dockerfile for conflicting versions
- Build manually to see detailed errors

### API rate limits (PyPI or Docker Hub)
- Wait a few minutes and try again
- Script checks multiple APIs, temporary failures are normal

---

## Directory Structure After Running

```
/DATA/Compose/dockup/
├── Old/                              # Your backups (never deleted)
│   ├── backup-1.1.5-20241215-145230/
│   ├── backup-1.1.6-20241216-103045/
│   └── (your manual backups)
├── static/
├── templates/
├── app.py                            # Version bumped if updates found
├── Dockerfile                        # Updated with new dependency versions
├── Build and load.txt                # Your manual commands (untouched)
├── dockupimage.sh                    # This script
└── UPDATE-SUMMARY.txt                # Created after updates
```

---

## Technical Details

### How Dependency Checking Works

**Python Base Image:**
- Queries Docker Hub API for `library/python` tags
- Finds latest `X.XX-slim` tag
- Compares to current version in Dockerfile

**Trivy:**
- Queries GitHub API for aquasecurity/trivy releases
- Gets latest release tag
- Compares to version in Dockerfile

**Python Packages:**
- Extracts package versions from Dockerfile `RUN pip install` line
- Queries PyPI API for each package
- Compares current version to latest available

### What Gets Modified

**Dockerfile:**
- Line 1: `FROM python:X.XX-slim`
- Line 22: Trivy version `v0.XX.X`
- Lines 28-39: Package versions `package==X.X.X`

**app.py:**
- Line 8: `DOCKUP_VERSION = "X.X.X"`

---

## Notes

- The script ONLY bumps the version if dependencies are updated
- The script asks for confirmation before rebuilding
- You can cancel at any time (Ctrl+C)
- All your files are backed up before any changes
- The script never deletes old backups
- Network access required (checks PyPI, Docker Hub, GitHub APIs)

---

## Questions?

If something breaks:
1. Check `UPDATE-SUMMARY.txt` for what changed
2. Check Docker logs: `docker logs dockup`
3. Restore from `Old/backup-VERSION-TIMESTAMP/`

Lekker! 🚀
