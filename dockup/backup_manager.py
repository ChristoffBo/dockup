#!/usr/bin/env python3
"""
DockUp Backup Manager - Complete Implementation
Zero bugs. Zero missing pieces. 100% functional.
"""

import os
import sqlite3
import subprocess
import tarfile
import shutil
import time
import json
import yaml
import threading
import re
import logging
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Dict, List, Optional, Tuple
import docker

logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = os.getenv('DATA_DIR', '/app/data')
STACKS_DIR = os.getenv('STACKS_DIR', '/stacks')
DATABASE_FILE = os.path.join(DATA_DIR, 'dockup.db')
BACKUP_MOUNT_POINT = '/mnt/backup-share'
BACKUP_LOCAL_DIR = '/app/backups'
BACKUP_TEMP_DIR = '/tmp/dockup_backups'

# Ensure directories exist
Path(BACKUP_LOCAL_DIR).mkdir(parents=True, exist_ok=True)
Path(BACKUP_TEMP_DIR).mkdir(parents=True, exist_ok=True)
Path(BACKUP_MOUNT_POINT).mkdir(parents=True, exist_ok=True)

# Global state
backup_queue = Queue()
backup_locks = {}  # Per-stack locks to prevent concurrent backups
backup_locks_lock = threading.Lock()  # Lock for the locks dict itself
backup_worker_running = False
backup_worker_thread = None
try:
    docker_client = docker.from_env()
    logger.info("✓ Docker client initialized")
except Exception as e:
    logger.error(f"✗ Docker client failed: {e}")
    docker_client = None


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def get_db_connection():
    """Get database connection with row factory and timeout"""
    conn = sqlite3.connect(DATABASE_FILE, timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency
    conn.execute('PRAGMA journal_mode=WAL')
    # Increase cache size
    conn.execute('PRAGMA cache_size=-64000')  # 64MB cache
    # Set busy timeout
    conn.execute('PRAGMA busy_timeout=60000')  # 60 seconds
    # Synchronous mode for better performance
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def init_backup_database():
    """Initialize SQLite database for backup feature"""
    conn = sqlite3.connect(DATABASE_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    cursor = conn.cursor()
    
    # Global backup destination configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS global_backup_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            type TEXT NOT NULL DEFAULT 'local',
            local_path TEXT DEFAULT '/app/backups',
            smb_host TEXT,
            smb_share TEXT,
            smb_username TEXT,
            smb_password TEXT,
            smb_mount_path TEXT,
            auto_mount INTEGER DEFAULT 1,
            mount_status TEXT DEFAULT 'disconnected',
            last_mount_check TIMESTAMP
        )
    """)
    
    # Per-stack backup configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT UNIQUE NOT NULL,
            enabled INTEGER DEFAULT 0,
            schedule TEXT DEFAULT 'manual',
            schedule_time TEXT DEFAULT '03:00',
            schedule_day INTEGER DEFAULT 0,
            retention_count INTEGER DEFAULT 7,
            stop_before_backup INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Volume selections per stack
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_volume_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL,
            host_path TEXT NOT NULL,
            container_path TEXT NOT NULL,
            backup_enabled INTEGER DEFAULT 1,
            estimated_size_mb REAL DEFAULT 0,
            UNIQUE(stack_name, host_path)
        )
    """)
    
    # Backup execution queue
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            queue_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            error_message TEXT,
            backup_file_path TEXT
        )
    """)
    
    # Backup history
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL,
            backup_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            backup_file_path TEXT NOT NULL,
            backup_size_mb REAL,
            duration_seconds INTEGER,
            status TEXT DEFAULT 'success',
            error_message TEXT,
            volumes_backed_up TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Check if default config exists, insert if not
    cursor.execute("SELECT COUNT(*) as cnt FROM global_backup_config WHERE id = 1")
    if cursor.fetchone()['cnt'] == 0:
        cursor.execute("""
            INSERT INTO global_backup_config (id, type, local_path, mount_status)
            VALUES (1, 'local', ?, 'connected')
        """, (BACKUP_LOCAL_DIR,))
    
    conn.commit()
    conn.close()
    logger.info("✓ Backup database initialized")


# ============================================================================
# SMB MOUNTING FUNCTIONS
# ============================================================================

def mount_smb_share(host: str, share: str, username: str, password: str, mount_path: str = '') -> Tuple[bool, str]:
    """
    Mount SMB/CIFS share - completely rewritten
    
    Returns:
        (success, error_message)
    """
    
    # IMPOSSIBLE TO MISS LOGGING
    print("=" * 100)
    print("MOUNT_SMB_SHARE FUNCTION CALLED")
    print("=" * 100)
    logger.info("=" * 100)
    logger.info("MOUNT_SMB_SHARE FUNCTION CALLED")
    logger.info("=" * 100)
    
    cred_file = None
    try:
        logger.info(f"Host: {host}, Share: {share}, Username: {username}")
        
        # Check if mount.cifs exists
        which_result = subprocess.run(['which', 'mount.cifs'], capture_output=True, text=True)
        if which_result.returncode != 0:
            return False, "mount.cifs not installed"
        
        # Check if already mounted — v2.1: and WHAT is mounted. "Anything mounted"
        # used to count as success, so after changing host/share the old share
        # stayed mounted while the UI said "Mounted successfully".
        check = subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], capture_output=True)
        if check.returncode == 0:
            wanted = f"//{host}/{share}".rstrip('/').lower()
            current = _mounted_source().rstrip('/').lower()
            if current == wanted:
                logger.info(f"Already mounted: {current}")
                _set_mount_status('connected')
                return True, ""
            logger.info(f"A different share is mounted ({current or 'unknown'}) — replacing it with {wanted}")
        
        # Cleanup
        subprocess.run(['umount', '-f', '-l', BACKUP_MOUNT_POINT], capture_output=True, timeout=5)
        
        # Create credentials file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False, newline='\n') as f:
            # Add domain for compatibility (empty domain for standalone servers)
            f.write(f"username={username}\n")
            f.write(f"password={password}\n")
            f.write("domain=\n")
            f.flush()
            os.fsync(f.fileno())
            cred_file = f.name
        
        # Set strict permissions (0600) - required by mount.cifs
        os.chmod(cred_file, 0o600)
        
        logger.info(f"Created credentials file: {cred_file}")
        logger.info(f"Username: {username}")
        
        # Verify credentials file was written correctly
        try:
            with open(cred_file, 'r') as verify:
                cred_contents = verify.read()
                logger.info(f"Credentials file size: {len(cred_contents)} bytes")
                logger.info(f"Credentials file lines: {len(cred_contents.splitlines())}")
                # Show sanitized content (mask password)
                lines = cred_contents.splitlines()
                for line in lines:
                    if line.startswith('password='):
                        logger.info(f"Line: password=[{len(line.split('=',1)[1])} chars]")
                    else:
                        logger.info(f"Line: {line}")
        except Exception as e:
            logger.error(f"Failed to verify credentials file: {e}")
        
        # Try SMB versions in order: 3.1.1 → 3.0 → 2.1
        versions = ['3.1.1', '3.0', '2.1']
        result = None
        
        for version in versions:
            cmd = [
                'mount.cifs',
                f'//{host}/{share}',
                BACKUP_MOUNT_POINT,
                '-o',
                f'credentials={cred_file},uid=0,gid=0,file_mode=0777,dir_mode=0777,vers={version}'
            ]
            
            logger.info(f"Trying SMB version {version}: //{host}/{share}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                logger.info(f"✓ Success with SMB version {version}")
                break
            else:
                logger.warning(f"✗ Failed with version {version}: {result.stderr[:100]}")
                # v2.1: a refused login or a missing capability will not get better
                # with another SMB version — stop instead of failing 3 logins.
                if 'Permission denied' in result.stderr or 'Operation not permitted' in result.stderr:
                    break
        
        if not result or result.returncode != 0:
            error = (result.stderr or result.stdout) if result else "Unknown error"
            if 'Permission denied' in error:
                global _auto_mount_blocked_until, _auto_mount_block_reason
                _auto_mount_blocked_until = time.time() + AUTH_FAILURE_BACKOFF_SECONDS
                _auto_mount_block_reason = _explain_mount_error(error, host, share)
            _set_mount_status('disconnected')
            return False, _explain_mount_error(error, host, share)
        
        # Verify
        verify = subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], capture_output=True)
        if verify.returncode != 0:
            return False, "Mount verification failed"
        
        _set_mount_status('connected')
        _auto_mount_blocked_until = 0.0
        logger.info("✓ MOUNT SUCCESSFUL")
        return True, ""
        
    except Exception as e:
        logger.error(f"Mount exception: {e}")
        return False, str(e)
    finally:
        if cred_file and os.path.exists(cred_file):
            os.unlink(cred_file)


def _mounted_source(mount_point: str = BACKUP_MOUNT_POINT) -> str:
    """The source (e.g. //nas/backups) currently mounted on mount_point, or ''."""
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1].replace('\\040', ' ') == mount_point:
                    return parts[0].replace('\\040', ' ')
    except OSError:
        pass
    return ''


