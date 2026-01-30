#!/usr/bin/env python3
"""
DockUp Backup Manager
Complete backup functionality for Docker Compose stacks

Phases Implemented:
- Phase 1: Core backup (database, SMB mounting, execution, retention)
- Phase 2: Queue system (sequential backup execution)
- Phase 3: Scheduler integration
- Phase 4: Restore functionality
- Phase 5: Notifications and polish
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
backup_worker_running = False
backup_worker_thread = None
docker_client = None  # Will be set by main app


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def init_backup_database():
    """Initialize SQLite database for backup feature"""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Global backup destination configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS global_backup_config (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            local_path TEXT,
            smb_host TEXT,
            smb_share TEXT,
            smb_username TEXT,
            smb_password TEXT,
            smb_mount_path TEXT,
            auto_mount BOOLEAN DEFAULT 1,
            mount_status TEXT DEFAULT 'disconnected',
            last_mount_check TIMESTAMP
        )
    """)
    
    # Per-stack backup configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL UNIQUE,
            enabled BOOLEAN DEFAULT 0,
            schedule TEXT DEFAULT 'manual',
            schedule_time TEXT DEFAULT '03:00',
            schedule_day INTEGER DEFAULT 0,
            retention_count INTEGER DEFAULT 7,
            stop_before_backup BOOLEAN DEFAULT 1,
            priority INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Volume selection per stack
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_volume_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL,
            host_path TEXT NOT NULL,
            container_path TEXT NOT NULL,
            backup_enabled BOOLEAN DEFAULT 1,
            estimated_size_mb REAL
        )
    """)
    
    # Backup queue
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL,
            scheduled_time TIMESTAMP NOT NULL,
            queued_at TIMESTAMP NOT NULL,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'queued',
            error_message TEXT,
            backup_file_path TEXT,
            backup_size_mb REAL,
            duration_seconds INTEGER
        )
    """)
    
    # Backup history
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_name TEXT NOT NULL,
            backup_date TIMESTAMP NOT NULL,
            backup_file_path TEXT NOT NULL,
            backup_size_mb REAL,
            duration_seconds INTEGER,
            status TEXT NOT NULL,
            error_message TEXT,
            volumes_backed_up TEXT
        )
    """)
    
    # Insert default global config if doesn't exist
    cursor.execute("SELECT COUNT(*) as cnt FROM global_backup_config")
    if cursor.fetchone()['cnt'] == 0:
        cursor.execute("""
            INSERT INTO global_backup_config (id, type, local_path, mount_status)
            VALUES (1, 'local', ?, 'connected')
        """, (BACKUP_LOCAL_DIR,))
    
    conn.commit()
    conn.close()
    logger.info("✓ Backup database initialized")


def get_db_connection():
    """Get database connection with row factory"""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================================
# SMB MOUNTING FUNCTIONS
# ============================================================================

def mount_smb_share(host: str, share: str, username: str, password: str, mount_path: str = '') -> bool:
    """
    Mount SMB/CIFS share
    
    Args:
        host: SMB server hostname or IP
        share: Share name
        username: SMB username
        password: SMB password
        mount_path: Subdirectory on share (optional)
    
    Returns:
        True if mounted successfully
    """
    try:
        # Check if already mounted
        result = subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], 
                              capture_output=True)
        if result.returncode == 0:
            logger.info("SMB share already mounted")
            return True
        
        # Create credentials file (more secure than command line)
        creds_file = '/tmp/smb_creds'
        with open(creds_file, 'w') as f:
            f.write(f"username={username}\n")
            f.write(f"password={password}\n")
        os.chmod(creds_file, 0o600)
        
        # Build mount command
        share_path = f"//{host}/{share}"
        mount_cmd = [
            'mount',
            '-t', 'cifs',
            share_path,
            BACKUP_MOUNT_POINT,
            '-o', f'credentials={creds_file},uid=0,gid=0,file_mode=0644,dir_mode=0755'
        ]
        
        # Execute mount
        result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
        
        # Clean up credentials file
        try:
            os.remove(creds_file)
        except:
            pass
        
        if result.returncode == 0:
            logger.info(f"✓ SMB share mounted: {share_path}")
            
            # Update database
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE global_backup_config 
                SET mount_status = 'connected', last_mount_check = CURRENT_TIMESTAMP
                WHERE id = 1
            """)
            conn.commit()
            conn.close()
            
            return True
        else:
            logger.error(f"Failed to mount SMB share: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Error mounting SMB share: {e}")
        return False


