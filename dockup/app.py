#!/usr/bin/env python3
"""
Dockup - Docker Compose Stack Manager with Auto-Update
Combines Dockge functionality with Tugtainer/Watchtower update capabilities
"""

import os
import json
import yaml
import docker
import threading
import time
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_sock import Sock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
import apprise
import pytz

# Initialize Flask app
app = Flask(__name__, static_folder='static', static_url_path='')
sock = Sock(app)

# Configuration
STACKS_DIR = os.getenv('STACKS_DIR', '/stacks')
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
SCHEDULES_FILE = os.path.join(DATA_DIR, 'schedules.json')

# Ensure directories exist
Path(STACKS_DIR).mkdir(parents=True, exist_ok=True)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# Docker client
docker_client = docker.from_env()

# Global state
config = {}
schedules = {}
update_status = {}
websocket_clients = []
stack_stats_cache = {}  # Cache for stack stats

# Initialize Apprise
apobj = apprise.Apprise()


def load_config():
    """Load configuration from file"""
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    else:
        config = {
            'gotify_url': '',
            'gotify_token': '',
            'apprise_urls': [],
            'default_cron': '0 2 * * *',
            'notify_on_check': True,
            'notify_on_update': True,
            'notify_on_error': True,
            'timezone': 'UTC',
            'auto_prune': False,
            'auto_prune_cron': '0 3 * * 0'
        }
        save_config()
    
    # Setup notification services
    setup_notifications()


def save_config():
    """Save configuration to file"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def load_schedules():
    """Load update schedules from file"""
    global schedules
    if os.path.exists(SCHEDULES_FILE):
        with open(SCHEDULES_FILE, 'r') as f:
            schedules = json.load(f)
    else:
        schedules = {}
        save_schedules()


def save_schedules():
    """Save schedules to file"""
    with open(SCHEDULES_FILE, 'w') as f:
        json.dump(schedules, f, indent=2)


def setup_notifications():
    """Setup Apprise notification services"""
    global apobj
    apobj = apprise.Apprise()
    
    # Add Gotify
    if config.get('gotify_url') and config.get('gotify_token'):
        gotify_url = f"gotify://{config['gotify_url'].replace('http://', '').replace('https://', '')}/{config['gotify_token']}"
        apobj.add(gotify_url)
    
    # Add Apprise URLs
    for url in config.get('apprise_urls', []):
        if url.strip():
            apobj.add(url)


def send_notification(title, message, notify_type='info'):
    """Send notification via configured services"""
    if not config.get('notify_on_check') and 'update available' in message.lower():
        return
    if not config.get('notify_on_update') and 'updated successfully' in message.lower():
        return
    if not config.get('notify_on_error') and notify_type == 'error':
        return
    
    if len(apobj) > 0:
        apobj.notify(title=title, body=message, notify_type=notify_type)


def broadcast_ws(data):
    """Broadcast message to all WebSocket clients"""
    for client in websocket_clients:
        try:
            client.send(json.dumps(data))
        except:
            websocket_clients.remove(client)


def get_stack_stats(stack_name):
    """Get CPU and RAM usage for a stack"""
    try:
        containers = docker_client.containers.list(
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        total_cpu = 0.0
        total_mem = 0
        total_mem_limit = 0
        
        for container in containers:
            try:
                stats = container.stats(stream=False)
                
                # Calculate CPU percentage
                cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
                system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
                cpu_count = stats['cpu_stats'].get('online_cpus', 1)
                
                if system_delta > 0 and cpu_delta > 0:
                    cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0
                    total_cpu += cpu_percent
                
                # Calculate memory usage
                mem_usage = stats['memory_stats'].get('usage', 0)
                mem_limit = stats['memory_stats'].get('limit', 0)
                total_mem += mem_usage
                total_mem_limit += mem_limit
                
            except Exception as e:
                print(f"Error getting stats for {container.name}: {e}")
        
        return {
            'cpu_percent': round(total_cpu, 2),
            'mem_usage_mb': round(total_mem / (1024 * 1024), 2),
            'mem_limit_mb': round(total_mem_limit / (1024 * 1024), 2),
            'mem_percent': round((total_mem / total_mem_limit * 100) if total_mem_limit > 0 else 0, 2)
        }
    except Exception as e:
        print(f"Error getting stats for {stack_name}: {e}")
        return {
            'cpu_percent': 0,
            'mem_usage_mb': 0,
            'mem_limit_mb': 0,
            'mem_percent': 0
        }


def format_timestamp(timestamp_str):
    """Convert UTC timestamp to user's timezone"""
    if not timestamp_str:
        return None
    
    try:
        # Parse ISO format timestamp
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        
        # Convert to user's timezone
        user_tz = pytz.timezone(config.get('timezone', 'UTC'))
        dt_local = dt.astimezone(user_tz)
        
        return dt_local.isoformat()
    except:
        return timestamp_str