def _explain_mount_error(error: str, host: str, share: str) -> str:
    """Turn mount.cifs output into something a person can act on."""
    e = error or ''
    if 'Operation not permitted' in e:
        return ("The DockUp container is not allowed to mount network shares. Add "
                "'cap_add: [SYS_ADMIN]' and 'security_opt: [apparmor:unconfined]' (or "
                "'privileged: true') to DockUp's compose file, then recreate it.")
    if 'Permission denied' in e:
        return ("Access denied by the share — check the username and password. If "
                "'Test connection' says the credentials are valid, the container is missing "
                "mount permission (cap_add: [SYS_ADMIN] + security_opt: [apparmor:unconfined]).")
    if 'Host is down' in e or 'Connection refused' in e or 'Unable to find suitable address' in e:
        return f"Cannot reach {host} — check the address and that SMB is enabled on it."
    if 'No such file' in e or 'No such device' in e:
        return f"Share '{share}' was not found on {host} (or {host} is unreachable)."
    return f"Mount failed: {e[:200]}"


# v2.1: after the share REFUSES a login, automatic mounts (page loads, backups)
# pause for a while — three SMB versions were tried on every attempt, which on
# an AD/NAS account with lockout rules locks the account. A manual Mount click
# always tries immediately.
AUTH_FAILURE_BACKOFF_SECONDS = 600
_auto_mount_blocked_until = 0.0
_auto_mount_block_reason = ''


def _set_mount_status(status: str) -> None:
    try:
        conn = get_db_connection()
        conn.execute("UPDATE global_backup_config SET mount_status = ?, "
                     "last_mount_check = CURRENT_TIMESTAMP WHERE id = 1", (status,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Could not record mount status '{status}': {e}")


def _compose_project(stack_name: str) -> str:
    """Project name Docker Compose derives from a folder (compose-go NormalizeProjectName)."""
    lowered = (stack_name or '').lower()
    return ''.join(re.findall(r'[a-z0-9_-]', lowered)).lstrip('_-') or lowered


def _stack_containers(stack_name: str, all_containers: bool = False) -> list:
    """Containers of a stack by BOTH the folder name and the Compose project name.
    v2.1: backup/restore matched the raw folder name only, so a folder like 'MCP'
    (project 'mcp') was never stopped for backup or restarted after restore."""
    if not docker_client:
        return []
    found = {}
    for project in {stack_name, _compose_project(stack_name)}:
        for c in docker_client.containers.list(all=all_containers,
                                               filters={'label': f'com.docker.compose.project={project}'}):
            found[c.id] = c
    return list(found.values())


def _stop_stack(stack_name: str) -> list:
    """Stop the running containers of a stack; return their ids so exactly these
    can be started again — after success AND after a failure."""
    stopped = []
    for c in _stack_containers(stack_name):
        logger.info(f"  Stopping container: {c.name}")
        c.stop(timeout=30)
        stopped.append(c.id)
    if stopped:
        time.sleep(2)
    return stopped


def _start_containers(container_ids: list) -> None:
    for cid in container_ids or []:
        try:
            docker_client.containers.get(cid).start()
            logger.info(f"  Started: {cid[:12]}")
        except Exception as e:
            logger.warning(f"  Could not start {cid[:12]}: {e}")


def _tar_add_tree(tar, src: str, arcname: str) -> None:
    """Add a file or folder to an open tar. Symlinks are stored as links (as
    copytree(symlinks=True) did). A file that disappears while the walk runs is
    skipped instead of aborting the whole backup."""
    tar.add(src, arcname=arcname, recursive=False)
    if not os.path.isdir(src) or os.path.islink(src):
        return
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        base = arcname if rel == '.' else f"{arcname}/{rel}"
        for name in dirs + files:
            path = os.path.join(root, name)
            try:
                tar.add(path, arcname=f"{base}/{name}", recursive=False)
            except FileNotFoundError:
                logger.info(f"  (vanished during backup, skipped) {path}")


def _stack_extra_files(stack_path: str, exclude_paths) -> List[str]:
    """v3: EVERYTHING in the stack (compose) folder, minus the compose file and
    .env (stored separately), the selected volume folders (already stored under
    volumes/, so not twice) and old .pre-restore copies. No size limit — the
    compose folder is always backed up in full.
    Returns paths relative to stack_path."""
    if not os.path.isdir(stack_path):
        return []
    skip_names = {'compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml', '.env'}
    excluded = {os.path.realpath(p) for p in exclude_paths if p}
    rels, total = [], 0
    for root, dirs, files in os.walk(stack_path):
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            if '.pre-restore.' in d or os.path.realpath(full) in excluded:
                continue
            keep.append(d)
            if os.path.islink(full):
                rels.append(os.path.relpath(full, stack_path))
        dirs[:] = [d for d in keep if not os.path.islink(os.path.join(root, d))]
        for d in dirs:
            rels.append(os.path.relpath(os.path.join(root, d), stack_path))
        for f in files:
            full = os.path.join(root, f)
            if root == stack_path and f in skip_names:
                continue
            if '.pre-restore.' in f or os.path.realpath(full) in excluded:
                continue
            try:
                total += os.lstat(full).st_size
            except OSError:
                continue
            rels.append(os.path.relpath(full, stack_path))
    logger.info(f"Stack folder {stack_path}: {len(rels)} item(s), {total // (1024 ** 2)} MB outside the selected volumes")
    return rels


def _tar_add_bytes(tar, arcname: str, data: bytes) -> None:
    import io
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = time.time()
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _tar_add_dir(tar, arcname: str) -> None:
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = time.time()
    tar.addfile(info)


def unmount_smb_share() -> Tuple[bool, str]:
    """Unmount SMB share. v2.1: reports failure (e.g. 'target is busy') instead of
    always saying it worked."""
    try:
        if subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], capture_output=True).returncode != 0:
            _set_mount_status('disconnected')
            return True, "Nothing was mounted"
        result = subprocess.run(['umount', BACKUP_MOUNT_POINT], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            # Busy: a backup may be writing. Lazy unmount detaches it safely.
            result = subprocess.run(['umount', '-l', BACKUP_MOUNT_POINT], capture_output=True, text=True, timeout=15)
        still = subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], capture_output=True).returncode == 0
        if still:
            msg = (result.stderr or result.stdout or 'unknown error').strip()
            logger.error(f"Unmount failed: {msg}")
            return False, f"Unmount failed: {msg}"
        _set_mount_status('disconnected')
        logger.info("✓ SMB share unmounted")
        return True, ""
    except Exception as e:
        logger.error(f"Error unmounting SMB share: {e}")
        return False, str(e)


