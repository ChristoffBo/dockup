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
import io
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
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
    # Check notification preferences
    if not config.get('notify_on_check') and 'update available' in message.lower():
        print(f"Notification skipped (notify_on_check=False): {title}")
        return
    if not config.get('notify_on_update') and 'updated successfully' in message.lower():
        print(f"Notification skipped (notify_on_update=False): {title}")
        return
    if not config.get('notify_on_error') and notify_type == 'error':
        print(f"Notification skipped (notify_on_error=False): {title}")
        return
    
    if len(apobj) > 0:
        print(f"Sending notification: {title} - {message}")
        try:
            result = apobj.notify(title=title, body=message, notify_type=notify_type)
            if result:
                print(f"✓ Notification sent successfully")
            else:
                print(f"✗ Notification failed to send")
        except Exception as e:
            print(f"✗ Notification error: {e}")
    else:
        print(f"No notification services configured. Title: {title}")


def broadcast_ws(data):
    """Broadcast message to all WebSocket clients"""
    for client in websocket_clients:
        try:
            client.send(json.dumps(data))
        except:
            websocket_clients.remove(client)


def import_orphan_containers_as_stacks():
    """Automatically detect and import containers without com.docker.compose.project label"""
    try:
        all_containers = docker_client.containers.list(all=True)

        for container in all_containers:
            labels = container.labels

            # Skip if already part of a compose stack
            if labels.get('com.docker.compose.project'):
                continue

            # Skip if already imported by Dockup
            if labels.get('dockup.imported') == 'true':
                continue

            # Permanently ignore Dockup and Dockge (case-insensitive)
            name_lower = container.name.lower()
            if 'dockup' in name_lower or 'dockge' in name_lower:
                continue

            if container.image and container.image.tags:
                image_tags_lower = ' '.join(tag.lower() for tag in container.image.tags)
                if 'dockup' in image_tags_lower or 'dockge' in image_tags_lower:
                    continue

            if labels.get('dockup.self') == 'true':
                continue

            stack_name = f"imported-{container.name}"
            stack_path = os.path.join(STACKS_DIR, stack_name)

            if os.path.exists(stack_path):
                continue

            print(f"[Dockup] Auto-importing orphan container: {container.name} → stack '{stack_name}'")

            inspect = container.attrs
            cfg = inspect['Config']
            host_cfg = inspect['HostConfig']

            service = {
                'image': container.image.tags[0] if container.image.tags else cfg['Image'],
                'container_name': container.name,
                'restart': 'unless-stopped',
            }

            # Ports
            if host_cfg['PortBindings']:
                service['ports'] = []
                for container_port, bindings in host_cfg['PortBindings'].items():
                    for binding in bindings:
                        host_port = binding['HostPort']
                        host_ip = binding.get('HostIp', '')
                        if host_ip == '0.0.0.0':
                            host_ip = ''
                        proto = container_port.split('/')[-1]
                        port_str = f"{host_ip + ':' if host_ip else ''}{host_port}:{container_port.split('/')[0]}"
                        if proto != 'tcp':
                            port_str += '/' + proto
                        service['ports'].append(port_str)

            # Volumes
            volumes = []
            for mount in inspect.get('Mounts', []):
                if mount['Type'] == 'bind':
                    vol = f"{mount['Source']}:{mount['Destination']}"
                    if not mount.get('RW', True):
                        vol += ':ro'
                    volumes.append(vol)
            if host_cfg.get('Binds'):
                volumes.extend(host_cfg['Binds'])
            if volumes:
                service['volumes'] = list(set(volumes))

            # Environment variables (skip PATH)
            env = [e for e in cfg.get('Env', []) if not e.startswith('PATH=')]
            if env:
                service['environment'] = env

            # Network mode
            if host_cfg['NetworkMode'] not in ('bridge', 'default'):
                service['network_mode'] = host_cfg['NetworkMode']

            # Create compose file
            compose_data = {'services': {container.name: service}}

            os.makedirs(stack_path, exist_ok=True)
            compose_file = os.path.join(stack_path, 'compose.yaml')
            with open(compose_file, 'w') as f:
                yaml.safe_dump(compose_data, f, sort_keys=False)

            # Mark container as imported
            try:
                docker_client.api.add_label(container.id, 'com.docker.compose.project', stack_name)
                docker_client.api.add_label(container.id, 'dockup.imported', 'true')
            except:
                pass

            send_notification(
                "Dockup – Orphan Imported",
                f"Container `{container.name}` automatically imported as stack `{stack_name}`",
                'info'
            )
            broadcast_ws({'type': 'stacks_changed'})

    except Exception as e:
        print(f"[Dockup] Orphan import error: {e}")


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



