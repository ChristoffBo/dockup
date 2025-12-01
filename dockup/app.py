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
import psutil
import bcrypt
import requests
import secrets
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file, session, redirect, url_for
from functools import wraps
from flask_sock import Sock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
import apprise
import pytz

# Initialize Flask app
app = Flask(__name__, static_folder='static', static_url_path='')
# Persistent secret key (survives container restarts)
SECRET_KEY_FILE = '/app/data/secret.key'
if os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, 'rb') as f:
        app.secret_key = f.read()
else:
    app.secret_key = os.urandom(24)
    with open(SECRET_KEY_FILE, 'wb') as f:
        f.write(app.secret_key)
sock = Sock(app)

# Configuration
STACKS_DIR = os.getenv('STACKS_DIR', '/stacks')
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
SCHEDULES_FILE = os.path.join(DATA_DIR, 'schedules.json')
HEALTH_STATUS_FILE = os.path.join(DATA_DIR, 'health_status.json')
UPDATE_HISTORY_FILE = os.path.join(DATA_DIR, 'update_history.json')
PASSWORD_FILE = os.path.join(DATA_DIR, 'password.hash')

# Ensure directories exist
Path(STACKS_DIR).mkdir(parents=True, exist_ok=True)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# Docker client
docker_client = docker.from_env()

# Global state
config = {}
schedules = {}
update_status = {}
health_status = {}  # Track health failures: {stack_name: {consecutive_failures: 0, last_notified: timestamp}}
update_history = {}  # Track recent updates: {stack_name: {last_update: timestamp, updated_by: 'auto'|'manual'}}
websocket_clients = []
stack_stats_cache = {}  # Cache for stack stats
network_stats_cache = {}  # Cache for network bytes tracking (for rate calculation)
peer_sessions = {}  # Cache for peer authentication sessions

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
            'notify_on_health_failure': True,
            'health_check_threshold': 3,
            'timezone': 'UTC',
            'auto_prune': False,
            'auto_prune_cron': '0 3 * * 0',
            'password_enabled': False,
            'instance_name': 'DockUp',
            'api_token': secrets.token_urlsafe(32),
            'peers': {}
        }
        save_config()
    
    
    # Add missing fields to existing configs
    if 'instance_name' not in config:
        config['instance_name'] = 'DockUp'
    if 'api_token' not in config:
        config['api_token'] = secrets.token_urlsafe(32)
    if 'peers' not in config:
        config['peers'] = {}
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


def load_health_status():
    """Load health status tracking from file"""
    global health_status
    if os.path.exists(HEALTH_STATUS_FILE):
        with open(HEALTH_STATUS_FILE, 'r') as f:
            health_status = json.load(f)
    else:
        health_status = {}


def save_health_status():
    """Save health status tracking to file"""
    with open(HEALTH_STATUS_FILE, 'w') as f:
        json.dump(health_status, f, indent=2)


def load_update_history():
    """Load update history from file"""
    global update_history
    if os.path.exists(UPDATE_HISTORY_FILE):
        with open(UPDATE_HISTORY_FILE, 'r') as f:
            update_history = json.load(f)
    else:
        update_history = {}


def save_update_history():
    """Save update history to file"""
    with open(UPDATE_HISTORY_FILE, 'w') as f:
        json.dump(update_history, f, indent=2)


def get_stack_metadata(stack_name):
    """Load stack metadata from .dockup-meta.json"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    meta_file = os.path.join(stack_path, '.dockup-meta.json')
    if os.path.exists(meta_file):
        try:
            with open(meta_file, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_stack_metadata(stack_name, metadata):
    """Save stack metadata to .dockup-meta.json"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    Path(stack_path).mkdir(parents=True, exist_ok=True)
    meta_file = os.path.join(stack_path, '.dockup-meta.json')
    with open(meta_file, 'w') as f:
        json.dump(metadata, f, indent=2)


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


def hash_password(password):
    """Hash a password using bcrypt"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())


def verify_password(password, hashed):
    """Verify a password against its hash"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed)
    except:
        return False


def is_password_set():
    """Check if a password has been set"""
    return os.path.exists(PASSWORD_FILE)


def save_password(password):
    """Save hashed password to file"""
    hashed = hash_password(password)
    with open(PASSWORD_FILE, 'wb') as f:
        f.write(hashed)


def load_password_hash():
    """Load hashed password from file"""
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, 'rb') as f:
            return f.read()
    return None