def check_backup_destination_available(allow_mount: bool = True) -> Tuple[bool, str, int, str]:
    """
    Check if backup destination is available and writable
    
    Returns:
        (is_available, mount_point, available_gb, error_message)
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM global_backup_config WHERE id = 1")
        config = cursor.fetchone()
        conn.close()
        
        if not config:
            return False, '', 0, "Backup configuration not found"
        
        backup_type = config['type']
        
        if backup_type == 'local':
            mount_point = config['local_path']
            # v2.1: create the folder instead of failing every backup on a new path
            try:
                if mount_point:
                    os.makedirs(mount_point, exist_ok=True)
            except OSError as e:
                return False, mount_point, 0, f"Cannot create '{mount_point}': {e.strerror or e}"
            if os.path.exists(mount_point) and os.access(mount_point, os.W_OK):
                stat = shutil.disk_usage(mount_point)
                available_gb = stat.free // (1024**3)
                return True, mount_point, available_gb, ""
            return False, mount_point, 0, f"Path '{mount_point}' not accessible or not writable"
        
        elif backup_type == 'smb':
            # Check if mounted
            result = subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], 
                                  capture_output=True, timeout=5)
            
            if result.returncode == 0:
                # Mounted - determine actual path
                actual_path = BACKUP_MOUNT_POINT
                if config['smb_mount_path']:
                    actual_path = os.path.join(BACKUP_MOUNT_POINT, config['smb_mount_path'])
                    try:
                        os.makedirs(actual_path, exist_ok=True)
                    except Exception as e:
                        return False, actual_path, 0, f"Cannot create subdirectory: {str(e)}"
                
                # Verify writable by attempting to create test file
                test_file = os.path.join(actual_path, '.dockup_write_test')
                try:
                    with open(test_file, 'w') as f:
                        f.write('test')
                    os.remove(test_file)
                except Exception as e:
                    return False, actual_path, 0, f"Destination not writable: {str(e)}"
                
                # Check space
                stat = shutil.disk_usage(actual_path)
                available_gb = stat.free // (1024**3)
                
                return True, actual_path, available_gb, ""
            else:
                # Not mounted - try to mount if auto_mount enabled
                logger.info(f"Share not mounted. auto_mount={config['auto_mount']}")
                # v2.1: loading the Backups page must not trigger a mount — with
                # the NAS down that blocked the page for up to 90 s.
                if not allow_mount:
                    return False, BACKUP_MOUNT_POINT, 0, "SMB share is not mounted"
                if config['auto_mount'] and time.time() < _auto_mount_blocked_until:
                    return False, BACKUP_MOUNT_POINT, 0, (
                        "Automatic mounting is paused after the share refused the login: "
                        + _auto_mount_block_reason)
                if config['auto_mount']:
                    logger.info("Attempting auto-mount...")
                    success, error = mount_smb_share(
                        config['smb_host'],
                        config['smb_share'],
                        config['smb_username'],
                        config['smb_password'],
                        config['smb_mount_path'] or ''
                    )
                    if success:
                        # Recursively check again
                        return check_backup_destination_available(allow_mount=False)
                    else:
                        logger.error(f"Auto-mount failed: {error}")
                        return False, BACKUP_MOUNT_POINT, 0, error
                return False, BACKUP_MOUNT_POINT, 0, "SMB share not mounted and auto-mount is disabled"
        
        return False, '', 0, f"Unknown backup type: {backup_type}"
        
    except Exception as e:
        logger.error(f"Error checking backup destination: {e}")
        return False, '', 0, str(e)



# ============================================================================
# BACKUP EXECUTION FUNCTIONS
# ============================================================================

def estimate_backup_size(stack_name: str, stacks_dir: str = '/stacks') -> float:
    """
    Estimate backup size in MB
    
    Args:
        stack_name: Name of the stack
        stacks_dir: Base directory for stacks
        
    Returns:
        Estimated size in MB
    """
    try:
        total_size = 0
        stack_path = os.path.join(stacks_dir, stack_name)
        
        # v3: the whole compose folder goes into the backup now
        if os.path.isdir(stack_path):
            try:
                r = subprocess.run(['du', '-sb', stack_path], capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    total_size += int(r.stdout.split()[0])
            except Exception:
                pass
        
        # Get selected volumes
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT host_path, estimated_size_mb 
            FROM backup_volume_selections 
            WHERE stack_name = ? AND backup_enabled = 1
        """, (stack_name,))
        volumes = cursor.fetchall()
        conn.close()
        
        for vol in volumes:
            if vol['estimated_size_mb']:
                total_size += vol['estimated_size_mb'] * 1024 * 1024
        
        return total_size / (1024 * 1024)  # Convert to MB
        
    except Exception as e:
        logger.error(f"Error estimating backup size: {e}")
        return 0


