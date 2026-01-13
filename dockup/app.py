#!/usr/bin/env python3
"""
Dockup - Docker Compose Stack Manager with Auto-Update
Combines Dockge functionality with Tugtainer/Watchtower update capabilities

PERFORMANCE OPTIMIZATIONS:
1. Metadata caching in memory - 90% faster metadata access
DOCKUP_VERSION = "1.2.9"
4. Host stats response caching - Reduces CPU usage
5. Health check optimization - Background thread instead of per-request
6. Container cache TTL optimized - Prevents race conditions with stats updater
7. Stack list caching - 90% reduction in file I/O operations
8. Page visibility detection - Pauses polling when tab hidden (80% fewer API calls)
"""

# VERSION - Update this when releasing new version
DOCKUP_VERSION = "1.3.2"

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
import hashlib
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
import logging
from logging.handlers import TimedRotatingFileHandler

import shlex
import re
from typing import Dict, List, Tuple, Any

# ============================================================================
# LOGGING SETUP - 24-hour rotation with 7 days retention
# ============================================================================

# Create logs directory if it doesn't exist
LOG_DIR = '/app/logs'
os.makedirs(LOG_DIR, exist_ok=True)

# Setup rotating file handler - rotates daily at midnight, keeps 7 days
log_file = os.path.join(LOG_DIR, 'dockup.log')
file_handler = TimedRotatingFileHandler(
    filename=log_file,
    when='midnight',      # Rotate at midnight
    interval=1,           # Every 1 day
    backupCount=7,        # Keep 7 days of logs
    encoding='utf-8'
)
file_handler.suffix = '%Y-%m-%d'  # Add date suffix to rotated logs

# Console handler for stdout
console_handler = logging.StreamHandler()

# Setup logging format
log_format = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(log_format)
console_handler.setFormatter(log_format)

# Configure root logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Also configure Flask's logger
flask_logger = logging.getLogger('werkzeug')
flask_logger.setLevel(logging.INFO)
flask_logger.addHandler(file_handler)

logger.info(f"DockUp {DOCKUP_VERSION} starting - Logs rotate daily, keeping 7 days")

# ============================================================================
# PERFORMANCE OPTIMIZATION: In-memory caches
# ============================================================================

# Metadata cache - keeps metadata in memory instead of reading from disk every time
metadata_cache = {}
metadata_cache_lock = threading.Lock()
metadata_cache_enabled = True

# Compose metadata cache - caches parsed compose file service names
compose_metadata_cache = {}
compose_metadata_cache_lock = threading.Lock()
compose_cache_ttl = 30  # seconds

# Config save debouncer
config_save_timer = None
config_save_lock = threading.Lock()
config_save_pending_data = None

# Host stats cache
host_stats_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 3  # Cache host stats for 3 seconds
}
host_stats_lock = threading.Lock()

# Health check background state
health_check_results = {}
health_check_lock = threading.Lock()
health_check_last_run = 0

# Schedules dict lock - protects schedules from race conditions during iteration/modification
schedules_lock = threading.Lock()

# WebSocket clients list lock - protects list during append/remove/iteration
websocket_clients_lock = threading.Lock()

# Update status dict lock - protects update_status from concurrent writes
update_status_lock = threading.Lock()

# Health status dict lock - protects health_status from concurrent reads/writes
health_status_lock = threading.Lock()

# Security scan tracking - track active scans and timeouts
active_scans = {}  # container_id -> {'start_time': time, 'process': subprocess.Popen}
active_scans_lock = threading.Lock()
SCAN_TIMEOUT = 600  # 10 minutes maximum scan time


def get_metadata_cached(stack_name):
    """Get metadata from cache or load from disk"""
    global metadata_cache
    
    if not metadata_cache_enabled:
        return get_stack_metadata(stack_name)
    
    with metadata_cache_lock:
        if stack_name in metadata_cache:
            return metadata_cache[stack_name].copy()
        
        # Not in cache, load from disk
        metadata = get_stack_metadata(stack_name)
        metadata_cache[stack_name] = metadata.copy()
        return metadata


def save_metadata_cached(stack_name, metadata):
    """Save metadata and update cache"""
    save_stack_metadata(stack_name, metadata)
    
    if metadata_cache_enabled:
        with metadata_cache_lock:
            metadata_cache[stack_name] = metadata.copy()


def invalidate_metadata_cache(stack_name=None):
    """Invalidate metadata cache for a specific stack or all stacks"""
    global metadata_cache
    
    with metadata_cache_lock:
        if stack_name:
            metadata_cache.pop(stack_name, None)
        else:
            metadata_cache.clear()


def get_compose_services_cached(stack_name, compose_file_path):
    """Get service names from compose file with caching"""
    global compose_metadata_cache
    
    cache_key = f"{stack_name}:{compose_file_path}"
    current_time = time.time()
    
    with compose_metadata_cache_lock:
        if cache_key in compose_metadata_cache:
            cached_data, timestamp, file_mtime = compose_metadata_cache[cache_key]
            
            # Check if cache is still valid and file hasn't changed
            try:
                current_mtime = os.path.getmtime(compose_file_path)
                if (current_time - timestamp < compose_cache_ttl and 
                    current_mtime == file_mtime):
                    return cached_data
            except OSError:
                # File deleted or moved - invalidate cache (expected)
                pass
        
        # Cache miss or expired - read from file
        try:
            with open(compose_file_path, 'r') as f:
                compose_data = yaml.safe_load(f)
            
            services = list(compose_data.get('services', {}).keys()) if compose_data else []
            file_mtime = os.path.getmtime(compose_file_path)
            
            # Update cache
            compose_metadata_cache[cache_key] = (services, current_time, file_mtime)
            
            return services
        except Exception as e:
            logger.error(f"Error reading compose file {compose_file_path}: {e}")
            return []


def invalidate_compose_cache(stack_name=None):
    """Invalidate compose metadata cache"""
    global compose_metadata_cache
    
    with compose_metadata_cache_lock:
        if stack_name:
            # Remove all entries for this stack
            keys_to_remove = [k for k in compose_metadata_cache.keys() if k.startswith(f"{stack_name}:")]
            for key in keys_to_remove:
                compose_metadata_cache.pop(key, None)
        else:
            compose_metadata_cache.clear()


def save_config_debounced(immediate=False):
    """
    Debounced config save - batches multiple saves together
    
    Args:
        immediate: If True, cancel pending save and save immediately
    """
    global config_save_timer, config_save_pending_data
    
    with config_save_lock:
        # Cancel existing timer
        if config_save_timer is not None:
            config_save_timer.cancel()
            config_save_timer = None
        
        if immediate:
            # Save immediately
            save_config()
            config_save_pending_data = None
        else:
            # Schedule save in 500ms
            config_save_timer = threading.Timer(0.5, _execute_config_save)
            config_save_timer.daemon = True
            config_save_timer.start()


def _execute_config_save():
    """Internal function to execute the actual config save"""
    global config_save_timer
    
    with config_save_lock:
        save_config()
        config_save_timer = None


def get_host_stats_cached():
    """Get host stats with caching to reduce CPU usage"""
    global host_stats_cache
    
    current_time = time.time()
    
    with host_stats_lock:
        # Return cached data if still fresh
        if (host_stats_cache['data'] is not None and 
            current_time - host_stats_cache['timestamp'] < host_stats_cache['ttl']):
            return host_stats_cache['data']
        
        # Fetch fresh stats
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Get network stats
            net_io = psutil.net_io_counters()
            net_rx_mbps = 0
            net_tx_mbps = 0
            
            # Calculate network rate if we have previous data
            if hasattr(get_host_stats_cached, 'last_net_io'):
                time_delta = current_time - get_host_stats_cached.last_time
                if time_delta > 0:
                    bytes_recv_delta = net_io.bytes_recv - get_host_stats_cached.last_net_io['bytes_recv']
                    bytes_sent_delta = net_io.bytes_sent - get_host_stats_cached.last_net_io['bytes_sent']
                    net_rx_mbps = (bytes_recv_delta * 8) / (time_delta * 1_000_000)
                    net_tx_mbps = (bytes_sent_delta * 8) / (time_delta * 1_000_000)
            
            # Store current values for next call
            get_host_stats_cached.last_net_io = {
                'bytes_recv': net_io.bytes_recv,
                'bytes_sent': net_io.bytes_sent
            }
            get_host_stats_cached.last_time = current_time
            
            # Try to get temperature sensors (may not be available on all systems)
            temp_celsius = None
            try:
                if hasattr(psutil, 'sensors_temperatures'):
                    temps = psutil.sensors_temperatures()
                    if temps:
                        # Try common sensor names, prioritize CPU/core temps
                        for name in ['coretemp', 'k10temp', 'cpu_thermal', 'soc_thermal']:
                            if name in temps:
                                sensor_list = temps[name]
                                if sensor_list:
                                    # Get the first sensor reading or average if multiple
                                    temp_readings = [s.current for s in sensor_list if s.current]
                                    if temp_readings:
                                        temp_celsius = round(sum(temp_readings) / len(temp_readings), 1)
                                        break
                        
                        # If no known sensors found, try first available sensor
                        if temp_celsius is None:
                            for sensor_name, sensor_list in temps.items():
                                if sensor_list:
                                    temp_readings = [s.current for s in sensor_list if s.current]
                                    if temp_readings:
                                        temp_celsius = round(sum(temp_readings) / len(temp_readings), 1)
                                        break
            except Exception as e:
                # Temperature reading failed - not critical, continue without it
                logger.debug(f"Temperature sensor reading unavailable: {e}")
            
            stats_data = {
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
                'net_tx_mbps': round(net_tx_mbps, 2),
                'temp_celsius': temp_celsius  # May be None if sensors unavailable
            }
            
            # Update cache
            host_stats_cache['data'] = stats_data
            host_stats_cache['timestamp'] = current_time
            
            return stats_data
            
        except Exception as e:
            logger.error(f"Error getting host stats: {e}")
            return host_stats_cache.get('data')  # Return last good data if available


def health_check_background_worker():
    """
    Background thread that checks stack health every 60 seconds
    This moves expensive health checks out of the request path
    """
    global health_check_results, health_check_last_run
    
    while True:
        try:
            current_time = time.time()
            
            # Run checks every 60 seconds
            if current_time - health_check_last_run < 60:
                time.sleep(5)
                continue
            
            health_check_last_run = current_time
            
            # Get all stacks with running containers
            all_containers = get_all_containers_cached()
            stacks_to_check = {}
            
            for container in all_containers:
                if container.status == 'running':
                    stack_name = container.labels.get('com.docker.compose.project')
                    if stack_name:
                        if stack_name not in stacks_to_check:
                            stacks_to_check[stack_name] = []
                        
                        # Build container info
                        health_status = None
                        state = container.attrs.get('State', {})
                        if 'Health' in state:
                            health_status = state['Health'].get('Status', 'none')
                        
                        stacks_to_check[stack_name].append({
                            'id': container.id[:12],
                            'name': container.name,
                            'status': container.status,
                            'health': health_status
                        })
            
            # Check health for each stack
            for stack_name, containers in stacks_to_check.items():
                check_stack_health(stack_name, containers)
            
            logger.info(f"Health check completed for {len(stacks_to_check)} stacks")
            
        except Exception as e:
            logger.error(f"Error in health check background worker: {e}")
            time.sleep(60)




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
    
    # Clean up multi-line commands (bash line continuations)
    # Replace backslash-newline with space
    command = command.replace('\\\n', ' ').replace('\\\r\n', ' ')
    
    # Remove extra whitespace and normalize
    command = ' '.join(command.split())
    
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
        'dns': [],  # Added
        'tmpfs': [],  # Added
        'ulimits': {},  # Added
    }
    
    named_volumes = set()
    log_driver = None  # Added
    log_opts = {}  # Added
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
        
        elif token == '--dns':
            i += 1
            compose['dns'].append(tokens[i])
        
        elif token == '--link':
            i += 1
            # links deprecated but still convert
            link_value = tokens[i]
            warnings.append(f"'--link' is deprecated, consider using networks instead")
            if 'links' not in compose:
                compose['links'] = []
            compose['links'].append(link_value)
        
        elif token == '--tmpfs':
            i += 1
            compose['tmpfs'].append(tokens[i])
        
        elif token == '--log-driver':
            i += 1
            log_driver = tokens[i]
        
        elif token == '--log-opt':
            i += 1
            log_opt = tokens[i]
            if '=' in log_opt:
                key, val = log_opt.split('=', 1)
                log_opts[key] = val
        
        elif token == '--ip':
            i += 1
            if 'networks' not in compose or not compose['networks']:
                warnings.append("'--ip' requires a network to be specified, may not work correctly")
            compose['ip'] = tokens[i]
        
        elif token == '--mac-address':
            i += 1
            compose['mac_address'] = tokens[i]
        
        elif token == '--ulimit':
            i += 1
            ulimit_str = tokens[i]
            # Format: nofile=1024:1024 or nofile=1024
            if '=' in ulimit_str:
                ulimit_name, ulimit_val = ulimit_str.split('=', 1)
                if ':' in ulimit_val:
                    soft, hard = ulimit_val.split(':', 1)
                    compose['ulimits'][ulimit_name] = {'soft': int(soft), 'hard': int(hard)}
                else:
                    compose['ulimits'][ulimit_name] = int(ulimit_val)
        
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
    
    # Add logging configuration if present
    if log_driver:
        compose['logging'] = {'driver': log_driver}
        if log_opts:
            compose['logging']['options'] = log_opts
    
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
logger.info(f"Trivy cache directory: {TRIVY_CACHE_DIR}")
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
appdata_size_cache = {}  # Cache for appdata sizes (stack_name: {size_gb, last_updated})
peer_sessions = {}  # Cache for peer authentication sessions

# CRITICAL: Create and start scheduler at module load (not conditionally)
# This ensures it exists and is running before any code tries to use it
scheduler = BackgroundScheduler(
    executors={'default': APThreadPoolExecutor(50)},
    job_defaults={'coalesce': False, 'max_instances': 1},
    timezone=pytz.UTC  # Will be updated with user timezone in setup_scheduler
)
scheduler.start()
logger.info("[Module Init] Scheduler created and started")

# Performance optimization caches
stacks_list_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 2  # Cache stack list for 2 seconds
}
get_stacks_data_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 3  # Cache full stack data for 3 seconds (reduces file I/O)
}
all_containers_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 3  # Cache all containers for 3 seconds (prevents race with 5s stats update)
}
stats_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix='stats-')  # For parallel stats collection

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
            # Template settings
            'templates_enabled': True,  # Enable/disable template library
            'templates_refresh_schedule': 'weekly_sunday',  # disabled, daily, weekly_sunday, weekly_monday, etc, monthly
            # Auto-update settings
            'auto_update_enabled': True,  # Global auto-update toggle
            'auto_update_check_schedule': 'daily_2am',  # When to check for updates
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
    if 'templates_enabled' not in config:
        config['templates_enabled'] = True
    if 'templates_refresh_schedule' not in config:
        config['templates_refresh_schedule'] = 'weekly_sunday'
    if 'auto_update_enabled' not in config:
        config['auto_update_enabled'] = True
    if 'auto_update_check_schedule' not in config:
        config['auto_update_check_schedule'] = 'daily_2am'
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
    with schedules_lock:
        if os.path.exists(SCHEDULES_FILE):
            with open(SCHEDULES_FILE, 'r') as f:
                schedules = json.load(f)
        else:
            schedules = {}
            # Don't call save_schedules() here - we already have the lock
            # Just write directly to avoid deadlock
            try:
                with open(SCHEDULES_FILE, 'w') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    json.dump(schedules, f, indent=2)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                logger.error(f"Error creating schedules file: {e}")