def unmount_smb_share() -> bool:
    """Unmount SMB share"""
    try:
        result = subprocess.run(['umount', BACKUP_MOUNT_POINT], 
                              capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            logger.info("✓ SMB share unmounted")
            
            # Update database
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE global_backup_config 
                SET mount_status = 'disconnected', last_mount_check = CURRENT_TIMESTAMP
                WHERE id = 1
            """)
            conn.commit()
            conn.close()
            
            return True
        else:
            logger.warning(f"Unmount warning: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Error unmounting SMB share: {e}")
        return False


def check_backup_destination_available() -> Tuple[bool, str, int]:
    """
    Check if backup destination is available and writable
    
    Returns:
        (is_available, mount_point, available_gb)
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM global_backup_config WHERE id = 1")
        config = cursor.fetchone()
        conn.close()
        
        if not config:
            return False, '', 0
        
        backup_type = config['type']
        
        if backup_type == 'local':
            mount_point = config['local_path']
            if os.path.exists(mount_point) and os.access(mount_point, os.W_OK):
                stat = shutil.disk_usage(mount_point)
                available_gb = stat.free // (1024**3)
                return True, mount_point, available_gb
            return False, mount_point, 0
        
        elif backup_type == 'smb':
            # Check if mounted
            result = subprocess.run(['mountpoint', '-q', BACKUP_MOUNT_POINT], 
                                  capture_output=True)
            
            if result.returncode == 0:
                # Mounted - check space
                stat = shutil.disk_usage(BACKUP_MOUNT_POINT)
                available_gb = stat.free // (1024**3)
                
                # Apply mount_path if specified
                if config['smb_mount_path']:
                    actual_path = os.path.join(BACKUP_MOUNT_POINT, config['smb_mount_path'])
                    os.makedirs(actual_path, exist_ok=True)
                    return True, actual_path, available_gb
                
                return True, BACKUP_MOUNT_POINT, available_gb
            else:
                # Not mounted - try to mount
                if config['auto_mount']:
                    success = mount_smb_share(
                        config['smb_host'],
                        config['smb_share'],
                        config['smb_username'],
                        config['smb_password'],
                        config['smb_mount_path'] or ''
                    )
                    if success:
                        return check_backup_destination_available()
                return False, BACKUP_MOUNT_POINT, 0
        
        return False, '', 0
        
    except Exception as e:
        logger.error(f"Error checking backup destination: {e}")
        return False, '', 0


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
        
        # Always include compose file
        stack_dir = os.path.join(stacks_dir, stack_name)
        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        if os.path.exists(compose_file):
            total_size += os.path.getsize(compose_file)
        
        # Check for .env files
        env_file = os.path.join(stack_dir, '.env')
        if os.path.exists(env_file):
            total_size += os.path.getsize(env_file)
        
        stack_env_file = os.path.join(stack_dir, 'stack.env')
        if os.path.exists(stack_env_file):
            total_size += os.path.getsize(stack_env_file)
        
        # Add selected volumes
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT host_path, estimated_size_mb 
            FROM backup_volume_selections 
            WHERE stack_name = ? AND backup_enabled = 1
        """, (stack_name,))
        
        for row in cursor.fetchall():
            if row['estimated_size_mb']:
                total_size += row['estimated_size_mb'] * 1024 * 1024
            elif os.path.exists(row['host_path']):
                # Calculate actual size
                result = subprocess.run(['du', '-sb', row['host_path']], 
                                      capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    size = int(result.stdout.split()[0])
                    total_size += size
        
        conn.close()
        
        return total_size / (1024 * 1024)  # Convert to MB
        
    except Exception as e:
        logger.error(f"Error estimating backup size for {stack_name}: {e}")
        return 0


def stop_stack_containers(stack_name: str, timeout: int = 30) -> bool:
    """Stop all containers in a stack"""
    try:
        containers = docker_client.containers.list(filters={'label': f'com.docker.compose.project={stack_name}'})
        
        for container in containers:
            try:
                logger.info(f"Stopping container: {container.name}")
                container.stop(timeout=timeout)
            except Exception as e:
                logger.warning(f"Timeout stopping {container.name}, force killing")
                container.kill()
        
        return True
    except Exception as e:
        logger.error(f"Error stopping containers for {stack_name}: {e}")
        return False


def start_stack_containers(stack_name: str) -> bool:
    """Start all containers in a stack"""
    try:
        containers = docker_client.containers.list(all=True, filters={'label': f'com.docker.compose.project={stack_name}'})
        
        for container in containers:
            if container.status != 'running':
                logger.info(f"Starting container: {container.name}")
                container.start()
        
        return True
    except Exception as e:
        logger.error(f"Error starting containers for {stack_name}: {e}")
        return False


def verify_stack_health(stack_name: str, timeout: int = 60) -> bool:
    """Verify stack containers are healthy after restart"""
    try:
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            containers = docker_client.containers.list(filters={'label': f'com.docker.compose.project={stack_name}'})
            
            all_healthy = True
            for container in containers:
                container.reload()
                
                if container.status != 'running':
                    all_healthy = False
                    break
                
                # Check health if container has healthcheck
                health_status = container.attrs.get('State', {}).get('Health', {}).get('Status')
                if health_status and health_status != 'healthy':
                    all_healthy = False
                    break
            
            if all_healthy and len(containers) > 0:
                logger.info(f"✓ Stack {stack_name} is healthy")
                return True
            
            time.sleep(2)
        
        logger.warning(f"Stack {stack_name} failed health check within {timeout}s")
        return False
        
    except Exception as e:
        logger.error(f"Error verifying stack health for {stack_name}: {e}")
        return False


def create_backup_archive(stack_name: str, stacks_dir: str = '/stacks') -> Optional[str]:
    """
    Create backup archive for a stack
    
    Args:
        stack_name: Name of the stack
        stacks_dir: Base directory for stacks
    
    Returns:
        Path to created archive or None on failure
    """
    try:
        # Create temp directory for this backup
        timestamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
        temp_backup_dir = os.path.join(BACKUP_TEMP_DIR, f"{stack_name}_{timestamp}")
        os.makedirs(temp_backup_dir, exist_ok=True)
        
        stack_dir = os.path.join(stacks_dir, stack_name)
        
        # Copy docker-compose.yml
        compose_file = os.path.join(stack_dir, 'docker-compose.yml')
        if os.path.exists(compose_file):
            shutil.copy2(compose_file, os.path.join(temp_backup_dir, 'docker-compose.yml'))
        
        # Copy .env files
        for env_file in ['.env', 'stack.env']:
            env_path = os.path.join(stack_dir, env_file)
            if os.path.exists(env_path):
                shutil.copy2(env_path, os.path.join(temp_backup_dir, env_file))
        
        # Copy selected volumes
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT host_path, container_path 
            FROM backup_volume_selections 
            WHERE stack_name = ? AND backup_enabled = 1
        """, (stack_name,))
        
        volumes_list = []
        for row in cursor.fetchall():
            host_path = row['host_path']
            container_path = row['container_path'].lstrip('/')
            
            if os.path.exists(host_path):
                dest_path = os.path.join(temp_backup_dir, 'volumes', container_path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                
                if os.path.isdir(host_path):
                    shutil.copytree(host_path, dest_path, symlinks=True)
                else:
                    shutil.copy2(host_path, dest_path)
                
                volumes_list.append(f"{host_path} → {container_path}")
        
        conn.close()
        
        # Create tar.gz archive
        archive_name = f"{stack_name}_{timestamp}.tar.gz"
        archive_path = os.path.join(BACKUP_TEMP_DIR, archive_name)
        
        with tarfile.open(archive_path, 'w:gz') as tar:
            tar.add(temp_backup_dir, arcname=stack_name)
        
        # Clean up temp directory
        shutil.rmtree(temp_backup_dir)
        
        # Get archive size
        archive_size_mb = os.path.getsize(archive_path) / (1024 * 1024)
        
        logger.info(f"✓ Created backup archive: {archive_name} ({archive_size_mb:.2f} MB)")
        
        return archive_path, volumes_list, archive_size_mb
        
    except Exception as e:
        logger.error(f"Error creating backup archive for {stack_name}: {e}")
        # Clean up on failure
        if os.path.exists(temp_backup_dir):
            shutil.rmtree(temp_backup_dir, ignore_errors=True)
        return None, [], 0


def execute_backup(stack_name: str, stacks_dir: str = '/stacks') -> Dict:
    """
    Execute complete backup for a stack
    
    Returns:
        Dict with status, message, and backup details
    """
    start_time = time.time()
    
    try:
        logger.info(f"Starting backup for stack: {stack_name}")
        
        # Check backup destination
        is_available, mount_point, available_gb = check_backup_destination_available()
        if not is_available:
            return {
                'success': False,
                'error': 'Backup destination not available'
            }
        
        # Estimate size and check space
        estimated_size_mb = estimate_backup_size(stack_name, stacks_dir)
        required_gb = (estimated_size_mb * 2) / 1024  # Need 2x for safety
        
        if available_gb < required_gb:
            return {
                'success': False,
                'error': f'Insufficient space: need {required_gb:.1f} GB, have {available_gb} GB'
            }
        
        # Get backup config
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM backup_configs WHERE stack_name = ?", (stack_name,))
        config = cursor.fetchone()
        
        # Stop containers if configured
        containers_stopped = False
        if config and config['stop_before_backup']:
            logger.info(f"Stopping containers for {stack_name}")
            if not stop_stack_containers(stack_name):
                conn.close()
                return {
                    'success': False,
                    'error': 'Failed to stop containers'
                }
            containers_stopped = True
        
        # Create backup archive
        result = create_backup_archive(stack_name, stacks_dir)
        if not result or not result[0]:
            if containers_stopped:
                start_stack_containers(stack_name)
            conn.close()
            return {
                'success': False,
                'error': 'Failed to create backup archive'
            }
        
        archive_path, volumes_list, archive_size_mb = result
        
        # Move archive to destination
        archive_filename = os.path.basename(archive_path)
        dest_path = os.path.join(mount_point, archive_filename)
        shutil.move(archive_path, dest_path)
        
        # Restart containers if we stopped them
        if containers_stopped:
            logger.info(f"Restarting containers for {stack_name}")
            if not start_stack_containers(stack_name):
                logger.error(f"Failed to restart containers for {stack_name}")
                # Don't fail the backup, but log error
            else:
                # Verify health
                verify_stack_health(stack_name)
        
        duration_seconds = int(time.time() - start_time)
        
        # Record in history
        cursor.execute("""
            INSERT INTO backup_history 
            (stack_name, backup_date, backup_file_path, backup_size_mb, duration_seconds, status, volumes_backed_up)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, 'success', ?)
        """, (stack_name, dest_path, archive_size_mb, duration_seconds, '\n'.join(volumes_list)))
        
        # Apply retention policy
        if config and config['retention_count'] > 0:
            cursor.execute("""
                SELECT id, backup_file_path 
                FROM backup_history 
                WHERE stack_name = ? AND status = 'success'
                ORDER BY backup_date DESC
            """, (stack_name,))
            
            all_backups = cursor.fetchall()
            if len(all_backups) > config['retention_count']:
                for old_backup in all_backups[config['retention_count']:]:
                    # Delete file
                    try:
                        if os.path.exists(old_backup['backup_file_path']):
                            os.remove(old_backup['backup_file_path'])
                        logger.info(f"Deleted old backup: {old_backup['backup_file_path']}")
                    except Exception as e:
                        logger.warning(f"Failed to delete old backup: {e}")
                    
                    # Mark as deleted in database
                    cursor.execute("""
                        UPDATE backup_history 
                        SET status = 'deleted' 
                        WHERE id = ?
                    """, (old_backup['id'],))
        
        conn.commit()
        conn.close()
        
        logger.info(f"✓ Backup completed for {stack_name} in {duration_seconds}s")
        
        return {
            'success': True,
            'backup_file': dest_path,
            'size_mb': archive_size_mb,
            'duration_seconds': duration_seconds
        }
        
    except Exception as e:
        logger.error(f"Error executing backup for {stack_name}: {e}")
        
        # Record failure in history
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            duration_seconds = int(time.time() - start_time)
            cursor.execute("""
                INSERT INTO backup_history 
                (stack_name, backup_date, backup_file_path, duration_seconds, status, error_message)
                VALUES (?, CURRENT_TIMESTAMP, '', ?, 'failed', ?)
            """, (stack_name, duration_seconds, str(e)))
            conn.commit()
            conn.close()
        except:
            pass
        
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# BACKUP QUEUE WORKER
# ============================================================================

def backup_worker():
    """Background worker that processes backup queue sequentially"""
    global backup_worker_running
    
    logger.info("Backup worker thread started")
    
    while backup_worker_running:
        try:
            # Get next job from queue (wait up to 5 seconds)
            job = backup_queue.get(timeout=5)
            
            queue_id = job['queue_id']
            stack_name = job['stack_name']
            stacks_dir = job.get('stacks_dir', '/stacks')
            notify_callback = job.get('notify_callback')
            
            logger.info(f"Processing backup job {queue_id} for {stack_name}")
            
            # Update queue status to running
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE backup_queue 
                SET status = 'running', started_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (queue_id,))
            conn.commit()
            conn.close()
            
            # Execute backup
            result = execute_backup(stack_name, stacks_dir)
            
            # Update queue with result
            conn = get_db_connection()
            cursor = conn.cursor()
            
            if result['success']:
                cursor.execute("""
                    UPDATE backup_queue 
                    SET status = 'completed', 
                        completed_at = CURRENT_TIMESTAMP,
                        backup_file_path = ?,
                        backup_size_mb = ?,
                        duration_seconds = ?
                    WHERE id = ?
                """, (result['backup_file'], result['size_mb'], result['duration_seconds'], queue_id))
                
                # Send success notification
                if notify_callback:
                    try:
                        notify_callback(
                            f"✓ Backup completed: {stack_name}",
                            f"Size: {result['size_mb']:.1f} MB\nDuration: {result['duration_seconds']}s",
                            'success'
                        )
                    except Exception as e:
                        logger.error(f"Notification error: {e}")
            else:
                cursor.execute("""
                    UPDATE backup_queue 
                    SET status = 'failed', 
                        completed_at = CURRENT_TIMESTAMP,
                        error_message = ?
                    WHERE id = ?
                """, (result.get('error', 'Unknown error'), queue_id))
                
                # Send failure notification
                if notify_callback:
                    try:
                        notify_callback(
                            f"✗ Backup failed: {stack_name}",
                            f"Error: {result.get('error', 'Unknown error')}",
                            'failure'
                        )
                    except Exception as e:
                        logger.error(f"Notification error: {e}")
            
            conn.commit()
            conn.close()
            
            backup_queue.task_done()
            
        except Empty:
            # No jobs in queue, continue loop
            continue
        except Exception as e:
            logger.error(f"Error in backup worker: {e}")
    
    logger.info("Backup worker thread stopped")


