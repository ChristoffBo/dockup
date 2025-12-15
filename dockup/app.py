#!/usr/bin/env python3
"""
Dockup - Docker Compose Stack Manager with Auto-Update
Combines Dockge functionality with Tugtainer/Watchtower update capabilities
"""

# VERSION - Update this when releasing new version
DOCKUP_VERSION = "1.1.7"

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
import fcntl
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file, session, redirect, url_for
from functools import wraps
from flask_sock import Sock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from croniter import croniter
import apprise
import pytz

import shlex
import re
from typing import Dict, List, Tuple, Any


# ============================================================================
# Docker Run Command Parser
# ============================================================================

def parse_docker_run_command(command: str) -> Tuple[Dict[str, Any], List[str]]:
    """
    Parse a docker run command and return compose-compatible dict + warnings
    
    Returns:
        (compose_dict, warnings_list)
    """
    warnings = []
    
    # Remove 'docker run' prefix
    command = command.strip()
    if command.startswith('docker run'):
        command = command[10:].strip()
    
    # Parse command with shlex (handles quotes properly)
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise ValueError(f"Invalid command syntax: {e}")
    
    # Initialize compose structure
    compose = {
        'ports': [],
        'volumes': [],
        'environment': {},
        'labels': {},
        'networks': [],
        'extra_hosts': [],
        'cap_add': [],
        'cap_drop': [],
        'devices': [],
    }
    
    named_volumes = set()
    i = 0
    container_name = None
    image = None
    command_args = []
    
    while i < len(tokens):
        token = tokens[i]
        
        # Flags that take no arguments
        if token in ['-d', '--detach', '-i', '--interactive', '-t', '--tty', '--rm']:
            if token == '--rm':
                warnings.append("'--rm' flag ignored (compose containers are persistent)")
            i += 1
            continue
        
        # Flags that take one argument
        if token in ['-p', '--publish']:
            i += 1
            compose['ports'].append(tokens[i])
        
        elif token in ['-v', '--volume']:
            i += 1
            volume = tokens[i]
            compose['volumes'].append(volume)
            # Check if it's a named volume
            if ':' in volume and not volume.startswith('/') and not volume.startswith('.'):
                vol_name = volume.split(':')[0]
                named_volumes.add(vol_name)
        
        elif token in ['-e', '--env']:
            i += 1
            env = tokens[i]
            if '=' in env:
                key, val = env.split('=', 1)
                compose['environment'][key] = val
            else:
                compose['environment'][env] = ''
        
        elif token == '--env-file':
            i += 1
            compose['env_file'] = tokens[i]
        
        elif token in ['--name']:
            i += 1
            container_name = tokens[i]
        
        elif token in ['--network']:
            i += 1
            compose['networks'].append(tokens[i])
        
        elif token in ['--restart']:
            i += 1
            compose['restart'] = tokens[i]
        
        elif token in ['-l', '--label']:
            i += 1
            label = tokens[i]
            if '=' in label:
                key, val = label.split('=', 1)
                compose['labels'][key] = val
        
        elif token in ['--hostname', '-h']:
            i += 1
            compose['hostname'] = tokens[i]
        
        elif token == '--add-host':
            i += 1
            compose['extra_hosts'].append(tokens[i])
        
        elif token == '--cap-add':
            i += 1
            compose['cap_add'].append(tokens[i])
        
        elif token == '--cap-drop':
            i += 1
            compose['cap_drop'].append(tokens[i])
        
        elif token == '--device':
            i += 1
            compose['devices'].append(tokens[i])
        
        elif token in ['--user', '-u']:
            i += 1
            compose['user'] = tokens[i]
        
        elif token in ['--workdir', '-w']:
            i += 1
            compose['working_dir'] = tokens[i]
        
        elif token == '--entrypoint':
            i += 1
            compose['entrypoint'] = tokens[i]
        
        elif token == '--privileged':
            compose['privileged'] = True
            warnings.append("'--privileged' detected - SECURITY RISK! Consider removing.")
        
        elif token == '--read-only':
            compose['read_only'] = True
        
        elif token in ['--memory', '-m']:
            i += 1
            if 'deploy' not in compose:
                compose['deploy'] = {'resources': {'limits': {}}}
            compose['deploy']['resources']['limits']['memory'] = tokens[i]
        
        elif token == '--cpus':
            i += 1
            if 'deploy' not in compose:
                compose['deploy'] = {'resources': {'limits': {}}}
            compose['deploy']['resources']['limits']['cpus'] = tokens[i]
        
        elif token == '--health-cmd':
            i += 1
            if 'healthcheck' not in compose:
                compose['healthcheck'] = {}
            compose['healthcheck']['test'] = ['CMD-SHELL', tokens[i]]
        
        elif token == '--health-interval':
            i += 1
            if 'healthcheck' not in compose:
                compose['healthcheck'] = {}
            compose['healthcheck']['interval'] = tokens[i]
        
        elif token == '--health-timeout':
            i += 1
            if 'healthcheck' not in compose:
                compose['healthcheck'] = {}
            compose['healthcheck']['timeout'] = tokens[i]
        
        elif token == '--health-retries':
            i += 1
            if 'healthcheck' not in compose:
                compose['healthcheck'] = {}
            compose['healthcheck']['retries'] = int(tokens[i])
        
        elif token.startswith('-'):
            # Unknown flag - skip it and next token if it doesn't start with -
            warnings.append(f"Unsupported flag '{token}' ignored")
            i += 1
            if i < len(tokens) and not tokens[i].startswith('-'):
                i += 1
            continue
        
        else:
            # This should be the image name
            if not image:
                image = token
            else:
                # Everything after image is the command
                command_args.append(token)
        
        i += 1
    
    # Build final compose structure
    if not image:
        raise ValueError("No image specified in docker run command")
    
    # Use container name as service name, or derive from image
    if container_name:
        service_name = container_name
        compose['container_name'] = container_name
    else:
        # Derive service name from image (e.g., 'nginx:latest' -> 'nginx')
        service_name = image.split(':')[0].split('/')[-1]
    
    compose['image'] = image
    
    # Add command if present
    if command_args:
        compose['command'] = command_args
    
    # Clean up empty lists/dicts
    compose = {k: v for k, v in compose.items() if v}
    
    # Build full compose yaml structure
    compose_yaml = {
        'services': {
            service_name: compose
        }
    }
    
    # Add named volumes section if any
    if named_volumes:
        compose_yaml['volumes'] = {vol: {} for vol in named_volumes}
    
    return compose_yaml, warnings


