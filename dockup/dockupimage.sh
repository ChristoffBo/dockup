#!/bin/bash

################################################################################
# DockUp Image Builder & Dependency Updater
# 
# This script:
# 1. Backs up all files to Old/backup-VERSION-TIMESTAMP/
# 2. Checks for dependency updates (base image, Trivy, pip packages in Dockerfile)
# 3. Updates Dockerfile with new versions
# 4. Bumps version in app.py (line 8) if updates found
# 5. Rebuilds and restarts DockUp container
# 6. Creates UPDATE-SUMMARY.txt with changes
#
# Author: Christoff
# Version: 1.0.0
################################################################################

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PY="$SCRIPT_DIR/app.py"
DOCKERFILE="$SCRIPT_DIR/Dockerfile"
OLD_DIR="$SCRIPT_DIR/Old"
SUMMARY_FILE="$SCRIPT_DIR/UPDATE-SUMMARY.txt"

# Docker run command
DOCKER_STOP_CMD="docker stop dockup && docker rm dockup"
DOCKER_BUILD_CMD="docker build -t dockup:latest ."
DOCKER_RUN_CMD='docker run -d \
  --name dockup \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /DATA/Compose:/stacks \
  -v dockup_data:/app/data \
  --restart unless-stopped \
  dockup:latest'

################################################################################
# Functions
################################################################################