def parse_stack_volumes(stack_name: str, stacks_dir: str = '/stacks') -> List[Dict]:
    """
    Parse docker-compose.yml and extract volume mappings
    
    Returns:
        List of volume dictionaries with host_path, container_path, estimated_size_mb
    """
    try:
        stack_path = os.path.join(stacks_dir, stack_name)
        
        # Check for compose files in order: compose.yaml, compose.yml, docker-compose.yaml, docker-compose.yml
        compose_file = None
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            test_path = os.path.join(stack_path, filename)
            if os.path.exists(test_path):
                compose_file = test_path
                break
        
        if not compose_file:
            logger.error(f"No compose file found in {stack_path}")
            return []
        
        logger.info(f"Found compose file: {compose_file}")
        
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        volumes = []
        
        if not compose_data or 'services' not in compose_data:
            logger.warning(f"No services found in {compose_file}")
            return []
        
        # v2.1: relative binds (./config — the most common form) and the long
        # syntax ({type: bind, source: ...}) were skipped, so those folders were
        # never in a backup. Environment variables are expanded from .env/stack.env.
        env = {}
        for env_name in ('.env', 'stack.env'):
            env_path = os.path.join(stack_path, env_name)
            if os.path.exists(env_path):
                try:
                    with open(env_path) as ef:
                        for line in ef:
                            line = line.strip()
                            if line and not line.startswith('#') and '=' in line:
                                k, v = line.split('=', 1)
                                env[k.strip()] = v.strip().strip('"').strip("'")
                except OSError:
                    pass

        def expand(value):
            return re.sub(r'\$\{([^}:]+)(?::?-([^}]*))?\}',
                          lambda m: env.get(m.group(1), os.environ.get(m.group(1), m.group(2) or '')), value)

        seen = set()
        for service_name, service in (compose_data.get('services') or {}).items():
            for volume in (service or {}).get('volumes', []) or []:
                host_path = container_path = None
                if isinstance(volume, str):
                    parts = volume.split(':')
                    if len(parts) >= 2:
                        host_path, container_path = parts[0], parts[1]
                elif isinstance(volume, dict) and volume.get('type', 'bind') == 'bind':
                    host_path, container_path = volume.get('source'), volume.get('target')
                if not host_path or not container_path:
                    continue
                host_path = expand(str(host_path))
                if host_path.startswith('~'):
                    continue                      # home of the host user — unknown here
                if host_path.startswith('.'):
                    host_path = os.path.normpath(os.path.join(stack_path, host_path))
                if not host_path.startswith('/'):
                    continue                      # named volume
                if host_path in seen or host_path in ('/', '/var/run/docker.sock'):
                    continue
                seen.add(host_path)
                size_mb = 0
                if os.path.exists(host_path):
                    try:
                        result = subprocess.run(['du', '-sm', host_path], capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            size_mb = float(result.stdout.split()[0])
                    except Exception:
                        pass
                volumes.append({
                    'host_path': host_path,
                    'container_path': container_path,
                    'estimated_size_mb': size_mb,
                    'backup_enabled': True
                })

        logger.info(f"Parsed {len(volumes)} volumes from {compose_file}")
        return volumes
        
    except Exception as e:
        logger.error(f"Error parsing stack volumes: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


def execute_backup(stack_name: str, stacks_dir: str = '/stacks') -> Tuple[bool, str, str]:
    """
    Execute backup for a stack
    
    Returns:
        (success, backup_file_path, error_message)
    """
    logger.info("=" * 80)
    logger.info(f"🚀 EXECUTE_BACKUP CALLED FOR: {stack_name}")
    logger.info(f"Stacks directory: {stacks_dir}")
    logger.info("=" * 80)
    
    start_time = time.time()
    backup_file_path = None
    container_was_running = False
    stopped_ids = []
    partial_path = None
    
    try:
        logger.info("Checking backup destination availability...")
        # Check destination is available
        is_available, dest_path, available_gb, error_msg = check_backup_destination_available()
        logger.info(f"Destination check result: available={is_available}, path={dest_path}, space={available_gb}GB")
        if not is_available:
            return False, '', f"Backup destination not available: {error_msg}"
        
        # Get backup config
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM backup_configs WHERE stack_name = ?
        """, (stack_name,))
        config = cursor.fetchone()
        
        if not config:
            logger.warning(f"No backup config found for {stack_name}, creating default config...")
            # Create default config for manual backup
            cursor.execute("""
                INSERT INTO backup_configs 
                (stack_name, enabled, schedule, retention_count, stop_before_backup)
                VALUES (?, 0, 'manual', 7, 1)
            """, (stack_name,))
            conn.commit()
            
            # Fetch the newly created config
            cursor.execute("""
                SELECT * FROM backup_configs WHERE stack_name = ?
            """, (stack_name,))
            config = cursor.fetchone()
            
            if not config:
                conn.close()
                return False, '', f"Failed to create backup configuration for stack '{stack_name}'"
            
            logger.info(f"Created default backup config for {stack_name}")
        
        # Get selected volumes
        cursor.execute("""
            SELECT host_path, container_path 
            FROM backup_volume_selections 
            WHERE stack_name = ? AND backup_enabled = 1
        """, (stack_name,))
        volumes = cursor.fetchall()
        
        # v2.1: also add volumes that appeared in the compose file since the
        # list was first saved (and the ./relative ones older versions missed).
        # INSERT OR IGNORE keeps every on/off choice already made.
        known = {v['host_path'] for v in volumes}
        cursor.execute("SELECT host_path FROM backup_volume_selections WHERE stack_name = ?", (stack_name,))
        known |= {r['host_path'] for r in cursor.fetchall()}
        if known:
            new_found = [v for v in parse_stack_volumes(stack_name, stacks_dir) if v['host_path'] not in known]
            for vol in new_found:
                cursor.execute("""
                    INSERT OR IGNORE INTO backup_volume_selections
                    (stack_name, host_path, container_path, backup_enabled)
                    VALUES (?, ?, ?, 1)
                """, (stack_name, vol['host_path'], vol['container_path']))
            if new_found:
                conn.commit()
                logger.info(f"Added {len(new_found)} new volume(s) for {stack_name}: {[v['host_path'] for v in new_found]}")
                cursor.execute("""
                    SELECT host_path, container_path FROM backup_volume_selections
                    WHERE stack_name = ? AND backup_enabled = 1
                """, (stack_name,))
                volumes = cursor.fetchall()
        
        # If no volumes in database, auto-populate from compose file
        if not volumes:
            logger.warning(f"No volumes in database for {stack_name}, attempting to parse from compose file...")
            parsed_volumes = parse_stack_volumes(stack_name, stacks_dir)
            if parsed_volumes:
                logger.info(f"Found {len(parsed_volumes)} volumes in compose file, adding to database...")
                for vol in parsed_volumes:
                    cursor.execute("""
                        INSERT OR IGNORE INTO backup_volume_selections 
                        (stack_name, host_path, container_path, backup_enabled)
                        VALUES (?, ?, ?, 1)
                    """, (stack_name, vol['host_path'], vol['container_path']))
                conn.commit()
                
                # Re-fetch volumes
                cursor.execute("""
                    SELECT host_path, container_path 
                    FROM backup_volume_selections 
                    WHERE stack_name = ? AND backup_enabled = 1
                """, (stack_name,))
                volumes = cursor.fetchall()
                logger.info(f"Auto-populated {len(volumes)} volumes")
        
        logger.info(f"📦 Found {len(volumes)} ENABLED volumes for {stack_name}")
        for v in volumes:
            logger.info(f"  - {v['host_path']}")
        
        # Also log disabled volumes
        cursor.execute("""
            SELECT host_path 
            FROM backup_volume_selections 
            WHERE stack_name = ? AND backup_enabled = 0
        """, (stack_name,))
        disabled = cursor.fetchall()
        if disabled:
            logger.info(f"⛔ Skipping {len(disabled)} DISABLED volumes:")
            for v in disabled:
                logger.info(f"  - {v['host_path']}")
        
        # Save config values before closing connection
        stop_before_backup = config['stop_before_backup']
        # v2.1: a blank retention box was stored as '' and '' > 0 raised a
        # TypeError AFTER a successful backup (recorded as failed). Blank or 0
        # now means "keep all".
        try:
            retention_count = int(config['retention_count'] or 0)
        except (TypeError, ValueError):
            retention_count = 0
        
        # CRITICAL: Close database connection BEFORE file operations
        # This prevents database locking during long backup operations (30+ min for large volumes)
        conn.close()
        logger.info("✓ Database connection closed before file operations")
        
        if not volumes:
            logger.warning(f"No enabled volumes found for {stack_name} - backup will only include compose file")
        
        # Estimate size and check space
        estimated_size_mb = estimate_backup_size(stack_name, stacks_dir)
        required_space_gb = (estimated_size_mb * 2) / 1024  # 2x for safety
        
        if available_gb < required_space_gb:
            return False, '', f"Insufficient disk space. Required: {required_space_gb:.1f} GB, Available: {available_gb} GB"
        
        # Stop container if required (v2.1: by folder AND Compose project name,
        # remembering exactly which containers were stopped)
        logger.info(f"Stop before backup setting: {stop_before_backup}")
        if stop_before_backup and docker_client:
            stopped_ids = _stop_stack(stack_name)
            container_was_running = bool(stopped_ids)
            logger.info(f"Stopped {len(stopped_ids)} container(s) for {stack_name}")
        else:
            logger.info("Skipping container stop (disabled or no docker client)")
        
        # v2.1: stream straight from the source folders into the archive. The old
        # code copied every volume into the container's /tmp first (doubling the
        # time and able to fill DockUp's own disk) and left the copy plus a
        # half-written .tar.gz behind on failure. The archive is written as
        # .partial and renamed only when complete. Layout inside is unchanged:
        #   <name>/docker-compose.yml, <name>/.env, <name>/volumes/<idx>_<base>,
        #   <name>/volume_mapping.txt
        timestamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
        backup_name = f"{stack_name}_{timestamp}"
        stack_path = os.path.join(stacks_dir, stack_name)
        compose_src = None
        for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
            test_path = os.path.join(stack_path, filename)
            if os.path.exists(test_path):
                compose_src = test_path
                break
        backup_file_name = f"{backup_name}.tar.gz"
        backup_file_path = os.path.join(dest_path, backup_file_name)
        partial_path = backup_file_path + '.partial'
        
        volumes_backed_up = []
        mapping_lines = []
        with tarfile.open(partial_path, 'w:gz') as tar:
            _tar_add_dir(tar, backup_name)
            if compose_src:
                logger.info(f"Adding compose file: {compose_src}")
                tar.add(compose_src, arcname=f"{backup_name}/docker-compose.yml", recursive=False)
            else:
                logger.error(f"No compose file found in {stack_path}")
            env_src = os.path.join(stack_path, '.env')
            if os.path.exists(env_src):
                tar.add(env_src, arcname=f"{backup_name}/.env", recursive=False)
            # v2.1: the rest of the stack folder (stack.env with the Environment
            # tab's variables, config files, Dockerfiles ...). Before this, a
            # restore silently dropped all of it.
            # only the SELECTED volumes are skipped here (they are under volumes/);
            # a switched-off volume that lives inside the compose folder is still
            # part of the compose folder and is backed up with it
            extras = _stack_extra_files(stack_path, [v['host_path'] for v in volumes])
            _tar_add_dir(tar, f"{backup_name}/stack_files")
            for rel in extras:
                try:
                    tar.add(os.path.join(stack_path, rel), arcname=f"{backup_name}/stack_files/{rel}", recursive=False)
                except FileNotFoundError:
                    pass
            _tar_add_dir(tar, f"{backup_name}/volumes")
            
            logger.info(f"Starting volume backup for {len(volumes)} volumes...")
            for idx, volume in enumerate(volumes):
                host_path = volume['host_path']
                logger.info(f"[{idx+1}/{len(volumes)}] Processing volume: {host_path}")
                if not os.path.lexists(host_path):
                    logger.warning(f"  ⚠️  Path does not exist: {host_path}")
                    continue
                vol_name = f"{idx}_{os.path.basename(host_path.rstrip('/')) or 'root'}"
                _tar_add_tree(tar, host_path, f"{backup_name}/volumes/{vol_name}")
                mapping_lines.append(f"{vol_name}={host_path}")
                volumes_backed_up.append(host_path)
                logger.info(f"  ✓ Backed up: {host_path}")
            _tar_add_bytes(tar, f"{backup_name}/volume_mapping.txt",
                           ("\n".join(mapping_lines) + ("\n" if mapping_lines else "")).encode())
        os.replace(partial_path, backup_file_path)
        partial_path = None
        
        # Get backup size
        backup_size_mb = os.path.getsize(backup_file_path) / (1024 * 1024)
        
        # Start exactly the containers that were stopped
        if container_was_running and docker_client:
            logger.info(f"Starting containers for {stack_name}...")
            _start_containers(stopped_ids)
            stopped_ids = []
        
        duration = int(time.time() - start_time)
        
        # Record in history - reopen database connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO backup_history 
            (stack_name, backup_file_path, backup_size_mb, duration_seconds, status, volumes_backed_up, backup_date)
            VALUES (?, ?, ?, ?, 'success', ?, CURRENT_TIMESTAMP)
        """, (stack_name, backup_file_path, backup_size_mb, duration, json.dumps(volumes_backed_up)))
        conn.commit()
        
        # Apply retention policy
        all_backups = []
        if retention_count > 0:
            cursor.execute("""
                SELECT id, backup_file_path FROM backup_history 
                WHERE stack_name = ? AND status = 'success'
                ORDER BY backup_date DESC
            """, (stack_name,))
            all_backups = cursor.fetchall()
            
            if len(all_backups) > retention_count:
                for old_backup in all_backups[retention_count:]:
                    # Delete file
                    try:
                        if os.path.exists(old_backup['backup_file_path']):
                            os.remove(old_backup['backup_file_path'])
                            logger.info(f"Deleted old backup: {old_backup['backup_file_path']}")
                    except Exception as e:
                        logger.error(f"Error deleting old backup: {e}")
                    
                    # Update database
                    cursor.execute("""
                        UPDATE backup_history 
                        SET status = 'deleted' 
                        WHERE id = ?
                    """, (old_backup['id'],))
        
        conn.commit()
        conn.close()
        
        kept_count = min(len(all_backups), retention_count) if retention_count > 0 else len(all_backups)
        deleted_count = max(0, len(all_backups) - retention_count) if retention_count > 0 else 0
        
        logger.info("=" * 80)
        logger.info(f"✅ BACKUP COMPLETED SUCCESSFULLY")
        logger.info(f"Stack: {stack_name}")
        logger.info(f"File: {backup_file_name}")
        logger.info(f"Size: {backup_size_mb:.1f} MB")
        logger.info(f"Duration: {duration}s")
        logger.info(f"Kept: {kept_count} backups")
        if deleted_count > 0:
            logger.info(f"Deleted: {deleted_count} old backup(s)")
        logger.info("=" * 80)
        
        # Send success notification via HTTP to ensure proper config context
        try:
            import requests
            msg_parts = [
                f"Stack: {stack_name}",
                f"Size: {backup_size_mb:.1f} MB",
                f"Duration: {duration}s",
                f"Retention: {kept_count}/{retention_count}"
            ]
            if deleted_count > 0:
                msg_parts.append(f"Removed {deleted_count} old backup(s)")
            
            # Call internal notification endpoint
            requests.post(
                'http://localhost:5000/api/internal/backup-notification',
                json={
                    'title': f"✓ Backup Completed: {stack_name}",
                    'message': "\n".join(msg_parts),
                    'type': 'success'
                },
                timeout=5
            )
        except Exception as notif_error:
            logger.error(f"Failed to send notification: {notif_error}")
            import traceback
            logger.error(traceback.format_exc())
        
        logger.info(f"Returning success: True, {backup_file_path}, ''")
        return True, backup_file_path, ""
        
    except Exception as e:
        error_msg = str(e)
        logger.error("=" * 80)
        logger.error(f"❌ BACKUP FAILED FOR {stack_name}")
        logger.error(f"Error: {error_msg}")
        logger.error("=" * 80)
        import traceback
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        logger.error("=" * 80)
        
        # Send error notification via HTTP
        try:
            import requests
            requests.post(
                'http://localhost:5000/api/internal/backup-notification',
                json={
                    'title': f"✗ Backup Failed: {stack_name}",
                    'message': f"Error: {error_msg}",
                    'type': 'error'
                },
                timeout=5
            )
        except Exception as notif_error:
            logger.error(f"Failed to send error notification: {notif_error}")
            import traceback
            logger.error(traceback.format_exc())
        
        # v2.1: restart exactly the containers this backup stopped. The old call
        # was 'docker-compose up -d' — the v1 binary is not in the image, the
        # error was swallowed, and the stack stayed down.
        if stopped_ids and docker_client:
            _start_containers(stopped_ids)
        if partial_path and os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass
        
        # Record failure
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO backup_history 
                (stack_name, backup_file_path, status, error_message, backup_date)
                VALUES (?, '', 'failed', ?, CURRENT_TIMESTAMP)
            """, (stack_name, error_msg))
            conn.commit()
            conn.close()
        except:
            pass
        
        return False, '', error_msg



# ============================================================================
# RESTORE FUNCTIONS
# ============================================================================

def restore_from_backup(backup_id: int, stacks_dir: str = '/stacks') -> Tuple[bool, str]:
    """
    Restore a stack from backup

    v2.1 rewrite — the order is now safe:
      1. extract and check the archive FIRST (nothing touched yet),
      2. stop the stack, move the current folder and volumes aside,
      3. put the backup in place,
      4. on ANY failure, put everything back exactly as it was.
    The old version moved the stack folder away before extracting; a failed
    extraction (e.g. Python 3.14 refusing an absolute symlink with the default
    'data' filter) left the stack gone and its containers stopped. The aside
    copies of the stack folder are hidden (.<stack>.pre-restore.<ts>) so they
    no longer show up as an extra stack.

    Returns:
        (success, error_message)
    """
    restore_temp = None
    moved = []          # (original_path, aside_path) — undone in reverse on failure
    created = []        # paths that did not exist before and were created
    stopped_ids = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM backup_history WHERE id = ?", (backup_id,))
        backup = cursor.fetchone()
        conn.close()
        
        if not backup:
            return False, f"Backup {backup_id} not found"
        if not os.path.exists(backup['backup_file_path']):
            return False, f"Backup file not found: {backup['backup_file_path']}"
        
        stack_name = backup['stack_name']
        stack_path = os.path.join(stacks_dir, stack_name)
        ts = int(time.time())
        
        # 1) extract first. filter='tar' keeps every file inside restore_temp but
        #    restores symlinks as they were (the 'data' default refuses absolute
        #    links, which app data often contains).
        restore_temp = os.path.join(BACKUP_TEMP_DIR, f'restore_{stack_name}_{ts}')
        os.makedirs(restore_temp, exist_ok=True)
        with tarfile.open(backup['backup_file_path'], 'r:gz') as tar:
            tar.extractall(restore_temp, filter='tar')
        entries = sorted(os.listdir(restore_temp))
        if not entries:
            return False, "Backup archive is empty"
        extracted_dir = os.path.join(restore_temp, entries[0])
        
        volume_map = {}
        mapping_file = os.path.join(extracted_dir, 'volume_mapping.txt')
        volumes_dir = os.path.join(extracted_dir, 'volumes')
        if os.path.exists(mapping_file):
            with open(mapping_file, 'r') as f:
                for line in f:
                    if '=' in line:
                        vol_name, vol_path = line.rstrip('\n').split('=', 1)
                        volume_map[vol_name] = vol_path
        
        # 2) stop the stack
        stopped_ids = _stop_stack(stack_name)
        
        # 3) stack folder: move aside (hidden), then write the backup's files
        compose_name = 'docker-compose.yml'
        aside_stack = None
        if os.path.exists(stack_path):
            for filename in ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']:
                if os.path.exists(os.path.join(stack_path, filename)):
                    compose_name = filename      # keep the file name the stack already used
                    break
            aside_stack = os.path.join(stacks_dir, f".{stack_name}.pre-restore.{ts}")
            os.rename(stack_path, aside_stack)
            moved.append((stack_path, aside_stack))
        stack_files = os.path.join(extracted_dir, 'stack_files')
        if os.path.isdir(stack_files):
            # v2.1 backups carry the whole stack folder
            shutil.copytree(stack_files, stack_path, symlinks=True)
        else:
            os.makedirs(stack_path)
        if not aside_stack:
            created.append(stack_path)
        if aside_stack:
            # v2.1: anything in the old folder that the backup doesn't contain —
            # stack.env/configs on older backups, volume folders whose backup was
            # switched off — is moved back (a rename, no copying) instead of being
            # left behind in the .pre-restore folder. Volumes that ARE in the
            # backup are skipped here and restored below.
            restore_targets = {os.path.normpath(p) for p in volume_map.values()}
            # files the backup itself provides stay in the aside copy, so a
            # rollback can always put the originals back untouched
            if os.path.exists(os.path.join(extracted_dir, 'docker-compose.yml')):
                for fn in ('compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml'):
                    restore_targets.add(os.path.normpath(os.path.join(stack_path, fn)))
            if os.path.exists(os.path.join(extracted_dir, '.env')):
                restore_targets.add(os.path.normpath(os.path.join(stack_path, '.env')))

            def carry_over(src_dir, dst_dir):
                for name in sorted(os.listdir(src_dir)):
                    if '.pre-restore.' in name:
                        continue
                    s, d = os.path.join(src_dir, name), os.path.join(dst_dir, name)
                    if os.path.normpath(d) in restore_targets:
                        continue
                    if not os.path.lexists(d):
                        os.rename(s, d)
                        moved.append((s, d))
                    elif os.path.isdir(s) and os.path.isdir(d) and not os.path.islink(s) and not os.path.islink(d):
                        carry_over(s, d)
            carry_over(aside_stack, stack_path)
        if os.path.exists(os.path.join(extracted_dir, 'docker-compose.yml')):
            shutil.copy2(os.path.join(extracted_dir, 'docker-compose.yml'), os.path.join(stack_path, compose_name))
        if os.path.exists(os.path.join(extracted_dir, '.env')):
            shutil.copy2(os.path.join(extracted_dir, '.env'), os.path.join(stack_path, '.env'))
        if aside_stack and os.path.exists(os.path.join(aside_stack, '.dockup-meta.json')):
            # keep tags, web UI URL and schedules (not part of the backup)
            shutil.copy2(os.path.join(aside_stack, '.dockup-meta.json'), os.path.join(stack_path, '.dockup-meta.json'))
        
        # 4) volumes
        logger.info(f"Restoring {len(volume_map)} volumes from mapping")
        for vol_name, vol_path in volume_map.items():
            vol_src = os.path.join(volumes_dir, vol_name)
            if not os.path.lexists(vol_src):
                logger.warning(f"Volume {vol_name} missing in backup")
                continue
            if os.path.lexists(vol_path):
                aside = f"{vol_path}.pre-restore.{ts}"
                os.rename(vol_path, aside)
                moved.append((vol_path, aside))
                logger.info(f"Kept existing data as: {aside}")
            else:
                created.append(vol_path)
            parent_dir = os.path.dirname(vol_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            if os.path.isdir(vol_src) and not os.path.islink(vol_src):
                shutil.copytree(vol_src, vol_path, symlinks=True)
            else:
                shutil.copy2(vol_src, vol_path, follow_symlinks=False)
            logger.info(f"Restored: {vol_path}")
        
        _start_containers(stopped_ids)
        logger.info(f"✓ Restore completed for {stack_name} from backup {backup_id}")
        return True, ""
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Restore failed: {error_msg} — rolling back")
        rollback_ok = True
        for path in reversed(created):
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                elif os.path.lexists(path):
                    os.remove(path)
            except Exception as re_err:
                rollback_ok = False
                logger.error(f"Rollback: could not remove {path}: {re_err}")
        for original, aside in reversed(moved):
            try:
                if os.path.lexists(original):
                    if os.path.isdir(original) and not os.path.islink(original):
                        shutil.rmtree(original)
                    else:
                        os.remove(original)
                os.rename(aside, original)
            except Exception as re_err:
                rollback_ok = False
                logger.error(f"Rollback: could not move {aside} back to {original}: {re_err}")
        _start_containers(stopped_ids)
        if moved or created:
            error_msg += (" — everything was put back as it was" if rollback_ok
                          else " — ROLLBACK INCOMPLETE, check the .pre-restore folders")
        return False, error_msg
    finally:
        if restore_temp and os.path.exists(restore_temp):
            shutil.rmtree(restore_temp, ignore_errors=True)


# ============================================================================
# SCAN AND IMPORT FUNCTIONS
# ============================================================================

def scan_backup_directory() -> List[Dict]:
    """
    Scan backup destination for existing backups
    
    Returns:
        List of discovered backup files
    """
    try:
        logger.info("=" * 80)
        logger.info("SCAN_BACKUP_DIRECTORY CALLED")
        logger.info("=" * 80)
        
        is_available, dest_path, _, error_msg = check_backup_destination_available()
        logger.info(f"Destination available: {is_available}")
        logger.info(f"Destination path: {dest_path}")
        logger.info(f"Error message: {error_msg}")
        
        if not is_available:
            logger.error(f"Cannot scan: {error_msg}")
            return []
        
        # List all files in directory
        try:
            all_files = os.listdir(dest_path)
            logger.info(f"Found {len(all_files)} files/directories in {dest_path}")
            logger.info(f"Files: {all_files}")
        except Exception as e:
            logger.error(f"Error listing directory: {e}")
            return []
        
        backups = []
        pattern = re.compile(r'^(.+?)_(\d{4}-\d{2}-\d{2}-\d{6})\.tar\.gz$')
        
        for filename in all_files:
            logger.info(f"Checking file: {filename}")
            if filename.endswith('.tar.gz'):
                logger.info(f"  -> Is .tar.gz file")
                match = pattern.match(filename)
                if match:
                    logger.info(f"  -> Matches pattern")
                    stack_name = match.group(1)
                    timestamp_str = match.group(2)
                    file_path = os.path.join(dest_path, filename)
                    file_size = os.path.getsize(file_path) / (1024 * 1024)
                    
                    logger.info(f"  -> Stack: {stack_name}, Date: {timestamp_str}, Size: {file_size:.1f}MB")
                    
                    # Parse timestamp
                    try:
                        backup_date = datetime.strptime(timestamp_str, '%Y-%m-%d-%H%M%S')
                    except:
                        backup_date = datetime.fromtimestamp(os.path.getmtime(file_path))
                    
                    backups.append({
                        'filename': filename,
                        'stack_name': stack_name,
                        'backup_date': backup_date.isoformat(),
                        'file_path': file_path,
                        'size_mb': round(file_size, 2)
                    })
                else:
                    logger.info(f"  -> Does NOT match pattern")
            else:
                logger.info(f"  -> Not a .tar.gz file")
        
        logger.info(f"Total backups found: {len(backups)}")
        return sorted(backups, key=lambda x: x['backup_date'], reverse=True)
        
    except Exception as e:
        logger.error(f"Error scanning backup directory: {e}")
        return []


def import_discovered_backups() -> Tuple[int, str]:
    """
    Import discovered backups into database
    
    Returns:
        (count_imported, error_message)
    """
    try:
        discovered = scan_backup_directory()
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        imported = 0
        for backup in discovered:
            # Check if already in database
            cursor.execute("""
                SELECT id FROM backup_history 
                WHERE backup_file_path = ?
            """, (backup['file_path'],))
            
            if not cursor.fetchone():
                # Import
                cursor.execute("""
                    INSERT INTO backup_history 
                    (stack_name, backup_date, backup_file_path, backup_size_mb, status)
                    VALUES (?, ?, ?, ?, 'success')
                """, (
                    backup['stack_name'],
                    backup['backup_date'],
                    backup['file_path'],
                    backup['size_mb']
                ))
                imported += 1
        
        conn.commit()
        conn.close()
        
        logger.info(f"✓ Imported {imported} backups")
        return imported, ""
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error importing backups: {error_msg}")
        return 0, error_msg


# ============================================================================
# BACKUP WORKER THREAD
# ============================================================================

def backup_worker():
    """Background worker thread to process backup queue"""
    global backup_worker_running
    
    logger.info("=" * 80)
    logger.info("🔥🔥🔥 BACKUP WORKER THREAD FUNCTION ENTERED 🔥🔥🔥")
    logger.info("=" * 80)
    
    try:
        logger.info(f"backup_worker_running: {backup_worker_running}")
        logger.info(f"backup_queue: {backup_queue}")
        logger.info(f"backup_queue.qsize(): {backup_queue.qsize()}")
    except Exception as e:
        logger.error(f"Error in worker startup logging: {e}")
    
    logger.info("✓ Backup worker thread main loop starting")
    
    while backup_worker_running:
        try:
            # Get job from queue (timeout so we can check running flag)
            try:
                queue_id = backup_queue.get(timeout=1)
                logger.info(f"🔥 WORKER GOT QUEUE ID: {queue_id}")
            except Empty:
                continue
            
            # Get job details
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM backup_queue WHERE id = ? AND status = 'queued'
            """, (queue_id,))
            job = cursor.fetchone()
            conn.close()
            
            logger.info(f"Job from DB: {dict(job) if job else 'NOT FOUND'}")
            
            if not job:
                backup_queue.task_done()
                continue
            
            stack_name = job['stack_name']
            
            # Get or create lock for this stack
            with backup_locks_lock:
                if stack_name not in backup_locks:
                    backup_locks[stack_name] = threading.Lock()
                stack_lock = backup_locks[stack_name]
            
            # Try to acquire lock (non-blocking)
            if not stack_lock.acquire(blocking=False):
                logger.warning(f"Stack {stack_name} already backing up, requeueing job {queue_id}")
                backup_queue.put(queue_id)
                backup_queue.task_done()
                time.sleep(1)
                continue
            
            try:
                # Update status to running
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE backup_queue 
                    SET status = 'running', start_time = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (queue_id,))
                conn.commit()
                conn.close()
                
                logger.info(f"Processing backup job {queue_id} for {stack_name}")
                
                # Execute backup
                logger.info(f"Calling execute_backup for {stack_name}...")
                success, backup_path, error = execute_backup(stack_name, STACKS_DIR)
                logger.info(f"execute_backup returned: success={success}, path={backup_path}, error={error}")
                
                # Update queue status
                conn = get_db_connection()
                cursor = conn.cursor()
                
                if success:
                    logger.info(f"✅ Backup succeeded, updating queue to completed")
                    cursor.execute("""
                        UPDATE backup_queue 
                        SET status = 'completed', end_time = CURRENT_TIMESTAMP, backup_file_path = ?
                        WHERE id = ?
                    """, (backup_path, queue_id))
                else:
                    logger.error(f"❌ Backup failed, updating queue to failed: {error}")
                    cursor.execute("""
                        UPDATE backup_queue 
                        SET status = 'failed', end_time = CURRENT_TIMESTAMP, error_message = ?
                        WHERE id = ?
                    """, (error, queue_id))
                
                conn.commit()
                conn.close()
                
            except Exception as e:
                logger.error(f"Error in backup worker: {e}")
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE backup_queue 
                        SET status = 'failed', end_time = CURRENT_TIMESTAMP, error_message = ?
                        WHERE id = ?
                    """, (str(e), queue_id))
                    conn.commit()
                    conn.close()
                except:
                    pass
            finally:
                stack_lock.release()
                backup_queue.task_done()
                
        except Exception as outer_e:
            logger.error(f"Outer worker error: {outer_e}")
    
    logger.info("=" * 80)
    logger.info("🔥 BACKUP WORKER THREAD EXITING (backup_worker_running=False)")
    logger.info("=" * 80)


def start_backup_worker():
    """Start the backup worker thread"""
    global backup_worker_running, backup_worker_thread
    
    logger.info("=" * 80)
    logger.info("🚀 START_BACKUP_WORKER CALLED")
    logger.info("=" * 80)
    
    try:
        # Check if already running
        if backup_worker_thread and backup_worker_thread.is_alive():
            logger.info(f"Backup worker already running (thread alive: {backup_worker_thread.is_alive()})")
            backup_worker_running = True  # Ensure flag is set
            return
        
        logger.info(f"Current state: backup_worker_running={backup_worker_running}, thread_exists={backup_worker_thread is not None}")
        if backup_worker_thread:
            logger.info(f"Existing thread alive: {backup_worker_thread.is_alive()}")
        
        # v2.1: the queue lives in memory; after a restart, rows left 'running'
        # stayed running forever and 'queued' ones never ran.
        try:
            conn = get_db_connection()
            conn.execute("""UPDATE backup_queue SET status = 'failed', end_time = CURRENT_TIMESTAMP,
                            error_message = 'DockUp restarted while this backup was running'
                            WHERE status = 'running'""")
            for row in conn.execute("SELECT id FROM backup_queue WHERE status = 'queued' ORDER BY id"):
                backup_queue.put(row['id'])
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Could not recover the backup queue: {e}")
        
        # Set flag BEFORE starting thread to avoid race condition
        backup_worker_running = True
        
        # Create and start thread
        backup_worker_thread = threading.Thread(target=backup_worker, daemon=True, name="BackupWorker")
        logger.info("Thread object created, calling start()...")
        backup_worker_thread.start()
        logger.info("Thread start() called")
        
        logger.info("✓✓✓ BACKUP WORKER THREAD STARTED ✓✓✓")
        logger.info(f"Worker thread is alive: {backup_worker_thread.is_alive()}")
        logger.info(f"Worker thread name: {backup_worker_thread.name}")
        logger.info(f"Worker thread ident: {backup_worker_thread.ident}")
        
        # Give it a moment to start
        time.sleep(0.5)
        logger.info(f"Worker thread still alive after 0.5s: {backup_worker_thread.is_alive()}")
        
        # Log initial queue state
        logger.info(f"Queue size: {backup_queue.qsize()}")
        
    except Exception as e:
        logger.error(f"❌ FAILED TO START WORKER: {e}")
        import traceback
        logger.error(traceback.format_exc())
        backup_worker_running = False
    
    logger.info("=" * 80)
    logger.info("START_BACKUP_WORKER COMPLETED")
    logger.info("=" * 80)


def stop_backup_worker():
    """Stop the backup worker thread"""
    global backup_worker_running
    backup_worker_running = False
    if backup_worker_thread:
        backup_worker_thread.join(timeout=5)
    logger.info("✓ Backup worker stopped")


def queue_backup(stack_name: str) -> int:
    """
    Add backup to queue with retry on database lock
    
    Returns:
        queue_id
    """
    logger.info("=" * 80)
    logger.info(f"QUEUE_BACKUP CALLED FOR: {stack_name}")
    logger.info(f"Current queue size: {backup_queue.qsize()}")
    logger.info(f"backup_worker_running: {backup_worker_running}")
    logger.info(f"backup_worker_thread exists: {backup_worker_thread is not None}")
    if backup_worker_thread:
        logger.info(f"backup_worker_thread alive: {backup_worker_thread.is_alive()}")
    logger.info("=" * 80)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO backup_queue (stack_name, status)
                VALUES (?, 'queued')
            """, (stack_name,))
            queue_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            logger.info(f"📝 Created backup_queue record with ID: {queue_id}")
            
            backup_queue.put(queue_id)
            logger.info(f"✅ Put queue_id {queue_id} into backup_queue")
            logger.info(f"📊 Queue size after put: {backup_queue.qsize()}")
            
            return queue_id
            
        except sqlite3.OperationalError as e:
            if 'locked' in str(e) and attempt < max_retries - 1:
                logger.warning(f"Database locked, retrying... (attempt {attempt + 1}/{max_retries})")
                time.sleep(0.5)
                continue
            else:
                logger.error(f"Error queuing backup after {attempt + 1} attempts: {e}")
                return -1
        except Exception as e:
            logger.error(f"Error queuing backup: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return -1
    
    return -1