# ============================================================================
# App Data Root - Opinionated, Safe by Design
# ============================================================================

def resolve_bind_mount_path(volume_spec: str, app_data_root: str, stack_name: str) -> Tuple[str, List[str]]:
    """
    Resolve volume spec using app_data_root/stack_name structure
    
    Args:
        volume_spec: Volume specification (e.g., "./config:/config")
        app_data_root: Root directory (e.g., "/DATA/AppData")
        stack_name: Name of the stack (e.g., "plex")
    
    Returns:
        (resolved_spec, warnings)
    """
    warnings = []
    
    if not app_data_root or not app_data_root.strip():
        return volume_spec, warnings
    
    app_data_root = app_data_root.rstrip('/')
    stack_base = f"{app_data_root}/{stack_name}"
    
    # Parse volume spec
    parts = volume_spec.split(':')
    if len(parts) == 1:
        return volume_spec, warnings  # Named volume
    
    source = parts[0]
    target = parts[1] if len(parts) > 1 else ''
    mode = parts[2] if len(parts) > 2 else ''
    
    # Named volume check (no slash in source)
    if '/' not in source:
        return volume_spec, warnings
    
    # Check if already under correct stack path
    if source.startswith(stack_base + '/'):
        return volume_spec, warnings  # Already correct
    
    # Check if under app_data_root but different stack (intentional cross-stack sharing)
    if source.startswith(app_data_root + '/'):
        return volume_spec, warnings
    
    # Handle relative paths
    if source.startswith('./') or source.startswith('../') or not source.startswith('/'):
        # Remove leading ./
        clean_path = source.lstrip('./')
        # Build path: /DATA/AppData/{stack_name}/{path}
        source = f"{stack_base}/{clean_path}"
    
    # Handle absolute paths
    elif source.startswith('/'):
        # Check system exemptions
        exempt_paths = [
            '/etc/localtime',
            '/var/run/docker.sock',
            '/dev/',
            '/sys/',
            '/proc/',
            '/tmp'
        ]
        
        is_exempt = any(source.startswith(prefix) for prefix in exempt_paths)
        
        if not is_exempt:
            warnings.append(f"Bind mount '{source}' is outside App Data Root '{app_data_root}'")
    
    # Rebuild volume spec
    result_parts = [source, target]
    if mode:
        result_parts.append(mode)
    
    return ':'.join(result_parts), warnings


def apply_app_data_root_to_compose(compose_content: str, app_data_root: str, stack_name: str) -> Tuple[str, List[str]]:
    """
    Apply app_data_root ONLY to volumes section
    
    Everything else in the compose file is left completely untouched
    """
    warnings = []
    
    if not app_data_root or not app_data_root.strip():
        return compose_content, warnings
    
    try:
        # Parse YAML
        compose_data = yaml.safe_load(compose_content)
        
        if not compose_data or 'services' not in compose_data:
            return compose_content, warnings
        
        # Process ONLY the volumes section of each service
        services = compose_data.get('services', {})
        for service_name, service_config in services.items():
            
            # Skip services without volumes
            if 'volumes' not in service_config:
                continue
            
            # Get volumes list
            volumes = service_config['volumes']
            if not isinstance(volumes, list):
                continue
            
            # Process each volume
            resolved_volumes = []
            for volume_spec in volumes:
                if isinstance(volume_spec, str):
                    # Short-form: "./config:/config"
                    resolved_spec, vol_warnings = resolve_bind_mount_path(
                        volume_spec, 
                        app_data_root, 
                        stack_name
                    )
                    resolved_volumes.append(resolved_spec)
                    warnings.extend(vol_warnings)
                    
                elif isinstance(volume_spec, dict):
                    # Long-form: {type: bind, source: "./config", target: "/config"}
                    if volume_spec.get('type') == 'bind':
                        source = volume_spec.get('source', '')
                        target = volume_spec.get('target', '')
                        
                        # Create temp spec for resolution
                        temp_spec = f"{source}:{target}"
                        resolved_spec, vol_warnings = resolve_bind_mount_path(
                            temp_spec, 
                            app_data_root, 
                            stack_name
                        )
                        
                        # Update only the source
                        volume_spec['source'] = resolved_spec.split(':')[0]
                        warnings.extend(vol_warnings)
                    
                    resolved_volumes.append(volume_spec)
                else:
                    # Unknown format - keep as-is
                    resolved_volumes.append(volume_spec)
            
            # Update ONLY the volumes section
            service_config['volumes'] = resolved_volumes
        
        # Convert back to YAML
        modified_content = yaml.dump(compose_data, default_flow_style=False, sort_keys=False)
        return modified_content, warnings
    
    except Exception as e:
        # If anything goes wrong, return original unchanged
        warnings.append(f"Error processing volumes: {str(e)}")
        return compose_content, warnings