def refresh_container_metadata(stack_name):
    """Force refresh of container and image metadata after update"""
    try:
        print(f"  → Refreshing metadata for {stack_name}")
        
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        for container in containers:
            # Force refresh container inspect
            container.reload()
            
            # Force refresh image inspect
            image_name = container.image.tags[0] if container.image.tags else container.image.id
            try:
                docker_client.images.get(image_name)
            except:
                pass
        
        print(f"  ✓ Metadata refreshed for {stack_name}")
        return True
    
    except Exception as e:
        print(f"  ✗ Metadata refresh failed: {e}")
        return False


def check_stack_updates(stack_name):
    """Check if updates are available - GROK'S METHOD: Compare RepoDigest to Descriptor digest"""
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
            return True, False
        
        update_available = False
        
        for container in containers:
            try:
                image_name = container.image.tags[0] if container.image.tags else None
                if not image_name:
                    continue
                
                # Get local image info
                local_image = container.image
                local_id = local_image.id
                local_digests = local_image.attrs.get('RepoDigests', [])
                
                print(f"Checking updates for {container.name} ({image_name})")
                
                # Skip if no RepoDigests (locally built image)
                if not local_digests:
                    print(f"  ℹ No RepoDigests (locally built?), skipping")
                    continue
                
                # Extract local digest: "nginx@sha256:abc123..." -> "sha256:abc123..."
                local_digest = local_digests[0].split('@')[1]
                print(f"  Local digest: {local_digest[:20]}...")
                
                # Get registry data
                try:
                    registry_data = docker_client.images.get_registry_data(image_name)
                    
                    # Try Grok's method: get Descriptor digest
                    remote_digest = None
                    if hasattr(registry_data, 'attrs') and 'Descriptor' in registry_data.attrs:
                        remote_digest = registry_data.attrs['Descriptor']['digest']
                        print(f"  Remote digest (Descriptor): {remote_digest[:20]}...")
                        
                        # Compare digests directly
                        if local_digest != remote_digest:
                            update_available = True
                            print(f"  ✓ UPDATE AVAILABLE (digest mismatch)")
                            break
                        else:
                            print(f"  ✓ UP TO DATE (digests match)")
                    else:
                        # Fallback: use ID comparison with safety check
                        remote_id = registry_data.id
                        print(f"  Remote ID (fallback): {remote_id[:20]}...")
                        
                        if remote_id != local_id:
                            # Double-check against repo digests
                            if not any(remote_id in digest for digest in local_digests):
                                update_available = True
                                print(f"  ✓ UPDATE AVAILABLE (ID mismatch)")
                                break
                            else:
                                print(f"  ✓ UP TO DATE (ID in digests)")
                        else:
                            print(f"  ✓ UP TO DATE (ID match)")
                
                except docker.errors.NotFound:
                    print(f"  ℹ Image not found in registry, skipping")
                    continue
                    
                except Exception as e:
                    print(f"  ⚠ Registry check failed, trying manifest: {e}")
                    # Fallback to manifest inspect
                    try:
                        result = subprocess.run(
                            ['docker', 'manifest', 'inspect', image_name],
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        
                        if result.returncode == 0:
                            manifest = json.loads(result.stdout)
                            
                            # Get digest from manifest
                            remote_digest = None
                            if 'config' in manifest:
                                remote_digest = manifest['config'].get('digest', '')
                            elif 'manifests' in manifest:
                                # Multi-arch: use first manifest
                                remote_digest = manifest['manifests'][0].get('digest', '')
                            
                            if remote_digest:
                                print(f"  Remote digest (manifest): {remote_digest[:20]}...")
                                if local_digest != remote_digest:
                                    update_available = True
                                    print(f"  ✓ UPDATE AVAILABLE (manifest check)")
                                    break
                                else:
                                    print(f"  ✓ UP TO DATE (manifest check)")
                    except Exception as e2:
                        print(f"  ✗ Manifest check failed: {e2}")
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

def verify_update_success(stack_name):
    """Verify that update was successful by checking if local digest now matches remote"""
    try:
        print(f"  → Verifying update for {stack_name}")
        
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        if not containers:
            print(f"  ⚠ No containers found for verification")
            return False
        
        all_verified = True
        
        for container in containers:
            try:
                image_name = container.image.tags[0] if container.image.tags else None
                if not image_name:
                    continue
                
                # Get local image info (after update)
                local_image = container.image
                local_digests = local_image.attrs.get('RepoDigests', [])
                
                if not local_digests:
                    print(f"  ℹ {container.name}: No RepoDigests, skipping verification")
                    continue
                
                local_digest = local_digests[0].split('@')[1]
                
                # Get registry data
                try:
                    registry_data = docker_client.images.get_registry_data(image_name)
                    remote_digest = None
                    
                    if hasattr(registry_data, 'attrs') and 'Descriptor' in registry_data.attrs:
                        remote_digest = registry_data.attrs['Descriptor']['digest']
                    
                    if remote_digest:
                        if local_digest == remote_digest:
                            print(f"  ✓ {container.name}: Verified (digests match)")
                        else:
                            print(f"  ✗ {container.name}: Verification failed (digest mismatch)")
                            all_verified = False
                    else:
                        print(f"  ⚠ {container.name}: Cannot verify (no remote digest)")
                        
                except Exception as e:
                    print(f"  ⚠ {container.name}: Verification inconclusive ({e})")
                    
            except Exception as e:
                print(f"  ⚠ Error verifying {container.name}: {e}")
        
        return all_verified
        
    except Exception as e:
        print(f"  ✗ Verification error: {e}")
        return False


def auto_update_stack(stack_name):
    """Check and optionally update a stack based on its schedule - with safety checks"""
    schedule_info = schedules.get(stack_name, {})
    mode = schedule_info.get('mode', 'off')
    
    if mode == 'off':
        return
    
    print(f"=" * 50)
    print(f"[{datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S %Z')}] Scheduled check for: {stack_name}")
    print(f"Mode: {mode}, Cron: {schedule_info.get('cron')}")
    print(f"=" * 50)
    
    success, update_available = check_stack_updates(stack_name)
    
    if not success:
        print(f"✗ Failed to check updates for {stack_name}")
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
        print(f"✓ Update available for {stack_name}")
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
            print(f"  → Pulling new images...")
            success_pull, output_pull = stack_operation(stack_name, 'pull')
            if not success_pull:
                send_notification(
                    f"Dockup - Update Failed",
                    f"Failed to pull images for '{stack_name}': {output_pull}",
                    'error'
                )
                return
            
            print(f"  ✓ Images pulled successfully")
            
            # Restart with new images
            print(f"  → Restarting containers...")
            success_up, output_up = stack_operation(stack_name, 'up')
            
            if not success_up:
                print(f"  ✗ First start failed, attempting retry...")
                # FALLBACK RESTART: Try one more time
                time.sleep(3)
                success_up, output_up = stack_operation(stack_name, 'up')
            
            if success_up:
                # Wait for containers to stabilize
                time.sleep(5)
                
                # FORCE REFRESH metadata
                refresh_container_metadata(stack_name)
                
                # POST-UPDATE VERIFICATION
                if verify_update_success(stack_name):
                    print(f"  ✓ Update verified: local digest matches remote")
                    
                    # Clear stale update flags
                    update_status[stack_name] = {
                        'last_check': datetime.now().isoformat(),
                        'update_available': False
                    }
                    
                    send_notification(
                        f"Dockup - Updated",
                        f"Stack '{stack_name}' updated successfully and verified",
                        'success'
                    )
                    broadcast_ws({
                        'type': 'stack_updated',
                        'stack': stack_name
                    })
                else:
                    print(f"  ⚠ Update completed but verification inconclusive")
                    send_notification(
                        f"Dockup - Updated",
                        f"Stack '{stack_name}' updated (verification partial)",
                        'success'
                    )
            else:
                print(f"  ✗ Both start attempts failed")
                send_notification(
                    f"Dockup - Update Failed",
                    f"Failed to restart stack '{stack_name}' after update",
                    'error'
                )
        
        elif mode == 'check':
            # Check only mode
            print(f"Check only mode - notified user about update for {stack_name}")
            broadcast_ws({
                'type': 'update_available',
                'stack': stack_name
            })
    else:
        print(f"✓ No updates available for {stack_name}")
        broadcast_ws({
            'type': 'no_updates',
            'stack': stack_name
        })


def setup_scheduler():
    """Setup APScheduler for update checks and auto-prune"""
    # Get user timezone
    user_tz = pytz.timezone(config.get('timezone', 'UTC'))
    
    scheduler = BackgroundScheduler(timezone=user_tz)
    scheduler.start()
    
    def update_schedules():
        """Update scheduled jobs based on current configuration"""
        # Only remove user-configurable jobs — never touch system ones
        for job in list(scheduler.get_jobs()):
            if job.id.startswith('update_') or job.id in ('auto_prune', 'import_orphans'):
                try:
                    scheduler.remove_job(job.id)
                except:
                    pass
        
        # Stack update schedules
        for stack_name, schedule_info in schedules.items():
            if schedule_info.get('mode', 'off') != 'off':
                cron_expr = schedule_info.get('cron', config.get('default_cron', '0 2 * * *'))
                try:
                    trigger = CronTrigger.from_crontab(cron_expr, timezone=user_tz)
                    scheduler.add_job(
                        auto_update_stack,
                        trigger,
                        args=[stack_name],
                        id=f"update_{stack_name}",
                        replace_existing=True
                    )
                    print(f"Scheduled update check for {stack_name}: {cron_expr} ({config.get('timezone', 'UTC')})")
                except Exception as e:
                    print(f"Error scheduling {stack_name}: {e}")
        
        # Auto-prune schedule
        if config.get('auto_prune', False):
            prune_cron = config.get('auto_prune_cron', '0 3 * * 0')
            try:
                trigger = CronTrigger.from_crontab(prune_cron, timezone=user_tz)
                scheduler.add_job(
                    auto_prune_images,
                    trigger,
                    id='auto_prune',
                    replace_existing=True
                )
                print(f"Scheduled auto-prune: {prune_cron} ({config.get('timezone', 'UTC')})")
            except Exception as e:
                print(f"Error scheduling auto-prune: {e}")

        # Schedule orphan container importer (every 15 minutes)
        scheduler.add_job(
            import_orphan_containers_as_stacks,
            'interval',
            minutes=15,
            id='import_orphans',
            replace_existing=True
        )
    
    # Initial setup
    update_schedules()
    
    # Re-setup schedules every hour — SAFE version that preserves itself
    scheduler.add_job(
        update_schedules,
        'interval',
        hours=1,
        id='refresh_schedules',
        replace_existing=True,
        coalesce=True
    )
    
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
    """Save stack compose file content with validation"""
    data = request.json
    content = data.get('content', '')
    
    try:
        # Validate YAML syntax
        compose_data = yaml.safe_load(content)
        
        # Validate compose structure
        warnings = []
        suggestions = []
        
        if not compose_data:
            return jsonify({'error': 'Empty compose file'}), 400
        
        if 'services' not in compose_data:
            return jsonify({'error': 'No services defined'}), 400
        
        # Check for common issues
        if 'version' in compose_data:
            warnings.append("'version' is deprecated in modern Compose (can be removed)")
        
        services = compose_data.get('services', {})
        for service_name, service in services.items():
            # Check for restart policy
            if 'restart' not in service:
                suggestions.append(f"'{service_name}': Consider adding 'restart: unless-stopped'")
            
            # Check for container_name
            if 'container_name' not in service:
                suggestions.append(f"'{service_name}': Consider adding 'container_name for easier management")
        
        # Save the file
        save_stack_compose(stack_name, content)
        
        return jsonify({
            'success': True,
            'warnings': warnings,
            'suggestions': suggestions
        })
    except yaml.YAMLError as e:
        return jsonify({'error': f'Invalid YAML: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stack/<stack_name>/operation', methods=['POST'])
def api_stack_operation(stack_name):
    """Perform operation on stack"""
    data = request.json
    operation = data.get('operation')
    
    success, output = stack_operation(stack_name, operation)
    
    if success:
        # Clear update_available flag after up/pull operations
        if operation in ['up', 'pull']:
            if stack_name in update_status:
                update_status[stack_name]['update_available'] = False
                update_status[stack_name]['last_check'] = datetime.now(pytz.UTC).isoformat()
        
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


@app.route('/api/backup/create', methods=['POST'])
def api_backup_create():
    """Create a backup of all stacks"""
    try:
        import zipfile
        import io
        from flask import send_file
        
        # Create zip in memory
        backup_buffer = io.BytesIO()
        
        with zipfile.ZipFile(backup_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Add all compose files
            for root, dirs, files in os.walk(STACKS_DIR):
                for file in files:
                    if file in ['compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml', '.env']:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, STACKS_DIR)
                        zip_file.write(file_path, arcname)
            
            # Add schedules and config
            zip_file.writestr('_dockup_schedules.json', json.dumps(schedules, indent=2))
            zip_file.writestr('_dockup_config.json', json.dumps({
                'default_cron': config.get('default_cron'),
                'timezone': config.get('timezone'),
                'auto_prune': config.get('auto_prune'),
                'auto_prune_cron': config.get('auto_prune_cron')
            }, indent=2))
        
        backup_buffer.seek(0)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        return send_file(
            backup_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'dockup_backup_{timestamp}.zip'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/backup/restore', methods=['POST'])
def api_backup_restore():
    """Restore stacks from backup"""
    try:
        import zipfile
        
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        # Read zip file
        zip_data = io.BytesIO(file.read())
        
        restored_stacks = []
        
        with zipfile.ZipFile(zip_data, 'r') as zip_file:
            # Extract all files
            for file_info in zip_file.filelist:
                if file_info.filename.startswith('_dockup_'):
                    # Handle config files
                    if file_info.filename == '_dockup_schedules.json':
                        data = json.loads(zip_file.read(file_info))
                        schedules.update(data)
                        save_schedules()
                    elif file_info.filename == '_dockup_config.json':
                        data = json.loads(zip_file.read(file_info))
                        for key, value in data.items():
                            if key in config:
                                config[key] = value
                        save_config()
                else:
                    # Extract compose files
                    target_path = os.path.join(STACKS_DIR, file_info.filename)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    
                    with open(target_path, 'wb') as f:
                        f.write(zip_file.read(file_info))
                    
                    # Track restored stack
                    stack_name = file_info.filename.split('/')[0]
                    if stack_name not in restored_stacks:
                        restored_stacks.append(stack_name)
        
        return jsonify({
            'success': True,
            'restored_stacks': restored_stacks
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stack/<stack_name>/changelog')
def api_stack_changelog(stack_name):
    """Get changelog/update history for a stack"""
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        changelog = []
        
        for container in containers:
            image_name = container.image.tags[0] if container.image.tags else None
            if not image_name:
                continue
            
            # Try to get image history
            try:
                image = docker_client.images.get(image_name)
                history = image.history()
                
                # Get last few changes
                for entry in history[:5]:
                    if entry.get('CreatedBy'):
                        changelog.append({
                            'image': image_name,
                            'created': entry.get('Created'),
                            'created_by': entry.get('CreatedBy', '')[:100],
                            'size': entry.get('Size', 0)
                        })
            except:
                pass
        
        return jsonify({'changelog': changelog})
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




# ============================================================================
# SPLIT EDITOR ENDPOINTS - ZimaOS Style
# ============================================================================

@app.route('/api/stack/<stack_name>/service/<service_name>/sections', methods=['GET'])
def get_service_sections(stack_name, service_name):
    """Get service configuration split into sections for ZimaOS-style editor"""
    try:
        stack_path = os.path.join(STACKS_DIR, stack_name)
        compose_file = None
        
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            potential_file = os.path.join(stack_path, filename)
            if os.path.exists(potential_file):
                compose_file = potential_file
                break
        
        if not compose_file:
            return jsonify({'error': 'Compose file not found'}), 404
        
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        services = compose_data.get('services', {})
        if service_name not in services:
            return jsonify({'error': 'Service not found'}), 404
        
        service = services[service_name]
        
        # Split into sections
        sections = {
            'name_image': {
                'name': service_name,
                'image': service.get('image', ''),
                'container_name': service.get('container_name', '')
            },
            'environment': service.get('environment', []),
            'ports': service.get('ports', []),
            'volumes': service.get('volumes', []),
            'devices': service.get('devices', []),
            'labels': service.get('labels', {}),
            'command': service.get('command', ''),
            'entrypoint': service.get('entrypoint', ''),
            'network_mode': service.get('network_mode', ''),
            'restart': service.get('restart', 'unless-stopped'),
            'extra': {}
        }
        
        # Gather extra keys
        known_keys = ['image', 'container_name', 'environment', 'ports', 'volumes', 
                      'devices', 'labels', 'command', 'entrypoint', 'network_mode', 'restart']
        
        for key, value in service.items():
            if key not in known_keys:
                sections['extra'][key] = value
        
        # Also provide raw YAML
        sections['raw_yaml'] = yaml.safe_dump({'services': {service_name: service}}, sort_keys=False)
        
        return jsonify(sections)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stack/<stack_name>/service/<service_name>/sections', methods=['POST'])
def save_service_sections(stack_name, service_name):
    """Save service configuration from sections"""
    try:
        stack_path = os.path.join(STACKS_DIR, stack_name)
        compose_file = None
        
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            potential_file = os.path.join(stack_path, filename)
            if os.path.exists(potential_file):
                compose_file = potential_file
                break
        
        if not compose_file:
            return jsonify({'error': 'Compose file not found'}), 404
        
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        services = compose_data.get('services', {})
        if service_name not in services:
            return jsonify({'error': 'Service not found'}), 404
        
        data = request.json
        
        # Rebuild service from sections
        new_service = {}
        
        # Name/Image section
        if 'name_image' in data:
            if data['name_image'].get('image'):
                new_service['image'] = data['name_image']['image']
            if data['name_image'].get('container_name'):
                new_service['container_name'] = data['name_image']['container_name']
        
        # Environment
        if 'environment' in data and data['environment']:
            new_service['environment'] = data['environment']
        
        # Ports
        if 'ports' in data and data['ports']:
            new_service['ports'] = data['ports']
        
        # Volumes
        if 'volumes' in data and data['volumes']:
            new_service['volumes'] = data['volumes']
        
        # Devices
        if 'devices' in data and data['devices']:
            new_service['devices'] = data['devices']
        
        # Labels
        if 'labels' in data and data['labels']:
            new_service['labels'] = data['labels']
        
        # Command
        if 'command' in data and data['command']:
            new_service['command'] = data['command']
        
        # Entrypoint
        if 'entrypoint' in data and data['entrypoint']:
            new_service['entrypoint'] = data['entrypoint']
        
        # Network mode
        if 'network_mode' in data and data['network_mode']:
            new_service['network_mode'] = data['network_mode']
        
        # Restart
        if 'restart' in data:
            new_service['restart'] = data['restart'] or 'unless-stopped'
        
        # Extra keys
        if 'extra' in data and data['extra']:
            new_service.update(data['extra'])
        
        # Validate YAML
        try:
            test_yaml = yaml.safe_dump({'services': {service_name: new_service}})
        except Exception as e:
            return jsonify({'error': f'Invalid YAML: {str(e)}'}), 400
        
        # Update compose data
        compose_data['services'][service_name] = new_service
        
        # Write to file
        with open(compose_file, 'w') as f:
            yaml.safe_dump(compose_data, f, sort_keys=False, default_flow_style=False)
        
        return jsonify({'success': True})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
        
        # Run orphan import on startup
        print("Scanning for orphan containers to import...")
        import_orphan_containers_as_stacks()
        
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