def get_stacks():
    """Get all Docker Compose stacks by scanning the stacks directory (like Dockge)"""
    stacks = []
    stacks_dict = {}  # Use dict to avoid duplicates
    
    if not os.path.exists(STACKS_DIR):
        return stacks
    
    # First, check direct subdirectories (most common)
    try:
        for item in os.listdir(STACKS_DIR):
            item_path = os.path.join(STACKS_DIR, item)
            if os.path.isdir(item_path):
                # Look for compose files
                compose_file = None
                for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
                    potential_file = os.path.join(item_path, filename)
                    if os.path.exists(potential_file):
                        compose_file = potential_file
                        break
                
                if compose_file:
                    stack_name = item
                    stacks_dict[stack_name] = {
                        'path': item_path,
                        'compose_file': compose_file
                    }
    except Exception as e:
        print(f"Error scanning stacks directory: {e}")
    
    # Also check if there are compose files directly in STACKS_DIR (flat structure)
    try:
        for filename in os.listdir(STACKS_DIR):
            if filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
                file_path = os.path.join(STACKS_DIR, filename)
                if os.path.isfile(file_path):
                    stack_name = os.path.basename(STACKS_DIR)
                    stacks_dict[stack_name] = {
                        'path': STACKS_DIR,
                        'compose_file': file_path
                    }
    except:
        pass
    
    # Build stack info for each found stack
    for stack_name, stack_info in stacks_dict.items():
        try:
            compose_file = stack_info['compose_file']
            stack_path = stack_info['path']
            
            with open(compose_file, 'r') as f:
                compose_data = yaml.safe_load(f)
            
            # Get containers for this stack (if any exist)
            containers = []
            try:
                stack_containers = docker_client.containers.list(
                    all=True,
                    filters={'label': f'com.docker.compose.project={stack_name}'}
                )
                for container in stack_containers:
                    # Calculate uptime
                    started_at = container.attrs.get('State', {}).get('StartedAt', '')
                    uptime_str = ''
                    if started_at and container.status == 'running':
                        try:
                            started = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                            uptime_delta = datetime.now(pytz.UTC) - started
                            
                            days = uptime_delta.days
                            hours = uptime_delta.seconds // 3600
                            minutes = (uptime_delta.seconds % 3600) // 60
                            
                            if days > 0:
                                uptime_str = f"{days}d {hours}h"
                            elif hours > 0:
                                uptime_str = f"{hours}h {minutes}m"
                            else:
                                uptime_str = f"{minutes}m"
                        except:
                            uptime_str = ''
                    
                    containers.append({
                        'id': container.id[:12],
                        'name': container.name,
                        'status': container.status,
                        'image': container.image.tags[0] if container.image.tags else container.image.id[:12],
                        'uptime': uptime_str
                    })
            except Exception as e:
                print(f"Error getting containers for {stack_name}: {e}")
            
            # Get schedule info
            schedule_info = schedules.get(stack_name, {
                'mode': 'off',
                'cron': config.get('default_cron', '0 2 * * *')
            })
            
            # Determine if stack is running (any container with status 'running')
            is_running = any(c['status'] == 'running' for c in containers)
            
            # Get services from compose file
            services = list(compose_data.get('services', {}).keys()) if compose_data else []
            
            # Get cached resource stats (updated by background thread)
            stats = stack_stats_cache.get(stack_name, {
                'cpu_percent': 0,
                'mem_usage_mb': 0,
                'mem_limit_mb': 0,
                'mem_percent': 0
            })
            
            stacks.append({
                'name': stack_name,
                'path': stack_path,
                'compose_file': compose_file,
                'services': services,
                'containers': containers,
                'running': is_running,
                'update_mode': schedule_info.get('mode', 'off'),
                'cron': schedule_info.get('cron', config.get('default_cron', '0 2 * * *')),
                'last_check': format_timestamp(update_status.get(stack_name, {}).get('last_check')),
                'update_available': update_status.get(stack_name, {}).get('update_available', False),
                'stats': stats,
                'inactive': len(containers) == 0  # True if never started
            })
        except Exception as e:
            print(f"Error loading stack {stack_name}: {e}")
    
    # Sort alphabetically by name (case-insensitive)
    stacks.sort(key=lambda x: x['name'].lower())
    
    return stacks