print_header() {
    echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  DockUp Image Builder & Dependency Updater${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

get_current_version() {
    grep "^DOCKUP_VERSION" "$APP_PY" | cut -d'"' -f2
}

bump_version() {
    local current=$1
    local IFS='.'
    read -ra parts <<< "$current"
    
    local major=${parts[0]}
    local minor=${parts[1]}
    local patch=${parts[2]}
    
    patch=$((patch + 1))
    
    echo "$major.$minor.$patch"
}

create_backup() {
    local version=$1
    local timestamp=$(date +%Y%m%d-%H%M%S)
    local backup_name="backup-${version}-${timestamp}"
    local backup_path="$OLD_DIR/$backup_name"
    
    print_info "Creating backup: $backup_name"
    
    # Create Old directory if it doesn't exist
    mkdir -p "$OLD_DIR"
    
    # Create backup directory
    mkdir -p "$backup_path"
    
    # Copy all files and directories (except Old directory itself)
    for item in "$SCRIPT_DIR"/*; do
        if [ "$(basename "$item")" != "Old" ]; then
            cp -r "$item" "$backup_path/" 2>/dev/null || true
        fi
    done
    
    if [ $? -eq 0 ]; then
        print_success "Backup created: $backup_path"
        echo "$backup_path"
    else
        print_error "Backup failed!"
        exit 1
    fi
}

check_base_image_updates() {
    local current_image=$(grep "^FROM" "$DOCKERFILE" | awk '{print $2}')
    
    # Extract image name and tag
    local image_name=$(echo "$current_image" | cut -d':' -f1)
    local current_tag=$(echo "$current_image" | cut -d':' -f2)
    
    # For python images, check Docker Hub for newer versions
    if [[ "$image_name" == "python" ]]; then
        # Get latest slim tag for Python
        local latest_tag=$(curl -s "https://registry.hub.docker.com/v2/repositories/library/python/tags?page_size=100&name=slim" 2>/dev/null | \
            python3 -c "
import sys, json, re
try:
    data = json.load(sys.stdin)
    versions = []
    for tag in data.get('results', []):
        name = tag['name']
        # Match pattern like 3.14-slim, 3.13-slim etc
        match = re.match(r'^(\d+\.\d+)-slim\$', name)
        if match:
            versions.append(name)
    if versions:
        # Sort by version number
        versions.sort(key=lambda x: [int(n) for n in x.split('-')[0].split('.')], reverse=True)
        print(versions[0])
except:
    pass
" 2>/dev/null)
        
        if [ -n "$latest_tag" ] && [ "$latest_tag" != "$current_tag" ]; then
            echo "$current_image|$image_name:$latest_tag|true"
        else
            echo "$current_image|$current_image|false"
        fi
    else
        echo "$current_image|$current_image|false"
    fi
}

check_trivy_updates() {
    local current_version=$(grep "trivy/main" "$DOCKERFILE" | grep -oP 'v\d+\.\d+\.\d+')
    
    if [ -z "$current_version" ]; then
        echo "||false"
        return
    fi
    
    # Get latest Trivy release from GitHub
    local latest_version=$(curl -s "https://api.github.com/repos/aquasecurity/trivy/releases/latest" 2>/dev/null | \
        python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['tag_name'])
except:
    pass
" 2>/dev/null)
    
    if [ -n "$latest_version" ] && [ "$latest_version" != "$current_version" ]; then
        echo "$current_version|$latest_version|true"
    else
        echo "$current_version|$current_version|false"
    fi
}

check_pip_updates() {
    # Extract package versions from Dockerfile
    local packages=$(grep -E "^\s*(flask|flask-sock|docker|pyyaml|apscheduler|apprise|croniter|requests|pytz|psutil|bcrypt)==" "$DOCKERFILE")
    
    # Create JSON array of outdated packages by checking PyPI
    local outdated_json="["
    local first=true
    
    while IFS= read -r line; do
        if [ -n "$line" ]; then
            # Extract package name and version
            local pkg_line=$(echo "$line" | tr -d ' ' | grep -oP '[a-z-]+==[\d.]+')
            local package=$(echo "$pkg_line" | cut -d'=' -f1)
            local current_version=$(echo "$pkg_line" | cut -d'=' -f3)
            
            if [ -n "$package" ] && [ -n "$current_version" ]; then
                # Get latest version from PyPI
                local latest=$(curl -s "https://pypi.org/pypi/${package}/json" 2>/dev/null | \
                    python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['info']['version'])
except:
    pass
" 2>/dev/null)
                
                if [ -n "$latest" ] && [ "$latest" != "$current_version" ]; then
                    if [ "$first" = false ]; then
                        outdated_json+=","
                    fi
                    outdated_json+="{\"name\":\"$package\",\"version\":\"$current_version\",\"latest_version\":\"$latest\"}"
                    first=false
                fi
            fi
        fi
    done <<< "$packages"
    
    outdated_json+="]"
    
    echo "$outdated_json"
}

update_dockerfile_packages() {
    local outdated_json=$1
    local updates_made=false
    
    if [ "$outdated_json" != "[]" ] && [ -n "$outdated_json" ]; then
        # Parse JSON and update Dockerfile
        echo "$outdated_json" | python3 -c "
import sys
import json
import re

try:
    packages = json.load(sys.stdin)
    
    with open('$DOCKERFILE', 'r') as f:
        content = f.read()
    
    updated = False
    for pkg in packages:
        name = pkg['name']
        current = pkg['version']
        latest = pkg['latest_version']
        
        # Update version in Dockerfile (handles both flask==X and flask == X formats)
        pattern = f'{name}\\\\s*==\\\\s*[0-9.]+'
        replacement = f'{name}=={latest}'
        new_content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
        
        if new_content != content:
            print(f'  {name}: {current} → {latest}')
            content = new_content
            updated = True
    
    if updated:
        with open('$DOCKERFILE', 'w') as f:
            f.write(content)
        sys.exit(0)
    else:
        sys.exit(1)
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
"
        if [ $? -eq 0 ]; then
            updates_made=true
        fi
    fi
    
    echo "$updates_made"
}

update_base_image() {
    local new_image=$1
    
    # Update FROM line in Dockerfile
    sed -i "1s|FROM.*|FROM $new_image|" "$DOCKERFILE"
    
    if [ $? -eq 0 ]; then
        return 0
    else
        return 1
    fi
}

update_trivy_version() {
    local new_version=$1
    
    # Update Trivy version in Dockerfile
    sed -i "s|trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin v[0-9.]*|trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin $new_version|g" "$DOCKERFILE"
    
    if [ $? -eq 0 ]; then
        return 0
    else
        return 1
    fi
}

update_app_version() {
    local new_version=$1
    
    # Update line 8 in app.py
    sed -i "8s/.*/DOCKUP_VERSION = \"$new_version\"/" "$APP_PY"
    
    if [ $? -eq 0 ]; then
        return 0
    else
        return 1
    fi
}

build_and_restart() {
    print_info "Stopping and removing existing container..."
    eval "$DOCKER_STOP_CMD" 2>/dev/null || true
    
    print_info "Building new DockUp image..."
    cd "$SCRIPT_DIR"
    eval "$DOCKER_BUILD_CMD"
    
    if [ $? -ne 0 ]; then
        print_error "Docker build failed!"
        return 1
    fi
    
    print_success "Build successful!"
    
    print_info "Starting new DockUp container..."
    eval "$DOCKER_RUN_CMD"
    
    if [ $? -ne 0 ]; then
        print_error "Failed to start container!"
        return 1
    fi
    
    print_success "Container started successfully!"
    
    # Wait a moment and check if container is running
    sleep 2
    if docker ps | grep -q dockup; then
        print_success "DockUp is running!"
        return 0
    else
        print_error "Container is not running!"
        return 1
    fi
}

create_summary() {
    local old_version=$1
    local new_version=$2
    local backup_path=$3
    local updates=$4
    local build_status=$5
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    cat > "$SUMMARY_FILE" << EOF
DockUp Update Summary
=====================
Date: $timestamp
Backup: $backup_path

Version Change: $old_version → $new_version

Dependencies Updated:
$updates

Build Status: $build_status
Container Status: $(docker ps | grep -q dockup && echo "RUNNING" || echo "STOPPED")

Ready to push to Docker Hub:
  docker push cbothma/dockup:latest
  docker tag cbothma/dockup:latest cbothma/dockup:$new_version
  docker push cbothma/dockup:$new_version
EOF
    
    print_success "Summary saved to UPDATE-SUMMARY.txt"
}

################################################################################
# Main Script
################################################################################

main() {
    print_header
    
    # Check if we're in the right directory
    if [ ! -f "$APP_PY" ] || [ ! -f "$DOCKERFILE" ]; then
        print_error "Required files not found! Make sure you're running this from the DockUp directory."
        exit 1
    fi
    
    # Get current version
    CURRENT_VERSION=$(get_current_version)
    print_info "Current version: $CURRENT_VERSION"
    echo ""
    
    # Step 1: Create backup
    BACKUP_PATH=$(create_backup "$CURRENT_VERSION")
    echo ""
    
    # Step 2: Check for updates
    print_info "Checking for dependency updates..."
    echo ""
    
    BASE_IMAGE_INFO=$(check_base_image_updates)
    TRIVY_INFO=$(check_trivy_updates)
    PIP_UPDATES=$(check_pip_updates)
    
    # Parse results
    UPDATES_FOUND=false
    UPDATE_DETAILS=""
    
    # Check base image
    BASE_CURRENT=$(echo "$BASE_IMAGE_INFO" | cut -d'|' -f1)
    BASE_LATEST=$(echo "$BASE_IMAGE_INFO" | cut -d'|' -f2)
    BASE_HAS_UPDATE=$(echo "$BASE_IMAGE_INFO" | cut -d'|' -f3)
    
    if [ "$BASE_HAS_UPDATE" = "true" ]; then
        UPDATES_FOUND=true
        UPDATE_DETAILS+="Base Image:\n"
        UPDATE_DETAILS+="  - Python: $BASE_CURRENT → $BASE_LATEST\n"
        UPDATE_DETAILS+="\n"
    fi
    
    # Check Trivy
    TRIVY_CURRENT=$(echo "$TRIVY_INFO" | cut -d'|' -f1)
    TRIVY_LATEST=$(echo "$TRIVY_INFO" | cut -d'|' -f2)
    TRIVY_HAS_UPDATE=$(echo "$TRIVY_INFO" | cut -d'|' -f3)
    
    if [ "$TRIVY_HAS_UPDATE" = "true" ]; then
        UPDATES_FOUND=true
        UPDATE_DETAILS+="Trivy Scanner:\n"
        UPDATE_DETAILS+="  - Trivy: $TRIVY_CURRENT → $TRIVY_LATEST\n"
        UPDATE_DETAILS+="\n"
    fi
    
    # Check pip updates
    if [ "$PIP_UPDATES" != "[]" ] && [ -n "$PIP_UPDATES" ]; then
        UPDATES_FOUND=true
        UPDATE_DETAILS+="Python Packages:\n"
        UPDATE_DETAILS+=$(echo "$PIP_UPDATES" | python3 -c "
import sys
import json
try:
    packages = json.load(sys.stdin)
    for pkg in packages:
        print(f\"  - {pkg['name']}: {pkg['version']} → {pkg['latest_version']}\")
except:
    pass
")
        UPDATE_DETAILS+="\n"
    fi
    
    if [ "$UPDATES_FOUND" = false ]; then
        print_success "All dependencies are up to date!"
        print_info "No updates needed."
        exit 0
    fi
    
    # Step 3: Show updates found
    echo -e "${GREEN}Updates found:${NC}"
    echo -e "$UPDATE_DETAILS"
    echo ""
    
    # Step 4: Update files
    print_info "Updating dependency files..."
    
    if [ "$BASE_HAS_UPDATE" = "true" ]; then
        if update_base_image "$BASE_LATEST"; then
            print_success "Base image updated in Dockerfile"
        fi
    fi
    
    if [ "$TRIVY_HAS_UPDATE" = "true" ]; then
        if update_trivy_version "$TRIVY_LATEST"; then
            print_success "Trivy version updated in Dockerfile"
        fi
    fi
    
    PACKAGES_UPDATED=$(update_dockerfile_packages "$PIP_UPDATES")
    
    if [ "$PACKAGES_UPDATED" = "true" ]; then
        print_success "Python packages updated in Dockerfile"
    fi
    
    # Step 5: Bump version
    NEW_VERSION=$(bump_version "$CURRENT_VERSION")
    if update_app_version "$NEW_VERSION"; then
        print_success "Version bumped in app.py: $CURRENT_VERSION → $NEW_VERSION"
    fi
    echo ""
    
    # Step 6: Ask to build
    print_warning "Ready to rebuild DockUp with version $NEW_VERSION"
    read -p "Build and restart DockUp? (y/n): " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Build cancelled. Files have been updated but container not rebuilt."
        print_info "To restore backup: cp -r $BACKUP_PATH/* $SCRIPT_DIR/"
        exit 0
    fi
    
    echo ""
    
    # Step 7: Build and restart
    if build_and_restart; then
        BUILD_STATUS="SUCCESS"
        print_success "DockUp updated successfully!"
    else
        BUILD_STATUS="FAILED"
        print_error "Build or restart failed!"
        print_info "To restore backup: cp -r $BACKUP_PATH/* $SCRIPT_DIR/"
        exit 1
    fi
    
    echo ""
    
    # Step 8: Create summary
    create_summary "$CURRENT_VERSION" "$NEW_VERSION" "$BACKUP_PATH" "$UPDATE_DETAILS" "$BUILD_STATUS"
    
    echo ""
    print_success "Update complete! Check UPDATE-SUMMARY.txt for details."
    print_info "Don't forget to push to Docker Hub when ready!"
}

# Run main function
main "$@"