def start_backup_worker():
    """Start the backup queue worker thread"""
    global backup_worker_running, backup_worker_thread
    
    if backup_worker_running:
        logger.info("Backup worker already running")
        return
    
    backup_worker_running = True
    backup_worker_thread = threading.Thread(target=backup_worker, daemon=True)
    backup_worker_thread.start()
    logger.info("✓ Backup worker thread started")


def stop_backup_worker():
    """Stop the backup queue worker thread"""
    global backup_worker_running, backup_worker_thread
    
    if not backup_worker_running:
        return
    
    logger.info("Stopping backup worker...")
    backup_worker_running = False
    
    if backup_worker_thread:
        backup_worker_thread.join(timeout=10)
    
    logger.info("✓ Backup worker stopped")


def queue_backup(stack_name: str, stacks_dir: str = '/stacks', notify_callback=None) -> int:
    """
    Add a backup job to the queue
    
    Args:
        stack_name: Name of the stack to backup
        stacks_dir: Base directory for stacks
        notify_callback: Optional callback function for notifications
    
    Returns:
        Queue ID
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert into queue database
        cursor.execute("""
            INSERT INTO backup_queue 
            (stack_name, scheduled_time, queued_at, status)
            VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'queued')
        """, (stack_name,))
        
        queue_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Add to queue
        backup_queue.put({
            'queue_id': queue_id,
            'stack_name': stack_name,
            'stacks_dir': stacks_dir,
            'notify_callback': notify_callback
        })
        
        logger.info(f"Queued backup for {stack_name} (queue ID: {queue_id})")
        
        return queue_id
        
    except Exception as e:
        logger.error(f"Error queuing backup for {stack_name}: {e}")
        return -1


# ============================================================================
# RESTORE FUNCTIONS
# ============================================================================

def list_available_backups(stack_name: str) -> List[Dict]:
    """List all available backups for a stack"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM backup_history 
            WHERE stack_name = ? AND status = 'success'
            ORDER BY backup_date DESC
        """, (stack_name,))
        
        backups = []
        for row in cursor.fetchall():
            backups.append(dict(row))
        
        conn.close()
        return backups
        
    except Exception as e:
        logger.error(f"Error listing backups for {stack_name}: {e}")
        return []


def restore_from_backup(stack_name: str, backup_id: int, stacks_dir: str = '/stacks') -> Dict:
    """
    Restore a stack from backup
    
    Args:
        stack_name: Name of the stack
        backup_id: ID of the backup to restore from
        stacks_dir: Base directory for stacks
    
    Returns:
        Dict with success status and details
    """
    try:
        logger.info(f"Starting restore for {stack_name} from backup ID {backup_id}")
        
        # Get backup info
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM backup_history 
            WHERE id = ? AND stack_name = ?
        """, (backup_id, stack_name))
        
        backup = cursor.fetchone()
        conn.close()
        
        if not backup:
            return {
                'success': False,
                'error': 'Backup not found'
            }
        
        if not os.path.exists(backup['backup_file_path']):
            return {
                'success': False,
                'error': 'Backup file not found on disk'
            }
        
        # Stop containers
        logger.info(f"Stopping containers for {stack_name}")
        stop_stack_containers(stack_name)
        time.sleep(2)
        
        # Extract backup to temp directory
        temp_restore_dir = os.path.join(BACKUP_TEMP_DIR, f"restore_{stack_name}_{int(time.time())}")
        os.makedirs(temp_restore_dir, exist_ok=True)
        
        with tarfile.open(backup['backup_file_path'], 'r:gz') as tar:
            tar.extractall(temp_restore_dir)
        
        # Find extracted stack directory
        extracted_stack_dir = os.path.join(temp_restore_dir, stack_name)
        if not os.path.exists(extracted_stack_dir):
            # Try first subdirectory
            subdirs = [d for d in os.listdir(temp_restore_dir) if os.path.isdir(os.path.join(temp_restore_dir, d))]
            if subdirs:
                extracted_stack_dir = os.path.join(temp_restore_dir, subdirs[0])
        
        # Restore compose files
        stack_dir = os.path.join(stacks_dir, stack_name)
        os.makedirs(stack_dir, exist_ok=True)
        
        for filename in ['docker-compose.yml', '.env', 'stack.env']:
            src = os.path.join(extracted_stack_dir, filename)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(stack_dir, filename))
                logger.info(f"Restored: {filename}")
        
        # Restore volumes
        volumes_dir = os.path.join(extracted_stack_dir, 'volumes')
        if os.path.exists(volumes_dir):
            # Get volume mappings from backup config
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT host_path, container_path 
                FROM backup_volume_selections 
                WHERE stack_name = ?
            """, (stack_name,))
            
            for row in cursor.fetchall():
                container_path = row['container_path'].lstrip('/')
                host_path = row['host_path']
                
                src_volume = os.path.join(volumes_dir, container_path)
                if os.path.exists(src_volume):
                    # Backup existing data
                    if os.path.exists(host_path):
                        backup_path = f"{host_path}.backup.{int(time.time())}"
                        shutil.move(host_path, backup_path)
                        logger.info(f"Backed up existing data to: {backup_path}")
                    
                    # Restore from backup
                    os.makedirs(os.path.dirname(host_path), exist_ok=True)
                    if os.path.isdir(src_volume):
                        shutil.copytree(src_volume, host_path, symlinks=True)
                    else:
                        shutil.copy2(src_volume, host_path)
                    
                    logger.info(f"Restored volume: {container_path} → {host_path}")
            
            conn.close()
        
        # Clean up temp directory
        shutil.rmtree(temp_restore_dir)
        
        # Restart containers
        logger.info(f"Restarting containers for {stack_name}")
        start_stack_containers(stack_name)
        time.sleep(2)
        verify_stack_health(stack_name)
        
        logger.info(f"✓ Restore completed for {stack_name}")
        
        return {
            'success': True,
            'message': f'Successfully restored {stack_name} from backup'
        }
        
    except Exception as e:
        logger.error(f"Error restoring {stack_name}: {e}")
        
        # Try to restart containers anyway
        try:
            start_stack_containers(stack_name)
        except:
            pass
        
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def parse_compose_volumes(stack_name: str, stacks_dir: str = '/stacks') -> List[Dict]:
    """
    Parse docker-compose.yml to extract volume mounts
    
    Returns:
        List of dicts with host_path, container_path, estimated_size_mb
    """
    try:
        compose_file = os.path.join(stacks_dir, stack_name, 'docker-compose.yml')
        if not os.path.exists(compose_file):
            return []
        
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        volumes = []
        services = compose_data.get('services', {})
        
        for service_name, service_config in services.items():
            service_volumes = service_config.get('volumes', [])
            
            for volume_spec in service_volumes:
                if isinstance(volume_spec, str):
                    # Parse "host:container" format
                    parts = volume_spec.split(':')
                    if len(parts) >= 2:
                        host_path = parts[0]
                        container_path = parts[1]
                        
                        # Skip named volumes (not absolute paths)
                        if not host_path.startswith('/'):
                            continue
                        
                        # Estimate size if path exists
                        size_mb = None
                        if os.path.exists(host_path):
                            try:
                                result = subprocess.run(['du', '-sm', host_path], 
                                                      capture_output=True, text=True, timeout=30)
                                if result.returncode == 0:
                                    size_mb = float(result.stdout.split()[0])
                            except:
                                pass
                        
                        volumes.append({
                            'host_path': host_path,
                            'container_path': container_path,
                            'estimated_size_mb': size_mb,
                            'backup_enabled': '/AppData/' in host_path or '/config' in container_path
                        })
        
        return volumes
        
    except Exception as e:
        logger.error(f"Error parsing volumes for {stack_name}: {e}")
        return []


def cleanup_old_queue_entries(days: int = 7):
    """Clean up old completed/failed queue entries"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM backup_queue 
            WHERE status IN ('completed', 'failed') 
            AND completed_at < datetime('now', '-' || ? || ' days')
        """, (days,))
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old queue entries")
        
        return deleted
        
    except Exception as e:
        logger.error(f"Error cleaning up queue entries: {e}")
        return 0




# ============================================================================
# BACKUP IMPORT/SCAN FUNCTIONS (for migration between DockUp instances)
# ============================================================================

def scan_backup_directory() -> List[Dict]:
    """
    Scan backup destination for existing backup files
    
    Returns list of discovered backups with metadata
    """
    try:
        is_available, mount_point, _ = check_backup_destination_available()
        if not is_available:
            logger.error("Backup destination not available for scanning")
            return []
        
        discovered_backups = []
        
        # Scan directory for .tar.gz files matching pattern: stackname_YYYY-MM-DD-HHMMSS.tar.gz
        backup_pattern = re.compile(r'^(.+?)_(\d{4}-\d{2}-\d{2}-\d{6})\.tar\.gz$')
        
        for filename in os.listdir(mount_point):
            if not filename.endswith('.tar.gz'):
                continue
            
            match = backup_pattern.match(filename)
            if not match:
                continue
            
            stack_name = match.group(1)
            timestamp_str = match.group(2)
            
            file_path = os.path.join(mount_point, filename)
            file_size = os.path.getsize(file_path)
            file_mtime = os.path.getmtime(file_path)
            
            discovered_backups.append({
                'stack_name': stack_name,
                'filename': filename,
                'file_path': file_path,
                'size_mb': file_size / (1024 * 1024),
                'timestamp': timestamp_str,
                'discovered_at': datetime.fromtimestamp(file_mtime).isoformat()
            })
        
        logger.info(f"Discovered {len(discovered_backups)} backup files")
        return discovered_backups
        
    except Exception as e:
        logger.error(f"Error scanning backup directory: {e}")
        return []


def import_discovered_backups() -> Dict:
    """
    Import discovered backups into database
    
    Returns dict with import results
    """
    try:
        discovered = scan_backup_directory()
        
        if not discovered:
            return {
                'success': True,
                'imported': 0,
                'skipped': 0,
                'message': 'No backups found to import'
            }
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        imported = 0
        skipped = 0
        
        for backup in discovered:
            # Check if already in database
            cursor.execute("""
                SELECT id FROM backup_history 
                WHERE stack_name = ? AND backup_file_path = ?
            """, (backup['stack_name'], backup['file_path']))
            
            if cursor.fetchone():
                skipped += 1
                continue
            
            # Parse timestamp for backup_date
            try:
                # Format: YYYY-MM-DD-HHMMSS
                ts = backup['timestamp']
                backup_date = f"{ts[0:4]}-{ts[5:7]}-{ts[8:10]} {ts[11:13]}:{ts[13:15]}:{ts[15:17]}"
            except:
                backup_date = backup['discovered_at']
            
            # Import into history
            cursor.execute("""
                INSERT INTO backup_history 
                (stack_name, backup_date, backup_file_path, backup_size_mb, 
                 duration_seconds, status, error_message, volumes_backed_up)
                VALUES (?, ?, ?, ?, 0, 'imported', 'Imported from existing backup directory', 'Unknown')
            """, (
                backup['stack_name'],
                backup_date,
                backup['file_path'],
                backup['size_mb']
            ))
            
            imported += 1
        
        conn.commit()
        conn.close()
        
        logger.info(f"Imported {imported} backups, skipped {skipped} duplicates")
        
        return {
            'success': True,
            'imported': imported,
            'skipped': skipped,
            'total_discovered': len(discovered),
            'message': f'Imported {imported} backups'
        }
        
    except Exception as e:
        logger.error(f"Error importing backups: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def verify_backup_integrity(backup_path: str) -> bool:
    """
    Verify a backup archive is valid and not corrupted
    
    Args:
        backup_path: Path to backup file
    
    Returns:
        True if backup is valid
    """
    try:
        if not os.path.exists(backup_path):
            return False
        
        # Try to open and list contents
        with tarfile.open(backup_path, 'r:gz') as tar:
            members = tar.getmembers()
            
            # Verify has docker-compose.yml
            has_compose = any('docker-compose.yml' in m.name for m in members)
            
            if not has_compose:
                logger.warning(f"Backup {backup_path} missing docker-compose.yml")
                return False
            
            logger.info(f"Backup {backup_path} verified: {len(members)} files")
            return True
            
    except Exception as e:
        logger.error(f"Backup verification failed for {backup_path}: {e}")
        return False




# ============================================================================
# INITIALIZATION
# ============================================================================

def initialize_backup_system(docker_client_instance):
    """
    Initialize the backup system
    
    Args:
        docker_client_instance: Docker client instance from main app
    """
    global docker_client
    
    docker_client = docker_client_instance
    
    # Initialize database
    init_backup_database()
    
    # Start backup worker thread
    start_backup_worker()
    
    # Auto-mount SMB if configured
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM global_backup_config WHERE id = 1")
        config = cursor.fetchone()
        conn.close()
        
        if config and config['type'] == 'smb' and config['auto_mount']:
            logger.info("Auto-mounting SMB share...")
            mount_smb_share(
                config['smb_host'],
                config['smb_share'],
                config['smb_username'],
                config['smb_password'],
                config['smb_mount_path'] or ''
            )
    except Exception as e:
        logger.error(f"Error auto-mounting SMB: {e}")
    
    logger.info("✓ Backup system initialized")