def get_stack_compose(stack_name):
    """Get compose file content for a stack"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    if not os.path.exists(compose_file):
        compose_file = os.path.join(stack_path, 'docker-compose.yml')
    
    if os.path.exists(compose_file):
        with open(compose_file, 'r') as f:
            return f.read()
    return None


def save_stack_compose(stack_name, content):
    """Save compose file content for a stack"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    Path(stack_path).mkdir(parents=True, exist_ok=True)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    
    with open(compose_file, 'w') as f:
        f.write(content)


def stack_operation(stack_name, operation):
    """Perform operation on stack (up/down/restart/pull) with real-time output"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    if not os.path.exists(compose_file):
        compose_file = os.path.join(stack_path, 'docker-compose.yml')
    
    try:
        if operation == 'up':
            cmd = ['docker', 'compose', '-f', compose_file, 'up', '-d']
        elif operation == 'down':
            cmd = ['docker', 'compose', '-f', compose_file, 'down']
        elif operation == 'restart':
            cmd = ['docker', 'compose', '-f', compose_file, 'restart']
        elif operation == 'pull':
            cmd = ['docker', 'compose', '-f', compose_file, 'pull']
        else:
            return False, f"Unknown operation: {operation}"
        
        # Run command and stream output
        process = subprocess.Popen(
            cmd,
            cwd=stack_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        output_lines = []
        for line in iter(process.stdout.readline, ''):
            if line:
                output_lines.append(line.rstrip())
                # Broadcast to WebSocket clients
                broadcast_ws({
                    'type': 'operation_output',
                    'stack': stack_name,
                    'operation': operation,
                    'line': line.rstrip()
                })
        
        process.wait()
        output = '\n'.join(output_lines)
        
        if process.returncode != 0:
            return False, output
        
        return True, output
    except Exception as e:
        return False, str(e)


def check_stack_updates(stack_name):
    """Check if updates are available for a stack - supports all major registries"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    
    # Find compose file
    compose_file = None
    for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
        potential_file = os.path.join(stack_path, filename)
        if os.path.exists(potential_file):
            compose_file = potential_file
            break
    
    if not compose_file:
        return False, None
    
    try:
        # Get containers for this stack
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        if not containers:
            # No containers means it's never been started, can't check updates
            return True, False
        
        update_available = False
        
        # Check each container's image against remote registry
        for container in containers:
            try:
                image_name = container.image.tags[0] if container.image.tags else None
                if not image_name:
                    continue
                
                # Get local image info
                local_image = container.image
                local_id = local_image.id
                local_digests = local_image.attrs.get('RepoDigests', [])
                
                # Try to get registry data (works for Docker Hub, GHCR, Quay, LSIO, etc.)
                try:
                    # This works for all OCI-compliant registries
                    registry_data = docker_client.images.get_registry_data(image_name)
                    remote_id = registry_data.id
                    
                    # Compare image IDs
                    if remote_id != local_id:
                        # Double-check against repo digests
                        if not any(remote_id in digest for digest in local_digests):
                            update_available = True
                            print(f"Update found for {image_name}: {local_id[:12]} -> {remote_id[:12]}")
                            break
                
                except docker.errors.NotFound:
                    # Image not found in registry (might be local-only)
                    print(f"Image {image_name} not found in registry, skipping")
                    continue
                    
                except Exception as e:
                    # Fallback: try docker manifest inspect (works for all registries)
                    print(f"Registry API failed for {image_name}, trying manifest: {e}")
                    try:
                        result = subprocess.run(
                            ['docker', 'manifest', 'inspect', image_name],
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        
                        if result.returncode == 0:
                            import json
                            manifest = json.loads(result.stdout)
                            
                            # Get digest from manifest
                            if 'config' in manifest:
                                remote_digest = manifest['config'].get('digest', '')
                            elif 'manifests' in manifest:
                                # Multi-arch image
                                remote_digest = manifest['manifests'][0].get('digest', '')
                            else:
                                remote_digest = ''
                            
                            # Compare with local
                            if remote_digest and remote_digest not in str(local_digests) and remote_digest not in local_id:
                                update_available = True
                                print(f"Update found for {image_name} via manifest")
                                break
                    except Exception as e2:
                        print(f"Manifest check also failed for {image_name}: {e2}")
                        continue
            
            except Exception as e:
                print(f"Error checking {container.name}: {e}")
                continue
        
        # Update status
        update_status[stack_name] = {
            'last_check': datetime.now().isoformat(),
            'update_available': update_available
        }
        
        return True, update_available
        
    except Exception as e:
        print(f"Error checking updates for {stack_name}: {e}")
        return False, None


def auto_update_stack(stack_name):
    """Check and optionally update a stack based on its schedule - with safety checks"""
    schedule_info = schedules.get(stack_name, {})
    mode = schedule_info.get('mode', 'off')
    
    if mode == 'off':
        return
    
    print(f"Checking updates for stack: {stack_name}")
    
    success, update_available = check_stack_updates(stack_name)
    
    if not success:
        send_notification(
            f"Dockup - Error",
            f"Failed to check updates for stack: {stack_name}",
            'error'
        )
        broadcast_ws({
            'type': 'update_check_error',
            'stack': stack_name
        })
        return
    
    if update_available:
        send_notification(
            f"Dockup - Update Available",
            f"Update available for stack: {stack_name}",
            'info'
        )
        
        if mode == 'auto':
            print(f"Auto-updating stack: {stack_name}")
            
            # Get current container states for rollback
            containers_before = []
            try:
                containers = docker_client.containers.list(
                    all=True,
                    filters={'label': f'com.docker.compose.project={stack_name}'}
                )
                for c in containers:
                    containers_before.append({
                        'name': c.name,
                        'image': c.image.id,
                        'status': c.status
                    })
            except:
                pass
            
            # Pull new images
            success_pull, output_pull = stack_operation(stack_name, 'pull')
            if not success_pull:
                send_notification(
                    f"Dockup - Update Failed",
                    f"Failed to pull images for '{stack_name}': {output_pull}",
                    'error'
                )
                return
            
            # Restart with new images
            success_up, output_up = stack_operation(stack_name, 'up')
            
            if success_up:
                # Wait a bit for containers to stabilize
                time.sleep(5)
                
                # Verify containers are healthy
                try:
                    containers_after = docker_client.containers.list(
                        filters={'label': f'com.docker.compose.project={stack_name}'}
                    )
                    
                    all_healthy = True
                    for container in containers_after:
                        if container.status != 'running':
                            all_healthy = False
                            print(f"Container {container.name} not running: {container.status}")
                            break
                        
                        # Check health if healthcheck is defined
                        if 'Health' in container.attrs.get('State', {}):
                            health = container.attrs['State']['Health']['Status']
                            if health not in ['healthy', 'none']:
                                all_healthy = False
                                print(f"Container {container.name} unhealthy: {health}")
                                break
                    
                    if all_healthy:
                        send_notification(
                            f"Dockup - Updated",
                            f"Stack '{stack_name}' updated successfully and containers are healthy",
                            'success'
                        )
                        broadcast_ws({
                            'type': 'stack_updated',
                            'stack': stack_name
                        })
                    else:
                        send_notification(
                            f"Dockup - Warning",
                            f"Stack '{stack_name}' updated but some containers may be unhealthy. Check logs.",
                            'warning'
                        )
                
                except Exception as e:
                    print(f"Health check error: {e}")
                    send_notification(
                        f"Dockup - Updated",
                        f"Stack '{stack_name}' updated (health check skipped)",
                        'success'
                    )
            else:
                send_notification(
                    f"Dockup - Update Failed",
                    f"Failed to restart stack '{stack_name}': {output_up}",
                    'error'
                )
        else:
            # Check only mode
            broadcast_ws({
                'type': 'update_available',
                'stack': stack_name
            })
    else:
        print(f"No updates available for stack: {stack_name}")
        broadcast_ws({
            'type': 'no_updates',
            'stack': stack_name
        })


def setup_scheduler():
    """Setup APScheduler for update checks and auto-prune"""
    scheduler = BackgroundScheduler()
    scheduler.start()
    
    def update_schedules():
        """Update scheduled jobs based on current configuration"""
        scheduler.remove_all_jobs()
        
        # Stack update schedules
        for stack_name, schedule_info in schedules.items():
            if schedule_info.get('mode', 'off') != 'off':
                cron_expr = schedule_info.get('cron', config.get('default_cron', '0 2 * * *'))
                try:
                    trigger = CronTrigger.from_crontab(cron_expr)
                    scheduler.add_job(
                        auto_update_stack,
                        trigger,
                        args=[stack_name],
                        id=f"update_{stack_name}",
                        replace_existing=True
                    )
                    print(f"Scheduled update check for {stack_name}: {cron_expr}")
                except Exception as e:
                    print(f"Error scheduling {stack_name}: {e}")
        
        # Auto-prune schedule
        if config.get('auto_prune', False):
            prune_cron = config.get('auto_prune_cron', '0 3 * * 0')
            try:
                trigger = CronTrigger.from_crontab(prune_cron)
                scheduler.add_job(
                    auto_prune_images,
                    trigger,
                    id='auto_prune',
                    replace_existing=True
                )
                print(f"Scheduled auto-prune: {prune_cron}")
            except Exception as e:
                print(f"Error scheduling auto-prune: {e}")
    
    # Initial setup
    update_schedules()
    
    # Re-setup schedules every hour in case of changes
    scheduler.add_job(update_schedules, 'interval', hours=1, id='refresh_schedules')
    
    return scheduler


# Flask routes
@app.route('/')
def index():
    """Serve the main page"""
    return send_from_directory('static', 'index.html')


@app.route('/api/stacks')
def api_stacks():
    """Get all stacks"""
    return jsonify(get_stacks())


@app.route('/api/stack/<stack_name>')
def api_stack(stack_name):
    """Get specific stack details"""
    stacks = get_stacks()
    stack = next((s for s in stacks if s['name'] == stack_name), None)
    if stack:
        return jsonify(stack)
    return jsonify({'error': 'Stack not found'}), 404


@app.route('/api/stack/<stack_name>/compose')
def api_stack_compose(stack_name):
    """Get stack compose file content"""
    content = get_stack_compose(stack_name)
    if content:
        return jsonify({'content': content})
    return jsonify({'error': 'Stack not found'}), 404


@app.route('/api/stack/<stack_name>/compose', methods=['POST'])
def api_stack_compose_save(stack_name):
    """Save stack compose file content"""
    data = request.json
    content = data.get('content', '')
    
    try:
        # Validate YAML
        yaml.safe_load(content)
        save_stack_compose(stack_name, content)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stack/<stack_name>/operation', methods=['POST'])
def api_stack_operation(stack_name):
    """Perform operation on stack"""
    data = request.json
    operation = data.get('operation')
    
    success, output = stack_operation(stack_name, operation)
    
    if success:
        return jsonify({'success': True, 'output': output})
    return jsonify({'error': output}), 400


@app.route('/api/stack/<stack_name>', methods=['DELETE'])
def api_stack_delete(stack_name):
    """Delete a stack"""
    try:
        data = request.json or {}
        delete_images = data.get('delete_images', False)
        
        # Get images before stopping if we need to delete them
        images_to_delete = []
        if delete_images:
            containers = docker_client.containers.list(
                all=True,
                filters={'label': f'com.docker.compose.project={stack_name}'}
            )
            for container in containers:
                images_to_delete.append(container.image.id)
        
        # Stop stack first
        stack_operation(stack_name, 'down')
        
        # Delete images if requested
        if delete_images:
            for image_id in set(images_to_delete):  # Remove duplicates
                try:
                    docker_client.images.remove(image_id, force=True)
                except Exception as e:
                    print(f"Warning: Could not delete image {image_id}: {e}")
        
        # Delete directory if not keeping compose
        if data.get('delete_compose', True):
            stack_path = os.path.join(STACKS_DIR, stack_name)
            import shutil
            shutil.rmtree(stack_path)
        
        # Remove schedule
        if stack_name in schedules:
            del schedules[stack_name]
            save_schedules()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stack/<stack_name>/delete-images', methods=['POST'])
def api_stack_delete_images(stack_name):
    """Delete images for a stack without removing the stack"""
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        deleted = []
        for container in containers:
            try:
                image_id = container.image.id
                image_tags = container.image.tags
                docker_client.images.remove(image_id, force=True)
                deleted.append(image_tags[0] if image_tags else image_id[:12])
            except Exception as e:
                print(f"Warning: Could not delete image: {e}")
        
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/apply-to-all', methods=['POST'])
def api_apply_to_all():
    """Apply schedule settings to all stacks"""
    try:
        data = request.json
        mode = data.get('mode', 'off')
        cron = data.get('cron', config.get('default_cron', '0 2 * * *'))
        
        # Validate cron expression
        try:
            croniter(cron)
        except:
            return jsonify({'error': 'Invalid cron expression'}), 400
        
        # Get all stacks
        stacks = get_stacks()
        
        # Apply to all
        for stack in stacks:
            schedules[stack['name']] = {
                'mode': mode,
                'cron': cron
            }
        
        save_schedules()
        
        # Trigger scheduler update
        setup_scheduler()
        
        return jsonify({'success': True, 'updated': len(stacks)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/test-notification', methods=['POST'])
def api_test_notification():
    """Send a test notification"""
    try:
        send_notification(
            'Dockup - Test Notification',
            'If you receive this, notifications are working correctly! 🎉',
            'success'
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/images')
def api_images():
    """Get all Docker images"""
    try:
        images = docker_client.images.list()
        image_list = []
        
        for img in images:
            # Get size in MB
            size_mb = round(img.attrs.get('Size', 0) / (1024 * 1024), 2)
            
            # Get tags
            tags = img.tags if img.tags else ['<none>:<none>']
            
            # Check if used by any container
            containers_using = docker_client.containers.list(
                all=True,
                filters={'ancestor': img.id}
            )
            in_use = len(containers_using) > 0
            
            for tag in tags:
                image_list.append({
                    'id': img.id[:12],
                    'tag': tag,
                    'size_mb': size_mb,
                    'created': img.attrs.get('Created', ''),
                    'in_use': in_use,
                    'containers': [c.name for c in containers_using]
                })
        
        # Sort by size (largest first)
        image_list.sort(key=lambda x: x['size_mb'], reverse=True)
        
        return jsonify(image_list)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/images/prune', methods=['POST'])
def api_images_prune():
    """Prune unused Docker images"""
    try:
        result = docker_client.images.prune(filters={'dangling': False})
        
        # Calculate space saved
        space_saved = result.get('SpaceReclaimed', 0)
        space_saved_mb = round(space_saved / (1024 * 1024), 2)
        
        deleted = len(result.get('ImagesDeleted', []))
        
        send_notification(
            'Dockup - Images Pruned',
            f'Pruned {deleted} unused images, freed {space_saved_mb} MB',
            'success'
        )
        
        return jsonify({
            'success': True,
            'deleted': deleted,
            'space_saved_mb': space_saved_mb
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/images/<image_id>', methods=['DELETE'])
def api_image_delete(image_id):
    """Delete a specific image"""
    try:
        docker_client.images.remove(image_id, force=True)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


def auto_prune_images():
    """Automatically prune unused images"""
    if not config.get('auto_prune', False):
        return
    
    print("Running auto-prune...")
    try:
        result = docker_client.images.prune(filters={'dangling': False})
        space_saved = result.get('SpaceReclaimed', 0)
        space_saved_mb = round(space_saved / (1024 * 1024), 2)
        deleted = len(result.get('ImagesDeleted', []))
        
        if deleted > 0:
            send_notification(
                'Dockup - Auto-Prune',
                f'Automatically pruned {deleted} unused images, freed {space_saved_mb} MB',
                'success'
            )
            print(f"Auto-prune: {deleted} images, {space_saved_mb} MB freed")
    except Exception as e:
        print(f"Auto-prune error: {e}")


def update_stats_background():
    """Background thread to update stack stats every 10 seconds"""
    while True:
        try:
            # Get list of running stacks
            stacks = get_stacks()
            
            for stack in stacks:
                if stack['running']:
                    # Update stats for running stacks only
                    stats = get_stack_stats(stack['name'])
                    stack_stats_cache[stack['name']] = stats
                else:
                    # Clear stats for stopped stacks
                    if stack['name'] in stack_stats_cache:
                        stack_stats_cache[stack['name']] = {
                            'cpu_percent': 0,
                            'mem_usage_mb': 0,
                            'mem_limit_mb': 0,
                            'mem_percent': 0
                        }
            
            # Wait 10 seconds before next update
            time.sleep(10)
        except Exception as e:
            print(f"Stats update error: {e}")
            time.sleep(10)


@app.route('/api/stack/<stack_name>/logs')
def api_stack_logs(stack_name):
    """Get logs for a stack"""
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        logs = {}
        for container in containers:
            try:
                logs[container.name] = container.logs(tail=100).decode('utf-8')
            except:
                logs[container.name] = "Unable to fetch logs"
        
        return jsonify({'logs': logs})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stack/<stack_name>/schedule', methods=['GET'])
def api_stack_schedule_get(stack_name):
    """Get stack schedule configuration"""
    schedule_info = schedules.get(stack_name, {
        'mode': 'off',
        'cron': config.get('default_cron', '0 2 * * *')
    })
    return jsonify(schedule_info)


@app.route('/api/stack/<stack_name>/schedule', methods=['POST'])
def api_stack_schedule_set(stack_name):
    """Set stack schedule configuration"""
    data = request.json
    mode = data.get('mode', 'off')
    cron = data.get('cron', config.get('default_cron', '0 2 * * *'))
    
    # Validate cron expression
    try:
        croniter(cron)
    except:
        return jsonify({'error': 'Invalid cron expression'}), 400
    
    schedules[stack_name] = {
        'mode': mode,
        'cron': cron
    }
    save_schedules()
    
    # Trigger scheduler update
    setup_scheduler()
    
    return jsonify({'success': True})


@app.route('/api/stack/<stack_name>/check-updates', methods=['POST'])
def api_stack_check_updates(stack_name):
    """Manually check for updates"""
    success, update_available = check_stack_updates(stack_name)
    
    if success:
        return jsonify({
            'success': True,
            'update_available': update_available,
            'last_check': update_status.get(stack_name, {}).get('last_check')
        })
    return jsonify({'error': 'Failed to check updates'}), 400


@app.route('/api/config', methods=['GET'])
def api_config_get():
    """Get configuration"""
    return jsonify(config)


@app.route('/api/config', methods=['POST'])
def api_config_set():
    """Set configuration"""
    global config
    data = request.json
    
    # Update config
    config.update(data)
    save_config()
    setup_notifications()
    
    return jsonify({'success': True})


@sock.route('/ws')
def websocket(ws):
    """WebSocket endpoint for real-time updates"""
    websocket_clients.append(ws)
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
    except:
        pass
    finally:
        if ws in websocket_clients:
            websocket_clients.remove(ws)


if __name__ == '__main__':
    try:
        # Test Docker connection
        print("Testing Docker connection...")
        docker_client.ping()
        print("✓ Docker connected successfully")
        
        # Load configuration
        print("Loading configuration...")
        load_config()
        load_schedules()
        print("✓ Configuration loaded")
        
        # Setup scheduler
        print("Setting up scheduler...")
        scheduler = setup_scheduler()
        print("✓ Scheduler started")
        
        # Start background stats updater
        print("Starting background stats updater...")
        stats_thread = threading.Thread(target=update_stats_background, daemon=True)
        stats_thread.start()
        print("✓ Stats updater started")
        
        # Start Flask app
        print("Starting Dockup on port 5000...")
        print("=" * 50)
        print("Access the UI at: http://localhost:5000")
        print("=" * 50)
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    except Exception as e:
        print(f"FATAL ERROR: Failed to start Dockup")
        print(f"Error: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure Docker socket is mounted: -v /var/run/docker.sock:/var/run/docker.sock")
        print("2. Check if all dependencies are installed: pip install -r requirements.txt")
        print("3. Ensure port 5000 is not already in use")
        import traceback
        traceback.print_exc()
        exit(1)