def analyze_app_data_root_changes(compose_content: str, app_data_root: str, stack_name: str) -> Dict[str, Any]:
    """
    Analyze what would change without actually modifying content
    
    Returns:
    {
        'modified': [{'from': '...', 'to': '...'}],
        'unchanged': [{'path': '...', 'reason': '...'}],
        'warnings': [{'path': '...', 'warning': '...'}]
    }
    """
    result = {
        'modified': [],
        'unchanged': [],
        'warnings': []
    }
    
    if not app_data_root or not app_data_root.strip():
        return result
    
    try:
        compose_data = yaml.safe_load(compose_content)
        if not compose_data or 'services' not in compose_data:
            return result
        
        app_data_root = app_data_root.rstrip('/')
        stack_base = f"{app_data_root}/{stack_name}"
        
        for service_name, service_config in compose_data.get('services', {}).items():
            if 'volumes' not in service_config:
                continue
            
            for volume_spec in service_config['volumes']:
                if not isinstance(volume_spec, str):
                    continue
                
                # Parse volume
                parts = volume_spec.split(':')
                if len(parts) < 2:
                    result['unchanged'].append({
                        'path': volume_spec,
                        'reason': 'named_volume'
                    })
                    continue
                
                source = parts[0]
                
                # Named volume check
                if '/' not in source:
                    result['unchanged'].append({
                        'path': volume_spec,
                        'reason': 'named_volume'
                    })
                    continue
                
                # Already under app_data_root
                if source.startswith(app_data_root + '/'):
                    result['unchanged'].append({
                        'path': volume_spec,
                        'reason': 'already_under_root'
                    })
                    continue
                
                # System path exemption
                exempt_paths = ['/etc/localtime', '/var/run/docker.sock', '/dev/', '/sys/', '/proc/', '/tmp']
                if any(source.startswith(prefix) for prefix in exempt_paths):
                    result['unchanged'].append({
                        'path': volume_spec,
                        'reason': 'system_path'
                    })
                    continue
                
                # Relative path - will be modified
                if source.startswith('./') or source.startswith('../') or not source.startswith('/'):
                    resolved_spec, _ = resolve_bind_mount_path(volume_spec, app_data_root, stack_name)
                    result['modified'].append({
                        'from': volume_spec,
                        'to': resolved_spec
                    })
                    continue
                
                # Absolute path outside root - warning
                if source.startswith('/'):
                    result['warnings'].append({
                        'path': volume_spec,
                        'warning': f"Outside App Data Root '{app_data_root}'"
                    })
                    continue
        
        return result
    
    except Exception as e:
        raise Exception(f"Failed to analyze changes: {str(e)}")


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
TRIVY_CACHE_DIR = os.path.join(DATA_DIR, 'trivy-cache')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
SCHEDULES_FILE = os.path.join(DATA_DIR, 'schedules.json')
HEALTH_STATUS_FILE = os.path.join(DATA_DIR, 'health_status.json')
UPDATE_HISTORY_FILE = os.path.join(DATA_DIR, 'update_history.json')
PASSWORD_FILE = os.path.join(DATA_DIR, 'password.hash')
SCAN_RESULTS_FILE = os.path.join(DATA_DIR, 'scan_results.json')


# Ensure directories exist
Path(STACKS_DIR).mkdir(parents=True, exist_ok=True)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(TRIVY_CACHE_DIR).mkdir(parents=True, exist_ok=True)
print(f"Trivy cache directory: {TRIVY_CACHE_DIR}")
# Docker client
docker_client = docker.from_env()