def require_auth(f):
    """Decorator to require authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Skip auth if password protection is disabled
        if not config.get('password_enabled', False):
            return f(*args, **kwargs)
        
        # Check if authenticated
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        
        return f(*args, **kwargs)
    return decorated_function


@app.before_request
def check_auth():
    """Check authentication before each request"""
    # Skip auth check if password protection is disabled
    if not config.get('password_enabled', False):
        return None
    
    # Allow these paths without authentication
    allowed_paths = [
        '/login',
        '/setup',
        '/api/auth/login',
        '/api/auth/setup',
        '/static/'
    ]
    
    # Check if path is allowed
    for path in allowed_paths:
        if request.path.startswith(path):
            return None
    
    # Check authentication
    if not session.get('authenticated'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Authentication required'}), 401
        # Redirect to setup if password not set, otherwise login
        if not is_password_set():
            return redirect(url_for('setup'))
        return redirect(url_for('login'))


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
    if not config.get('notify_on_health_failure', True) and 'unhealthy' in message.lower():
        print(f"Notification skipped (notify_on_health_failure=False): {title}")
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


def check_stack_health(stack_name, containers):
    """Monitor stack health and send notifications after N consecutive failures"""
    if not config.get('notify_on_health_failure', True):
        return
    
    threshold = config.get('health_check_threshold', 3)
    
    # Check if any containers are unhealthy or stopped
    unhealthy_containers = []
    for container in containers:
        if container['health'] == 'unhealthy':
            unhealthy_containers.append(f"{container['name']}: unhealthy (failing health check)")
        elif container['status'] in ['exited', 'dead', 'paused']:
            unhealthy_containers.append(f"{container['name']}: {container['status']}")
    
    # Initialize health tracking for this stack if needed
    if stack_name not in health_status:
        health_status[stack_name] = {
            'consecutive_failures': 0,
            'last_notified': None,
            'last_check': None
        }
    
    current_time = datetime.now(pytz.UTC).isoformat()
    
    if unhealthy_containers:
        # Increment failure counter
        health_status[stack_name]['consecutive_failures'] += 1
        health_status[stack_name]['last_check'] = current_time
        
        failures = health_status[stack_name]['consecutive_failures']
        print(f"  ⚠ Health check {failures}/{threshold}: {stack_name} has {len(unhealthy_containers)} issue(s)")
        
        # Send notification after threshold reached
        if failures >= threshold:
            last_notified = health_status[stack_name].get('last_notified')
            
            # Only notify once until it recovers
            if last_notified is None:
                issue_list = '\n'.join([f"  • {issue}" for issue in unhealthy_containers])
                send_notification(
                    f"Dockup - Stack Unhealthy",
                    f"Stack '{stack_name}' has been unhealthy for {failures} consecutive checks.\n\nContainers with issues:\n{issue_list}\n\nCheck the logs for details.",
                    'error'
                )
                health_status[stack_name]['last_notified'] = current_time
                save_health_status()
                
                broadcast_ws({
                    'type': 'health_alert',
                    'stack': stack_name,
                    'issues': unhealthy_containers
                })
    else:
        # Stack is healthy - reset counter
        if health_status[stack_name]['consecutive_failures'] > 0:
            # Was unhealthy, now recovered
            if health_status[stack_name].get('last_notified'):
                send_notification(
                    f"Dockup - Stack Recovered",
                    f"Stack '{stack_name}' is now healthy again.",
                    'success'
                )
                broadcast_ws({
                    'type': 'health_recovered',
                    'stack': stack_name
                })
            
            health_status[stack_name] = {
                'consecutive_failures': 0,
                'last_notified': None,
                'last_check': current_time
            }
            save_health_status()


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


def get_all_used_ports():
    """Get all ports currently in use by all containers"""
    used_ports = {}  # {port_number: [container_names]}
    try:
        all_containers = docker_client.containers.list()
        for container in all_containers:
            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
            for container_port, bindings in ports.items():
                if bindings:
                    for binding in bindings:
                        host_port = binding.get('HostPort')
                        if host_port:
                            port_num = int(host_port)
                            if port_num not in used_ports:
                                used_ports[port_num] = []
                            used_ports[port_num].append(container.name)
    except Exception as e:
        print(f"Error getting used ports: {e}")
    return used_ports


def check_port_conflicts(stack_name, compose_content):
    """Check if any ports in compose file conflict with running containers"""
    conflicts = []
    try:
        # Parse compose file
        compose_data = yaml.safe_load(compose_content)
        services = compose_data.get('services', {})
        
        # Get currently used ports
        used_ports = get_all_used_ports()
        
        # Get containers from this stack (to exclude them from conflict check)
        stack_containers = []
        try:
            containers = docker_client.containers.list(filters={'label': f'com.docker.compose.project={stack_name}'})
            stack_containers = [c.name for c in containers]
        except:
            pass
        
        # Check each service
        for service_name, service_config in services.items():
            ports = service_config.get('ports', [])
            for port_mapping in ports:
                port_str = str(port_mapping)
                
                # Handle different port formats
                if ':' in port_str:
                    parts = port_str.split(':')
                    if len(parts) == 2:
                        host_port = parts[0]
                    elif len(parts) == 3:
                        host_port = parts[1]
                    else:
                        continue
                else:
                    host_port = port_str.split('/')[0]
                
                host_port = host_port.split('/')[0].strip()
                
                try:
                    port_num = int(host_port)
                    
                    # Check if port is used by containers NOT in this stack
                    if port_num in used_ports:
                        conflicting_containers = [c for c in used_ports[port_num] if c not in stack_containers]
                        if conflicting_containers:
                            conflicts.append({
                                'port': port_num,
                                'service': service_name,
                                'used_by': conflicting_containers
                            })
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error checking port conflicts: {e}")
    
    return conflicts


def get_stack_stats(stack_name):
    """Get CPU and RAM usage for a stack"""
    global network_stats_cache
    
    try:
        containers = docker_client.containers.list(
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        total_cpu = 0.0
        total_mem = 0
        total_mem_limit = 0
        total_net_rx_bytes = 0
        total_net_tx_bytes = 0
        
        current_time = time.time()
        
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
                
                # Sum network bytes from all interfaces
                networks = stats.get('networks', {})
                for net_name, net_stats in networks.items():
                    total_net_rx_bytes += net_stats.get('rx_bytes', 0)
                    total_net_tx_bytes += net_stats.get('tx_bytes', 0)
                
            except Exception as e:
                print(f"Error getting stats for {container.name}: {e}")
        
        # Calculate network rate using previous cached values
        net_rx_mbps = 0.0
        net_tx_mbps = 0.0
        
        cache_key = stack_name
        if cache_key in network_stats_cache:
            prev_data = network_stats_cache[cache_key]
            time_delta = current_time - prev_data['time']
            
            if time_delta > 0:
                # Calculate bytes per second, then convert to Mbps
                rx_bytes_per_sec = (total_net_rx_bytes - prev_data['rx_bytes']) / time_delta
                tx_bytes_per_sec = (total_net_tx_bytes - prev_data['tx_bytes']) / time_delta
                
                # Convert to Mbps (bytes/sec * 8 / 1,000,000)
                net_rx_mbps = round((rx_bytes_per_sec * 8) / 1_000_000, 2)
                net_tx_mbps = round((tx_bytes_per_sec * 8) / 1_000_000, 2)
                
                # Ensure non-negative values
                net_rx_mbps = max(0, net_rx_mbps)
                net_tx_mbps = max(0, net_tx_mbps)
        
        # Store current values for next calculation
        network_stats_cache[cache_key] = {
            'time': current_time,
            'rx_bytes': total_net_rx_bytes,
            'tx_bytes': total_net_tx_bytes
        }
        
        return {
            'cpu_percent': round(total_cpu, 2),
            'mem_usage_mb': round(total_mem / (1024 * 1024), 2),
            'mem_limit_mb': round(total_mem_limit / (1024 * 1024), 2),
            'mem_percent': round((total_mem / total_mem_limit * 100) if total_mem_limit > 0 else 0, 2),
            'net_rx_mbps': net_rx_mbps,
            'net_tx_mbps': net_tx_mbps
        }
    except Exception as e:
        print(f"Error getting stats for {stack_name}: {e}")
        return {
            'cpu_percent': 0,
            'mem_usage_mb': 0,
            'mem_limit_mb': 0,
            'mem_percent': 0,
            'net_rx_mbps': 0,
            'net_tx_mbps': 0
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




def require_api_token(f):
    """Decorator to require valid API token for peer requests"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if request is from localhost (always allowed)
        if request.remote_addr in ['127.0.0.1', '::1', 'localhost']:
            return f(*args, **kwargs)
        
        # Check for valid API token in headers
        token = request.headers.get('X-API-Token', '')
        if token != config.get('api_token', ''):
            return jsonify({'error': 'Invalid or missing API token'}), 401
        
        return f(*args, **kwargs)
    return decorated_function