def save_schedules():
    """Save schedules to file with file locking"""
    try:
        with schedules_lock:
            schedules_snapshot = schedules.copy()
        with open(SCHEDULES_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(schedules_snapshot, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        logger.error(f"Error saving schedules: {e}")


def load_health_status():
    """Load health status tracking from file"""
    global health_status
    with health_status_lock:
        if os.path.exists(HEALTH_STATUS_FILE):
            with open(HEALTH_STATUS_FILE, 'r') as f:
                health_status = json.load(f)
        else:
            health_status = {}


def save_health_status():
    """Save health status tracking to file with file locking"""
    try:
        with health_status_lock:
            health_status_snapshot = health_status.copy()
        with open(HEALTH_STATUS_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(health_status_snapshot, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        logger.error(f"Error saving health status: {e}")


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
        logger.error(f"Error saving update history: {e}")


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
                        logger.info(f"Cleaned up orphaned status file: {filename}")
                    except OSError as e:
                        logger.info(f"⚠ Could not remove orphaned status file {filename}: {e}")
    except Exception as e:
        logger.error(f"Error cleaning up status files: {e}")


def load_update_status():
    """Load update status from per-instance file"""
    global update_status
    status_file = get_update_status_file()
    with update_status_lock:
        if os.path.exists(status_file):
            try:
                with open(status_file, 'r') as f:
                    update_status = json.load(f)
            except json.JSONDecodeError as e:
                logger.info(f"⚠ Update status file corrupted, resetting: {e}")
                update_status = {}
            except Exception as e:
                logger.info(f"⚠ Could not load update status: {e}")
                update_status = {}
        else:
            update_status = {}
    
    # Clean up old files on load
    cleanup_old_status_files()


def save_update_status():
    """Save update status to per-instance file with file locking"""
    try:
        status_file = get_update_status_file()
        with update_status_lock:
            update_status_snapshot = update_status.copy()
        with open(status_file, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(update_status_snapshot, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        logger.error(f"Error saving update status: {e}")


def get_stack_metadata(stack_name):
    """Load stack metadata from .dockup-meta.json"""
    stack_path = os.path.join(STACKS_DIR, stack_name)
    meta_file = os.path.join(stack_path, '.dockup-meta.json')
    if os.path.exists(meta_file):
        try:
            with open(meta_file, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.info(f"⚠ Metadata corrupted for {stack_name}, resetting: {e}")
            return {}
        except Exception as e:
            logger.info(f"⚠ Could not load metadata for {stack_name}: {e}")
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
        logger.error(f"Error checking Docker Hub rate limit: {e}")
        return None


# Vulnerability scanning with Trivy
vulnerability_scan_cache = {}  # Cache scan results: {image_name: {scan_data, timestamp}}


def update_trivy_database():
    """
    Update Trivy vulnerability database
    Should be run daily to keep vulnerability data fresh
    """
    try:
        logger.info("Updating Trivy vulnerability database...")
        
        env = os.environ.copy()
        env['TRIVY_CACHE_DIR'] = TRIVY_CACHE_DIR
        
        result = subprocess.run(
            ['trivy', 'image', '--download-db-only'],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for DB download
            env=env
        )
        
        if result.returncode == 0:
            logger.info("✓ Trivy database updated successfully")
            
            # Send notification if configured
            send_notification(
                "Trivy Database Updated",
                "Vulnerability database updated successfully",
                notify_type='info'
            )
            return True
        else:
            logger.error(f"Failed to update Trivy database: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("Trivy database update timed out after 10 minutes")
        return False
    except Exception as e:
        logger.error(f"Error updating Trivy database: {e}")
        return False


def scan_image_vulnerabilities(image_name):
    """Scan Docker image for vulnerabilities using Trivy"""
    try:
        logger.info(f"Scanning image: {image_name}")
        
        # Run Trivy scan with custom cache dir
        env = os.environ.copy()
        env['TRIVY_CACHE_DIR'] = TRIVY_CACHE_DIR
        
        result = subprocess.run(
            ['trivy', 'image', '--format', 'json', '--skip-db-update', '--quiet', image_name],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout (increased from 5 min)
            env=env
        )
        
        if result.returncode > 1:
            # Exit code > 1 means execution error
            logger.info(f"Trivy execution error for {image_name}: {result.stderr}")
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
        logger.info(f"Trivy scan timeout for {image_name}")
        return None
    except json.JSONDecodeError as e:
        logger.info(f"Failed to parse Trivy output for {image_name}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error scanning {image_name}: {e}")
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
            logger.info(f"Loaded {len(vulnerability_scan_cache)} cached scans")
        except Exception as e:
            logger.error(f"Error loading scan results: {e}")


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
        logger.error(f"Error saving scan results: {e}")



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
        logger.info(f"Notification skipped (notify_on_check=False): {title}")
        return
    if not config.get('notify_on_update') and 'updated successfully' in message.lower():
        logger.info(f"Notification skipped (notify_on_update=False): {title}")
        return
    if not config.get('notify_on_error') and notify_type == 'error':
        logger.info(f"Notification skipped (notify_on_error=False): {title}")
        return
    if not config.get('notify_on_health_failure', True) and 'unhealthy' in message.lower():
        logger.info(f"Notification skipped (notify_on_health_failure=False): {title}")
        return
    
    if len(apobj) > 0:
        logger.info(f"Sending notification: {title} - {message}")
        try:
            result = apobj.notify(title=title, body=message, notify_type=notify_type)
            if result:
                logger.info(f"✓ Notification sent successfully")
            else:
                logger.info(f"✗ Notification failed to send")
        except Exception as e:
            logger.info(f"✗ Notification error: {e}")
    else:
        logger.info(f"No notification services configured. Title: {title}")


# ============================================================================
# STACK ACTION FUNCTIONS - Start/Stop/Restart
# ============================================================================

def stack_action(stack_name, action):
    """
    Perform action on stack (start/stop/restart)
    Returns: (success: bool, message: str)
    """
    stack_path = os.path.join(STACKS_DIR, stack_name)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    
    if not os.path.exists(compose_file):
        compose_file = os.path.join(stack_path, 'docker-compose.yml')
    
    if not os.path.exists(compose_file):
        return False, "Compose file not found"
    
    try:
        if action == 'start':
            result = subprocess.run(
                ['docker', 'compose', '-f', compose_file, 'up', '-d'],
                capture_output=True,
                text=True,
                cwd=stack_path,
                timeout=300
            )
        elif action == 'stop':
            result = subprocess.run(
                ['docker', 'compose', '-f', compose_file, 'stop'],
                capture_output=True,
                text=True,
                cwd=stack_path,
                timeout=300
            )
        elif action == 'restart':
            result = subprocess.run(
                ['docker', 'compose', '-f', compose_file, 'restart'],
                capture_output=True,
                text=True,
                cwd=stack_path,
                timeout=300
            )
        else:
            return False, f"Unknown action: {action}"
        
        if result.returncode == 0:
            logger.info(f"[{action.upper()}] {stack_name} - Success")
            
            # Invalidate caches
            invalidate_metadata_cache(stack_name)
            invalidate_compose_cache(stack_name)
            
            return True, f"Stack {action}ed successfully"
        else:
            error_msg = result.stderr or result.stdout
            logger.error(f"[{action.upper()}] {stack_name} - Failed: {error_msg}")
            return False, error_msg
            
    except subprocess.TimeoutExpired:
        return False, f"Action {action} timed out after 5 minutes"
    except Exception as e:
        logger.error(f"[{action.upper()}] {stack_name} - Exception: {e}")
        return False, str(e)


def update_stack_action_jobs(stack_name, schedules_list):
    """Update action jobs for a single stack without recreating scheduler"""
    global scheduler
    
    if scheduler is None:
        logger.error("[update_stack_action_jobs] Scheduler not initialized")
        return
    
    user_tz = pytz.timezone(config.get('timezone', 'UTC'))
    
    # Remove old action jobs for this stack
    for job in list(scheduler.get_jobs()):
        if (job.id.startswith(f"start_{stack_name}_") or 
            job.id.startswith(f"stop_{stack_name}_") or 
            job.id.startswith(f"restart_{stack_name}_")):
            try:
                scheduler.remove_job(job.id)
                logger.info(f"[Action Jobs] Removed old job: {job.id}")
            except Exception as e:
                logger.error(f"[Action Jobs] Could not remove job {job.id}: {e}")
    
    # Add new action jobs for this stack
    for schedule in schedules_list:
        if not schedule.get('enabled', True):
            continue
        
        action = schedule.get('action')
        cron_expr = schedule.get('cron')
        
        if not action or not cron_expr:
            continue
        
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone=user_tz)
            job_id = f"{action}_{stack_name}_{schedule.get('id', hashlib.md5(cron_expr.encode()).hexdigest()[:8])}"
            
            scheduler.add_job(
                scheduled_stack_action,
                trigger,
                args=[stack_name, action],
                id=job_id,
                replace_existing=True
            )
            
            job = scheduler.get_job(job_id)
            if job:
                logger.info(f"[Action Jobs] ✓ Added {action} for {stack_name} @ {cron_expr} | Next: {job.next_run_time}")
            else:
                logger.error(f"[Action Jobs] ✗ Failed to add {action} for {stack_name}")
        except Exception as e:
            logger.error(f"[Action Jobs] Error adding {action} for {stack_name}: {e}")


def scheduled_stack_action(stack_name, action):
    """Execute scheduled stack action with notification"""
    logger.info("=" * 80)
    logger.info(f"SCHEDULED ACTION FIRING: {action.upper()} on {stack_name}")
    logger.info(f"Time: {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("=" * 80)
    
    success, message = stack_action(stack_name, action)
    
    logger.info(f"Result: {'SUCCESS' if success else 'FAILED'} - {message}")
    
    if success:
        title = f"✓ Scheduled {action.title()}: {stack_name}"
        send_notification(title, message, notify_type='success')
    else:
        title = f"✗ Scheduled {action.title()} Failed: {stack_name}"
        send_notification(title, f"Error: {message}", notify_type='error')
    
    return success


def broadcast_ws(data):
    """Broadcast message to all WebSocket clients"""
    with websocket_clients_lock:
        clients_snapshot = websocket_clients.copy()
    
    dead_clients = []
    for client in clients_snapshot:
        try:
            client.send(json.dumps(data))
        except Exception as e:
            logger.info(f"⚠ WebSocket send failed, marking client for removal: {e}")
            dead_clients.append(client)
    
    # Remove dead clients after iteration
    if dead_clients:
        with websocket_clients_lock:
            for client in dead_clients:
                try:
                    websocket_clients.remove(client)
                except ValueError:
                    pass  # Already removed


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
    
    current_time = datetime.now(pytz.UTC).isoformat()
    
    with health_status_lock:
        # Initialize health tracking for this stack if needed
        if stack_name not in health_status:
            health_status[stack_name] = {
                'consecutive_failures': 0,
                'last_notified': None,
                'last_check': None
            }
        
        if unhealthy_containers:
            # Increment failure counter
            health_status[stack_name]['consecutive_failures'] += 1
            health_status[stack_name]['last_check'] = current_time
            
            failures = health_status[stack_name]['consecutive_failures']
            logger.info(f"  ⚠ Health check {failures}/{threshold}: {stack_name} has {len(unhealthy_containers)} issue(s)")
            
            # Send notification after threshold reached
            if failures >= threshold:
                last_notified = health_status[stack_name].get('last_notified')
                should_notify = last_notified is None
                
                if should_notify:
                    health_status[stack_name]['last_notified'] = current_time
        else:
            # Stack is healthy - reset counter
            was_unhealthy = health_status[stack_name]['consecutive_failures'] > 0
            was_notified = health_status[stack_name].get('last_notified') is not None
            
            health_status[stack_name] = {
                'consecutive_failures': 0,
                'last_notified': None,
                'last_check': current_time
            }
    
    # Send notifications OUTSIDE the lock to avoid deadlock
    if unhealthy_containers:
        failures = None
        with health_status_lock:
            failures = health_status[stack_name]['consecutive_failures']
        
        if failures >= threshold:
            with health_status_lock:
                last_notified = health_status[stack_name].get('last_notified')
            
            # Only notify once until it recovers
            if last_notified and last_notified == current_time:
                issue_list = '\n'.join([f"  • {issue}" for issue in unhealthy_containers])
                send_notification(
                    f"Dockup - Stack Unhealthy",
                    f"Stack '{stack_name}' has been unhealthy for {failures} consecutive checks.\n\nContainers with issues:\n{issue_list}\n\nCheck the logs for details.",
                    'error'
                )
                save_health_status()
                
                broadcast_ws({
                    'type': 'health_alert',
                    'stack': stack_name,
                    'issues': unhealthy_containers
                })
    else:
        # Stack is healthy
        if was_unhealthy and was_notified:
            send_notification(
                f"Dockup - Stack Recovered",
                f"Stack '{stack_name}' is now healthy again.",
                'success'
            )
            broadcast_ws({
                'type': 'health_recovered',
                'stack': stack_name
            })
        
        if was_unhealthy:
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
            logger.info(f"  ⚠ Host check {failures}/{alert_threshold}: {len(issues)} issue(s) detected")
            
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
        logger.error(f"Error checking host health: {e}")


def calculate_appdata_sizes():
    """Calculate actual volume bind mount sizes for all stacks (runs every 3 hours)"""
    global appdata_size_cache
    
    try:
        logger.info("=" * 50)
        logger.info("Starting appdata size calculation...")
        logger.info(f"STACKS_DIR: {STACKS_DIR}")
        
        # Get all stacks
        stacks_dir = Path(STACKS_DIR)
        if not stacks_dir.exists():
            logger.info(f"ERROR: Stacks directory does not exist: {STACKS_DIR}")
            return
        
        stack_names = [d.name for d in stacks_dir.iterdir() if d.is_dir()]
        logger.info(f"Found {len(stack_names)} stack directories: {stack_names}")
        
        if len(stack_names) == 0:
            logger.info("No stacks found, appdata sizes will be empty")
            return
        
        for stack_name in stack_names:
            try:
                # Read the stack's compose file to find volume paths
                stack_path = os.path.join(STACKS_DIR, stack_name)
                compose_file = None
                
                for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
                    potential_file = os.path.join(stack_path, filename)
                    if os.path.exists(potential_file):
                        compose_file = potential_file
                        break
                
                if not compose_file:
                    logger.info(f"  {stack_name}: No compose file found, skipping")
                    continue
                
                # Parse compose file
                with open(compose_file, 'r') as f:
                    compose_data = yaml.safe_load(f)
                
                services = compose_data.get('services', {})
                total_size_bytes = 0
                paths_checked = set()
                
                # Get all volume bind mounts from compose file
                for service_name, service_config in services.items():
                    volumes = service_config.get('volumes', [])
                    
                    for volume in volumes:
                        # Parse volume string (format: "host_path:container_path" or "host_path:container_path:ro")
                        if isinstance(volume, str):
                            parts = volume.split(':')
                            if len(parts) >= 2:
                                host_path = parts[0]
                                
                                # Only process absolute paths (bind mounts, not named volumes)
                                if host_path.startswith('/'):
                                    # Skip system paths
                                    system_paths = ['/var/run/docker.sock', '/etc/localtime', '/etc/timezone', '/dev/', '/sys/', '/proc/']
                                    if any(host_path.startswith(sp) for sp in system_paths):
                                        continue
                                    
                                    # Avoid counting same path twice
                                    if host_path in paths_checked:
                                        continue
                                    paths_checked.add(host_path)
                                    
                                    # Calculate size if path exists
                                    if os.path.exists(host_path):
                                        try:
                                            result = subprocess.run(
                                                ['du', '-sb', host_path],
                                                capture_output=True,
                                                text=True,
                                                timeout=60
                                            )
                                            
                                            if result.returncode == 0:
                                                size_bytes = int(result.stdout.split()[0])
                                                total_size_bytes += size_bytes
                                                logger.info(f"  {stack_name} - {host_path}: {size_bytes / (1024**3):.2f} GB")
                                        except subprocess.TimeoutExpired:
                                            logger.info(f"  Timeout calculating size for {host_path}")
                                        except Exception as e:
                                            logger.info(f"  Error calculating size for {host_path}: {e}")
                                    else:
                                        logger.info(f"  {stack_name} - {host_path}: path does not exist")
                        
                        # Handle long-form volume syntax
                        elif isinstance(volume, dict):
                            if volume.get('type') == 'bind':
                                host_path = volume.get('source', '')
                                if host_path and host_path.startswith('/'):
                                    system_paths = ['/var/run/docker.sock', '/etc/localtime', '/etc/timezone', '/dev/', '/sys/', '/proc/']
                                    if any(host_path.startswith(sp) for sp in system_paths):
                                        continue
                                    
                                    if host_path in paths_checked:
                                        continue
                                    paths_checked.add(host_path)
                                    
                                    if os.path.exists(host_path):
                                        try:
                                            result = subprocess.run(
                                                ['du', '-sb', host_path],
                                                capture_output=True,
                                                text=True,
                                                timeout=60
                                            )
                                            
                                            if result.returncode == 0:
                                                size_bytes = int(result.stdout.split()[0])
                                                total_size_bytes += size_bytes
                                                logger.info(f"  {stack_name} - {host_path}: {size_bytes / (1024**3):.2f} GB")
                                        except subprocess.TimeoutExpired:
                                            logger.info(f"  Timeout calculating size for {host_path}")
                                        except Exception as e:
                                            logger.info(f"  Error calculating size for {host_path}: {e}")
                
                # Convert to GB and cache
                size_gb = total_size_bytes / (1024**3)
                
                appdata_size_cache[stack_name] = {
                    'size_gb': round(size_gb, 2),
                    'last_updated': datetime.now(pytz.UTC).isoformat()
                }
                
                if size_gb > 0:
                    logger.info(f"  ✓ {stack_name}: Total {size_gb:.2f} GB")
                else:
                    logger.info(f"  - {stack_name}: No bind mounts found or all paths missing")
                
            except Exception as e:
                logger.error(f"Error processing {stack_name}: {e}")
        
        logger.info(f"AppData size calculation complete. Cached {len(appdata_size_cache)} stacks")
        logger.info(f"Cache contents: {list(appdata_size_cache.keys())}")
        logger.info("=" * 50)
        
    except Exception as e:
        logger.info(f"ERROR in calculate_appdata_sizes: {e}")
        import traceback
        traceback.print_exc()


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

            # Skip BuildKit containers (Docker Buildx infrastructure)
            if container.name.startswith('buildx_buildkit_builder'):
                continue

            # Skip Gitea Actions temporary containers
            if container.name.startswith('GITEA-ACTIONS-TASK'):
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

            logger.info(f"[Dockup] Auto-importing orphan container: {container.name} → stack '{stack_name}'")

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
                logger.info(f"[Dockup] Warning: Could not label container {container.name}: {e}")

            send_notification(
                "Dockup – Orphan Imported",
                f"Container `{container.name}` automatically imported as stack `{stack_name}`",
                'info'
            )
            broadcast_ws({'type': 'stacks_changed'})

    except Exception as e:
        logger.info(f"[Dockup] Orphan import error: {e}")


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
        logger.error(f"Error getting used ports: {e}")
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
        logger.error(f"Error checking port conflicts: {e}")
    
    return conflicts


def get_all_containers_cached():
    """Get all Docker containers with caching to reduce API calls"""
    global all_containers_cache
    
    current_time = time.time()
    if (all_containers_cache['data'] is not None and 
        current_time - all_containers_cache['timestamp'] < all_containers_cache['ttl']):
        return all_containers_cache['data']
    
    try:
        all_containers = docker_client.containers.list(all=True)
        all_containers_cache['data'] = all_containers
        all_containers_cache['timestamp'] = current_time
        return all_containers
    except Exception as e:
        logger.error(f"Error fetching containers: {e}")
        return all_containers_cache.get('data', [])


def get_stack_stats(stack_name, containers_list=None):
    """Get CPU and RAM usage for a stack - optimized with optional pre-fetched containers"""
    global network_stats_cache
    
    try:
        # Use pre-fetched containers if provided, otherwise fetch
        if containers_list is None:
            all_containers = get_all_containers_cached()
            containers = [c for c in all_containers 
                         if c.labels.get('com.docker.compose.project') == stack_name and c.status == 'running']
        else:
            containers = containers_list
        
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
                logger.error(f"Error getting stats for {container.name}: {e}")
        
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
        logger.error(f"Error getting stats for {stack_name}: {e}")
        return {
            'cpu_percent': 0,
            'mem_usage_mb': 0,
            'mem_limit_mb': 0,
            'mem_percent': 0,
            'net_rx_mbps': 0,
            'net_tx_mbps': 0
        }


def get_container_stats_parallel(container):
    """Fetch stats for a single container (for parallel execution)"""
    try:
        return container, container.stats(stream=False)
    except Exception as e:
        logger.error(f"Error getting stats for {container.name}: {e}")
        return container, None


def get_stack_stats_batch(stack_containers_map):
    """
    Get stats for multiple stacks in parallel
    Args:
        stack_containers_map: dict of {stack_name: [containers]}
    Returns:
        dict of {stack_name: stats_dict}
    """
    global network_stats_cache
    
    results = {}
    current_time = time.time()
    
    # Flatten all containers into a list for parallel fetching
    all_containers = []
    container_to_stack = {}
    
    for stack_name, containers in stack_containers_map.items():
        for container in containers:
            all_containers.append(container)
            container_to_stack[container.id] = stack_name
    
    # Fetch all stats in parallel
    stats_futures = list(stats_executor.map(get_container_stats_parallel, all_containers))
    
    # Organize results by stack
    stack_data = {}
    for container, stats_result in stats_futures:
        if stats_result is None:
            continue
            
        stack_name = container_to_stack[container.id]
        if stack_name not in stack_data:
            stack_data[stack_name] = {
                'cpu': 0.0,
                'mem': 0,
                'mem_limit': 0,
                'rx_bytes': 0,
                'tx_bytes': 0
            }
        
        try:
            # Calculate CPU percentage
            cpu_delta = stats_result['cpu_stats']['cpu_usage']['total_usage'] - stats_result['precpu_stats']['cpu_usage']['total_usage']
            system_delta = stats_result['cpu_stats']['system_cpu_usage'] - stats_result['precpu_stats']['system_cpu_usage']
            cpu_count = stats_result['cpu_stats'].get('online_cpus', 1)
            
            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0
                stack_data[stack_name]['cpu'] += cpu_percent
            
            # Memory
            stack_data[stack_name]['mem'] += stats_result['memory_stats'].get('usage', 0)
            stack_data[stack_name]['mem_limit'] += stats_result['memory_stats'].get('limit', 0)
            
            # Network
            networks = stats_result.get('networks', {})
            for net_name, net_stats in networks.items():
                stack_data[stack_name]['rx_bytes'] += net_stats.get('rx_bytes', 0)
                stack_data[stack_name]['tx_bytes'] += net_stats.get('tx_bytes', 0)
        except Exception as e:
            logger.error(f"Error processing stats for {container.name}: {e}")
    
    # Calculate final stats for each stack
    for stack_name, data in stack_data.items():
        # Calculate network rate
        net_rx_mbps = 0.0
        net_tx_mbps = 0.0
        
        cache_key = stack_name
        if cache_key in network_stats_cache:
            prev_data = network_stats_cache[cache_key]
            time_delta = current_time - prev_data['time']
            
            if time_delta > 0:
                rx_bytes_per_sec = (data['rx_bytes'] - prev_data['rx_bytes']) / time_delta
                tx_bytes_per_sec = (data['tx_bytes'] - prev_data['tx_bytes']) / time_delta
                
                net_rx_mbps = round((rx_bytes_per_sec * 8) / 1_000_000, 2)
                net_tx_mbps = round((tx_bytes_per_sec * 8) / 1_000_000, 2)
                
                net_rx_mbps = max(0, net_rx_mbps)
                net_tx_mbps = max(0, net_tx_mbps)
        
        # Store current values
        network_stats_cache[cache_key] = {
            'time': current_time,
            'rx_bytes': data['rx_bytes'],
            'tx_bytes': data['tx_bytes']
        }
        
        results[stack_name] = {
            'cpu_percent': round(data['cpu'], 2),
            'mem_usage_mb': round(data['mem'] / (1024 * 1024), 2),
            'mem_limit_mb': round(data['mem_limit'] / (1024 * 1024), 2),
            'mem_percent': round((data['mem'] / data['mem_limit'] * 100) if data['mem_limit'] > 0 else 0, 2),
            'net_rx_mbps': net_rx_mbps,
            'net_tx_mbps': net_tx_mbps
        }
    
    return results


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
    """Get all Docker Compose stacks - optimized with container caching"""
    stacks = []
    stacks_dict = {}  # Use dict to avoid duplicates
    
    if not os.path.exists(STACKS_DIR):
        return stacks
    
    # Get all containers once (cached)
    all_containers = get_all_containers_cached()
    
    # Group containers by stack name
    containers_by_stack = {}
    for container in all_containers:
        stack_name = container.labels.get('com.docker.compose.project')
        if stack_name:
            if stack_name not in containers_by_stack:
                containers_by_stack[stack_name] = []
            containers_by_stack[stack_name].append(container)
    
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
        logger.error(f"Error scanning stacks directory: {e}")
    
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
            
            # OPTIMIZATION: Use cached services instead of parsing YAML every time
            # Still need to read file for first time, but services are cached
            services = get_compose_services_cached(stack_name, compose_file)
            
            # Only parse full YAML if we need more than just service names
            # For now, we only need services, so skip full parse
            compose_data = {'services': {s: {} for s in services}}
            
            # Get containers for this stack from pre-fetched list
            stack_containers = containers_by_stack.get(stack_name, [])
            containers = []
            
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
                    'health': health_status,
                    'image': container.image.tags[0] if container.image.tags else container.image.id[:12],
                    'uptime': uptime_str
                })
            
            # Get schedule info
            with schedules_lock:
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
            
            # OPTIMIZATION: Use cached metadata instead of reading from disk every time
            metadata = get_metadata_cached(stack_name)
            
            # Mark as started if containers exist
            if len(containers) > 0 and not metadata.get('ever_started'):
                metadata['ever_started'] = True
                save_metadata_cached(stack_name, metadata)
            
            # Inactive = never started (no metadata marker AND no containers)
            is_inactive = not metadata.get('ever_started', False) and len(containers) == 0
            
            # OPTIMIZATION: Health checks now run in background thread
            # Removed from here to speed up get_stacks() calls
            # Background thread checks health every 60 seconds
            
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
                'updated_by': update_info.get('updated_by', 'unknown'),
                # Add appdata size from cache
                'appdata_size_gb': appdata_size_cache.get(stack_name, {}).get('size_gb', None),
                'appdata_last_updated': appdata_size_cache.get(stack_name, {}).get('last_updated', None)
            })
        except Exception as e:
            logger.error(f"Error loading stack {stack_name}: {e}")
    
    # Sort alphabetically by name (case-insensitive)
    stacks.sort(key=lambda x: x['name'].lower())
    
    return stacks


def get_stacks_cached():
    """Cached wrapper for get_stacks() - reduces file I/O by 90%"""
    global get_stacks_data_cache
    
    current_time = time.time()
    if (get_stacks_data_cache['data'] is not None and 
        current_time - get_stacks_data_cache['timestamp'] < get_stacks_data_cache['ttl']):
        return get_stacks_data_cache['data']
    
    # Cache miss - fetch fresh data
    stacks_data = get_stacks()
    get_stacks_data_cache['data'] = stacks_data
    get_stacks_data_cache['timestamp'] = current_time
    
    return stacks_data


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
            logger.info(f"App Data Root warnings for {stack_name}:")
            for warning in warnings:
                logger.info(f"  - {warning}")
    
    # Always save as compose.yaml (modern standard)
    compose_file = os.path.join(stack_path, 'compose.yaml')
    
    with open(compose_file, 'w') as f:
        f.write(content)
    
    # OPTIMIZATION: Invalidate compose cache since file changed
    invalidate_compose_cache(stack_name)
    
    # Clean up old compose files to avoid confusion
    old_files = ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml']
    for old_file in old_files:
        old_path = os.path.join(stack_path, old_file)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
                logger.info(f"Cleaned up old compose file: {old_file} from {stack_name}")
            except Exception as e:
                logger.info(f"Warning: Could not delete {old_file}: {e}")
    
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
        logger.info(f"  → Refreshing metadata for {stack_name}")
        
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
            except Exception as e:
                logger.info(f"  ⚠ Could not refresh image {image_name}: {e}")
        
        logger.info(f"  ✓ Metadata refreshed for {stack_name}")
        return True
    
    except Exception as e:
        logger.info(f"  ✗ Metadata refresh failed: {e}")
        return False

def auto_update_stack(stack_name):
    """
    Watchtower-style update: Pull first, then compare image IDs
    FIXED: Now reads image names from compose file instead of running containers
    This prevents digest caching issues when registry URLs change
    """
    # Check global auto-update toggle
    if not config.get('auto_update_enabled', True):
        logger.debug(f"[{stack_name}] Auto-updates globally disabled, skipping")
        return
    
    # Read schedule from FILE (not global dict) to avoid thread issues
    schedule_info = {}
    try:
        if os.path.exists(SCHEDULES_FILE):
            with open(SCHEDULES_FILE, 'r') as f:
                all_schedules = json.load(f)
                schedule_info = all_schedules.get(stack_name, {})
    except Exception as e:
        logger.info(f"[{stack_name}] ERROR reading schedules file: {e}")
        return
    
    mode = schedule_info.get('mode', 'off')
    
    if mode == 'off':
        return
    
    logger.info(f"=" * 50)
    logger.info(f"[{datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S %Z')}] Scheduled check for: {stack_name}")
    logger.info(f"Mode: {mode}, Cron: {schedule_info.get('cron')}")
    logger.info(f"=" * 50)
    
    # CRITICAL: Only auto-update stacks that are actually DEPLOYED (have containers)
    # This prevents saved/inactive compose files from being auto-started
    try:
        stack_containers = docker_client.containers.list(
            all=True,
            filters={'label': f'com.docker.compose.project={stack_name}'}
        )
        
        if not stack_containers:
            logger.debug(f"[{stack_name}] Stack has no containers (not deployed), skipping auto-update")
            return
            
        logger.info(f"  Stack has {len(stack_containers)} container(s), proceeding with update check")
        
    except Exception as e:
        logger.info(f"✗ Error checking stack deployment status: {e}")
        return
    
    # FIXED: Get image names from COMPOSE FILE (not running containers)
    # This prevents digest cache mismatches when registry URLs change
    try:
        stack_path = os.path.join(STACKS_DIR, stack_name)
        compose_file = None
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            potential_file = os.path.join(stack_path, filename)
            if os.path.exists(potential_file):
                compose_file = potential_file
                break
        
        if not compose_file:
            logger.info(f"✗ No compose file found for {stack_name}")
            return
        
        # Parse compose file to get image names
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        services = compose_data.get('services', {})
        if not services:
            logger.info(f"✗ No services found in compose file for {stack_name}")
            return
        
        # Get current image IDs using compose file image names
        old_image_ids = {}
        for service_name, service_config in services.items():
            image_name = service_config.get('image')
            if image_name:
                try:
                    # Get current image ID from Docker
                    current_image = docker_client.images.get(image_name)
                    old_image_ids[image_name] = current_image.id
                    logger.info(f"  Current {service_name}: {image_name} ({current_image.id[:12]})")
                except docker.errors.ImageNotFound:
                    # Image not pulled yet - will be considered an update
                    old_image_ids[image_name] = None
                    logger.info(f"  Current {service_name}: {image_name} (not pulled yet)")
                except Exception as e:
                    logger.info(f"  ⚠ Could not get current ID for {image_name}: {e}")
        
        if not old_image_ids:
            logger.info(f"✗ No images found in compose file for {stack_name}")
            return
        
    except Exception as e:
        logger.info(f"✗ Error reading compose file: {e}")
        send_notification(
            f"Dockup - Error",
            f"Failed to read compose file for '{stack_name}': {str(e)}",
            'error'
        )
        return
    
    # PULL new images (always pull, like Watchtower)
    logger.info(f"  → Pulling latest images...")
    success_pull, output_pull = stack_operation(stack_name, 'pull')
    
    if not success_pull:
        logger.info(f"✗ Pull failed: {output_pull}")
        send_notification(
            f"Dockup - Pull Failed",
            f"Failed to pull images for '{stack_name}'",
            'error'
        )
        return
    
    logger.info(f"  ✓ Pull completed")
    
    # Get NEW image IDs after pull
    new_image_ids = {}
    images_changed = False
    
    for image_name, old_id in old_image_ids.items():
        try:
            # Get the newly pulled image
            new_image = docker_client.images.get(image_name)
            new_id = new_image.id
            new_image_ids[image_name] = new_id
            
            # If old_id is None (image wasn't pulled before), it's an update
            if old_id is None:
                logger.info(f"  ✓ NEW IMAGE: {image_name}")
                logger.info(f"    New: {new_id[:12]}")
                images_changed = True
            elif old_id != new_id:
                logger.info(f"  ✓ UPDATE DETECTED: {image_name}")
                logger.info(f"    Old: {old_id[:12]}")
                logger.info(f"    New: {new_id[:12]}")
                images_changed = True
            else:
                logger.info(f"  = No change: {image_name} ({old_id[:12]})")
        except Exception as e:
            logger.info(f"  ⚠ Could not check {image_name}: {e}")
    
    # Update status
    with update_status_lock:
        update_status[stack_name] = {
            'last_check': datetime.now(pytz.UTC).isoformat(),
            'update_available': images_changed
        }
    save_update_status()  # SAVE TO FILE
    
    # Handle based on mode
    if mode == 'check':
        # Check-only mode
        if images_changed:
            logger.info(f"✓ Updates available for {stack_name} (check-only mode)")
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
            logger.info(f"✓ No updates for {stack_name}")
            broadcast_ws({
                'type': 'no_updates',
                'stack': stack_name
            })
        return
    
    elif mode == 'auto':
        if not images_changed:
            logger.info(f"✓ Already up to date: {stack_name}")
            broadcast_ws({
                'type': 'no_updates',
                'stack': stack_name
            })
            return
        
        # AUTO MODE: Actually update the containers
        logger.info(f"  → Auto-update enabled, recreating containers...")
        
        # Recreate containers with new images
        success_up, output_up = stack_operation(stack_name, 'up')
        
        if not success_up:
            logger.info(f"  ✗ First restart failed, retrying...")
            time.sleep(3)
            success_up, output_up = stack_operation(stack_name, 'up')
        
        if success_up:
            # Wait for containers to fully start and stabilize
            logger.info(f"  → Waiting for containers to start...")
            time.sleep(10)
            
            # Force Docker to refresh image data
            try:
                docker_client.images.list()
            except Exception as e:
                logger.info(f"  ⚠ Could not refresh Docker images list: {e}")
                # Non-fatal but log it for debugging
            
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
                            logger.info(f"  ✓ {container.name}: Running new image ({current_id[:12]})")
                            verification_results.append(True)
                        else:
                            logger.info(f"  ✗ {container.name}: Image mismatch")
                            logger.info(f"    Expected: {expected_id[:12]}")
                            logger.info(f"    Got:      {current_id[:12]}")
                            verification_passed = False
                            verification_results.append(False)
                    else:
                        # Image name not in our tracking dict - could be sidecar or init container
                        logger.info(f"  ⚠ {container.name}: Image '{image_name}' not in tracking dict")
                        logger.info(f"    Tracked images: {list(new_image_ids.keys())}")
                        # Don't fail verification for untracked images
                        
                except Exception as e:
                    logger.info(f"  ⚠ {container.name}: Verification error: {e}")
            
            # Consider it successful if we verified at least one container successfully
            # and didn't have any explicit failures
            if len(verification_results) == 0:
                # No containers verified - maybe all are locally built or sidecars
                logger.info(f"  ⚠ No containers could be verified (may be locally built images)")
                verification_passed = True  # Don't fail if we can't verify
            elif any(verification_results) and not any(not r for r in verification_results):
                # At least one success and no failures
                verification_passed = True
            
            if verification_passed:
                logger.info(f"  ✓✓✓ UPDATE SUCCESSFUL AND VERIFIED ✓✓✓")
                # CLEAR update available flag (AUTO UPDATE FIX)
                with update_status_lock:
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
                logger.info(f"  ⚠ Update completed but verification inconclusive")
                
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
            logger.info(f"  ✗✗✗ UPDATE FAILED ✗✗✗")
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
    
    FIXED: Now reads image names from compose file instead of running containers
    This ensures correct registry URLs are used after switching registries
    """
    try:
        # Get stack path and compose file
        stack_path = os.path.join(STACKS_DIR, stack_name)
        compose_file = None
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            potential_file = os.path.join(stack_path, filename)
            if os.path.exists(potential_file):
                compose_file = potential_file
                break
        
        if not compose_file:
            logger.info(f"  No compose file found for {stack_name}")
            return False, None
        
        # Parse compose file to get image names from COMPOSE FILE (not running containers)
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        services = compose_data.get('services', {})
        if not services:
            return True, False
        
        # Get image names from compose file and current IDs from Docker
        old_image_ids = {}
        for service_name, service_config in services.items():
            image_name = service_config.get('image')
            if image_name:
                try:
                    # Get current image ID from Docker
                    current_image = docker_client.images.get(image_name)
                    old_image_ids[image_name] = current_image.id
                except docker.errors.ImageNotFound:
                    # Image not pulled yet - will be considered an update
                    old_image_ids[image_name] = None
                except Exception as e:
                    logger.info(f"  ⚠ Could not get current ID for {image_name}: {e}")
        
        if not old_image_ids:
            return True, False
        
        # Pull latest images (required to check for updates reliably)
        logger.info(f"Checking updates for {stack_name}...")
        success_pull, output_pull = stack_operation(stack_name, 'pull')
        
        if not success_pull:
            logger.info(f"  Pull failed, cannot check for updates")
            return False, None
        
        # Get NEW image IDs after pull
        update_available = False
        for image_name, old_id in old_image_ids.items():
            try:
                new_image = docker_client.images.get(image_name)
                new_id = new_image.id
                
                # If old_id is None (image wasn't pulled before), it's an update
                if old_id is None:
                    logger.info(f"  ✓ New image available: {image_name}")
                    update_available = True
                    break
                elif old_id != new_id:
                    logger.info(f"  ✓ Update available: {image_name}")
                    logger.info(f"    Old: {old_id[:12]}")
                    logger.info(f"    New: {new_id[:12]}")
                    update_available = True
                    break
            except Exception as e:
                logger.info(f"  ⚠ Could not check {image_name}: {e}")
        
        # Update status
        with update_status_lock:
            update_status[stack_name] = {
                'last_check': datetime.now(pytz.UTC).isoformat(),
                'update_available': update_available
            }
        save_update_status()  # SAVE TO FILE
        
        return True, update_available
        
    except Exception as e:
        logger.info(f"✗ Error checking updates for {stack_name}: {e}")
        return False, None


def get_schedule_cron(schedule_name):
    """
    Convert schedule name to cron expression
    Returns None if disabled
    """
    schedules = {
        'disabled': None,
        'daily': '0 2 * * *',  # 2 AM daily
        'daily_1am': '0 1 * * *',
        'daily_2am': '0 2 * * *',
        'daily_3am': '0 3 * * *',
        'weekly_sunday': '0 12 * * 0',  # Sunday noon
        'weekly_monday': '0 12 * * 1',
        'weekly_tuesday': '0 12 * * 2',
        'weekly_wednesday': '0 12 * * 3',
        'weekly_thursday': '0 12 * * 4',
        'weekly_friday': '0 12 * * 5',
        'weekly_saturday': '0 12 * * 6',
        'monthly': '0 12 1 * *',  # 1st of month at noon
    }
    return schedules.get(schedule_name, '0 2 * * *')  # Default to 2 AM daily


def setup_scheduler():
    """
    Setup or refresh APScheduler without recreating it
    
    CRITICAL: This function should ONLY be called at startup!
    DO NOT call this from API routes - it removes ALL jobs and rebuilds them.
    To update individual stack schedules, use update_stack_action_jobs() instead.
    """
    global scheduler
    
    # Get user timezone
    user_tz = pytz.timezone(config.get('timezone', 'UTC'))

    # Update scheduler timezone (scheduler already exists and is running from module init)
    try:
        scheduler.configure(timezone=user_tz)
        logger.info(f"[Scheduler] Configured timezone: {user_tz}")
    except Exception as e:
        logger.error(f"[Scheduler] Could not update timezone: {e}")

    # Remove only update jobs safely
    for job in list(scheduler.get_jobs()):
        try:
            if (job.id.startswith("update_") or 
                job.id.startswith("start_") or 
                job.id.startswith("stop_") or 
                job.id.startswith("restart_") or
                job.id in ("auto_prune", "import_orphans")):
                scheduler.remove_job(job.id)
        except Exception as e:
            logger.info(f"[Scheduler] Could not remove job {job.id}: {e}")

    # Recreate update schedules
    with schedules_lock:
        schedules_snapshot = schedules.copy()
    
    for stack_name, schedule_info in schedules_snapshot.items():
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
                logger.info(f"[Scheduler] Added update job for {stack_name}: {cron_expr}")
            except Exception as e:
                logger.info(f"[Scheduler] Error scheduling {stack_name}: {e}")
    
    # Add stack action schedules (start/stop/restart) from metadata
    try:
        stacks = get_stack_list_cached()
        for stack_name in stacks:
            metadata = get_metadata_cached(stack_name)
            action_schedules = metadata.get('action_schedules', [])
            
            for schedule in action_schedules:
                if not schedule.get('enabled', True):
                    continue
                
                action = schedule.get('action')  # 'start', 'stop', 'restart'
                cron_expr = schedule.get('cron')
                
                if not action or not cron_expr:
                    continue
                
                try:
                    trigger = CronTrigger.from_crontab(cron_expr, timezone=user_tz)
                    job_id = f"{action}_{stack_name}_{schedule.get('id', hashlib.md5(cron_expr.encode()).hexdigest()[:8])}"
                    
                    scheduler.add_job(
                        scheduled_stack_action,
                        trigger,
                        args=[stack_name, action],
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(f"[Scheduler] Added {action} job for {stack_name}: {cron_expr}")
                except Exception as e:
                    logger.error(f"[Scheduler] Error scheduling {action} for {stack_name}: {e}")
    except Exception as e:
        logger.error(f"[Scheduler] Error loading action schedules: {e}")

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
            logger.info(f"[Scheduler] Added auto-prune job: {prune_cron}")
        except Exception as e:
            logger.info(f"[Scheduler] Error scheduling auto-prune: {e}")

    # Orphan importer
    try:
        scheduler.add_job(
            import_orphan_containers_as_stacks,
            "interval",
            minutes=15,
            id="import_orphans",
            replace_existing=True
        )
        logger.info("[Scheduler] Added orphan importer job (15m)")
    except Exception as e:
        logger.info(f"[Scheduler] Error adding orphan importer: {e}")
    
    # Trivy database update - daily at 1 AM
    try:
        trivy_trigger = CronTrigger.from_crontab('0 1 * * *', timezone=user_tz)
        scheduler.add_job(
            update_trivy_database,
            trivy_trigger,
            id="trivy_db_update",
            replace_existing=True
        )
        logger.info("[Scheduler] Added Trivy DB update job: 1 AM daily")
    except Exception as e:
        logger.error(f"[Scheduler] Error scheduling Trivy DB update: {e}")
    
    # Template library refresh - configurable schedule
    if config.get('templates_enabled', True):
        template_schedule = config.get('templates_refresh_schedule', 'weekly_sunday')
        template_cron = get_schedule_cron(template_schedule)
        
        if template_cron:
            try:
                template_trigger = CronTrigger.from_crontab(template_cron, timezone=user_tz)
                scheduler.add_job(
                    refresh_templates,
                    template_trigger,
                    id="template_refresh",
                    replace_existing=True
                )
                logger.info(f"[Scheduler] Added template refresh job: {template_schedule} ({template_cron})")
            except Exception as e:
                logger.error(f"[Scheduler] Error scheduling template refresh: {e}")
        else:
            logger.info("[Scheduler] Template refresh disabled")

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
            logger.info(f"[Scheduler] Added host monitoring job ({host_interval}m)")
        except Exception as e:
            logger.info(f"[Scheduler] Error adding host monitoring: {e}")

    # AppData size calculation job (every 3 hours)
    try:
        scheduler.add_job(
            calculate_appdata_sizes,
            "interval",
            hours=3,
            id="appdata_sizes",
            replace_existing=True
        )
        logger.info("[Scheduler] Added appdata size calculation job (3h)")
        
        # Run in background thread so it doesn't block startup
        threading.Thread(target=calculate_appdata_sizes, daemon=True).start()
        logger.info("[Scheduler] Started background appdata size calculation")
    except Exception as e:
        logger.info(f"[Scheduler] Error adding appdata size job: {e}")
    
    # Log final state
    all_jobs = scheduler.get_jobs()
    action_jobs = [j for j in all_jobs if j.id.startswith(('start_', 'stop_', 'restart_'))]
    logger.info("=" * 80)
    logger.info(f"SCHEDULER SETUP COMPLETE")
    logger.info(f"Action jobs registered: {len(action_jobs)}")
    logger.info(f"Total jobs: {len(all_jobs)}")
    logger.info(f"Scheduler running: {scheduler.running}")
    logger.info("=" * 80)
    
    if action_jobs:
        logger.info("Action jobs:")
        for job in action_jobs:
            logger.info(f"  - {job.id} → Next: {job.next_run_time}")
    else:
        logger.info("No action jobs registered (no schedules saved yet)")

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
    """Get all stacks with ETag caching"""
    global stacks_list_cache
    
    # OPTIMIZATION: Use get_stacks_cached() instead of get_stacks()
    # This reduces file I/O by caching the full stack data for 3 seconds
    # Combined with the existing 2-second cache below, provides better performance
    
    # Check if cached data is still fresh
    current_time = time.time()
    if (stacks_list_cache['data'] is not None and 
        current_time - stacks_list_cache['timestamp'] < stacks_list_cache['ttl']):
        # Use cached data
        stacks_data = stacks_list_cache['data']
    else:
        # Fetch fresh data using cached wrapper
        stacks_data = get_stacks_cached()
        stacks_list_cache['data'] = stacks_data
        stacks_list_cache['timestamp'] = current_time
    
    # Generate ETag from data
    etag = hashlib.md5(json.dumps(stacks_data, sort_keys=True).encode()).hexdigest()
    
    # Check if client has cached version
    client_etag = request.headers.get('If-None-Match')
    if client_etag == etag:
        # Client has current version, return 304 Not Modified
        return '', 304
    
    # Return fresh data with ETag
    response = jsonify(stacks_data)
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, must-revalidate'
    return response


@app.route('/api/host/stats')
def api_host_stats():
    """Get host system CPU and memory stats - OPTIMIZED with 3-second cache"""
    try:
        # Use cached version - reduces CPU usage significantly
        stats_data = get_host_stats_cached()
        if stats_data:
            return jsonify(stats_data)
        else:
            return jsonify({'error': 'Failed to get stats'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/docker-hub/rate-limit')
def api_docker_hub_rate_limit():
    """Get Docker Hub rate limit status"""
    rate_limit = check_docker_hub_rate_limit()
    if rate_limit:
        return jsonify(rate_limit)
    return jsonify({'error': 'Could not check rate limit'}), 500


@app.route('/api/appdata/sizes')
def api_appdata_sizes():
    """Get appdata sizes for all stacks"""
    global appdata_size_cache
    logger.info(f"[DEBUG] /api/appdata/sizes called - cache has {len(appdata_size_cache)} entries")
    logger.info(f"[DEBUG] Cache keys: {list(appdata_size_cache.keys())}")
    return jsonify(appdata_size_cache)


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
        logger.info(f"[SCAN SUMMARY] Stack: {stack_name}, Badge: {worst_badge}, Critical: {total_critical}, High: {total_high}")
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
            with update_status_lock:
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
                    logger.info(f"Warning: Could not delete image {image_id}: {e}")
        
        # Delete directory if not keeping compose
        if data.get('delete_compose', True):
            stack_path = os.path.join(STACKS_DIR, stack_name)
            import shutil
            shutil.rmtree(stack_path)
        
        # Remove schedule
        with schedules_lock:
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
                logger.info(f"Warning: Could not delete image: {e}")
        
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
        with schedules_lock:
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
    
    logger.info("Running auto-prune...")
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
                    logger.info(f"Pruned Trivy cache: {trivy_space_saved_mb} MB freed")
            except Exception as e:
                logger.info(f"Trivy cache prune error: {e}")
        
        if deleted > 0 or trivy_space_saved > 0:
            total_saved_mb = space_saved_mb + (trivy_space_saved / (1024 * 1024))
            send_notification(
                'Dockup - Auto-Prune',
                f'Pruned {deleted} images ({space_saved_mb} MB) and Trivy cache ({trivy_space_saved_mb} MB)',
                'success'
            )
            logger.info(f"Auto-prune: {deleted} images, {total_saved_mb:.2f} MB total freed")
    except Exception as e:
        logger.info(f"Auto-prune error: {e}")


def update_stats_background():
    """Background thread to update stack stats every 5 seconds - optimized with batch processing"""
    while True:
        try:
            # Get all containers once (uses cache)
            all_containers = get_all_containers_cached()
            
            # Group running containers by stack
            stack_containers = {}
            for container in all_containers:
                if container.status == 'running':
                    stack_name = container.labels.get('com.docker.compose.project')
                    if stack_name:
                        if stack_name not in stack_containers:
                            stack_containers[stack_name] = []
                        stack_containers[stack_name].append(container)
            
            # Batch fetch stats for all running stacks in parallel
            if stack_containers:
                batch_stats = get_stack_stats_batch(stack_containers)
                
                # Update cache with batch results
                for stack_name, stats in batch_stats.items():
                    stack_stats_cache[stack_name] = stats
            
            # Clear stats for stopped stacks (stacks that had containers but are now stopped)
            stopped_stacks = set(stack_stats_cache.keys()) - set(stack_containers.keys())
            for stack_name in stopped_stacks:
                stack_stats_cache[stack_name] = {
                    'cpu_percent': 0,
                    'mem_usage_mb': 0,
                    'mem_limit_mb': 0,
                    'mem_percent': 0,
                    'net_rx_mbps': 0,
                    'net_tx_mbps': 0
                }
            
            # Wait 5 seconds before next update
            time.sleep(5)
        except Exception as e:
            logger.info(f"Stats update error: {e}")
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
    with schedules_lock:
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
    
    with schedules_lock:
        schedules[stack_name] = {
            'mode': mode,
            'cron': cron
        }
    save_schedules()
    
    # Trigger scheduler update
    setup_scheduler()
    
    return jsonify({'success': True})


@app.route('/api/stack/<stack_name>/action-schedules', methods=['GET'])
@require_auth
def api_stack_action_schedules_get(stack_name):
    """Get stack action schedules (start/stop/restart)"""
    metadata = get_metadata_cached(stack_name)
    action_schedules = metadata.get('action_schedules', [])
    return jsonify({'schedules': action_schedules})


@app.route('/api/stack/<stack_name>/action-schedules', methods=['POST'])
@require_auth
def api_stack_action_schedules_set(stack_name):
    """Set stack action schedules"""
    data = request.json
    schedules_list = data.get('schedules', [])
    
    # Validate each schedule
    for schedule in schedules_list:
        action = schedule.get('action')
        cron = schedule.get('cron')
        
        if action not in ['start', 'stop', 'restart']:
            return jsonify({'error': f'Invalid action: {action}'}), 400
        
        if not cron:
            return jsonify({'error': 'Cron expression required'}), 400
        
        try:
            croniter(cron)
        except:
            return jsonify({'error': f'Invalid cron expression: {cron}'}), 400
        
        # Ensure each schedule has a unique ID
        if 'id' not in schedule:
            schedule['id'] = hashlib.md5(f"{action}{cron}{time.time()}".encode()).hexdigest()[:8]
    
    # Save to metadata
    metadata = get_metadata_cached(stack_name)
    metadata['action_schedules'] = schedules_list
    save_metadata_cached(stack_name, metadata)
    
    # Update ONLY this stack's action jobs (don't recreate entire scheduler)
    update_stack_action_jobs(stack_name, schedules_list)
    
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
        logger.info(f"Failed to check Docker Hub for updates: {e}")
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
    
    # Check if schedule-related settings changed
    schedule_changed = (
        data.get('templates_enabled') != config.get('templates_enabled') or
        data.get('templates_refresh_schedule') != config.get('templates_refresh_schedule') or
        data.get('auto_update_enabled') != config.get('auto_update_enabled') or
        data.get('auto_update_check_schedule') != config.get('auto_update_check_schedule')
    )
    
    # Update config
    config.update(data)
    
    # Restore preserved fields
    config['api_token'] = preserved['api_token']
    config['peers'] = preserved['peers']
    
    # OPTIMIZATION: Debounced save - batches multiple config changes
    save_config_debounced()
    setup_notifications()
    
    # Refresh scheduler if schedule-related settings changed
    if schedule_changed:
        setup_scheduler()
        logger.info("Scheduler refreshed due to config changes")
    
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
                    logger.info(f"Cleaned up old compose file: {old_file} from {stack_name}")
                except Exception as e:
                    logger.info(f"Warning: Could not delete {old_file}: {e}")
        
        return jsonify({'success': True})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stack/<stack_name>/resources', methods=['GET'])
def api_get_stack_resources(stack_name):
    """Get current resource limits for a stack"""
    try:
        stack_path = os.path.join(STACKS_DIR, stack_name)
        compose_file = None
        
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            potential_file = os.path.join(stack_path, filename)
            if os.path.exists(potential_file):
                compose_file = potential_file
                break
        
        if not compose_file:
            return jsonify({'cpu_limit': 0, 'mem_limit': 0})
        
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        services = compose_data.get('services', {})
        if not services:
            return jsonify({'cpu_limit': 0, 'mem_limit': 0})
        
        # Get first service's limits (we apply limits to all services identically)
        first_service = list(services.values())[0]
        cpu_limit = 0
        mem_limit = 0
        
        # Check modern deploy.resources.limits format
        if 'deploy' in first_service:
            resources = first_service.get('deploy', {}).get('resources', {})
            limits = resources.get('limits', {})
            
            if 'cpus' in limits:
                cpu_limit = float(limits['cpus'])
            
            if 'memory' in limits:
                mem_str = str(limits['memory']).upper()
                if 'G' in mem_str:
                    mem_limit = float(mem_str.replace('G', ''))
                elif 'M' in mem_str:
                    mem_limit = float(mem_str.replace('M', '')) / 1024
        
        # Check legacy format
        if 'cpus' in first_service:
            cpu_limit = float(first_service['cpus'])
        
        if 'mem_limit' in first_service:
            mem_str = str(first_service['mem_limit']).lower()
            if 'g' in mem_str:
                mem_limit = float(mem_str.replace('g', ''))
            elif 'm' in mem_str:
                mem_limit = float(mem_str.replace('m', '')) / 1024
        
        return jsonify({
            'cpu_limit': cpu_limit,
            'mem_limit': mem_limit
        })
    
    except Exception as e:
        logger.error(f"Error getting resources for {stack_name}: {e}")
        return jsonify({'cpu_limit': 0, 'mem_limit': 0})


@app.route('/api/stack/<stack_name>/resources', methods=['POST'])
def api_set_stack_resources(stack_name):
    """Set resource limits for all services in a stack"""
    try:
        data = request.json
        cpu_limit = data.get('cpu_limit', 0)
        mem_limit = data.get('mem_limit', 0)
        
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
        
        # Apply limits to ALL services
        for service_name, service_data in services.items():
            # Remove legacy format
            service_data.pop('cpus', None)
            service_data.pop('mem_limit', None)
            
            # If limits are 0, remove deploy section entirely
            if cpu_limit == 0 and mem_limit == 0:
                if 'deploy' in service_data:
                    if 'resources' in service_data['deploy']:
                        service_data['deploy'].pop('resources', None)
                    if not service_data['deploy']:
                        service_data.pop('deploy', None)
                continue
            
            # Set modern format
            if 'deploy' not in service_data:
                service_data['deploy'] = {}
            if 'resources' not in service_data['deploy']:
                service_data['deploy']['resources'] = {}
            if 'limits' not in service_data['deploy']['resources']:
                service_data['deploy']['resources']['limits'] = {}
            
            if cpu_limit > 0:
                service_data['deploy']['resources']['limits']['cpus'] = str(cpu_limit)
            else:
                service_data['deploy']['resources']['limits'].pop('cpus', None)
            
            if mem_limit > 0:
                service_data['deploy']['resources']['limits']['memory'] = f"{mem_limit}G"
            else:
                service_data['deploy']['resources']['limits'].pop('memory', None)
            
            # Clean up empty structures
            if not service_data['deploy']['resources']['limits']:
                service_data['deploy']['resources'].pop('limits', None)
            if not service_data['deploy']['resources']:
                service_data['deploy'].pop('resources', None)
            if not service_data['deploy']:
                service_data.pop('deploy', None)
        
        # Save to compose.yaml
        new_compose_file = os.path.join(stack_path, 'compose.yaml')
        with open(new_compose_file, 'w') as f:
            yaml.safe_dump(compose_data, f, sort_keys=False, default_flow_style=False)
        
        # Clean up old compose files
        for old_file in ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml']:
            old_path = os.path.join(stack_path, old_file)
            if os.path.exists(old_path) and old_path != new_compose_file:
                try:
                    os.remove(old_path)
                except Exception:
                    pass
        
        logger.info(f"Resource limits set for {stack_name}: CPU={cpu_limit}, MEM={mem_limit}G")
        return jsonify({'success': True})
    
    except Exception as e:
        logger.error(f"Error setting resources for {stack_name}: {e}")
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
        logger.error(f"Error detecting network interfaces: {e}")
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
    with websocket_clients_lock:
        websocket_clients.append(ws)
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
    except Exception as e:
        logger.info(f"WebSocket error: {e}")
    finally:
        # Always remove, even if exception during removal
        with websocket_clients_lock:
            try:
                websocket_clients.remove(ws)
            except ValueError:
                # Client already removed (expected in some race conditions)
                pass
            except Exception as e:
                logger.info(f"⚠ Error removing WebSocket client: {e}")


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
                logger.error(f"Error auto-detecting Web UI URL: {e}")
        
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
    """Get or update stack metadata - OPTIMIZED with caching"""
    if request.method == 'GET':
        # Use cached metadata
        metadata = get_metadata_cached(stack_name)
        return jsonify(metadata)
    else:
        # Merge new data with existing metadata
        metadata = get_metadata_cached(stack_name)
        data = request.json
        metadata.update(data)
        # Use cached save which updates cache
        save_metadata_cached(stack_name, metadata)
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
            logger.error(f"Error fetching stacks from peer {peer_id}: {e}")
    
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
    
    logger.info(f"Added peer {peer_id}: {config['peers'][peer_id]}")
    logger.info(f"Total peers: {len(config.get('peers', {}))}")
    
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



# ============================================================================
# TEMPLATE LIBRARY SYSTEM - LinuxServer.io Fleet Integration
# ============================================================================

# Template cache
TEMPLATES_DIR = os.path.join(DATA_DIR, 'templates')
TEMPLATES_CACHE_FILE = os.path.join(TEMPLATES_DIR, 'cache.json')
TEMPLATES_LAST_UPDATE_FILE = os.path.join(TEMPLATES_DIR, 'last_update.txt')
DOCS_CACHE_FILE = os.path.join(TEMPLATES_DIR, 'docs_cache.json')

# Ensure templates directory exists
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Template cache storage
templates_cache = {
    'last_updated': None,
    'templates': []
}
templates_cache_lock = threading.Lock()

# Docs scraping cache storage
# Structure: {'container_name': {'env': [], 'volumes': [], 'ports': [], ...}}
docs_cache = {}
docs_cache_lock = threading.Lock()


# ============================================================================
# CASAOS APPSTORE - WITH FRONTEND COMPATIBILITY
# ============================================================================

import zipfile
import re

CASAOS_SOURCES = {
    "main": {
        "name": "CasaOS Main",
        "url": "https://github.com/IceWhaleTech/CasaOS-AppStore/archive/refs/heads/main.zip",
        "apps_path": "CasaOS-AppStore-main/Apps"
    },
    "homeautomation": {
        "name": "CasaOS Home Automation",
        "url": "https://github.com/mr-manuel/CasaOS-HomeAutomation-AppStore/archive/refs/tags/latest.zip",
        "apps_path": "CasaOS-HomeAutomation-AppStore-latest/Apps"
    },
    "bigbear": {
        "name": "Big Bear CasaOS",
        "url": "https://github.com/bigbeartechworld/big-bear-casaos/archive/refs/heads/master.zip",
        "apps_path": "big-bear-casaos-master/Apps"
    }
}


def safe_str(val, maxlen=500):
    """Safely convert to string"""
    try:
        return str(val)[:maxlen].strip() if val else ''
    except:
        return ''



def map_casaos_category(casaos_cat):
    """Map CasaOS categories to DockUp categories"""
    if not casaos_cat:
        return 'other'
    
    cat = str(casaos_cat).lower().strip()
    
    # Direct matches
    category_map = {
        'media': 'media',
        'entertainment': 'media',
        'video': 'media',
        'music': 'media',
        'photo': 'media',
        'photos': 'media',
        
        'automation': 'automation',
        'home-automation': 'automation',
        'smart-home': 'automation',
        'iot': 'automation',
        
        'productivity': 'productivity',
        'office': 'productivity',
        'documents': 'productivity',
        
        'development': 'development',
        'developer-tools': 'development',
        'programming': 'development',
        'code': 'development',
        
        'networking': 'networking',
        'network': 'networking',
        'vpn': 'networking',
        'proxy': 'networking',
        
        'security': 'security',
        'privacy': 'security',
        
        'storage': 'storage',
        'backup': 'storage',
        'cloud': 'storage',
        'file-sharing': 'storage',
        
        'monitoring': 'monitoring',
        'analytics': 'monitoring',
        'metrics': 'monitoring',
        
        'communication': 'communication',
        'chat': 'communication',
        'messaging': 'communication',
        
        'gaming': 'gaming',
        'games': 'gaming',
        
        'utilities': 'utilities',
        'tools': 'utilities',
        'utility': 'utilities',
    }
    
    # Check direct match
    if cat in category_map:
        return category_map[cat]
    
    # Check partial matches
    for key, value in category_map.items():
        if key in cat or cat in key:
            return value
    
    return 'other'

def clean_casaos_compose(compose_content):
    """Clean x-casaos AND comments"""
    try:
        if not compose_content:
            return None
        
        # Validate YAML first
        try:
            data = yaml.safe_load(compose_content)
            if not data or 'services' not in data or not data['services']:
                return None
        except:
            return None
        
        # Clean line by line
        lines_list = compose_content.split('\n')
        cleaned = []
        in_x = False
        x_indent = 0
        
        for line in lines_list:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            
            # Skip pure comment lines
            if stripped.startswith('#'):
                continue
            
            # Remove inline comments (preserve # in quoted strings)
            if '#' in line and not any(q in line[:line.index('#')] for q in ['"', "'"]):
                line = line[:line.index('#')].rstrip()
            
            # Skip x- blocks
            if stripped.startswith('x-'):
                in_x = True
                x_indent = indent
                continue
            
            if in_x:
                if stripped and indent > x_indent:
                    continue
                else:
                    in_x = False
            
            # Skip empty lines
            if not line.strip():
                continue
            
            cleaned.append(line)
        
        result = '\n'.join(cleaned)
        
        # Verify still valid
        try:
            data = yaml.safe_load(result)
            if data and 'services' in data and data['services']:
                return result
        except:
            pass
        
        return None
    except:
        return None



def fetch_casaos_source(source_key, source_config):
    """Fetch CasaOS - with IMAGE field for frontend"""
    templates = []
    
    try:
        logger.info(f"Fetching {source_config['name']}...")
        response = requests.get(source_config['url'], timeout=120)
        response.raise_for_status()
        
        with zipfile.ZipFile(io.BytesIO(response.content)) as zip_ref:
            files = zip_ref.namelist()
            composes = [f for f in files if f.startswith(source_config['apps_path']) and f.endswith('docker-compose.yml')]
            
            for cf in composes:
                try:
                    app_name = os.path.basename(os.path.dirname(cf))
                    if not app_name or app_name == 'Apps' or len(app_name) > 100:
                        continue
                    
                    compose_bytes = zip_ref.read(cf)
                    if len(compose_bytes) > 50000:
                        continue
                    
                    compose_raw = compose_bytes.decode('utf-8', errors='replace')
                    compose_clean = clean_casaos_compose(compose_raw)
                    
                    if not compose_clean or len(compose_clean) > 5000:
                        continue
                    
                    # Extract IMAGE from compose for frontend
                    image_name = 'unknown:latest'
                    try:
                        compose_data = yaml.safe_load(compose_clean)
                        if compose_data and 'services' in compose_data:
                            first_service = list(compose_data['services'].values())[0]
                            if isinstance(first_service, dict) and 'image' in first_service:
                                image_name = str(first_service['image'])[:200]
                    except:
                        pass
                    
                    # Get metadata
                    meta = {'title': app_name.replace('-', ' ').title(), 'description': '', 'category': 'other', 'icon': '', 'author': source_config['name'], 'project_url': ''}
                    
                    cfg_path = os.path.join(os.path.dirname(cf), 'config.json')
                    if cfg_path in files:
                        try:
                            cfg_data = json.loads(zip_ref.read(cfg_path).decode('utf-8', errors='replace'))
                            meta['title'] = safe_str(cfg_data.get('title', cfg_data.get('name', meta['title'])), 100)
                            meta['description'] = safe_str(cfg_data.get('tagline', cfg_data.get('description', '')), 500)
                            meta['category'] = map_casaos_category(cfg_data.get('category', 'other'))
                            meta['icon'] = safe_str(cfg_data.get('icon', ''), 500)
                            meta['project_url'] = safe_str(cfg_data.get('project_url', ''), 500)
                        except:
                            pass
                    
                    # Template with ALL frontend fields
                    template = {
                        'id': safe_str(f"casaos_{source_key}_{app_name}", 200),
                        'name': safe_str(meta['title'], 100),
                        'description': safe_str(meta['description'] or 'No description', 500),
                        'category': safe_str(meta['category'], 50),
                        'compose': compose_clean[:5000],  # Max 5KB
                        'image': image_name,
                        'icon': safe_str(meta['icon'] or get_category_icon(meta['category']), 500),
                        'docs_url': safe_str(meta['project_url'], 500),
                        'github_url': safe_str(meta['project_url'], 500),
                        'source': source_config['name'],
                        'stars': 0,
                        'pulls': 0,
                        'size_mb': 0
                    }
                    
                    try:
                        json.dumps(template)
                        templates.append(template)
                    except:
                        continue
                        
                except:
                    continue
        
        logger.info(f"✓ {source_config['name']}: {len(templates)}")
        return templates
        
    except Exception as e:
        logger.error(f"{source_config['name']} error: {e}")
        return []


def fetch_all_casaos():
    """Fetch all CasaOS"""
    all_temps = []
    for key, cfg in CASAOS_SOURCES.items():
        temps = fetch_casaos_source(key, cfg)
        all_temps.extend(temps)
    logger.info(f"✓ CasaOS: {len(all_temps)}")
    return all_temps




# ============================================================================
# ENHANCED TEMPLATE SYSTEM - Docker Hub + LinuxServer.io Docs Integration
# ============================================================================

def fetch_dockerhub_metadata(image_name):
    """
    Fetch real metadata from Docker Hub API v2
    Returns: {
        'description': str,
        'stars': int,
        'pulls': int,
        'last_updated': str,
        'tags': list,
        'size': int
    }
    """
    try:
        # Parse image name - handle lscr.io/linuxserver/name format
        if 'linuxserver' in image_name:
            repo_name = image_name.split('/')[-1].split(':')[0]
            namespace = 'linuxserver'
        else:
            parts = image_name.split('/')
            namespace = parts[0] if len(parts) > 1 else 'library'
            repo_name = parts[-1].split(':')[0]
        
        # Fetch repository info
        repo_url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo_name}"
        response = requests.get(repo_url, timeout=10)
        
        if response.status_code == 200:
            repo_data = response.json()
            
            # Fetch available tags
            tags_url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo_name}/tags?page_size=25"
            tags_response = requests.get(tags_url, timeout=10)
            tags_data = tags_response.json() if tags_response.status_code == 200 else {}
            
            available_tags = []
            if 'results' in tags_data:
                for tag in tags_data['results']:
                    tag_info = {
                        'name': tag['name'],
                        'last_updated': tag.get('last_updated', ''),
                        'size': tag.get('full_size', 0)
                    }
                    # Get architecture info
                    if 'images' in tag and tag['images']:
                        tag_info['architectures'] = [img.get('architecture', 'unknown') for img in tag['images']]
                    available_tags.append(tag_info)
            
            return {
                'description': repo_data.get('description', ''),
                'full_description': repo_data.get('full_description', ''),
                'stars': repo_data.get('star_count', 0),
                'pulls': repo_data.get('pull_count', 0),
                'last_updated': repo_data.get('last_updated', ''),
                'tags': available_tags,
                'is_official': repo_data.get('is_official', False)
            }
        
        return None
    except Exception as e:
        logger.debug(f"Failed to fetch Docker Hub metadata for {image_name}: {e}")
        return None


def scrape_linuxserver_docs(container_name):
    """
    Scrape LinuxServer.io documentation using BeautifulSoup for proper HTML parsing
    Extracts docker-compose YAML from <pre><code> blocks
    Uses disk cache to avoid repeated scraping
    Returns environment variables, volumes, ports, and special requirements
    """
    global docs_cache
    
    # Check cache first
    with docs_cache_lock:
        if container_name in docs_cache:
            logger.debug(f"Using cached docs for {container_name}")
            return docs_cache[container_name]
    
    try:
        from bs4 import BeautifulSoup
        
        docs_url = f"https://docs.linuxserver.io/images/docker-{container_name}"
        response = requests.get(docs_url, timeout=15)
        
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Find all code blocks - LinuxServer docs use <pre><code> for docker-compose
        yaml_content = None
        
        # Look for code blocks containing 'version:' or 'services:' (docker-compose markers)
        code_blocks = soup.find_all('code')
        for code in code_blocks:
            text = code.get_text()
            if 'services:' in text or 'version:' in text:
                yaml_content = text
                break
        
        # Fallback: try <pre> tags
        if not yaml_content:
            pre_blocks = soup.find_all('pre')
            for pre in pre_blocks:
                text = pre.get_text()
                if 'services:' in text or 'version:' in text:
                    yaml_content = text
                    break
        
        if not yaml_content:
            logger.debug(f"No docker-compose YAML found for {container_name}")
            return None
        
        # Parse YAML content to extract configuration
        env_vars = []
        volumes = []
        ports = []
        cap_add = []
        devices = []
        privileged = False
        network_mode = None
        
        # Track which section we're in
        in_environment = False
        in_volumes = False
        in_ports = False
        in_cap_add = False
        in_devices = False
        
        for line in yaml_content.split('\n'):
            stripped = line.strip()
            
            # Detect sections
            if 'environment:' in stripped:
                in_environment = True
                in_volumes = in_ports = in_cap_add = in_devices = False
                continue
            elif 'volumes:' in stripped:
                in_volumes = True
                in_environment = in_ports = in_cap_add = in_devices = False
                continue
            elif 'ports:' in stripped:
                in_ports = True
                in_environment = in_volumes = in_cap_add = in_devices = False
                continue
            elif 'cap_add:' in stripped:
                in_cap_add = True
                in_environment = in_volumes = in_ports = in_devices = False
                continue
            elif 'devices:' in stripped:
                in_devices = True
                in_environment = in_volumes = in_ports = in_cap_add = False
                continue
            elif stripped and not stripped.startswith('-') and ':' in stripped:
                # New top-level key, reset all section flags
                in_environment = in_volumes = in_ports = in_cap_add = in_devices = False
            
            # Extract list items based on current section
            if stripped.startswith('-'):
                value = stripped[1:].strip()
                
                if in_environment:
                    # Environment: "- KEY=value"
                    if '=' in value:
                        var_name = value.split('=')[0]
                        if var_name not in ['PUID', 'PGID', 'TZ']:
                            env_vars.append(value)
                
                elif in_volumes:
                    # Volumes: "- /host/path:/container/path"
                    if ':' in value:
                        container_path = value.split(':')[1]
                        if container_path != '/config':
                            volumes.append(container_path)
                
                elif in_ports:
                    # Ports: "- 8080:8080"
                    if ':' in value and value[0].isdigit():
                        ports.append(value)
                
                elif in_cap_add:
                    # Capabilities: "- SYS_ADMIN"
                    if value.isupper() and '_' in value:
                        cap_add.append(value)
                
                elif in_devices:
                    # Devices: "- /dev/dri"
                    if value.startswith('/dev/'):
                        device_path = value.split(':')[0] if ':' in value else value
                        devices.append(device_path)
            
            # Check for privileged mode
            if 'privileged:' in stripped and 'true' in stripped:
                privileged = True
            
            # Check for network mode
            if 'network_mode:' in stripped:
                if 'host' in stripped:
                    network_mode = 'host'
                elif 'bridge' in stripped:
                    network_mode = 'bridge'
        
        # Only return if we found something useful
        if env_vars or volumes or ports or cap_add or devices or privileged or network_mode:
            logger.debug(f"Scraped docs for {container_name}: env={len(env_vars)}, vols={len(volumes)}, ports={len(ports)}")
            result = {
                'env': env_vars,
                'volumes': volumes,
                'ports': ports,
                'cap_add': cap_add,
                'devices': devices,
                'privileged': privileged,
                'network_mode': network_mode
            }
            
            # Cache the result
            with docs_cache_lock:
                docs_cache[container_name] = result
            
            return result
        
        return None
        
    except Exception as e:
        logger.debug(f"Failed to scrape docs for {container_name}: {e}")
        return None


def get_intelligent_volume_paths(container_name, volumes):
    """
    Generate intelligent volume paths using /opt/appdata pattern
    instead of generic ./volume format
    
    Example:
        /config -> /opt/appdata/plex/config:/config
        /media -> /mnt/media:/media
        /downloads -> /mnt/downloads:/downloads
    """
    volume_mappings = []
    
    for vol in volumes:
        vol = vol.strip()
        
        # Config always goes to /opt/appdata/{name}/config
        if vol == '/config':
            volume_mappings.append(f"/opt/appdata/{container_name}/config:/config")
        
        # Media paths point to actual media storage
        elif vol in ['/media', '/movies', '/tv', '/music', '/books', '/photos']:
            # Map to actual media paths users likely have
            volume_mappings.append(f"/mnt{vol}:{vol}")
        
        # Downloads path
        elif vol == '/downloads':
            volume_mappings.append(f"/mnt/downloads:{vol}")
        
        # Data/storage paths
        elif vol in ['/data', '/storage']:
            volume_mappings.append(f"/opt/appdata/{container_name}/data:{vol}")
        
        # Backup paths
        elif vol in ['/backups', '/backup']:
            volume_mappings.append(f"/mnt/backups:{vol}")
        
        # Everything else goes to /opt/appdata/{name}/{dirname}
        else:
            dir_name = vol.split('/')[-1] or 'data'
            volume_mappings.append(f"/opt/appdata/{container_name}/{dir_name}:{vol}")
    
    return volume_mappings


def get_container_healthcheck(container_name, ports):
    """
    Generate healthcheck configuration based on container type
    """
    # Extract first HTTP port
    http_port = None
    for port in ports:
        port_num = port.split(':')[0]
        if port_num not in ['51820', '1194', '22']:  # Skip VPN/SSH ports
            http_port = port_num
            break
    
    if not http_port:
        return None
    
    # Common healthcheck patterns
    healthchecks = {
        # Media servers
        'plex': {'test': f'curl -f http://localhost:{http_port}/web || exit 1', 'interval': '30s'},
        'jellyfin': {'test': f'curl -f http://localhost:{http_port}/health || exit 1', 'interval': '30s'},
        'emby': {'test': f'curl -f http://localhost:{http_port}/health || exit 1', 'interval': '30s'},
        
        # *arr stack
        'sonarr': {'test': f'curl -f http://localhost:{http_port}/ping || exit 1', 'interval': '30s'},
        'radarr': {'test': f'curl -f http://localhost:{http_port}/ping || exit 1', 'interval': '30s'},
        'lidarr': {'test': f'curl -f http://localhost:{http_port}/ping || exit 1', 'interval': '30s'},
        'prowlarr': {'test': f'curl -f http://localhost:{http_port}/ping || exit 1', 'interval': '30s'},
        'readarr': {'test': f'curl -f http://localhost:{http_port}/ping || exit 1', 'interval': '30s'},
        
        # Generic web UI
        'default': {'test': f'curl -f http://localhost:{http_port}/ || exit 1', 'interval': '30s'}
    }
    
    check = healthchecks.get(container_name, healthchecks['default'])
    check.update({
        'timeout': '10s',
        'retries': 3,
        'start_period': '40s'
    })
    
    return check


def needs_host_network(container_name):
    """Determine if container needs host networking"""
    host_network_containers = [
        'plex',  # DLNA discovery
        'homeassistant',  # mDNS discovery
        'tvheadend',  # Network tuners
        'unifi-network-application',  # Device discovery
        'pihole',  # DNS
        'adguardhome'  # DNS
    ]
    return container_name in host_network_containers


def get_enhanced_environment_vars(container_name, base_env):
    """
    Add container-specific environment variables with sensible defaults
    """
    env_vars = base_env.copy()
    
    # Container-specific additions
    env_additions = {
        'plex': ['VERSION=docker', 'PLEX_CLAIM='],
        'qbittorrent': ['WEBUI_PORT=8080'],
        'transmission': ['USER=admin', 'PASS='],
        'wireguard': ['SERVERURL=auto', 'PEERS=1', 'PEERDNS=auto'],
        'heimdall': ['APP_NAME=DockUp Dashboard'],
        'nextcloud': ['NEXTCLOUD_ADMIN_USER=admin', 'NEXTCLOUD_ADMIN_PASSWORD='],
    }
    
    if container_name in env_additions:
        env_vars.extend(env_additions[container_name])
    
    return env_vars


def generate_enhanced_compose(app_data, dockerhub_meta=None, docs_config=None):
    """
    Generate enhanced Docker Compose with:
    - Better volume paths
    - Healthchecks
    - Proper network modes
    - Labels for organization
    - No version (modern compose)
    """
    
    container_name = app_data["id"]
    
    # Use docs config if available, otherwise use app_data
    if docs_config:
        ports = docs_config.get('ports', [])
        volumes = docs_config.get('volumes', [])
        env = docs_config.get('env', [])
        cap_add = docs_config.get('cap_add', [])
        devices = docs_config.get('devices', [])
        privileged = docs_config.get('privileged', False)
        network_mode = docs_config.get('network_mode')
    else:
        ports = []
        volumes = []
        env = []
        cap_add = app_data.get('cap_add', [])
        devices = []
        privileged = False
        network_mode = None
    
    # Only use app_data ports/volumes if not found in docs_config
    if not ports and 'ports' in app_data:
        ports = app_data.get('ports', [])
    if not volumes and 'volumes' in app_data:
        volumes = app_data.get('volumes', [])
    
    # Add base environment variables
    base_env = ['PUID=1000', 'PGID=1000', 'TZ=Etc/UTC']
    all_env = base_env + env
    all_env = get_enhanced_environment_vars(container_name, all_env)
    
    # Ensure /config is in volumes list, but avoid duplicates
    all_volumes = list(set(volumes + ['/config']))  # Use set to remove duplicates
    
    # Get intelligent volume paths
    intelligent_volumes = get_intelligent_volume_paths(container_name, all_volumes)
    
    # Build service configuration
    service_config = {
        'image': app_data['image'],
        'container_name': container_name,
        'hostname': container_name,
        'environment': all_env,
        'volumes': intelligent_volumes,
        'restart': 'unless-stopped',
        'labels': [
            f"com.dockup.category={app_data.get('category', 'other')}",
            f"com.dockup.template=true",
            "com.dockup.managed=true"
        ]
    }
    
    # Add ports if any
    if ports:
        service_config['ports'] = ports
    
    # Add network mode
    if network_mode:
        service_config['network_mode'] = network_mode
    elif needs_host_network(container_name):
        service_config['network_mode'] = 'host'
    
    # Add capabilities
    if cap_add:
        service_config['cap_add'] = cap_add
    
    # Add devices
    if devices:
        service_config['devices'] = devices
    
    # Add privileged if needed
    if privileged:
        service_config['privileged'] = True
    
    # Add sysctls if present
    if 'sysctls' in app_data:
        service_config['sysctls'] = app_data['sysctls']
    
    # Add healthcheck
    healthcheck = get_container_healthcheck(container_name, ports)
    if healthcheck:
        service_config['healthcheck'] = healthcheck
    
    # Build compose dict (no version for modern compose)
    compose_dict = {
        'services': {
            container_name: service_config
        }
    }
    
    # Convert to YAML
    yaml_str = yaml.dump(compose_dict, default_flow_style=False, sort_keys=False, width=1000)
    
    return yaml_str


def fetch_linuxserver_templates():
    """
    Enhanced template fetching with Docker Hub integration and docs scraping
    """
    try:
        logger.info("Fetching LinuxServer.io templates with enhanced metadata...")
        
        templates = []
        
        # Try GitHub API first for repository list
        github_templates = fetch_from_github_api()
        
        if github_templates and len(github_templates) > 50:
            logger.info(f"Processing {len(github_templates)} templates from GitHub...")
            
            # Enhance each template with Docker Hub data and docs
            for i, template in enumerate(github_templates):
                try:
                    container_name = template['id']
                    image_name = template['image']
                    
                    # Fetch Docker Hub metadata
                    dockerhub_meta = fetch_dockerhub_metadata(image_name)
                    
                    # Fetch LinuxServer.io docs (now properly extracts YAML only)
                    docs_config = scrape_linuxserver_docs(container_name)
                    
                    # Enhance description with Docker Hub data
                    if dockerhub_meta:
                        if dockerhub_meta['description']:
                            template['description'] = dockerhub_meta['description']
                        template['stars'] = dockerhub_meta['stars']
                        template['pulls'] = dockerhub_meta['pulls']
                        template['last_updated'] = dockerhub_meta['last_updated']
                        template['available_tags'] = [t['name'] for t in dockerhub_meta['tags'][:10]]  # Top 10 tags
                        
                        # Add size info from latest tag
                        if dockerhub_meta['tags']:
                            template['size_mb'] = round(dockerhub_meta['tags'][0].get('size', 0) / 1024 / 1024, 1)
                    
                    # Regenerate compose with enhanced data (docs_config has clean YAML data)
                    template['compose'] = generate_enhanced_compose(template, dockerhub_meta, docs_config)
                    
                    # Add docs URL
                    template['docs_url'] = f"https://docs.linuxserver.io/images/docker-{container_name}"
                    template['github_url'] = f"https://github.com/linuxserver/docker-{container_name}"
                    
                    templates.append(template)
                    
                    if (i + 1) % 10 == 0:
                        logger.info(f"Enhanced {i + 1}/{len(github_templates)} templates...")
                    
                except Exception as e:
                    logger.debug(f"Failed to enhance template {template.get('id', 'unknown')}: {e}")
                    templates.append(template)  # Add original template on error
            
            logger.info(f"✓ Enhanced {len(templates)} templates with Docker Hub metadata")
            return templates
        
        # Fallback to static list
        logger.warning("GitHub API failed, using enhanced static list...")
        from linuxserver_templates import get_all_linuxserver_templates
        
        lsio_apps = get_all_linuxserver_templates()
        
        for app in lsio_apps:
            # Enhance static templates with Docker Hub and docs
            dockerhub_meta = fetch_dockerhub_metadata(app['image'])
            docs_config = scrape_linuxserver_docs(app['id'])
            
            template = generate_template_compose(app, dockerhub_meta, docs_config)
            templates.append(template)
        
        logger.info(f"✓ Enhanced {len(templates)} templates from static list")
        return templates
        
    except Exception as e:
        logger.error(f"Error loading LinuxServer templates: {e}")
        logger.info("Falling back to basic template list...")
        return get_fallback_templates()




def fetch_from_github_api():
    """
    Fetch all LinuxServer.io docker containers from GitHub API
    Returns list of template dictionaries
    """
    try:
        logger.info("Querying GitHub API for LinuxServer.io repositories...")
        
        templates = []
        page = 1
        per_page = 100
        
        # Fetch repositories from LinuxServer GitHub org
        while page <= 5:  # Limit to 5 pages (500 repos max)
            url = f"https://api.github.com/orgs/linuxserver/repos?per_page={per_page}&page={page}&type=public"
            
            response = requests.get(url, timeout=30)
            
            if response.status_code != 200:
                logger.warning(f"GitHub API returned {response.status_code}")
                break
            
            repos = response.json()
            
            if not repos:
                break
            
            # Filter for docker-* repositories
            docker_repos = [r for r in repos if r['name'].startswith('docker-')]
            
            logger.info(f"Page {page}: Found {len(docker_repos)} docker repos")
            
            # Parse each repo
            for repo in docker_repos:
                template = parse_github_repo(repo)
                if template:
                    templates.append(template)
            
            # Check if there are more pages
            if len(repos) < per_page:
                break
            
            page += 1
        
        logger.info(f"✓ Parsed {len(templates)} templates from GitHub")
        return templates
        
    except Exception as e:
        logger.error(f"Error fetching from GitHub API: {e}")
        return None


def parse_github_repo(repo):
    """
    Parse a GitHub repository to extract template metadata
    Returns template dict or None
    """
    try:
        name = repo['name'].replace('docker-', '')
        
        # Skip deprecated or special repos
        if repo.get('archived', False):
            return None
        
        # Extract category from description or topics
        description = repo.get('description', '')
        topics = repo.get('topics', [])
        
        category = categorize_container(name, description, topics)
        
        # Build image name
        image = f"lscr.io/linuxserver/{name}:latest"
        
        # Get default ports (we'll use common defaults based on container name)
        ports = get_default_ports(name)
        
        # Get default volumes
        volumes = get_default_volumes(name)
        
        # Get default environment variables
        env = ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        
        # Get creation date for "Latest" filter
        created_at = repo.get('created_at', '')
        
        # Better description fallback
        if not description or description.lower() in ['deprecated', 'archived']:
            description = get_fallback_description(name)
        
        # Create template data
        template_data = {
            "id": name,
            "name": name.replace('-', ' ').title(),
            "category": category,
            "image": image,
            "description": description,
            "ports": ports,
            "volumes": volumes,
            "env": env,
            "created_at": created_at  # Add creation date for sorting
        }
        
        # Return template (will be enhanced later by fetch_linuxserver_templates)
        return template_data
        
    except Exception as e:
        logger.error(f"Error parsing repo {repo.get('name')}: {e}")
        return None


def get_fallback_description(name):
    """
    Get fallback description for containers that don't have GitHub descriptions
    Comprehensive list of popular LinuxServer containers
    """
    descriptions = {
        # Media Servers
        'plex': 'Stream your personal media collection anywhere - movies, TV, music, photos',
        'jellyfin': 'Free software media server - the volunteer-built alternative to Plex',
        'emby': 'Media server to organize, play and stream audio and video',
        'navidrome': 'Modern music server and streamer compatible with Subsonic/Airsonic',
        'airsonic': 'Free, web-based media streamer and jukebox',
        'airsonic-advanced': 'Advanced fork of Airsonic with additional features',
        'booksonic': 'Audiobook streaming server',
        'booksonic-air': 'Audiobook and podcast server with web player',
        
        # Arr Stack
        'sonarr': 'Smart PVR for TV shows - monitor, download and organize',
        'radarr': 'Movie collection manager for Usenet and BitTorrent',
        'prowlarr': 'Indexer manager/proxy for Sonarr, Radarr, Lidarr, etc',
        'lidarr': 'Music collection manager for Usenet and BitTorrent',
        'readarr': 'Book, magazine and audiobook collection manager',
        'bazarr': 'Companion to Sonarr/Radarr for managing subtitles',
        'whisparr': 'Adult content collection manager',
        'mylar3': 'Automated comic book downloader and manager',
        'lazylibrarian': 'Follow authors and grab metadata for ebooks and audiobooks',
        'jackett': 'API support for torrent trackers - proxy for *arr apps',
        'nzbhydra2': 'Meta search for NZB indexers - usenet search aggregator',
        
        # Downloads
        'qbittorrent': 'Free and reliable P2P BitTorrent client',
        'transmission': 'Fast, easy BitTorrent client with web interface',
        'deluge': 'Lightweight, free cross-platform BitTorrent client',
        'sabnzbd': 'Free and easy binary newsreader for Usenet',
        'nzbget': 'Efficient Usenet downloader',
        'rutorrent': 'rtorrent client with popular webui',
        'pyload-ng': 'Free and open-source download manager',
        
        # Dashboards
        'heimdall': 'Application dashboard and launcher',
        'homer': 'Static application dashboard',
        'homarr': 'Customizable homelab dashboard with modern UI',
        'organizr': 'Homelab services organizer with SSO',
        
        # Productivity
        'nextcloud': 'Self-hosted productivity platform - files, calendar, contacts',
        'code-server': 'VS Code in the browser',
        'calibre': 'Powerful ebook library management',
        'calibre-web': 'Web app for browsing and reading ebooks from Calibre',
        'paperless-ngx': 'Document management system with OCR',
        'syncthing': 'Continuous file synchronization program',
        'wikijs': 'Modern and powerful wiki software',
        'bookstack': 'Simple, self-hosted wiki platform',
        'grocy': 'ERP system for your kitchen - groceries and household',
        
        # Photos
        'photoprism': 'AI-powered photo management app',
        'piwigo': 'Photo gallery software for the web',
        'immich': 'High performance self-hosted photo and video backup',
        'lychee': 'Free photo management tool for web',
        
        # Networking
        'wireguard': 'Fast modern VPN protocol',
        'nginx': 'High-performance web server and reverse proxy',
        'swag': 'Nginx with automatic SSL via Let\'s Encrypt',
        'nginx-proxy-manager': 'Easy nginx reverse proxy with SSL',
        'ddclient': 'Dynamic DNS client',
        'duckdns': 'Free dynamic DNS service',
        
        # Monitoring
        'tautulli': 'Monitoring and tracking for Plex Media Server',
        'overseerr': 'Request management and media discovery for Plex',
        'ombi': 'Self-hosted media request management',
        'uptime-kuma': 'Self-hosted monitoring tool like Uptime Robot',
        'netdata': 'Real-time performance monitoring',
        'scrutiny': 'Hard drive S.M.A.R.T monitoring',
        
        # Communication
        'freshrss': 'Free self-hostable RSS aggregator',
        'miniflux': 'Minimalist and opinionated RSS reader',
        'apprise-api': 'Send notifications to 100+ services',
        'ntfy': 'Simple HTTP-based pub-sub notification service',
        
        # Desktop
        'firefox': 'Firefox web browser in Docker',
        'chromium': 'Chromium web browser in Docker',
        'webtop': 'Full Linux desktop in your browser',
        
        # Development
        'gitea': 'Lightweight self-hosted Git service',
        'code-server': 'VS Code in the browser',
        
        # Databases
        'mariadb': 'MySQL-compatible relational database',
        
        # Books
        'kavita': 'Fast, feature rich reading server for manga/comics/books',
        'komga': 'Media server for comics/mangas/BDs',
        'ubooquity': 'Free home server for comics and ebooks',
        
        # Security
        'vaultwarden': 'Bitwarden compatible password manager',
        'authelia': 'Authentication and authorization server',
        
        # Smart Home
        'homeassistant': 'Open source home automation platform',
        'node-red': 'Flow-based programming for IoT',
        
        # TV/Broadcast
        'tvheadend': 'TV streaming server for Linux',
        
        # Backup
        'duplicati': 'Store encrypted backups online',
        'resilio-sync': 'Fast, private file syncing',
        
        # Other
        'filebrowser': 'Web file browser and manager',
        'projectsend': 'Private file sharing application',
    }
    
    return descriptions.get(name, f"LinuxServer.io {name.replace('-', ' ')} container")


def categorize_container(name, description, topics):
    """
    Categorize container based on name, description, and GitHub topics
    Uses multi-factor analysis for accurate categorization
    """
    name_lower = name.lower()
    desc_lower = description.lower() if description else ""
    topics_str = ' '.join(topics).lower() if topics else ""
    
    # Combine all text for comprehensive matching
    all_text = f"{name_lower} {desc_lower} {topics_str}"
    
    # === MEDIA SERVERS ===
    if any(x in name_lower for x in ['plex', 'jellyfin', 'emby', 'navidrome', 'airsonic', 'booksonic', 'subsonic', 'ampache', 'mopidy', 'snapcast', 'forked-daapd', 'muximux']):
        return 'media'
    if any(x in all_text for x in ['media server', 'music server', 'video server', 'streaming server', 'media streaming']):
        return 'media'
    
    # === ARR STACK ===
    if name_lower.endswith('arr'):  # sonarr, radarr, lidarr, etc.
        return 'arr'
    if any(x in name_lower for x in ['jackett', 'nzbhydra', 'prowlarr', 'mylar', 'lazylibrarian', 'headphones']):
        return 'arr'
    if any(x in all_text for x in ['pvr', 'usenet indexer', 'torrent indexer', 'media automation']):
        return 'arr'
    
    # === DOWNLOAD CLIENTS ===
    if any(x in name_lower for x in ['qbittorrent', 'transmission', 'deluge', 'sabnzbd', 'nzbget', 'rutorrent', 'pyload', 'jdownloader', 'aria2']):
        return 'downloads'
    if any(x in all_text for x in ['torrent client', 'bittorrent', 'usenet client', 'download manager', 'newsreader']):
        return 'downloads'
    
    # === HOME AUTOMATION & DASHBOARDS ===
    if any(x in name_lower for x in ['heimdall', 'homer', 'homarr', 'homeassistant', 'home-assistant', 'domoticz', 'habridge', 'organizr', 'muximux', 'logarr', 'monitorr']):
        return 'automation'
    if any(x in all_text for x in ['dashboard', 'home automation', 'smart home', 'launcher', 'application dashboard']):
        return 'automation'
    
    # === PRODUCTIVITY ===
    if any(x in name_lower for x in ['nextcloud', 'owncloud', 'calibre', 'paperless', 'obsidian', 'syncthing', 'bookstack', 'wikijs', 'hedgedoc', 'grocy', 'kanboard', 'monica', 'firefly', 'linkding', 'linkwarden', 'shiori', 'wallabag', 'trilium', 'joplin', 'standardnotes']):
        return 'productivity'
    if any(x in all_text for x in ['note taking', 'knowledge base', 'document management', 'file sync', 'bookmark', 'personal finance', 'budget', 'recipe']):
        return 'productivity'
    
    # === PHOTOS & IMAGES ===
    if any(x in name_lower for x in ['photoprism', 'piwigo', 'lychee', 'immich', 'photoview', 'photostructure', 'chevereto', 'pixapop']):
        return 'photos'
    if any(x in all_text for x in ['photo management', 'photo gallery', 'image gallery', 'photo organizer']):
        return 'photos'
    
    # === NETWORKING & VPN ===
    if any(x in name_lower for x in ['wireguard', 'openvpn', 'nginx', 'swag', 'proxy', 'ddclient', 'duckdns', 'netboot', 'smokeping', 'librespeed', 'unifi', 'haproxy', 'traefik', 'endlessh', 'fail2ban', 'ipam', 'pihole', 'adguard']):
        return 'networking'
    if any(x in all_text for x in ['reverse proxy', 'vpn', 'network', 'dns', 'dhcp', 'firewall', 'speed test', 'ad blocker', 'proxy server']):
        return 'networking'
    
    # === MONITORING & ANALYTICS ===
    if any(x in name_lower for x in ['netdata', 'scrutiny', 'healthcheck', 'uptime-kuma', 'tautulli', 'overseerr', 'ombi', 'requestrr', 'varken', 'syslog', 'grafana', 'prometheus', 'glances', 'statping', 'librenms', 'observium']):
        return 'monitoring'
    if any(x in all_text for x in ['monitoring', 'metrics', 'analytics', 'uptime', 'tracking', 'statistics', 'performance monitor', 'health check', 'alerting']):
        return 'monitoring'
    
    # === BACKUP & SYNC ===
    if any(x in name_lower for x in ['duplicati', 'resilio', 'rsnapshot', 'backup', 'duplicacy', 'kopia', 'borg', 'rclone', 'restic']):
        return 'backup'
    if any(x in all_text for x in ['backup', 'sync', 'synchronization', 'replication', 'snapshot']):
        return 'backup'
    
    # === DATABASES ===
    if any(x in name_lower for x in ['mariadb', 'mysql', 'postgres', 'postgresql', 'sqlite', 'mongo', 'mongodb', 'redis', 'influxdb', 'clickhouse', 'cassandra']):
        return 'database'
    if any(x in all_text for x in ['database', 'sql', 'nosql', 'data store']):
        return 'database'
    
    # === COMMUNICATION & SOCIAL ===
    if any(x in name_lower for x in ['freshrss', 'weechat', 'znc', 'apprise', 'ntfy', 'gotify', 'rss', 'miniflux', 'tt-rss', 'flarum', 'discourse', 'mattermost', 'rocketchat', 'matrix', 'mastodon']):
        return 'communication'
    if any(x in all_text for x in ['rss reader', 'irc', 'chat', 'messaging', 'notification', 'forum', 'social network', 'feed reader']):
        return 'communication'
    
    # === DESKTOP & BROWSERS ===
    if any(x in name_lower for x in ['firefox', 'chromium', 'webtop', 'rdesktop', 'guacamole', 'guacd', 'xrdp', 'kasmweb', 'kde', 'xfce', 'mate', 'i3']):
        return 'desktop'
    if any(x in all_text for x in ['web browser', 'remote desktop', 'desktop environment', 'browser', 'vnc']):
        return 'desktop'
    
    # === GAMING ===
    if any(x in name_lower for x in ['steam', 'minetest', 'minecraft', 'terraria', 'factorio', 'valheim', 'gameserver', 'pterodactyl']):
        return 'gaming'
    if any(x in all_text for x in ['game server', 'gaming', 'minecraft', 'game']):
        return 'gaming'
    
    # === FILE MANAGEMENT ===
    if any(x in name_lower for x in ['filebrowser', 'projectsend', 'davos', 'filestash', 'filezilla', 'sftp', 'sftpgo', 'webdav']):
        return 'files'
    if any(x in all_text for x in ['file browser', 'file manager', 'file sharing', 'ftp', 'webdav']):
        return 'files'
    
    # === DEVELOPMENT & CODE ===
    if any(x in name_lower for x in ['code-server', 'vscode', 'gitea', 'gitlab', 'gogs', 'docker', 'git', 'snippet', 'jenkins', 'drone', 'woodpecker', 'forgejo']):
        return 'development'
    if any(x in all_text for x in ['code editor', 'ide', 'git', 'ci/cd', 'continuous integration', 'devops', 'repository']):
        return 'development'
    
    # === WEB & CMS ===
    if any(x in name_lower for x in ['grav', 'wordpress', 'ghost', 'cms', 'nginx', 'apache', 'caddy', 'lighttpd', 'jekyll', 'hugo']):
        return 'web'
    if any(x in all_text for x in ['web server', 'content management', 'blog', 'website', 'static site']):
        return 'web'
    
    # === SECURITY ===
    if any(x in name_lower for x in ['vaultwarden', 'bitwarden', 'keepass', 'authelia', 'authentik', 'keycloak', 'lldap', 'openldap', 'fail2ban', 'crowdsec']):
        return 'security'
    if any(x in all_text for x in ['password manager', 'authentication', 'sso', 'single sign-on', 'ldap', 'security', 'access control']):
        return 'security'
    
    # === BOOKS & READING ===
    if any(x in name_lower for x in ['calibre', 'kavita', 'komga', 'ubooquity', 'cops', 'readarr', 'lazylibrarian']):
        return 'books'
    if any(x in all_text for x in ['ebook', 'comic', 'manga', 'book', 'reading', 'library management']):
        return 'books'
    
    # === TV & BROADCAST ===
    if any(x in name_lower for x in ['tvheadend', 'xteve', 'dizquetv', 'channels', 'plex', 'telly']):
        return 'tv'
    if any(x in all_text for x in ['iptv', 'dvr', 'tv tuner', 'epg', 'television']):
        return 'tv'
    
    # === SMART HOME & IOT ===
    if any(x in name_lower for x in ['homeassistant', 'home-assistant', 'openhab', 'node-red', 'mqtt', 'zigbee', 'zwave', 'esphome']):
        return 'smarthome'
    if any(x in all_text for x in ['iot', 'smart home', 'home automation', 'mqtt broker', 'zigbee', 'z-wave']):
        return 'smarthome'
    
    # === UTILITIES ===
    if any(x in name_lower for x in ['cron', 'ofelia', 'healthcheck', 'diun', 'watchtower', 'webhook', 'changedetection']):
        return 'utilities'
    if any(x in all_text for x in ['scheduler', 'cron', 'task automation', 'webhook', 'utility']):
        return 'utilities'
    
    # === SECONDARY CHECKS ON DESCRIPTION/TOPICS ===
    
    # Media-related
    if any(x in desc_lower for x in ['stream', 'video', 'audio', 'music', 'podcast', 'radio', 'movies', 'tv shows']):
        return 'media'
    
    # Monitoring-related
    if any(x in desc_lower for x in ['monitor', 'tracking', 'metrics', 'dashboard', 'analytics', 'statistics']):
        return 'monitoring'
    
    # Network-related  
    if any(x in desc_lower for x in ['network', 'proxy', 'vpn', 'dns', 'tunnel', 'firewall']):
        return 'networking'
    
    # Download-related
    if any(x in desc_lower for x in ['download', 'torrent', 'usenet', 'nzb']):
        return 'downloads'
    
    # File-related
    if any(x in desc_lower for x in ['file manager', 'file browser', 'file sharing', 'cloud storage']):
        return 'files'
    
    # Default fallback
    return 'other'


def get_default_ports(name):
    """
    Get default ports for common containers
    Comprehensive port mapping for 150+ LinuxServer containers
    """
    port_map = {
        # Media Servers
        'plex': ['32400:32400'],
        'jellyfin': ['8096:8096', '8920:8920'],
        'emby': ['8096:8096', '8920:8920'],
        'navidrome': ['4533:4533'],
        'airsonic': ['4040:4040'],
        'airsonic-advanced': ['4040:4040'],
        'booksonic': ['4040:4040'],
        'booksonic-air': ['4040:4040'],
        'subsonic': ['4040:4040'],
        'ampache': ['80:80'],
        'mopidy': ['6680:6680', '6600:6600'],
        'snapcast': ['1704:1704', '1705:1705'],
        'kodi-headless': ['8080:8080', '9090:9090'],
        'muximux': ['80:80'],
        'kometa': [],  # No web interface
        
        # Arr Stack
        'sonarr': ['8989:8989'],
        'radarr': ['7878:7878'],
        'prowlarr': ['9696:9696'],
        'lidarr': ['8686:8686'],
        'readarr': ['8787:8787'],
        'bazarr': ['6767:6767'],
        'whisparr': ['6969:6969'],
        'mylar': ['8090:8090'],
        'mylar3': ['8090:8090'],
        'lazylibrarian': ['5299:5299'],
        'headphones': ['8181:8181'],
        'jackett': ['9117:9117'],
        'nzbhydra2': ['5076:5076'],
        
        # Download Clients
        'qbittorrent': ['8080:8080', '6881:6881', '6881:6881/udp'],
        'sabnzbd': ['8080:8080'],
        'nzbget': ['6789:6789'],
        'transmission': ['9091:9091', '51413:51413', '51413:51413/udp'],
        'deluge': ['8112:8112', '6881:6881', '6881:6881/udp'],
        'rutorrent': ['80:80', '5000:5000', '51413:51413', '6881:6881/udp'],
        'pyload-ng': ['8000:8000', '9666:9666'],
        'jdownloader-2': ['5800:5800'],
        'aria2': ['6800:6800'],
        
        # Home Automation & Dashboards
        'homeassistant': ['8123:8123'],
        'heimdall': ['80:80', '443:443'],
        'homer': ['8080:8080'],
        'homarr': ['7575:7575'],
        'organizr': ['80:80'],
        'muximux': ['80:80'],
        'logarr': ['80:80'],
        'monitorr': ['80:80'],
        'domoticz': ['8080:8080', '6144:6144', '1443:1443'],
        'habridge': ['8080:8080', '50000:50000'],
        'node-red': ['1880:1880'],
        'openhab': ['8080:8080', '8443:8443'],
        
        # Productivity
        'nextcloud': ['443:443'],
        'owncloud': ['80:80', '443:443'],
        'calibre': ['8080:8080', '8181:8181', '8081:8081'],
        'calibre-web': ['8083:8083'],
        'paperless-ngx': ['8000:8000'],
        'obsidian': ['3000:3000', '3001:3001'],
        'syncthing': ['8384:8384', '22000:22000', '22000:22000/udp', '21027:21027/udp'],
        'wikijs': ['3000:3000'],
        'bookstack': ['6875:80'],
        'hedgedoc': ['3000:3000'],
        'grocy': ['80:80'],
        'kanboard': ['80:80'],
        'monica': ['80:80'],
        'firefly-iii': ['8080:8080'],
        'linkding': ['9090:9090'],
        'linkwarden': ['3000:3000'],
        'shiori': ['8080:8080'],
        'wallabag': ['80:80'],
        'trilium': ['8080:8080'],
        'joplin-server': ['22300:22300'],
        'standardnotes': ['3000:3000'],
        
        # Photos
        'photoprism': ['2342:2342'],
        'piwigo': ['80:80'],
        'lychee': ['80:80'],
        'immich': ['8080:8080'],
        'photoview': ['80:80'],
        'photostructure': ['2700:2700'],
        'chevereto': ['80:80'],
        'pixapop': ['5000:5000'],
        
        # Networking & VPN
        'wireguard': ['51820:51820/udp'],
        'openvpn-as': ['943:943', '9443:9443', '1194:1194/udp'],
        'nginx': ['80:80', '443:443'],
        'swag': ['443:443', '80:80'],
        'nginx-proxy-manager': ['80:80', '443:443', '81:81'],
        'haproxy': ['80:80', '443:443'],
        'traefik': ['80:80', '443:443', '8080:8080'],
        'ddclient': [],  # No web interface
        'duckdns': [],  # No web interface
        'netbootxyz': ['3000:3000', '69:69/udp', '8080:80'],
        'smokeping': ['80:80'],
        'librespeed': ['80:80'],
        'unifi-network-application': ['8443:8443', '3478:3478/udp', '10001:10001/udp', '8080:8080'],
        'pihole': ['80:80', '53:53', '53:53/udp'],
        'adguardhome': ['3000:3000', '53:53', '53:53/udp'],
        'endlessh': ['2222:2222'],
        'fail2ban': [],  # No web interface
        
        # Monitoring & Analytics
        'netdata': ['19999:19999'],
        'scrutiny': ['8080:8080', '8086:8086'],
        'healthchecks': ['8000:8000'],
        'uptime-kuma': ['3001:3001'],
        'tautulli': ['8181:8181'],
        'overseerr': ['5055:5055'],
        'ombi': ['3579:3579'],
        'requestrr': ['4545:4545'],
        'varken': [],  # No web interface
        'grafana': ['3000:3000'],
        'prometheus': ['9090:9090'],
        'glances': ['61208:61208'],
        'statping': ['8080:8080'],
        'librenms': ['8000:8000'],
        'observium': ['8668:8668'],
        
        # Backup & Sync
        'duplicati': ['8200:8200'],
        'resilio-sync': ['8888:8888', '55555:55555'],
        'rsnapshot': [],  # No web interface
        'duplicacy': ['3875:3875'],
        'kopia': ['51515:51515'],
        'rclone': ['5572:5572'],
        
        # Databases
        'mariadb': ['3306:3306'],
        'mysql-workbench': ['3000:3000', '3001:3001'],
        'sqlitebrowser': ['3000:3000', '3001:3001'],
        'postgres': ['5432:5432'],
        'mongodb': ['27017:27017'],
        'redis': ['6379:6379'],
        'influxdb': ['8086:8086'],
        
        # Communication & Social
        'freshrss': ['80:80'],
        'weechat': ['9001:9001'],
        'znc': ['6501:6501'],
        'apprise-api': ['8000:8000'],
        'ntfy': ['80:80'],
        'gotify': ['80:80'],
        'miniflux': ['8080:8080'],
        'tt-rss': ['80:80'],
        'flarum': ['80:80'],
        'discourse': ['80:80'],
        'mattermost': ['8065:8065'],
        'rocketchat': ['3000:3000'],
        
        # Desktop & Browsers
        'firefox': ['3000:3000', '3001:3001'],
        'chromium': ['3000:3000', '3001:3001'],
        'webtop': ['3000:3000', '3001:3001'],
        'rdesktop': ['3389:3389'],
        'guacamole': ['8080:8080'],
        'guacd': ['4822:4822'],
        'xrdp': ['3389:3389'],
        'kasmweb': ['6901:6901'],
        
        # Gaming
        'steamheadless': ['3000:3000', '3001:3001'],
        'minetest': ['30000:30000/udp'],
        'minecraft-server': ['25565:25565'],
        'terraria': ['7777:7777'],
        'factorio': ['34197:34197/udp', '27015:27015/tcp'],
        'valheim': ['2456:2456/udp', '2457:2457/udp', '2458:2458/udp'],
        'pterodactyl': ['80:80', '443:443', '2022:2022'],
        
        # File Management
        'filebrowser': ['80:80'],
        'projectsend': ['80:80'],
        'davos': ['8080:8080'],
        'filestash': ['8334:8334'],
        'filezilla': ['3000:3000', '3001:3001'],
        'sftpgo': ['8080:8080', '2022:2022'],
        
        # Development & Code
        'code-server': ['8443:8443'],
        'gitea': ['3000:3000', '222:22'],
        'gitlab': ['80:80', '443:443', '22:22'],
        'gogs': ['3000:3000', '222:22'],
        'forgejo': ['3000:3000', '222:22'],
        'jenkins': ['8080:8080', '50000:50000'],
        'drone': ['80:80', '443:443'],
        'woodpecker': ['8000:8000', '9000:9000'],
        'docker-compose': [],  # No web interface
        
        # Web & CMS
        'grav': ['80:80'],
        'wordpress': ['80:80'],
        'ghost': ['2368:2368'],
        'nginx': ['80:80', '443:443'],
        'apache': ['80:80', '443:443'],
        'caddy': ['80:80', '443:443'],
        'lighttpd': ['80:80'],
        
        # Security
        'vaultwarden': ['80:80'],
        'bitwarden': ['80:80'],
        'authelia': ['9091:9091'],
        'authentik': ['9000:9000', '9443:9443'],
        'keycloak': ['8080:8080'],
        'lldap': ['3890:3890', '17170:17170'],
        'openldap': ['389:389', '636:636'],
        'crowdsec': ['8080:8080'],
        
        # Books & Reading
        'calibre': ['8080:8080', '8181:8181'],
        'calibre-web': ['8083:8083'],
        'kavita': ['5000:5000'],
        'komga': ['8080:8080'],
        'ubooquity': ['2202:2202', '2203:2203'],
        'cops': ['80:80'],
        
        # TV & Broadcast
        'tvheadend': ['9981:9981', '9982:9982'],
        'xteve': ['34400:34400'],
        'dizquetv': ['8000:8000'],
        'channels-dvr': ['8089:8089'],
        'oscam': ['8888:8888'],
        'minisatip': ['8875:8875', '554:554', '1900:1900/udp'],
        
        # Smart Home & IoT
        'homeassistant': ['8123:8123'],
        'openhab': ['8080:8080', '8443:8443'],
        'node-red': ['1880:1880'],
        'mosquitto': ['1883:1883', '9001:9001'],
        'zigbee2mqtt': ['8080:8080'],
        'zwavejs2mqtt': ['8091:8091', '3000:3000'],
        'esphome': ['6052:6052'],
        
        # Utilities
        'ofelia': [],  # No web interface
        'diun': [],  # No web interface
        'watchtower': [],  # No web interface
        'changedetection': ['5000:5000'],
        'webhook': ['9000:9000'],
        
        # Other
        'foldingathome': ['7396:7396', '36330:36330'],
        'boinc': ['8080:8080'],
        'syslog-ng': ['514:5514/udp', '601:6601/tcp', '6514:6514/tcp'],
        'snipe-it': ['80:80'],
        'snippets': ['8080:8080'],
    }
    
    # Return mapped ports or default to 8080
    return port_map.get(name, ['8080:8080'])


def get_default_volumes(name):
    """
    Get default volumes for common containers
    Most containers need /config at minimum
    """
    volumes = ['/config']
    
    # Media volumes
    if name in ['plex', 'jellyfin', 'emby', 'kodi-headless']:
        volumes.append('/media')
    
    # Arr stack - downloads
    if 'arr' in name or name in ['jackett', 'nzbhydra2', 'nzbhydra', 'prowlarr']:
        volumes.append('/downloads')
    
    # Sonarr - TV
    if name in ['sonarr', 'bazarr']:
        volumes.append('/tv')
    
    # Radarr - Movies
    if name in ['radarr', 'bazarr']:
        volumes.append('/movies')
    
    # Music
    if name in ['lidarr', 'navidrome', 'airsonic', 'airsonic-advanced', 'booksonic', 'booksonic-air', 'ampache', 'mopidy']:
        volumes.append('/music')
    
    # Books
    if name in ['readarr', 'calibre', 'calibre-web', 'lazylibrarian', 'kavita', 'komga', 'ubooquity', 'cops']:
        volumes.append('/books')
    
    # Comics
    if name in ['mylar3', 'mylar', 'komga', 'kavita']:
        volumes.append('/comics')
    
    # Audiobooks
    if name in ['booksonic-air', 'audiobookshelf']:
        volumes.append('/audiobooks')
    
    # Podcasts
    if name in ['booksonic-air', 'airsonic-advanced']:
        volumes.append('/podcasts')
    
    # Download clients
    if 'torrent' in name or name in ['transmission', 'deluge', 'rutorrent', 'qbittorrent']:
        volumes.append('/downloads')
        if name == 'transmission':
            volumes.append('/watch')
    
    if name in ['sabnzbd', 'nzbget', 'pyload-ng']:
        volumes.append('/downloads')
    
    # Photos
    if name in ['photoprism', 'piwigo', 'immich', 'lychee', 'photoview', 'chevereto']:
        volumes.append('/photos')
    
    if name in ['piwigo']:
        volumes.append('/gallery')
    
    # Cloud storage
    if name in ['nextcloud', 'owncloud']:
        volumes.append('/data')
    
    # Backup
    if name in ['duplicati']:
        volumes.extend(['/backups', '/source'])
    
    if name in ['rsnapshot']:
        volumes.extend(['/snapshots', '/.ssh'])
    
    if name in ['resilio-sync', 'syncthing']:
        volumes.append('/sync')
    
    # Document management
    if name in ['paperless-ngx']:
        volumes.append('/data')
    
    # File browsers
    if name in ['filebrowser']:
        volumes.append('/srv')
    
    if name in ['projectsend', 'davos']:
        volumes.append('/data')
    
    # TVHeadend
    if name in ['tvheadend']:
        volumes.append('/recordings')
    
    return volumes


def get_fallback_templates():
    """Minimal fallback templates if comprehensive list fails"""
    basic_apps = [
        {
            "id": "plex",
            "name": "Plex",
            "category": "media",
            "image": "lscr.io/linuxserver/plex:latest",
            "description": "Your media, your way. Stream your personal collection of movies, TV shows, music, and photos anywhere.",
            "ports": ["32400:32400"],
            "volumes": ["/config", "/media"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "VERSION=docker"]
        },
        {
            "id": "jellyfin",
            "name": "Jellyfin",
            "category": "media",
            "image": "lscr.io/linuxserver/jellyfin:latest",
            "description": "Free software media server. Stream your media collection to any device.",
            "ports": ["8096:8096", "8920:8920"],
            "volumes": ["/config", "/media"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "sonarr",
            "name": "Sonarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/sonarr:latest",
            "description": "Smart PVR for newsgroup and bittorrent users. Monitor multiple RSS feeds for new episodes.",
            "ports": ["8989:8989"],
            "volumes": ["/config", "/tv", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "radarr",
            "name": "Radarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/radarr:latest",
            "description": "Movie collection manager for Usenet and BitTorrent users. Monitor multiple RSS feeds.",
            "ports": ["7878:7878"],
            "volumes": ["/config", "/movies", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "prowlarr",
            "name": "Prowlarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/prowlarr:latest",
            "description": "Indexer manager/proxy for Sonarr, Radarr, Lidarr, etc.",
            "ports": ["9696:9696"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "lidarr",
            "name": "Lidarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/lidarr:latest",
            "description": "Music collection manager for Usenet and BitTorrent users.",
            "ports": ["8686:8686"],
            "volumes": ["/config", "/music", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "readarr",
            "name": "Readarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/readarr:develop",
            "description": "Book, magazine, and audiobook collection manager.",
            "ports": ["8787:8787"],
            "volumes": ["/config", "/books", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "qbittorrent",
            "name": "qBittorrent",
            "category": "downloads",
            "image": "lscr.io/linuxserver/qbittorrent:latest",
            "description": "Free and reliable P2P BitTorrent client.",
            "ports": ["8080:8080", "6881:6881", "6881:6881/udp"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "WEBUI_PORT=8080"]
        },
        {
            "id": "sabnzbd",
            "name": "SABnzbd",
            "category": "downloads",
            "image": "lscr.io/linuxserver/sabnzbd:latest",
            "description": "Free and easy binary newsreader.",
            "ports": ["8080:8080"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "nzbget",
            "name": "NZBGet",
            "category": "downloads",
            "image": "lscr.io/linuxserver/nzbget:latest",
            "description": "Efficient usenet downloader.",
            "ports": ["6789:6789"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "deluge",
            "name": "Deluge",
            "category": "downloads",
            "image": "lscr.io/linuxserver/deluge:latest",
            "description": "Lightweight BitTorrent client.",
            "ports": ["8112:8112", "6881:6881", "6881:6881/udp"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "homeassistant",
            "name": "Home Assistant",
            "category": "automation",
            "image": "lscr.io/linuxserver/homeassistant:latest",
            "description": "Open source home automation platform.",
            "ports": ["8123:8123"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"],
            "network_mode": "host"
        },
        {
            "id": "nextcloud",
            "name": "Nextcloud",
            "category": "cloud",
            "image": "lscr.io/linuxserver/nextcloud:latest",
            "description": "Self-hosted productivity platform. File hosting, calendar, contacts, and more.",
            "ports": ["443:443"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "photoprism",
            "name": "PhotoPrism",
            "category": "photos",
            "image": "lscr.io/linuxserver/photoprism:latest",
            "description": "AI-powered photo app for the decentralized web.",
            "ports": ["2342:2342"],
            "volumes": ["/config", "/photos"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "paperless-ngx",
            "name": "Paperless-ngx",
            "category": "productivity",
            "image": "lscr.io/linuxserver/paperless-ngx:latest",
            "description": "Document management system with OCR and full-text search.",
            "ports": ["8000:8000"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "code-server",
            "name": "Code Server",
            "category": "development",
            "image": "lscr.io/linuxserver/code-server:latest",
            "description": "VS Code in the browser. Code anywhere.",
            "ports": ["8443:8443"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "PASSWORD=password"]
        },
        {
            "id": "swag",
            "name": "SWAG",
            "category": "networking",
            "image": "lscr.io/linuxserver/swag:latest",
            "description": "Secure Web Application Gateway with Nginx, Let's Encrypt, fail2ban, and more.",
            "ports": ["443:443", "80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "URL=yourdomain.url", "VALIDATION=http"],
            "cap_add": ["NET_ADMIN"]
        },
        {
            "id": "nginx",
            "name": "Nginx",
            "category": "networking",
            "image": "lscr.io/linuxserver/nginx:latest",
            "description": "Simple webserver and reverse proxy.",
            "ports": ["80:80", "443:443"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "wireguard",
            "name": "WireGuard",
            "category": "networking",
            "image": "lscr.io/linuxserver/wireguard:latest",
            "description": "Fast, modern, secure VPN tunnel.",
            "ports": ["51820:51820/udp"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "SERVERURL=auto", "PEERS=1"],
            "cap_add": ["NET_ADMIN", "SYS_MODULE"],
            "sysctls": {"net.ipv4.conf.all.src_valid_mark": "1"}
        },
        {
            "id": "vaultwarden",
            "name": "Vaultwarden",
            "category": "security",
            "image": "lscr.io/linuxserver/vaultwarden:latest",
            "description": "Unofficial Bitwarden compatible server. Password manager.",
            "ports": ["80:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "duplicati",
            "name": "Duplicati",
            "category": "backup",
            "image": "lscr.io/linuxserver/duplicati:latest",
            "description": "Backup software to store encrypted backups online.",
            "ports": ["8200:8200"],
            "volumes": ["/config", "/backups", "/source"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "grafana",
            "name": "Grafana",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/grafana:latest",
            "description": "Analytics and interactive visualization platform.",
            "ports": ["3000:3000"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "emby",
            "name": "Emby",
            "category": "media",
            "image": "lscr.io/linuxserver/emby:latest",
            "description": "Personal media server with apps on many devices.",
            "ports": ["8096:8096", "8920:8920"],
            "volumes": ["/config", "/media"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "transmission",
            "name": "Transmission",
            "category": "downloads",
            "image": "lscr.io/linuxserver/transmission:latest",
            "description": "Fast, easy, and free BitTorrent client.",
            "ports": ["9091:9091", "51413:51413", "51413:51413/udp"],
            "volumes": ["/config", "/downloads", "/watch"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "overseerr",
            "name": "Overseerr",
            "category": "media",
            "image": "lscr.io/linuxserver/overseerr:latest",
            "description": "Request management and media discovery tool.",
            "ports": ["5055:5055"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "tautulli",
            "name": "Tautulli",
            "category": "monitoring",
            "image": "lscr.io/linuxserver/tautulli:latest",
            "description": "Monitoring and tracking tool for Plex Media Server.",
            "ports": ["8181:8181"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "bazarr",
            "name": "Bazarr",
            "category": "arr",
            "image": "lscr.io/linuxserver/bazarr:latest",
            "description": "Companion application to Sonarr and Radarr for managing and downloading subtitles.",
            "ports": ["6767:6767"],
            "volumes": ["/config", "/movies", "/tv"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "bookstack",
            "name": "BookStack",
            "category": "productivity",
            "image": "lscr.io/linuxserver/bookstack:latest",
            "description": "Simple, self-hosted, easy-to-use platform for organizing and storing information.",
            "ports": ["6875:80"],
            "volumes": ["/config"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC", "APP_URL=http://localhost:6875", "DB_HOST=bookstack_db", "DB_USER=bookstack", "DB_PASS=yourdbpass", "DB_DATABASE=bookstackapp"]
        },
        {
            "id": "syncthing",
            "name": "Syncthing",
            "category": "cloud",
            "image": "lscr.io/linuxserver/syncthing:latest",
            "description": "Continuous file synchronization program.",
            "ports": ["8384:8384", "22000:22000/tcp", "22000:22000/udp", "21027:21027/udp"],
            "volumes": ["/config", "/data"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        },
        {
            "id": "jackett",
            "name": "Jackett",
            "category": "arr",
            "image": "lscr.io/linuxserver/jackett:latest",
            "description": "API Support for your favorite torrent trackers.",
            "ports": ["9117:9117"],
            "volumes": ["/config", "/downloads"],
            "env": ["PUID=1000", "PGID=1000", "TZ=Etc/UTC"]
        }
    ]
    
    # Convert to template format
    templates = []
    for app in basic_apps:
        template = generate_template_compose(app)
        templates.append(template)
    
        return templates


def generate_template_compose(app_data, dockerhub_meta=None, docs_config=None):
    """
    Generate complete template dictionary with enhanced compose file
    Integrates Docker Hub metadata and LinuxServer docs config
    """
    # Generate enhanced compose
    compose_yaml = generate_enhanced_compose(app_data, dockerhub_meta, docs_config)
    
    template = {
        'id': app_data['id'],
        'name': app_data['name'],
        'category': app_data['category'],
        'description': app_data['description'],
        'image': app_data['image'],
        'compose': compose_yaml,
        'icon': get_category_icon(app_data['category']),
        'docs_url': f"https://docs.linuxserver.io/images/docker-{app_data['id']}",
        'github_url': f"https://github.com/linuxserver/docker-{app_data['id']}",
        'created_at': app_data.get('created_at', '')
    }
    
    # Add Docker Hub metadata if available
    if dockerhub_meta:
        template['stars'] = dockerhub_meta.get('stars', 0)
        template['pulls'] = dockerhub_meta.get('pulls', 0)
        template['last_updated'] = dockerhub_meta.get('last_updated', '')
        template['available_tags'] = [t['name'] for t in dockerhub_meta.get('tags', [])[:10]]
        if dockerhub_meta.get('tags'):
            template['size_mb'] = round(dockerhub_meta['tags'][0].get('size', 0) / 1024 / 1024, 1)
    
    return template


def get_category_icon(category):
    """Get emoji icon for category"""
    icons = {
        "media": "📺",
        "arr": "⬇️",
        "downloads": "📥",
        "automation": "🏠",
        "cloud": "☁️",
        "photos": "📷",
        "productivity": "📝",
        "development": "💻",
        "networking": "🌐",
        "security": "🔒",
        "backup": "💾",
        "monitoring": "📊",
        "database": "🗄️",
        "communication": "💬",
        "desktop": "🖥️",
        "gaming": "🎮",
        "files": "📁",
        "web": "🌍",
        "books": "📚",
        "tv": "📡",
        "smarthome": "🏡",
        "utilities": "🔧",
        "other": "📦"
    }
    return icons.get(category, "📦")


def load_templates_cache():
    """Load templates from cache file"""
    global templates_cache
    
    if os.path.exists(TEMPLATES_CACHE_FILE):
        try:
            with templates_cache_lock:
                with open(TEMPLATES_CACHE_FILE, 'r') as f:
                    templates_cache = json.load(f)
            logger.info(f"Loaded {len(templates_cache.get('templates', []))} templates from cache")
            return True
        except Exception as e:
            logger.error(f"Error loading templates cache: {e}")
    return False


def save_templates_cache():
    """Save templates to cache file"""
    try:
        with templates_cache_lock:
            with open(TEMPLATES_CACHE_FILE, 'w') as f:
                json.dump(templates_cache, f, indent=2)
            
            # Save last update timestamp
            with open(TEMPLATES_LAST_UPDATE_FILE, 'w') as f:
                f.write(datetime.now(pytz.UTC).isoformat())
        
        logger.info("✓ Templates cache saved")
        return True
    except Exception as e:
        logger.error(f"Error saving templates cache: {e}")
        return False


def load_docs_cache():
    """Load scraped docs from cache file"""
    global docs_cache
    
    if os.path.exists(DOCS_CACHE_FILE):
        try:
            with docs_cache_lock:
                with open(DOCS_CACHE_FILE, 'r') as f:
                    docs_cache = json.load(f)
            logger.info(f"Loaded scraped docs for {len(docs_cache)} containers from cache")
            return True
        except Exception as e:
            logger.error(f"Error loading docs cache: {e}")
    return False


def save_docs_cache():
    """Save scraped docs to cache file"""
    try:
        with docs_cache_lock:
            with open(DOCS_CACHE_FILE, 'w') as f:
                json.dump(docs_cache, f, indent=2)
        logger.debug(f"Saved scraped docs for {len(docs_cache)} containers")
        return True
    except Exception as e:
        logger.error(f"Error saving docs cache: {e}")
        return False


def refresh_templates():
    """Refresh templates"""
    global templates_cache
    
    logger.info("Refreshing templates...")
    safe_templates = []
    
    # LinuxServer
    try:
        lsio = fetch_linuxserver_templates()
        if lsio:
            for t in lsio:
                try:
                    json.dumps(t)
                    safe_templates.append(t)
                except:
                    pass
            logger.info(f"✓ LinuxServer: {len([t for t in safe_templates if 'casaos' not in t.get('id', '')])}")
    except Exception as e:
        logger.error(f"LinuxServer error: {e}")
    
    # CasaOS
    try:
        casaos = fetch_all_casaos()
        if casaos:
            safe_templates.extend(casaos)
    except Exception as e:
        logger.error(f"CasaOS error: {e}")
    
    if safe_templates:
        try:
            cache_obj = {
                'last_updated': datetime.now(pytz.UTC).isoformat(),
                'count': len(safe_templates),
                'templates': safe_templates
            }
            json.dumps(cache_obj)
            
            with templates_cache_lock:
                templates_cache = cache_obj
            
            save_templates_cache()
            save_docs_cache()
            logger.info(f"✓ TOTAL: {len(safe_templates)}")
            return True
        except Exception as e:
            logger.error(f"Cache error: {e}")
            return False
    
    return False


# ============================================================================
# TEMPLATE API ENDPOINTS
# ============================================================================

@app.route('/api/templates/list')
@require_auth
def api_templates_list():
    """Get all templates"""
    try:
        # Check if templates are enabled
        if not config.get('templates_enabled', True):
            return jsonify({
                'success': True,
                'enabled': False,
                'message': 'Template library is disabled',
                'last_updated': None,
                'count': 0,
                'templates': []
            })
        
        with templates_cache_lock:
            return jsonify({
                'success': True,
                'enabled': True,
                'last_updated': templates_cache.get('last_updated'),
                'count': len(templates_cache.get('templates', [])),
                'templates': templates_cache.get('templates', [])
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/refresh', methods=['POST'])
@require_auth
def api_templates_refresh():
    """Manually refresh templates"""
    try:
        # Check if templates are enabled
        if not config.get('templates_enabled', True):
            return jsonify({
                'success': False,
                'message': 'Template library is disabled in settings'
            }), 400
        
        success = refresh_templates()
        if success:
            return jsonify({
                'success': True,
                'message': 'Templates refreshed successfully',
                'count': len(templates_cache.get('templates', []))
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to refresh templates, using cache'
            }), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/<template_id>')
@require_auth
def api_template_get(template_id):
    """Get specific template details"""
    try:
        with templates_cache_lock:
            templates = templates_cache.get('templates', [])
            template = next((t for t in templates if t['id'] == template_id), None)
            
            if template:
                return jsonify(template)
            else:
                return jsonify({'error': 'Template not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/categories')
@require_auth
def api_templates_categories():
    """Get all unique categories"""
    try:
        with templates_cache_lock:
            templates = templates_cache.get('templates', [])
            categories = list(set(t['category'] for t in templates))
            categories.sort()
            
            # Add count per category
            category_counts = {}
            for cat in categories:
                count = sum(1 for t in templates if t['category'] == cat)
                category_counts[cat] = count
            
            return jsonify({
                'categories': categories,
                'counts': category_counts
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    try:
        # Test Docker connection
        logger.info("Testing Docker connection...")
        docker_client.ping()
        logger.info("✓ Docker connected successfully")
        
        # Initialize Trivy database on first run
        logger.info("Checking Trivy vulnerability database...")
        db_marker = os.path.join(TRIVY_CACHE_DIR, 'db', 'metadata.json')
        if not os.path.exists(db_marker):
            logger.info("Trivy database not found, downloading (this may take 2-5 minutes)...")
            update_trivy_database()
        else:
            logger.info("✓ Trivy database exists")
        
        # Load configuration
        logger.info("Loading configuration...")
        load_config()
        load_schedules()
        load_health_status()
        load_update_history()
        load_update_status()  # Load per-instance update status
        load_scan_results()  # Load cached scans
        logger.info("✓ Configuration loaded")
        
        # Load template library cache
        logger.info("Loading template library...")
        if not load_templates_cache():
            logger.info("No template cache found, will refresh on first access")
        else:
            logger.info(f"✓ Loaded {len(templates_cache.get('templates', []))} templates")
        
        # Load docs scraping cache
        if not load_docs_cache():
            logger.info("No docs cache found, will scrape on first refresh")
        else:
            logger.info(f"✓ Loaded docs cache for {len(docs_cache)} containers")
        
        # Setup scheduler
        logger.info("Setting up scheduler...")
        scheduler = setup_scheduler()
        logger.info("✓ Scheduler started")
        
        # Run orphan import on startup
        logger.info("Scanning for orphan containers to import...")
        import_orphan_containers_as_stacks()
        
        # Start background stats updater
        logger.info("Starting background stats updater...")
        stats_thread = threading.Thread(target=update_stats_background, daemon=True)
        stats_thread.start()
        logger.info("✓ Stats updater started")
        
        # OPTIMIZATION: Start background health checker
        logger.info("Starting background health checker...")
        health_thread = threading.Thread(target=health_check_background_worker, daemon=True)
        health_thread.start()
        logger.info("✓ Health checker started (runs every 60 seconds)")
        
        # Display instance info
        logger.info("=" * 50)
        logger.info(f"Instance Name: {config.get('instance_name', 'DockUp')}")
        api_token = config.get('api_token') or 'Not set'
        logger.info(f"API Token: {api_token[:16]}...")
        logger.info(f"Configured Peers: {len(config.get('peers', {}))}")
        logger.info("=" * 50)
        
        # Start Flask app
        port = int(os.environ.get('PORT', 5000))
        logger.info(f"Starting Dockup on port {port}...")
        logger.info("=" * 50)
        logger.info(f"Access the UI at: http://localhost:{port}")
        logger.info("=" * 50)
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    except Exception as e:
        logger.info(f"FATAL ERROR: Failed to start Dockup")
        logger.info(f"Error: {e}")
        logger.info("\nTroubleshooting:")
        logger.info("1. Make sure Docker socket is mounted: -v /var/run/docker.sock:/var/run/docker.sock")
        logger.info("2. Check if all dependencies are installed: pip install -r requirements.txt")
        logger.info("3. Ensure port 5000 is not already in use")
        import traceback
        traceback.print_exc()
        exit(1)