# Global state
config = {}
schedules = {}
update_status = {}
health_status = {}  # Track health failures: {stack_name: {consecutive_failures: 0, last_notified: timestamp}}
update_history = {}  # Track recent updates: {stack_name: {last_update: timestamp, updated_by: 'auto'|'manual'}}
host_monitor_status = {  # Track host monitoring failures
    'consecutive_failures': 0,
    'last_notified': None,
    'last_check': None,
    'current_issues': []
}
websocket_clients = []
stack_stats_cache = {}  # Cache for stack stats
network_stats_cache = {}  # Cache for network bytes tracking (for rate calculation)
peer_sessions = {}  # Cache for peer authentication sessions
scheduler = None  # Global APScheduler instance

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
            'peers': {},
            'host_stats_interval': 5,  # seconds between host stats polling (0 = off)
            'stack_refresh_interval': 10,  # seconds between stack refresh (0 = off)
            'app_data_root': '/DATA/AppData',  # Root directory for container bind mounts
            'auto_apply_app_data_root': False,  # Require confirmation before applying
            # Host monitoring thresholds
            'host_monitor_enabled': True,
            'host_monitor_interval': 5,  # minutes between host checks
            'host_cpu_threshold': 90,  # % CPU usage to alert on
            'host_memory_threshold': 90,  # % RAM usage to alert on
            'host_disk_threshold': 85,  # % Disk usage to alert on
            'host_alert_threshold': 3,  # consecutive failures before alerting
        }
        save_config()
    
    
    # Add missing fields to existing configs
    if 'instance_name' not in config:
        config['instance_name'] = 'DockUp'
    if 'api_token' not in config:
        config['api_token'] = secrets.token_urlsafe(32)
    if 'peers' not in config:
        config['peers'] = {}
    if 'host_stats_interval' not in config:
        config['host_stats_interval'] = 5
    if 'stack_refresh_interval' not in config:
        config['stack_refresh_interval'] = 10
    if 'app_data_root' not in config:
        config['app_data_root'] = '/DATA/AppData'
    if 'auto_apply_app_data_root' not in config:
        config['auto_apply_app_data_root'] = False
        save_config()  # Save to persist the new default
    if 'host_monitor_enabled' not in config:
        config['host_monitor_enabled'] = True
    if 'host_monitor_interval' not in config:
        config['host_monitor_interval'] = 5
    if 'host_cpu_threshold' not in config:
        config['host_cpu_threshold'] = 90
    if 'host_memory_threshold' not in config:
        config['host_memory_threshold'] = 90
    if 'host_disk_threshold' not in config:
        config['host_disk_threshold'] = 85
    if 'host_alert_threshold' not in config:
        config['host_alert_threshold'] = 3
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
    """Save schedules to file with file locking"""
    try:
        with open(SCHEDULES_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(schedules, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"Error saving schedules: {e}")


def load_health_status():
    """Load health status tracking from file"""
    global health_status
    if os.path.exists(HEALTH_STATUS_FILE):
        with open(HEALTH_STATUS_FILE, 'r') as f:
            health_status = json.load(f)
    else:
        health_status = {}


def save_health_status():
    """Save health status tracking to file with file locking"""
    try:
        with open(HEALTH_STATUS_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(health_status, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"Error saving health status: {e}")


def load_update_history():
    """Load update history from file"""
    global update_history
    if os.path.exists(UPDATE_HISTORY_FILE):
        with open(UPDATE_HISTORY_FILE, 'r') as f:
            update_history = json.load(f)
    else:
        update_history = {}


def save_update_history():
    """Save update history to file with file locking"""
    try:
        with open(UPDATE_HISTORY_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(update_history, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"Error saving update history: {e}")


def get_update_status_file():
    """Get per-instance update status filename"""
    instance_name = config.get('instance_name', 'DockUp').replace(' ', '_').replace('/', '_')
    return os.path.join(DATA_DIR, f'update_status_{instance_name}.json')


def cleanup_old_status_files():
    """Remove orphaned update_status_*.json files when instance name changes"""
    try:
        current_file = get_update_status_file()
        for filename in os.listdir(DATA_DIR):
            if filename.startswith('update_status_') and filename.endswith('.json'):
                filepath = os.path.join(DATA_DIR, filename)
                if filepath != current_file:
                    try:
                        os.remove(filepath)
                        print(f"Cleaned up orphaned status file: {filename}")
                    except:
                        pass
    except Exception as e:
        print(f"Error cleaning up status files: {e}")


def load_update_status():
    """Load update status from per-instance file"""
    global update_status
    status_file = get_update_status_file()
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r') as f:
                update_status = json.load(f)
        except:
            update_status = {}
    else:
        update_status = {}
    
    # Clean up old files on load
    cleanup_old_status_files()


def save_update_status():
    """Save update status to per-instance file with file locking"""
    try:
        status_file = get_update_status_file()
        with open(status_file, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(update_status, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"Error saving update status: {e}")


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


def check_docker_hub_rate_limit():
    """Check Docker Hub rate limit status"""
    try:
        # Get authentication token
        auth_url = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:ratelimitpreview/test:pull"
        
        # Try to use Docker Hub credentials if available
        docker_config_path = os.path.expanduser('~/.docker/config.json')
        username = None
        password = None
        
        if os.path.exists(docker_config_path):
            try:
                with open(docker_config_path, 'r') as f:
                    docker_config = json.load(f)
                    auths = docker_config.get('auths', {})
                    if 'https://index.docker.io/v1/' in auths:
                        # Credentials are stored, but typically base64 encoded
                        # For simplicity, we'll make anonymous request
                        pass
            except:
                pass
        
        # Get token (anonymous)
        response = requests.get(auth_url, timeout=5)
        if response.status_code != 200:
            return None
        
        token = response.json().get('token')
        if not token:
            return None
        
        # Make HEAD request to check rate limit (doesn't count against quota)
        headers = {'Authorization': f'Bearer {token}'}
        response = requests.head(
            'https://registry-1.docker.io/v2/ratelimitpreview/test/manifests/latest',
            headers=headers,
            timeout=5
        )
        
        # Parse rate limit headers
        limit_header = response.headers.get('ratelimit-limit', '')
        remaining_header = response.headers.get('ratelimit-remaining', '')
        source_header = response.headers.get('docker-ratelimit-source', '')
        
        if not limit_header or not remaining_header:
            # No headers means unlimited (paid account or whitelisted)
            return {
                'limit': None,
                'remaining': None,
                'percentage': 100,
                'source': source_header or 'unknown',
                'unlimited': True
            }
        
        # Parse "100;w=21600" format
        limit = int(limit_header.split(';')[0]) if ';' in limit_header else int(limit_header)
        remaining = int(remaining_header.split(';')[0]) if ';' in remaining_header else int(remaining_header)
        
        percentage = (remaining / limit * 100) if limit > 0 else 0
        
        return {
            'limit': limit,
            'remaining': remaining,
            'percentage': round(percentage, 1),
            'source': source_header or 'unknown',
            'unlimited': False
        }
        
    except Exception as e:
        print(f"Error checking Docker Hub rate limit: {e}")
        return None


# Vulnerability scanning with Trivy
vulnerability_scan_cache = {}  # Cache scan results: {image_name: {scan_data, timestamp}}


def scan_image_vulnerabilities(image_name):
    """Scan Docker image for vulnerabilities using Trivy"""
    try:
        print(f"Scanning image: {image_name}")
        
        # Run Trivy scan with custom cache dir
        env = os.environ.copy()
        env['TRIVY_CACHE_DIR'] = TRIVY_CACHE_DIR
        
        result = subprocess.run(
            ['trivy', 'image', '--format', 'json', '--quiet', image_name],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            env=env
        )
        
        if result.returncode > 1:
            # Exit code > 1 means execution error
            print(f"Trivy execution error for {image_name}: {result.stderr}")
            return None
        
        # Parse JSON output
        scan_data = json.loads(result.stdout)
        
        # Count vulnerabilities by severity
        severity_counts = {
            'CRITICAL': 0,
            'HIGH': 0,
            'MEDIUM': 0,
            'LOW': 0,
            'UNKNOWN': 0
        }
        
        vulnerabilities = []
        
        # Extract vulnerabilities from all results
        for item in scan_data.get('Results', []):
            for vuln in item.get('Vulnerabilities', []):
                severity = vuln.get('Severity', 'UNKNOWN')
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
                
                vulnerabilities.append({
                    'cve_id': vuln.get('VulnerabilityID', 'N/A'),
                    'package': vuln.get('PkgName', 'N/A'),
                    'installed_version': vuln.get('InstalledVersion', 'N/A'),
                    'fixed_version': vuln.get('FixedVersion', 'Not available'),
                    'severity': severity,
                    'title': vuln.get('Title', 'No title'),
                    'description': vuln.get('Description', 'No description')[:200]  # Truncate long descriptions
                })
        
        # Determine badge color
        if severity_counts['CRITICAL'] > 0 or severity_counts['HIGH'] > 0:
            badge = 'red'
        elif severity_counts['MEDIUM'] > 0:
            badge = 'orange'
        else:
            badge = 'green'
        
        scan_result = {
            'image': image_name,
            'scanned_at': datetime.now(pytz.UTC).isoformat(),
            'badge': badge,
            'severity_counts': severity_counts,
            'total_vulnerabilities': sum(severity_counts.values()),
            'vulnerabilities': vulnerabilities[:100]  # Limit to first 100 for UI
        }
        
        # Cache the result
        vulnerability_scan_cache[image_name] = {
            'data': scan_result,
            'timestamp': time.time()
        }
        
        save_scan_results()
        
        return scan_result
        
    except subprocess.TimeoutExpired:
        print(f"Trivy scan timeout for {image_name}")
        return None
    except json.JSONDecodeError as e:
        print(f"Failed to parse Trivy output for {image_name}: {e}")
        return None
    except Exception as e:
        print(f"Error scanning {image_name}: {e}")
        return None


def get_cached_scan(image_name, max_age=3600):
    """Get cached scan result if available and not too old"""
    if image_name in vulnerability_scan_cache:
        cached = vulnerability_scan_cache[image_name]
        age = time.time() - cached['timestamp']
        if age < max_age:  # Cache valid for 1 hour by default
            return cached['data']
    return None



def load_scan_results():
    """Load persistent scan results"""
    global vulnerability_scan_cache
    if os.path.exists(SCAN_RESULTS_FILE):
        try:
            with open(SCAN_RESULTS_FILE, 'r') as f:
                saved = json.load(f)
                for image, data in saved.items():
                    vulnerability_scan_cache[image] = {
                        'data': data['data'],
                        'timestamp': data['timestamp']
                    }
            print(f"Loaded {len(vulnerability_scan_cache)} cached scans")
        except Exception as e:
            print(f"Error loading scan results: {e}")


def save_scan_results():
    """Save scan results to disk"""
    try:
        saved = {}
        for image, cache_data in vulnerability_scan_cache.items():
            saved[image] = {
                'data': cache_data['data'],
                'timestamp': cache_data['timestamp']
            }
        with open(SCAN_RESULTS_FILE, 'w') as f:
            json.dump(saved, f, indent=2)
    except Exception as e:
        print(f"Error saving scan results: {e}")



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


def check_host_health():
    """Monitor host system resources and send notifications after N consecutive failures"""
    global host_monitor_status
    
    if not config.get('host_monitor_enabled', True):
        return
    
    try:
        # Get current host stats
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Get thresholds from config
        cpu_threshold = config.get('host_cpu_threshold', 90)
        memory_threshold = config.get('host_memory_threshold', 90)
        disk_threshold = config.get('host_disk_threshold', 85)
        alert_threshold = config.get('host_alert_threshold', 3)
        
        # Check for issues
        issues = []
        if cpu_percent >= cpu_threshold:
            issues.append(f"CPU usage at {cpu_percent:.1f}% (threshold: {cpu_threshold}%)")
        if memory.percent >= memory_threshold:
            issues.append(f"Memory usage at {memory.percent:.1f}% (threshold: {memory_threshold}%)")
        if disk.percent >= disk_threshold:
            issues.append(f"Disk usage at {disk.percent:.1f}% (threshold: {disk_threshold}%)")
        
        current_time = datetime.now(pytz.UTC).isoformat()
        
        if issues:
            # Increment failure counter
            host_monitor_status['consecutive_failures'] += 1
            host_monitor_status['last_check'] = current_time
            host_monitor_status['current_issues'] = issues
            
            failures = host_monitor_status['consecutive_failures']
            print(f"  ⚠ Host check {failures}/{alert_threshold}: {len(issues)} issue(s) detected")
            
            # Send notification after threshold reached
            if failures >= alert_threshold:
                last_notified = host_monitor_status.get('last_notified')
                
                # Only notify once until it recovers
                if last_notified is None:
                    issue_list = '\n'.join([f"  • {issue}" for issue in issues])
                    instance_name = config.get('instance_name', 'DockUp')
                    send_notification(
                        f"{instance_name} - Host Alert",
                        f"Host system has exceeded thresholds for {failures} consecutive checks.\n\nIssues detected:\n{issue_list}\n\nCurrent stats:\n  • CPU: {cpu_percent:.1f}%\n  • Memory: {memory.percent:.1f}%\n  • Disk: {disk.percent:.1f}%",
                        'error'
                    )
                    host_monitor_status['last_notified'] = current_time
                    
                    broadcast_ws({
                        'type': 'host_alert',
                        'issues': issues,
                        'cpu_percent': round(cpu_percent, 1),
                        'memory_percent': round(memory.percent, 1),
                        'disk_percent': round(disk.percent, 1)
                    })
        else:
            # Host is healthy - reset counter
            if host_monitor_status['consecutive_failures'] > 0:
                # Was unhealthy, now recovered
                if host_monitor_status.get('last_notified'):
                    instance_name = config.get('instance_name', 'DockUp')
                    send_notification(
                        f"{instance_name} - Host Recovered",
                        f"Host system resources are now back to normal.\n\nCurrent stats:\n  • CPU: {cpu_percent:.1f}%\n  • Memory: {memory.percent:.1f}%\n  • Disk: {disk.percent:.1f}%",
                        'success'
                    )
                    broadcast_ws({
                        'type': 'host_recovered',
                        'cpu_percent': round(cpu_percent, 1),
                        'memory_percent': round(memory.percent, 1),
                        'disk_percent': round(disk.percent, 1)
                    })
                
                host_monitor_status = {
                    'consecutive_failures': 0,
                    'last_notified': None,
                    'last_check': current_time,
                    'current_issues': []
                }
    
    except Exception as e:
        print(f"Error checking host health: {e}")


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

            # Mark container as imported (modern docker-py 6.0+ way)
            try:
                container.update(labels={
                    'com.docker.compose.project': stack_name,
                    'dockup.imported': 'true'
                })
                container.reload()
            except Exception as e:
                print(f"[Dockup] Warning: Could not label container {container.name}: {e}")

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


def save_stack_compose(stack_name, content, apply_app_data_root=None):
    """
    Save compose file content for a stack with optional app_data_root resolution
    
    Args:
        stack_name: Name of the stack
        content: YAML content to save
        apply_app_data_root: True/False to override auto setting, None to use config
    
    Returns:
        warnings: List of warnings from app_data_root processing
    """
    stack_path = os.path.join(STACKS_DIR, stack_name)
    Path(stack_path).mkdir(parents=True, exist_ok=True)
    
    warnings = []
    
    # Determine if we should apply app_data_root
    should_apply = apply_app_data_root
    if should_apply is None:
        should_apply = config.get('auto_apply_app_data_root', False)
    
    # Apply app_data_root if configured and enabled
    app_data_root = config.get('app_data_root', '').strip()
    if app_data_root and should_apply:
        content, warnings = apply_app_data_root_to_compose(content, app_data_root, stack_name)
        
        # Log warnings
        if warnings:
            print(f"App Data Root warnings for {stack_name}:")
            for warning in warnings:
                print(f"  - {warning}")
    
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
    
    return warnings



def stack_operation(stack_name, operation):
    """Perform operation on stack (up/down/stop/restart/pull) with real-time output"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    if not os.path.exists(compose_file):
        compose_file = os.path.join(stack_path, 'docker-compose.yml')
    
    try:
        if operation == 'up':
            cmd = ['docker', 'compose', '-f', compose_file, 'up', '-d', '--remove-orphans']
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
    # Read schedule from FILE (not global dict) to avoid thread issues
    schedule_info = {}
    try:
        if os.path.exists(SCHEDULES_FILE):
            with open(SCHEDULES_FILE, 'r') as f:
                all_schedules = json.load(f)
                schedule_info = all_schedules.get(stack_name, {})
    except Exception as e:
        print(f"[{stack_name}] ERROR reading schedules file: {e}")
        return
    
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
        
        # Store current image IDs - use the ACTUAL running image, not tag reference
        old_image_ids = {}
        for container in containers:
            try:
                if container.image.tags:
                    image_name = container.image.tags[0]
                    # Try to get the SHA256 of what's ACTUALLY running
                    try:
                        running_image_sha = container.attrs['Image']
                        old_image_ids[image_name] = running_image_sha
                        print(f"  Current {container.name}: running {running_image_sha[:19]}")
                    except:
                        # Fallback to tag reference if attrs fail
                        old_image_ids[image_name] = container.image.id
                        print(f"  Current {container.name}: {container.image.id[:12]} (fallback)")
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
        'last_check': datetime.now(pytz.UTC).isoformat(),
        'update_available': images_changed
    }
    save_update_status()  # SAVE TO FILE
    
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
                }
                save_update_status()  # SAVE TO FILE
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
            'last_check': datetime.now(pytz.UTC).isoformat(),
            'update_available': update_available
        }
        save_update_status()  # SAVE TO FILE
        
        return True, update_available
        
    except Exception as e:
        print(f"✗ Error checking updates for {stack_name}: {e}")
        return False, None


def setup_scheduler():
    """Setup or refresh APScheduler without recreating it"""
    global scheduler
    
    # Get user timezone
    user_tz = pytz.timezone(config.get('timezone', 'UTC'))

    # Initialize scheduler ONLY once
    if scheduler is None:
        executors = {
            'default': APThreadPoolExecutor(50)
        }
        job_defaults = {
            'coalesce': False,
            'max_instances': 1
        }
        scheduler = BackgroundScheduler(
            executors=executors,
            job_defaults=job_defaults,
            timezone=user_tz
        )
        scheduler.start()
        print("[Scheduler] Created and started with 50 worker threads")
    else:
        # Update scheduler timezone if needed
        try:
            scheduler.configure(timezone=user_tz)
        except:
            pass
        print("[Scheduler] Refreshing jobs")

    # Remove only update jobs safely
    for job in list(scheduler.get_jobs()):
        try:
            if job.id.startswith("update_") or job.id in ("auto_prune", "import_orphans"):
                scheduler.remove_job(job.id)
        except:
            pass

    # Recreate update schedules
    for stack_name, schedule_info in schedules.items():
        mode = schedule_info.get("mode", "off")
        if mode != "off":
            cron_expr = schedule_info.get("cron", config.get("default_cron", "0 2 * * *"))
            try:
                trigger = CronTrigger.from_crontab(cron_expr, timezone=user_tz)
                scheduler.add_job(
                    auto_update_stack,
                    trigger,
                    args=[stack_name],
                    id=f"update_{stack_name}",
                    replace_existing=True
                )
                print(f"[Scheduler] Added update job for {stack_name}: {cron_expr}")
            except Exception as e:
                print(f"[Scheduler] Error scheduling {stack_name}: {e}")

    # Auto-prune job
    if config.get("auto_prune", False):
        prune_cron = config.get("auto_prune_cron", "0 3 * * 0")
        try:
            trigger = CronTrigger.from_crontab(prune_cron, timezone=user_tz)
            scheduler.add_job(
                auto_prune_images,
                trigger,
                id="auto_prune",
                replace_existing=True
            )
            print(f"[Scheduler] Added auto-prune job: {prune_cron}")
        except Exception as e:
            print(f"[Scheduler] Error scheduling auto-prune: {e}")

    # Orphan importer
    try:
        scheduler.add_job(
            import_orphan_containers_as_stacks,
            "interval",
            minutes=15,
            id="import_orphans",
            replace_existing=True
        )
        print("[Scheduler] Added orphan importer job (15m)")
    except Exception as e:
        print(f"[Scheduler] Error adding orphan importer: {e}")

    # Host monitoring job
    if config.get("host_monitor_enabled", True):
        host_interval = config.get("host_monitor_interval", 5)
        try:
            scheduler.add_job(
                check_host_health,
                "interval",
                minutes=host_interval,
                id="host_monitor",
                replace_existing=True
            )
            print(f"[Scheduler] Added host monitoring job ({host_interval}m)")
        except Exception as e:
            print(f"[Scheduler] Error adding host monitoring: {e}")

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


@app.route('/api/docker-hub/rate-limit')
def api_docker_hub_rate_limit():
    """Get Docker Hub rate limit status"""
    rate_limit = check_docker_hub_rate_limit()
    if rate_limit:
        return jsonify(rate_limit)
    return jsonify({'error': 'Could not check rate limit'}), 500


@app.route('/api/container/<container_id>/badge')
@require_auth
def api_container_badge(container_id):
    """Get security badge for a container"""
    try:
        container = docker_client.containers.get(container_id)
        image_name = container.image.tags[0] if container.image.tags else container.image.id
        
        # Check if we have a scan for this image
        cached = get_cached_scan(image_name, max_age=86400*7)  # 7 days
        
        if not cached:
            return jsonify({'scanned': False})
        
        return jsonify({
            'scanned': True,
            'badge': cached.get('badge'),
            'critical': cached.get('severity_counts', {}).get('CRITICAL', 0),
            'high': cached.get('severity_counts', {}).get('HIGH', 0),
            'medium': cached.get('severity_counts', {}).get('MEDIUM', 0),
            'scanned_at': cached.get('scanned_at')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/container/<container_id>/scan', methods=['POST'])
@require_auth
def api_container_scan(container_id):
    """Scan container image for vulnerabilities"""
    try:
        container = docker_client.containers.get(container_id)
        image_name = container.image.tags[0] if container.image.tags else container.image.id
        
        # Check cache first
        cached = get_cached_scan(image_name)
        if cached:
            return jsonify(cached)
        
        # Run scan in background thread
        def scan_thread():
            scan_image_vulnerabilities(image_name)
        
        threading.Thread(target=scan_thread, daemon=True).start()
        
        return jsonify({
            'status': 'scanning',
            'message': 'Vulnerability scan started',
            'image': image_name
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/container/<container_id>/scan-status')
@require_auth
def api_container_scan_status(container_id):
    """Get vulnerability scan status/results for container"""
    try:
        container = docker_client.containers.get(container_id)
        image_name = container.image.tags[0] if container.image.tags else container.image.id
        
        # Check if scan is cached
        cached = get_cached_scan(image_name, max_age=86400)  # 24 hour cache
        if cached:
            return jsonify(cached)
        
        return jsonify({
            'status': 'not_scanned',
            'image': image_name
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/security/stats')
@require_auth
def api_security_stats():
    """Get overall security statistics across all containers"""
    try:
        stacks = get_stacks()
        total_containers = 0
        scanned_containers = 0
        vulnerable_containers = 0
        critical_vulns = 0
        high_vulns = 0
        
        for stack in stacks:
            for container in stack.get('containers', []):
                total_containers += 1
                container_id = container.get('id')
                
                if container_id:
                    try:
                        docker_container = docker_client.containers.get(container_id)
                        image_name = docker_container.image.tags[0] if docker_container.image.tags else docker_container.image.id
                        
                        cached = get_cached_scan(image_name, max_age=86400)
                        if cached:
                            scanned_containers += 1
                            severity = cached.get('severity_counts', {})
                            
                            if cached.get('badge') in ['red', 'orange']:
                                vulnerable_containers += 1
                            
                            critical_vulns += severity.get('CRITICAL', 0)
                            high_vulns += severity.get('HIGH', 0)
                    except:
                        pass
        
        return jsonify({
            'total_containers': total_containers,
            'scanned_containers': scanned_containers,
            'vulnerable_containers': vulnerable_containers,
            'critical_vulnerabilities': critical_vulns,
            'high_vulnerabilities': high_vulns,
            'scan_coverage': round((scanned_containers / total_containers * 100) if total_containers > 0 else 0, 1)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400





@app.route('/api/stack/<stack_name>/scan-summary')
@require_auth
def api_stack_scan_summary(stack_name):
    """Get scan summary for all containers in a stack"""
    try:
        stacks = get_stacks()
        stack = next((s for s in stacks if s['name'] == stack_name), None)
        
        if not stack:
            return jsonify({'error': 'Stack not found'}), 404
        
        worst_badge = 'gray'
        total_critical = 0
        total_high = 0
        scanned_count = 0
        last_scan = None
        
        for container in stack.get('containers', []):
            container_id = container.get('id')
            if container_id:
                try:
                    docker_container = docker_client.containers.get(container_id)
                    image_name = docker_container.image.tags[0] if docker_container.image.tags else docker_container.image.id
                    
                    cached = get_cached_scan(image_name, max_age=86400*7)
                    if cached:
                        scanned_count += 1
                        badge = cached.get('badge', 'gray')
                        
                        # Priority: red > orange > green > gray
                        if badge == 'red':
                            worst_badge = 'red'
                        elif badge == 'orange' and worst_badge != 'red':
                            worst_badge = 'orange'
                        elif badge == 'green' and worst_badge not in ['red', 'orange']:
                            worst_badge = 'green'
                        elif worst_badge == 'gray':
                            worst_badge = badge
                        
                        severity = cached.get('severity_counts', {})
                        total_critical += severity.get('CRITICAL', 0)
                        total_high += severity.get('HIGH', 0)
                        
                        scan_time = cached.get('scanned_at')
                        if scan_time and (not last_scan or scan_time > last_scan):
                            last_scan = scan_time
                except:
                    pass
        
        result = {
            'badge': worst_badge,
            'critical_count': total_critical,
            'high_count': total_high,
            'scanned_containers': scanned_count,
            'total_containers': len(stack.get('containers', [])),
            'last_scan': last_scan
        }
        print(f"[SCAN SUMMARY] Stack: {stack_name}, Badge: {worst_badge}, Critical: {total_critical}, High: {total_high}")
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
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
    """Save stack compose file content with validation and app_data_root support"""
    data = request.json
    content = data.get('content', '')
    apply_app_data_root = data.get('apply_app_data_root', None)  # True/False/None
    
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
        
        # Save the file (with optional app_data_root processing)
        app_data_root_warnings = save_stack_compose(stack_name, content, apply_app_data_root)
        
        # Add app_data_root warnings to response
        if app_data_root_warnings:
            warnings.extend(app_data_root_warnings)
        
        return jsonify({
            'success': True,
            'warnings': warnings,
            'suggestions': suggestions
        })
    except yaml.YAMLError as e:
        return jsonify({'error': f'Invalid YAML: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stacks/<stack_name>/compose/preview', methods=['POST'])
def api_preview_app_data_root_changes(stack_name):
    """
    Preview what would change if App Data Root is applied
    Returns before/after comparison without saving
    """
    data = request.json
    content = data.get('content', '')
    
    app_data_root = config.get('app_data_root', '').strip()
    
    if not app_data_root:
        return jsonify({
            'has_changes': False,
            'message': 'App Data Root not configured'
        })
    
    try:
        # Analyze what would change
        changes = analyze_app_data_root_changes(content, app_data_root, stack_name)
        
        return jsonify({
            'has_changes': len(changes['modified']) > 0,
            'app_data_root': app_data_root,
            'stack_name': stack_name,
            'modified': changes['modified'],
            'unchanged': changes['unchanged'],
            'warnings': changes['warnings']
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/stacks/validate', methods=['POST'])
def api_validate_compose():
    """Validate Docker Compose YAML before saving"""
    data = request.json
    content = data.get('content', '')
    
    if not content:
        return jsonify({'success': False, 'error': 'No content provided'}), 400
    
    try:
        # Step 1: Validate YAML syntax
        try:
            compose_data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return jsonify({'success': False, 'error': f'YAML syntax error: {str(e)}'}), 200
        
        # Step 2: Write to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        
        try:
            # Step 3: Run docker compose config validation
            result = subprocess.run(
                ['docker', 'compose', '-f', tmp_path, 'config'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            # Clean up temp file
            os.unlink(tmp_path)
            
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip() or 'Unknown validation error'
                return jsonify({'success': False, 'error': f'Docker Compose validation failed: {error_msg}'}), 200
            
            return jsonify({'success': True}), 200
            
        except subprocess.TimeoutExpired:
            os.unlink(tmp_path)
            return jsonify({'success': False, 'error': 'Validation timeout'}), 200
        except Exception as e:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return jsonify({'success': False, 'error': f'Validation error: {str(e)}'}), 200
            
    except Exception as e:
        return jsonify({'success': False, 'error': f'Unexpected error: {str(e)}'}), 500


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
                save_update_status()  # SAVE TO FILE
            
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
    """Automatically prune unused images and Trivy cache"""
    if not config.get('auto_prune', False):
        return
    
    print("Running auto-prune...")
    try:
        # Prune Docker images
        result = docker_client.images.prune(filters={'dangling': False})
        space_saved = result.get('SpaceReclaimed', 0)
        space_saved_mb = round(space_saved / (1024 * 1024), 2)
        deleted = len(result.get('ImagesDeleted', []))
        
        # Prune Trivy cache (keep last 30 days)
        trivy_space_saved = 0
        if os.path.exists(TRIVY_CACHE_DIR):
            try:
                import shutil
                cutoff_time = time.time() - (30 * 24 * 60 * 60)  # 30 days
                for root, dirs, files in os.walk(TRIVY_CACHE_DIR):
                    for f in files:
                        filepath = os.path.join(root, f)
                        if os.path.getmtime(filepath) < cutoff_time:
                            size = os.path.getsize(filepath)
                            os.remove(filepath)
                            trivy_space_saved += size
                trivy_space_saved_mb = round(trivy_space_saved / (1024 * 1024), 2)
                if trivy_space_saved_mb > 0:
                    print(f"Pruned Trivy cache: {trivy_space_saved_mb} MB freed")
            except Exception as e:
                print(f"Trivy cache prune error: {e}")
        
        if deleted > 0 or trivy_space_saved > 0:
            total_saved_mb = space_saved_mb + (trivy_space_saved / (1024 * 1024))
            send_notification(
                'Dockup - Auto-Prune',
                f'Pruned {deleted} images ({space_saved_mb} MB) and Trivy cache ({trivy_space_saved_mb} MB)',
                'success'
            )
            print(f"Auto-prune: {deleted} images, {total_saved_mb:.2f} MB total freed")
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


@app.route('/api/version')
def api_version():
    """Get current version and check for updates on Docker Hub"""
    current_version = DOCKUP_VERSION
    
    try:
        # Check Docker Hub for cbothma/dockup tags
        response = requests.get(
            'https://registry.hub.docker.com/v2/repositories/cbothma/dockup/tags?page_size=25',
            timeout=5
        )
        data = response.json()
        
        # Find the highest version number (exclude 'latest')
        latest_version = current_version
        for tag in data.get('results', []):
            tag_name = tag['name']
            # Skip 'latest' and non-version tags
            if tag_name == 'latest':
                continue
            # Compare version strings (assumes semantic versioning like 1.1.4)
            if tag_name > latest_version:
                latest_version = tag_name
        
        update_available = latest_version != current_version
        
        return jsonify({
            'current': current_version,
            'latest': latest_version,
            'update_available': update_available
        })
    except Exception as e:
        print(f"Failed to check Docker Hub for updates: {e}")
        return jsonify({
            'current': current_version,
            'latest': current_version,
            'update_available': False
        })


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
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Always remove, even if exception during removal
        try:
            websocket_clients.remove(ws)
        except:
            pass


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




@app.route('/api/stacks/import-docker-run', methods=['POST'])
@require_auth
def api_import_docker_run():
    """Convert docker run command to compose stack"""
    try:
        data = request.get_json()
        docker_command = data.get('command', '').strip()
        stack_name = data.get('stack_name', '').strip()
        create_immediately = data.get('create', False)
        
        if not docker_command:
            return jsonify({'error': 'No docker run command provided'}), 400
        
        # Parse the docker run command
        try:
            compose_dict, warnings = parse_docker_run_command(docker_command)
        except Exception as e:
            return jsonify({'error': f'Failed to parse command: {str(e)}'}), 400
        
        # Convert to YAML
        compose_yaml = yaml.dump(compose_dict, default_flow_style=False, sort_keys=False)
        
        # If stack name provided and create=True, create the stack immediately
        if stack_name and create_immediately:
            # Validate stack name
            if not re.match(r'^[a-zA-Z0-9_-]+$', stack_name):
                return jsonify({'error': 'Invalid stack name. Use only letters, numbers, hyphens, and underscores.'}), 400
            
            stack_path = os.path.join(STACKS_DIR, stack_name)
            
            if os.path.exists(stack_path):
                return jsonify({'error': f'Stack "{stack_name}" already exists'}), 400
            
            # Create stack directory and compose file
            try:
                Path(stack_path).mkdir(parents=True, exist_ok=True)
                compose_file = os.path.join(stack_path, 'compose.yaml')
                
                with open(compose_file, 'w') as f:
                    f.write(compose_yaml)
                
                # Save metadata
                save_stack_metadata(stack_name, {
                    'created': datetime.now(pytz.UTC).isoformat(),
                    'imported_from': 'docker_run',
                    'original_command': docker_command
                })
                
                send_notification(
                    f"Dockup - Stack Created",
                    f"Stack '{stack_name}' created from docker run command",
                    'success'
                )
                
                broadcast_ws({
                    'type': 'stack_created',
                    'stack': stack_name
                })
                
                return jsonify({
                    'success': True,
                    'stack_name': stack_name,
                    'compose_yaml': compose_yaml,
                    'warnings': warnings,
                    'message': f'Stack "{stack_name}" created successfully'
                })
            
            except Exception as e:
                return jsonify({'error': f'Failed to create stack: {str(e)}'}), 500
        
        # Otherwise just return the converted YAML for preview
        return jsonify({
            'success': True,
            'compose_yaml': compose_yaml,
            'warnings': warnings,
            'suggested_name': list(compose_dict['services'].keys())[0]
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
        load_update_status()  # Load per-instance update status
        load_scan_results()  # Load cached scans
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