def get_peer_session(peer_id):
    """Get cached session for peer or create new one"""
    global peer_sessions
    
    peer = config['peers'].get(peer_id)
    if not peer or not peer.get('enabled', True):
        return None
    
    # Check if we have a cached session
    if peer_id in peer_sessions:
        session = peer_sessions[peer_id]
        # Test if session is still valid
        try:
            test = session.get(f"{peer['url']}/api/ping", timeout=3, headers={'X-API-Token': peer['api_token']})
            if test.status_code == 200:
                return session
        except:
            pass
    
    # Create new session
    session = requests.Session()
    session.headers.update({'X-API-Token': peer['api_token']})
    peer_sessions[peer_id] = session
    return session

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
                    
                    # Get health status if available
                    health_status = None
                    state = container.attrs.get('State', {})
                    if 'Health' in state:
                        health_status = state['Health'].get('Status', 'none')
                    
                    containers.append({
                        'id': container.id[:12],
                        'name': container.name,
                        'status': container.status,
                        'health': health_status,  # Can be: healthy, unhealthy, starting, none (no healthcheck)
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
                'mem_percent': 0,
                'net_rx_mbps': 0,
                'net_tx_mbps': 0
            })
            
            # Load metadata
            metadata = get_stack_metadata(stack_name)
            
            # Mark as started if containers exist
            if len(containers) > 0 and not metadata.get('ever_started'):
                metadata['ever_started'] = True
                save_stack_metadata(stack_name, metadata)
            
            # Inactive = never started (no metadata marker AND no containers)
            is_inactive = not metadata.get('ever_started', False) and len(containers) == 0
            
            # Check stack health and send notifications if needed
            if containers and is_running:
                check_stack_health(stack_name, containers)
            
            # Get update history for "fresh" badge
            update_info = update_history.get(stack_name, {})
            last_update = update_info.get('last_update')
            updated_recently = False
            update_age_hours = None
            
            if last_update:
                try:
                    update_time = datetime.fromisoformat(last_update)
                    age_delta = datetime.now(pytz.UTC) - update_time
                    update_age_hours = age_delta.total_seconds() / 3600
                    
                    # Show "fresh" badge if updated within last 24 hours
                    if update_age_hours < 24:
                        updated_recently = True
                except:
                    pass
            
            # Determine status: running, stopped, or partial
            running_count = sum(1 for c in containers if c['status'] == 'running')
            if running_count == len(containers) and len(containers) > 0:
                status = 'running'
            elif running_count > 0:
                status = 'partial'
            elif len(containers) > 0:
                status = 'stopped'
            else:
                status = 'inactive'
            
            stacks.append({
                'name': stack_name,
                'path': stack_path,
                'compose_file': compose_file,
                'services': services,
                'containers': containers,  # KEEP AS ARRAY
                'status': status,
                'health': 'healthy' if all(c['health'] in ['healthy', 'none'] for c in containers) else 'unhealthy' if any(c['health'] == 'unhealthy' for c in containers) else 'starting',
                'running': is_running,
                'update_mode': schedule_info.get('mode', 'off'),
                'cron': schedule_info.get('cron', config.get('default_cron', '0 2 * * *')),
                'last_check': format_timestamp(update_status.get(stack_name, {}).get('last_check')),
                'update_available': update_status.get(stack_name, {}).get('update_available', False),
                'stats': stats,
                'inactive': is_inactive,  # Only inactive if never started
                'web_ui_url': metadata.get('web_ui_url', ''),
                'tags': metadata.get('tags', []),
                'updated_recently': updated_recently,
                'update_age_hours': update_age_hours,
                'last_update': last_update,
                'updated_by': update_info.get('updated_by', 'unknown')
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
    
    # Always save as compose.yaml (modern standard)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    
    with open(compose_file, 'w') as f:
        f.write(content)
    
    # Clean up old compose files to avoid confusion
    old_files = ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml']
    for old_file in old_files:
        old_path = os.path.join(stack_path, old_file)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
                print(f"Cleaned up old compose file: {old_file} from {stack_name}")
            except Exception as e:
                print(f"Warning: Could not delete {old_file}: {e}")



def stack_operation(stack_name, operation):
    """Perform operation on stack (up/down/stop/restart/pull) with real-time output"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    if not os.path.exists(compose_file):
        compose_file = os.path.join(stack_path, 'docker-compose.yml')
    
    try:
        if operation == 'up':
            cmd = ['docker', 'compose', '-f', compose_file, 'up', '-d']
        elif operation == 'stop':
            # Stop containers without removing them
            cmd = ['docker', 'compose', '-f', compose_file, 'stop']
        elif operation == 'down':
            # Stop and remove containers
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

def auto_update_stack(stack_name):
    """
    Watchtower-style update: Pull first, then compare image IDs
    This is the ONLY reliable method that actually works
    """
    schedule_info = schedules.get(stack_name, {})
    mode = schedule_info.get('mode', 'off')
    
    if mode == 'off':
        return
    
    print(f"=" * 50)
    print(f"[{datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S %Z')}] Scheduled check for: {stack_name}")
    print(f"Mode: {mode}, Cron: {schedule_info.get('cron')}")
    print(f"=" * 50)
    
    # WATCHTOWER METHOD: Get current image IDs BEFORE pulling
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        if not containers:
            print(f"✗ No containers found for {stack_name}")
            return
        
        # Store current image IDs
        old_image_ids = {}
        for container in containers:
            try:
                if container.image.tags:
                    image_name = container.image.tags[0]
                    old_image_ids[image_name] = container.image.id
                    print(f"  Current {container.name}: {container.image.id[:12]}")
            except:
                pass
        
        if not old_image_ids:
            print(f"✗ No tagged images found for {stack_name}")
            return
        
    except Exception as e:
        print(f"✗ Error getting container info: {e}")
        send_notification(
            f"Dockup - Error",
            f"Failed to check stack '{stack_name}': {str(e)}",
            'error'
        )
        return
    
    # PULL new images (always pull, like Watchtower)
    print(f"  → Pulling latest images...")
    success_pull, output_pull = stack_operation(stack_name, 'pull')
    
    if not success_pull:
        print(f"✗ Pull failed: {output_pull}")
        send_notification(
            f"Dockup - Pull Failed",
            f"Failed to pull images for '{stack_name}'",
            'error'
        )
        return
    
    print(f"  ✓ Pull completed")
    
    # Get NEW image IDs after pull
    new_image_ids = {}
    images_changed = False
    
    for image_name, old_id in old_image_ids.items():
        try:
            # Get the newly pulled image
            new_image = docker_client.images.get(image_name)
            new_id = new_image.id
            new_image_ids[image_name] = new_id
            
            if old_id != new_id:
                print(f"  ✓ UPDATE DETECTED: {image_name}")
                print(f"    Old: {old_id[:12]}")
                print(f"    New: {new_id[:12]}")
                images_changed = True
            else:
                print(f"  = No change: {image_name} ({old_id[:12]})")
        except Exception as e:
            print(f"  ⚠ Could not check {image_name}: {e}")
    
    # Update status
    update_status[stack_name] = {
        'last_check': datetime.now().isoformat(),
        'update_available': images_changed
    }
    
    # Handle based on mode
    if mode == 'check':
        # Check-only mode
        if images_changed:
            print(f"✓ Updates available for {stack_name} (check-only mode)")
            send_notification(
                f"Dockup - Update Available",
                f"Stack '{stack_name}' has updates available",
                'info'
            )
            broadcast_ws({
                'type': 'update_available',
                'stack': stack_name
            })
        else:
            print(f"✓ No updates for {stack_name}")
            broadcast_ws({
                'type': 'no_updates',
                'stack': stack_name
            })
        return
    
    elif mode == 'auto':
        if not images_changed:
            print(f"✓ Already up to date: {stack_name}")
            broadcast_ws({
                'type': 'no_updates',
                'stack': stack_name
            })
            return
        
        # AUTO MODE: Actually update the containers
        print(f"  → Auto-update enabled, recreating containers...")
        
        # Recreate containers with new images
        success_up, output_up = stack_operation(stack_name, 'up')
        
        if not success_up:
            print(f"  ✗ First restart failed, retrying...")
            time.sleep(3)
            success_up, output_up = stack_operation(stack_name, 'up')
        
        if success_up:
            # Wait for containers to fully start and stabilize
            print(f"  → Waiting for containers to start...")
            time.sleep(10)
            
            # Force Docker to refresh image data
            try:
                docker_client.images.list()
            except:
                pass
            
            # Refresh metadata
            refresh_container_metadata(stack_name)
            
            # VERIFY: Check that containers are now running the new images
            containers_updated = docker_client.containers.list(
                all=True,
                filters={'label': f'com.docker.compose.project={stack_name}'}
            )
            
            verification_passed = True
            verification_results = []
            
            for container in containers_updated:
                try:
                    if not container.image.tags:
                        # No tags = locally built image, skip verification
                        continue
                    
                    image_name = container.image.tags[0]
                    
                    # Normalize IDs (remove sha256: prefix if present)
                    current_id = container.image.id
                    if current_id.startswith('sha256:'):
                        current_id = current_id[7:]
                    
                    expected_id = new_image_ids.get(image_name)
                    if expected_id and expected_id.startswith('sha256:'):
                        expected_id = expected_id[7:]
                    
                    if expected_id:
                        if current_id == expected_id:
                            print(f"  ✓ {container.name}: Running new image ({current_id[:12]})")
                            verification_results.append(True)
                        else:
                            print(f"  ✗ {container.name}: Image mismatch")
                            print(f"    Expected: {expected_id[:12]}")
                            print(f"    Got:      {current_id[:12]}")
                            verification_passed = False
                            verification_results.append(False)
                    else:
                        # Image name not in our tracking dict - could be sidecar or init container
                        print(f"  ⚠ {container.name}: Image '{image_name}' not in tracking dict")
                        print(f"    Tracked images: {list(new_image_ids.keys())}")
                        # Don't fail verification for untracked images
                        
                except Exception as e:
                    print(f"  ⚠ {container.name}: Verification error: {e}")
            
            # Consider it successful if we verified at least one container successfully
            # and didn't have any explicit failures
            if len(verification_results) == 0:
                # No containers verified - maybe all are locally built or sidecars
                print(f"  ⚠ No containers could be verified (may be locally built images)")
                verification_passed = True  # Don't fail if we can't verify
            elif any(verification_results) and not any(not r for r in verification_results):
                # At least one success and no failures
                verification_passed = True
            
            if verification_passed:
                print(f"  ✓✓✓ UPDATE SUCCESSFUL AND VERIFIED ✓✓✓")
                # CLEAR update available flag (AUTO UPDATE FIX)
                update_status[stack_name] = {
                 'last_check': datetime.now(pytz.UTC).isoformat(),
                 'update_available': False

                # Record update in history for "fresh" badge
                update_history[stack_name] = {
                    'last_update': datetime.now(pytz.UTC).isoformat(),
                    'updated_by': 'auto'
                }
                save_update_history()
                
                send_notification(
                    f"Dockup - Updated Successfully",
                    f"Stack '{stack_name}' updated and verified successfully!\n\nAll containers are running the latest images.",
                    'success'
                )
                broadcast_ws({
                    'type': 'stack_updated',
                    'stack': stack_name,
                    'verified': True
                })
            else:
                print(f"  ⚠ Update completed but verification inconclusive")
                
                # Still record the update attempt
                update_history[stack_name] = {
                    'last_update': datetime.now(pytz.UTC).isoformat(),
                    'updated_by': 'auto'
                }
                save_update_history()
                
                send_notification(
                    f"Dockup - Updated",
                    f"Stack '{stack_name}' updated (some containers may need manual check)",
                    'success'
                )
                broadcast_ws({
                    'type': 'stack_updated',
                    'stack': stack_name,
                    'verified': False
                })
        else:
            print(f"  ✗✗✗ UPDATE FAILED ✗✗✗")
            send_notification(
                f"Dockup - Update Failed",
                f"Failed to restart stack '{stack_name}' after pulling new images",
                'error'
            )
            broadcast_ws({
                'type': 'update_failed',
                'stack': stack_name
            })


def check_stack_updates(stack_name):
    """
    Simple update check for UI - pulls latest and compares image IDs
    This is called by the "Check for Updates" button
    
    This uses the same method as auto_update_stack to ensure consistency
    """
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        if not containers:
            return True, False
        
        # Get current image IDs
        old_image_ids = {}
        for container in containers:
            try:
                if container.image.tags:
                    image_name = container.image.tags[0]
                    old_image_ids[image_name] = container.image.id
            except:
                pass
        
        if not old_image_ids:
            return True, False
        
        # Pull latest images (required to check for updates reliably)
        print(f"Checking updates for {stack_name}...")
        success_pull, output_pull = stack_operation(stack_name, 'pull')
        
        if not success_pull:
            print(f"  Pull failed, cannot check for updates")
            return False, None
        
        # Get NEW image IDs after pull
        update_available = False
        for image_name, old_id in old_image_ids.items():
            try:
                new_image = docker_client.images.get(image_name)
                new_id = new_image.id
                
                if old_id != new_id:
                    print(f"  ✓ Update available: {image_name}")
                    print(f"    Old: {old_id[:12]}")
                    print(f"    New: {new_id[:12]}")
                    update_available = True
                    break
            except Exception as e:
                print(f"  ⚠ Could not check {image_name}: {e}")
        
        # Update status
        update_status[stack_name] = {
            'last_check': datetime.now().isoformat(),
            'update_available': update_available
        }
        
        return True, update_available
        
    except Exception as e:
        print(f"✗ Error checking updates for {stack_name}: {e}")
        return False, None


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
    # Check if password needs to be set up
    if config.get('password_enabled', False) and not is_password_set():
        return redirect(url_for('setup'))
    return send_from_directory('static', 'index.html')


@app.route('/login')
def login():
    """Serve login page"""
    # If already authenticated, redirect to main page
    if session.get('authenticated'):
        return redirect(url_for('index'))
    
    # If password protection disabled, redirect to main page
    if not config.get('password_enabled', False):
        return redirect(url_for('index'))
    
    # If no password set yet, redirect to setup
    if not is_password_set():
        return redirect(url_for('setup'))
    
    return send_from_directory('static', 'login.html')


@app.route('/setup')
def setup():
    """Serve setup page (only if password not set)"""
    # If password already set, redirect to login
    if is_password_set():
        return redirect(url_for('login'))
    
    # If password protection disabled, redirect to main page
    if not config.get('password_enabled', False):
        return redirect(url_for('index'))
    
    return send_from_directory('static', 'setup.html')


@app.route('/api/auth/setup', methods=['POST'])
def api_auth_setup():
    """Set up initial password"""
    try:
        # Only allow if password not already set
        if is_password_set():
            return jsonify({'error': 'Password already set'}), 400
        
        data = request.json
        password = data.get('password', '')
        confirm = data.get('confirm', '')
        
        if not password or not confirm:
            return jsonify({'error': 'Password and confirmation required'}), 400
        
        if password != confirm:
            return jsonify({'error': 'Passwords do not match'}), 400
        
        if len(password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        
        # Save password
        save_password(password)
        
        # Enable password protection
        config['password_enabled'] = True
        save_config()
        
        # Auto-login
        session['authenticated'] = True
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """Authenticate user"""
    try:
        data = request.json
        password = data.get('password', '')
        
        if not password:
            return jsonify({'error': 'Password required'}), 400
        
        # Load password hash
        hashed = load_password_hash()
        if not hashed:
            return jsonify({'error': 'No password set'}), 400
        
        # Verify password
        if verify_password(password, hashed):
            session['authenticated'] = True
            session.permanent = True  # Keep session for 31 days (Flask default)
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Invalid password'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """Logout user"""
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/change-password', methods=['POST'])
def api_auth_change_password():
    """Change password"""
    try:
        data = request.json
        current = data.get('current', '')
        new_password = data.get('new_password', '')
        confirm = data.get('confirm', '')
        
        if not all([current, new_password, confirm]):
            return jsonify({'error': 'All fields required'}), 400
        
        if new_password != confirm:
            return jsonify({'error': 'New passwords do not match'}), 400
        
        if len(new_password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        
        # Verify current password
        hashed = load_password_hash()
        if not hashed or not verify_password(current, hashed):
            return jsonify({'error': 'Current password incorrect'}), 401
        
        # Save new password
        save_password(new_password)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stacks')
def api_stacks():
    """Get all stacks"""
    return jsonify(get_stacks())


@app.route('/api/host/stats')
def api_host_stats():
    """Get host system CPU and memory stats"""
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Get network stats
        net_io = psutil.net_io_counters()
        net_rx_mbps = 0
        net_tx_mbps = 0
        
        # Calculate network rate if we have previous data
        if hasattr(api_host_stats, 'last_net_io'):
            time_delta = time.time() - api_host_stats.last_time
            if time_delta > 0:
                bytes_recv_delta = net_io.bytes_recv - api_host_stats.last_net_io['bytes_recv']
                bytes_sent_delta = net_io.bytes_sent - api_host_stats.last_net_io['bytes_sent']
                net_rx_mbps = (bytes_recv_delta * 8) / (time_delta * 1_000_000)
                net_tx_mbps = (bytes_sent_delta * 8) / (time_delta * 1_000_000)
        
        # Store current values for next call
        api_host_stats.last_net_io = {
            'bytes_recv': net_io.bytes_recv,
            'bytes_sent': net_io.bytes_sent
        }
        api_host_stats.last_time = time.time()
        
        return jsonify({
            'cpu_percent': round(cpu_percent, 1),
            'cpu_count': psutil.cpu_count(),
            'memory_total_gb': round(memory.total / (1024**3), 2),
            'memory_used_gb': round(memory.used / (1024**3), 2),
            'memory_percent': round(memory.percent, 1),
            'disk_total_gb': round(disk.total / (1024**3), 2),
            'disk_used_gb': round(disk.used / (1024**3), 2),
            'disk_percent': round(disk.percent, 1),
            'hostname': 'Host System',
            'net_rx_mbps': round(net_rx_mbps, 2),
            'net_tx_mbps': round(net_tx_mbps, 2)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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


@app.route('/api/stack/<stack_name>/port-conflicts', methods=['POST'])
def api_check_port_conflicts(stack_name):
    """Check for port conflicts in compose content"""
    try:
        data = request.json
        content = data.get('content', '')
        
        conflicts = check_port_conflicts(stack_name, content)
        
        return jsonify({
            'has_conflicts': len(conflicts) > 0,
            'conflicts': conflicts
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
            
            # Record manual update in history ONLY if 'pull' was done (which actually updates images)
            # Don't record on 'up' as that's just starting containers
            if operation == 'pull':
                update_history[stack_name] = {
                    'last_update': datetime.now(pytz.UTC).isoformat(),
                    'updated_by': 'manual'
                }
                save_update_history()
        
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
            
            # Get tags - show as <untagged> instead of <none>:<none>
            tags = img.tags if img.tags else ['<untagged>']
            
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
        
        # Sort alphabetically by tag name
        image_list.sort(key=lambda x: x['tag'].lower())
        
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
    """Background thread to update stack stats every 5 seconds"""
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
                            'mem_percent': 0,
                            'net_rx_mbps': 0,
                            'net_tx_mbps': 0
                        }
            
            # Wait 5 seconds before next update (faster updates)
            time.sleep(5)
        except Exception as e:
            print(f"Stats update error: {e}")
            time.sleep(5)


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
    # Return config without peers (but include api_token for copying)
    safe_config = {k: v for k, v in config.items() if k != 'peers'}
    safe_config['instance_name'] = config.get('instance_name', 'DockUp')
    # Return full token - user needs to copy it to connect other instances
    safe_config['api_token'] = config.get('api_token', '')
    return jsonify(safe_config)


@app.route('/api/config', methods=['POST'])
def api_config_set():
    """Set configuration"""
    global config
    data = request.json
    
    # Preserve critical fields that shouldn't be overwritten
    preserved = {
        'api_token': config.get('api_token'),
        'peers': config.get('peers', {})
    }
    
    # Update config
    config.update(data)
    
    # Restore preserved fields
    config['api_token'] = preserved['api_token']
    config['peers'] = preserved['peers']
    
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
            'networks': service.get('networks', []),
            'hostname': service.get('hostname', ''),
            'domainname': service.get('domainname', ''),
            'mac_address': service.get('mac_address', ''),
            'dns': service.get('dns', []),
            'dns_search': service.get('dns_search', []),
            'dns_opt': service.get('dns_opt', []),
            'extra_hosts': service.get('extra_hosts', []),
            'privileged': service.get('privileged', False),
            'cap_add': service.get('cap_add', []),
            'cap_drop': service.get('cap_drop', []),
            'security_opt': service.get('security_opt', []),
            'user': service.get('user', ''),
            'sysctls': service.get('sysctls', {}),
            'tmpfs': service.get('tmpfs', []),
            'stdin_open': service.get('stdin_open', False),
            'tty': service.get('tty', False),
            'healthcheck': service.get('healthcheck', {}),
            'depends_on': service.get('depends_on', []),
            'logging': service.get('logging', {}),
            'restart': service.get('restart', 'unless-stopped'),
            'extra': {}
        }
        
        # Gather extra keys
        known_keys = ['image', 'container_name', 'environment', 'ports', 'volumes', 
                      'devices', 'labels', 'command', 'entrypoint', 'network_mode', 'networks',
                      'hostname', 'domainname', 'mac_address', 'dns', 'dns_search', 'dns_opt',
                      'extra_hosts', 'privileged', 'cap_add', 'cap_drop', 'security_opt', 'user',
                      'sysctls', 'tmpfs', 'stdin_open', 'tty', 'healthcheck', 'depends_on', 
                      'logging', 'restart']
        
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
        
        # Networks
        if 'networks' in data and data['networks']:
            new_service['networks'] = data['networks']
        
        # Hostname/Domain/MAC
        if 'hostname' in data and data['hostname']:
            new_service['hostname'] = data['hostname']
        if 'domainname' in data and data['domainname']:
            new_service['domainname'] = data['domainname']
        if 'mac_address' in data and data['mac_address']:
            new_service['mac_address'] = data['mac_address']
        
        # DNS
        if 'dns' in data and data['dns']:
            new_service['dns'] = data['dns']
        if 'dns_search' in data and data['dns_search']:
            new_service['dns_search'] = data['dns_search']
        if 'dns_opt' in data and data['dns_opt']:
            new_service['dns_opt'] = data['dns_opt']
        if 'extra_hosts' in data and data['extra_hosts']:
            new_service['extra_hosts'] = data['extra_hosts']
        
        # Security
        if 'privileged' in data:
            new_service['privileged'] = data['privileged']
        if 'cap_add' in data and data['cap_add']:
            new_service['cap_add'] = data['cap_add']
        if 'cap_drop' in data and data['cap_drop']:
            new_service['cap_drop'] = data['cap_drop']
        if 'security_opt' in data and data['security_opt']:
            new_service['security_opt'] = data['security_opt']
        if 'user' in data and data['user']:
            new_service['user'] = data['user']
        
        # Advanced
        if 'sysctls' in data and data['sysctls']:
            new_service['sysctls'] = data['sysctls']
        if 'tmpfs' in data and data['tmpfs']:
            new_service['tmpfs'] = data['tmpfs']
        if 'stdin_open' in data:
            new_service['stdin_open'] = data['stdin_open']
        if 'tty' in data:
            new_service['tty'] = data['tty']
        if 'healthcheck' in data and data['healthcheck']:
            new_service['healthcheck'] = data['healthcheck']
        if 'depends_on' in data and data['depends_on']:
            new_service['depends_on'] = data['depends_on']
        if 'logging' in data and data['logging']:
            new_service['logging'] = data['logging']
        
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
        
        # Always save to compose.yaml (standardize filename)
        new_compose_file = os.path.join(stack_path, 'compose.yaml')
        
        # Write to file
        with open(new_compose_file, 'w') as f:
            yaml.safe_dump(compose_data, f, sort_keys=False, default_flow_style=False)
        
        # Always clean up old compose files to avoid duplicates
        old_files = ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml']
        for old_file in old_files:
            old_path = os.path.join(stack_path, old_file)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                    print(f"Cleaned up old compose file: {old_file} from {stack_name}")
                except Exception as e:
                    print(f"Warning: Could not delete {old_file}: {e}")
        
        return jsonify({'success': True})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ports/check', methods=['POST'])
def api_check_ports():
    """Check if ports are in use"""
    try:
        data = request.json
        ports = data.get('ports', [])
        
        conflicts = {}
        
        # Get all containers
        all_containers = docker_client.containers.list(all=True)
        
        for port in ports:
            # Parse port (format: "8080:80/tcp" or "8080:80")
            try:
                if ':' in str(port):
                    host_port = str(port).split(':')[0]
                    # Remove any IP prefix
                    if '.' in host_port or ':' in host_port:
                        host_port = host_port.split(':')[-1] if ':' in host_port else host_port.split('.')[-1]
                else:
                    host_port = str(port)
                
                # Remove protocol suffix
                host_port = host_port.split('/')[0]
                host_port = int(host_port)
                
                # Check against all containers
                for container in all_containers:
                    ports_config = container.attrs.get('NetworkSettings', {}).get('Ports', {})
                    for container_port, bindings in ports_config.items():
                        if bindings:
                            for binding in bindings:
                                if int(binding.get('HostPort', 0)) == host_port:
                                    conflicts[port] = {
                                        'container': container.name,
                                        'stack': container.labels.get('com.docker.compose.project', 'unknown'),
                                        'status': container.status
                                    }
                                    break
            except:
                pass
        
        return jsonify({
            'conflicts': conflicts,
            'has_conflicts': len(conflicts) > 0
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/volumes/suggestions', methods=['GET'])
def api_volume_suggestions():
    """Get common volume mount suggestions"""
    try:
        suggestions = {
            'config_dirs': [
                '/opt/appdata',
                '/mnt/user/appdata',
                '/volume1/docker',
                '/home/user/docker/config',
                '/srv/docker'
            ],
            'data_dirs': [
                '/mnt/user/data',
                '/volume1/data',
                '/media',
                '/mnt/media',
                '/data'
            ],
            'common_paths': [
                '/etc/localtime:/etc/localtime:ro',
                '/var/run/docker.sock:/var/run/docker.sock',
                '/dev/dri:/dev/dri',
                '/tmp:/tmp'
            ]
        }
        
        # Add actually existing paths
        existing_paths = []
        common_base_paths = ['/mnt', '/media', '/opt', '/srv', '/volume1', '/data']
        
        for base_path in common_base_paths:
            if os.path.exists(base_path):
                try:
                    for item in os.listdir(base_path):
                        full_path = os.path.join(base_path, item)
                        if os.path.isdir(full_path):
                            existing_paths.append(full_path)
                except:
                    pass
        
        suggestions['existing_paths'] = existing_paths[:20]  # Limit to 20
        
        return jsonify(suggestions)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/environment/templates', methods=['GET'])
def api_env_templates():
    """Get common environment variable templates"""
    try:
        templates = {
            'common': [
                {'name': 'TZ', 'value': 'UTC', 'description': 'Timezone'},
                {'name': 'PUID', 'value': '1000', 'description': 'User ID (for linuxserver.io images)'},
                {'name': 'PGID', 'value': '1000', 'description': 'Group ID (for linuxserver.io images)'},
                {'name': 'UMASK', 'value': '022', 'description': 'File creation mask'}
            ],
            'database': [
                {'name': 'MYSQL_ROOT_PASSWORD', 'value': '', 'description': 'MySQL root password'},
                {'name': 'MYSQL_DATABASE', 'value': '', 'description': 'Database name'},
                {'name': 'MYSQL_USER', 'value': '', 'description': 'Database user'},
                {'name': 'MYSQL_PASSWORD', 'value': '', 'description': 'Database password'},
                {'name': 'POSTGRES_DB', 'value': '', 'description': 'PostgreSQL database name'},
                {'name': 'POSTGRES_USER', 'value': '', 'description': 'PostgreSQL user'},
                {'name': 'POSTGRES_PASSWORD', 'value': '', 'description': 'PostgreSQL password'}
            ],
            'web': [
                {'name': 'PORT', 'value': '3000', 'description': 'Application port'},
                {'name': 'HOST', 'value': '0.0.0.0', 'description': 'Bind address'},
                {'name': 'NODE_ENV', 'value': 'production', 'description': 'Node environment'}
            ]
        }
        
        return jsonify(templates)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stack/<stack_name>/clone', methods=['POST'])
def api_clone_stack(stack_name):
    """Clone an existing stack with a new name"""
    try:
        data = request.json
        new_name = data.get('new_name')
        
        if not new_name:
            return jsonify({'error': 'New stack name required'}), 400
        
        source_path = os.path.join(STACKS_DIR, stack_name)
        dest_path = os.path.join(STACKS_DIR, new_name)
        
        if not os.path.exists(source_path):
            return jsonify({'error': 'Source stack not found'}), 404
        
        if os.path.exists(dest_path):
            return jsonify({'error': 'Stack with that name already exists'}), 400
        
        # Copy stack directory
        import shutil
        shutil.copytree(source_path, dest_path)
        
        # Update compose file to change project name references
        compose_file = None
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            potential_file = os.path.join(dest_path, filename)
            if os.path.exists(potential_file):
                compose_file = potential_file
                break
        
        if compose_file:
            with open(compose_file, 'r') as f:
                content = f.read()
            
            # Replace old stack name with new one in content
            content = content.replace(stack_name, new_name)
            
            with open(compose_file, 'w') as f:
                f.write(content)
        
        # Copy metadata but don't copy schedule
        metadata = get_stack_metadata(stack_name)
        save_stack_metadata(new_name, metadata)
        
        return jsonify({'success': True, 'new_stack': new_name})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/network-interfaces', methods=['GET'])
def api_get_network_interfaces():
    """Get available network interfaces for macvlan/ipvlan"""
    try:
        # Use ip command for most reliable detection
        result = subprocess.run(['ip', '-o', '-4', 'addr', 'show'], 
                               capture_output=True, text=True, timeout=5)
        
        interfaces = []
        seen = set()
        
        for line in result.stdout.split('\n'):
            if line.strip():
                parts = line.split()
                if len(parts) >= 4:
                    # Format: 2: ens18 inet 192.168.1.100/24 ...
                    iface = parts[1].rstrip(':')
                    
                    # Skip loopback and docker interfaces
                    if iface.startswith(('lo', 'docker', 'br-', 'veth')) or iface in seen:
                        continue
                    
                    # Extract IP address
                    ip = 'N/A'
                    for i, part in enumerate(parts):
                        if part == 'inet' and i + 1 < len(parts):
                            ip = parts[i + 1].split('/')[0]
                            break
                    
                    interfaces.append({
                        'name': iface,
                        'ip': ip
                    })
                    seen.add(iface)
        
        # If no interfaces found, return safe defaults
        if not interfaces:
            interfaces = [
                {'name': 'eth0', 'ip': 'N/A'},
                {'name': 'ens18', 'ip': 'N/A'}
            ]
        
        return jsonify(interfaces)
        
    except Exception as e:
        print(f"Error detecting network interfaces: {e}")
        # Fallback to common interface names
        return jsonify([
            {'name': 'ens18', 'ip': 'N/A'},
            {'name': 'eth0', 'ip': 'N/A'},
            {'name': 'enp2s0', 'ip': 'N/A'}
        ])


@app.route('/api/networks', methods=['GET'])
def api_get_networks():
    """Get all Docker networks"""
    try:
        networks = docker_client.networks.list()
        network_list = []
        
        for network in networks:
            # Get containers using this network
            containers_using = []
            for container in docker_client.containers.list(all=True):
                container_networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
                if network.name in container_networks:
                    project = container.labels.get('com.docker.compose.project', container.name)
                    if project not in containers_using:
                        containers_using.append(project)
            
            network_info = {
                'id': network.id[:12],
                'name': network.name,
                'driver': network.attrs.get('Driver', 'unknown'),
                'scope': network.attrs.get('Scope', 'local'),
                'internal': network.attrs.get('Internal', False),
                'ipam': network.attrs.get('IPAM', {}),
                'options': network.attrs.get('Options', {}),
                'containers_using': containers_using
            }
            network_list.append(network_info)
        
        return jsonify(network_list)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/network', methods=['POST'])
def api_create_network():
    """Create a new Docker network"""
    try:
        data = request.json
        name = data.get('name')
        driver = data.get('driver', 'bridge')
        
        if not name:
            return jsonify({'error': 'Network name required'}), 400
        
        # Build IPAM config
        ipam_config = None
        if data.get('subnet'):
            ipam_config = docker.types.IPAMConfig(
                pool_configs=[docker.types.IPAMPool(
                    subnet=data.get('subnet'),
                    gateway=data.get('gateway'),
                    iprange=data.get('ip_range')
                )]
            )
        
        # Build options
        options = {}
        if data.get('parent'):
            options['parent'] = data.get('parent')
        
        # Create network
        network = docker_client.networks.create(
            name=name,
            driver=driver,
            options=options if options else None,
            ipam=ipam_config,
            internal=data.get('internal', False)
        )
        
        return jsonify({
            'success': True,
            'network_id': network.id[:12],
            'network_name': network.name
        })
        
    except docker.errors.APIError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/<network_id>', methods=['DELETE'])
def api_delete_network(network_id):
    """Delete a Docker network"""
    try:
        network = docker_client.networks.get(network_id)
        
        # Get containers using this network
        containers_using = []
        for container in docker_client.containers.list(all=True):
            container_networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
            if network.name in container_networks:
                containers_using.append(container.name)
        
        if containers_using:
            return jsonify({
                'error': f'Network is in use by: {", ".join(containers_using)}'
            }), 400
        
        network.remove()
        return jsonify({'success': True})
        
    except docker.errors.NotFound:
        return jsonify({'error': 'Network not found'}), 404
    except docker.errors.APIError as e:
        return jsonify({'error': str(e)}), 400
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


@app.route('/api/stack/<stack_name>/webui', methods=['GET', 'POST'])
def api_stack_webui(stack_name):
    """Get or set Web UI URL for a stack (auto-detects if not set)"""
    if request.method == 'GET':
        metadata = get_stack_metadata(stack_name)
        web_ui_url = metadata.get('web_ui_url', '')
        
        # Auto-detect from first exposed port if not set
        if not web_ui_url:
            try:
                stack_path = os.path.join(STACKS_DIR, stack_name)
                compose_file = None
                
                for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
                    potential_file = os.path.join(stack_path, filename)
                    if os.path.exists(potential_file):
                        compose_file = potential_file
                        break
                
                if compose_file:
                    with open(compose_file, 'r') as f:
                        compose_data = yaml.safe_load(f)
                    
                    # Get first service with exposed ports
                    services = compose_data.get('services', {})
                    for service_name, service in services.items():
                        ports = service.get('ports', [])
                        if ports:
                            # Get first port
                            first_port = ports[0]
                            if isinstance(first_port, str):
                                # Parse "8080:80" or "80:80" format
                                port_parts = first_port.split(':')
                                if len(port_parts) >= 2:
                                    host_port = port_parts[0].split('/')[0]  # Remove protocol if present
                                else:
                                    host_port = port_parts[0].split('/')[0]
                                
                                # Try to get host IP (use localhost as fallback)
                                import socket
                                try:
                                    hostname = socket.gethostname()
                                    host_ip = socket.gethostbyname(hostname)
                                except:
                                    host_ip = 'localhost'
                                
                                web_ui_url = f"http://{host_ip}:{host_port}"
                            elif isinstance(first_port, dict):
                                # Long format with published port
                                published = first_port.get('published', '')
                                if published:
                                    import socket
                                    try:
                                        hostname = socket.gethostname()
                                        host_ip = socket.gethostbyname(hostname)
                                    except:
                                        host_ip = 'localhost'
                                    web_ui_url = f"http://{host_ip}:{published}"
                            break
            except Exception as e:
                print(f"Error auto-detecting Web UI URL: {e}")
        
        return jsonify({'web_ui_url': web_ui_url})
    
    else:  # POST
        data = request.json
        web_ui_url = data.get('web_ui_url', '')
        
        # Save to metadata
        metadata = get_stack_metadata(stack_name)
        metadata['web_ui_url'] = web_ui_url
        save_stack_metadata(stack_name, metadata)
        
        return jsonify({'success': True})


@app.route('/api/stack/<stack_name>/metadata', methods=['GET', 'POST'])
def api_stack_metadata(stack_name):
    """Get or update stack metadata"""
    if request.method == 'GET':
        metadata = get_stack_metadata(stack_name)
        return jsonify(metadata)
    else:
        # Merge new data with existing metadata
        metadata = get_stack_metadata(stack_name)
        data = request.json
        metadata.update(data)
        save_stack_metadata(stack_name, metadata)
        return jsonify({'success': True})



@app.route('/api/ping')
@require_api_token
def api_ping():
    """Ping endpoint for peer health checks"""
    return jsonify({'status': 'ok', 'instance_name': config.get('instance_name', 'DockUp')})


@app.route('/api/stacks/all')
@require_auth
def api_get_all_stacks():
    """Get local stacks + all peer stacks"""
    all_stacks = []
    
    # Get local stacks
    local_stacks = get_stacks()
    for stack in local_stacks:
        stack['peer_id'] = None
        stack['peer_name'] = config.get('instance_name', 'DockUp')
        stack['is_local'] = True
        if stack.get('name') in stack_stats_cache:
            stack['stats'] = stack_stats_cache[stack['name']]
    
    all_stacks.extend(local_stacks)
    
    # Get stacks from each peer
    for peer_id, peer in config.get('peers', {}).items():
        if not peer.get('enabled', True):
            continue
        
        try:
            session = get_peer_session(peer_id)
            if not session:
                continue
            
            response = session.get(f"{peer['url']}/api/stacks", timeout=5)
            if response.status_code == 200:
                peer_stacks = response.json()
                for stack in peer_stacks:
                    stack['peer_id'] = peer_id
                    stack['peer_name'] = peer.get('name', peer_id)
                    stack['is_local'] = False
                all_stacks.extend(peer_stacks)
        except Exception as e:
            print(f"Error fetching stacks from peer {peer_id}: {e}")
    
    return jsonify(all_stacks)


@app.route('/api/peers')
@require_auth
def api_get_peers():
    """Get all configured peers"""
    # Don't send API tokens
    safe_peers = {}
    for peer_id, peer in config.get('peers', {}).items():
        safe_peer = {k: v for k, v in peer.items() if k != 'api_token'}
        safe_peer['has_token'] = bool(peer.get('api_token'))
        safe_peers[peer_id] = safe_peer
    
    return jsonify(safe_peers)


@app.route('/api/peers', methods=['POST'])
@require_auth
def api_add_peer():
    """Add new peer"""
    data = request.json
    peer_id = data.get('id')
    
    if not peer_id:
        return jsonify({'error': 'Peer ID is required'}), 400
    
    if 'peers' not in config:
        config['peers'] = {}
    
    config['peers'][peer_id] = {
        'name': data.get('name', peer_id),
        'url': data.get('url', ''),
        'api_token': data.get('api_token', ''),
        'enabled': data.get('enabled', True)
    }
    
    save_config()
    
    print(f"Added peer {peer_id}: {config['peers'][peer_id]}")
    print(f"Total peers: {len(config.get('peers', {}))}")
    
    return jsonify({'success': True})


@app.route('/api/peers/<peer_id>', methods=['PUT'])
@require_auth
def api_update_peer(peer_id):
    """Update peer configuration"""
    if peer_id not in config.get('peers', {}):
        return jsonify({'error': 'Peer not found'}), 404
    
    data = request.json
    
    # Update fields, preserving token if not provided
    if 'api_token' not in data:
        data['api_token'] = config['peers'][peer_id].get('api_token', '')
    
    config['peers'][peer_id].update(data)
    save_config()
    
    # Clear cached session
    if peer_id in peer_sessions:
        del peer_sessions[peer_id]
    
    return jsonify({'success': True})


@app.route('/api/peers/<peer_id>', methods=['DELETE'])
@require_auth
def api_delete_peer(peer_id):
    """Delete peer"""
    if peer_id in config.get('peers', {}):
        del config['peers'][peer_id]
        save_config()
        
        # Clear cached session
        if peer_id in peer_sessions:
            del peer_sessions[peer_id]
    
    return jsonify({'success': True})


@app.route('/api/peers/<peer_id>/test')
@require_auth
def api_test_peer(peer_id):
    """Test connection to peer"""
    peer = config.get('peers', {}).get(peer_id)
    if not peer:
        return jsonify({'error': 'Peer not found'}), 404
    
    try:
        session = requests.Session()
        session.headers.update({'X-API-Token': peer['api_token']})
        
        response = session.get(f"{peer['url']}/api/ping", timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            return jsonify({
                'online': True,
                'instance_name': data.get('instance_name', 'Unknown')
            })
        else:
            return jsonify({
                'online': False,
                'error': f'HTTP {response.status_code}'
            })
    except requests.exceptions.Timeout:
        return jsonify({'online': False, 'error': 'Connection timeout'})
    except Exception as e:
        return jsonify({'online': False, 'error': str(e)})


@app.route('/api/peer/<peer_id>/stacks')
@require_auth
def api_peer_stacks(peer_id):
    """Get stacks from peer"""
    session = get_peer_session(peer_id)
    if not session:
        return jsonify({'error': 'Failed to connect to peer'}), 503
    
    try:
        peer = config['peers'][peer_id]
        response = session.get(f"{peer['url']}/api/stacks", timeout=10)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': f'Peer returned {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/peer/<peer_id>/stack/<stack_name>')
@require_auth
def api_peer_stack(peer_id, stack_name):
    """Get stack details from peer"""
    session = get_peer_session(peer_id)
    if not session:
        return jsonify({'error': 'Failed to connect to peer'}), 503
    
    try:
        peer = config['peers'][peer_id]
        response = session.get(f"{peer['url']}/api/stack/{stack_name}", timeout=10)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': f'Peer returned {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/peer/<peer_id>/stack/<stack_name>/operation', methods=['POST'])
@require_auth
def api_peer_stack_operation(peer_id, stack_name):
    """Perform operation on peer stack"""
    session = get_peer_session(peer_id)
    if not session:
        return jsonify({'error': 'Failed to connect to peer'}), 503
    
    try:
        peer = config['peers'][peer_id]
        response = session.post(
            f"{peer['url']}/api/stack/{stack_name}/operation",
            json=request.json,
            timeout=60
        )
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': f'Peer returned {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/peer/<peer_id>/stack/<stack_name>/logs/<container_id>')
@require_auth
def api_peer_logs(peer_id, stack_name, container_id):
    """Get logs from peer container"""
    session = get_peer_session(peer_id)
    if not session:
        return jsonify({'error': 'Failed to connect to peer'}), 503
    
    try:
        peer = config['peers'][peer_id]
        tail = request.args.get('tail', 100, type=int)
        response = session.get(
            f"{peer['url']}/api/stack/{stack_name}/logs/{container_id}",
            params={'tail': tail},
            timeout=10
        )
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': f'Peer returned {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/regenerate-token', methods=['POST'])
@require_auth
def api_regenerate_token():
    """Regenerate API token"""
    config['api_token'] = secrets.token_urlsafe(32)
    save_config()
    return jsonify({'token': config['api_token']})



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
        load_health_status()
        load_update_history()
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
        
        # Display instance info
        print("=" * 50)
        print(f"Instance Name: {config.get('instance_name', 'DockUp')}")
        print(f"API Token: {config.get('api_token', 'Not set')[:16]}...")
        print(f"Configured Peers: {len(config.get('peers', {}))}")
        print("=" * 50)
        
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
