import docker
import requests
import os
import subprocess
import json
import logging
import time
import re
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Setup logging for observability
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dockup.registry")

# Lock operation types (normalized enum)
LOCK_SYNC = "SYNC"
LOCK_GC = "GC"
LOCK_DELETE = "DELETE"

def _normalize_image_name(image_name):
    """
    Normalize image name to (repository, tag) tuple.
    
    Args:
        image_name: Image name (e.g., "linuxserver/plex:latest" or "nginx")
    
    Returns:
        tuple: (repository, tag)
    
    Examples:
        "linuxserver/plex:latest" -> ("linuxserver/plex", "latest")
        "nginx" -> ("nginx", "latest")
        "ghcr.io/user/app:v1.0" -> ("ghcr.io/user/app", "v1.0")
    
    Note:
        Currently does NOT handle digest pinning (e.g., "nginx@sha256:abc...").
        If digest pinning is added in the future, this should detect "@sha256:"
        and bypass tag logic. For now, digests are treated as tags (acceptable
        since UI doesn't expose digest pinning and Skopeo uses tags).
    """
    # TODO: Add digest support when digest pinning is implemented
    # if "@sha256:" in image_name:
    #     return parse_digest_reference(image_name)
    
    if ":" in image_name:
        # Split on last colon to handle registry URLs with ports
        repo, tag = image_name.rsplit(":", 1)
    else:
        repo = image_name
        tag = "latest"
    
    return (repo, tag)

class RegistryManager:
    def __init__(self, config_path="/DATA/AppData/dockup/config.json"):
        self.client = docker.from_env()
        self.config_path = config_path
        
        # Initial config load
        config = self._get_config()
        registry_config = config.get("registry", {})
        
        # Configurable settings with defaults
        self.registry_host = registry_config.get("host", "127.0.0.1")
        self.registry_port = registry_config.get("port", 5500)
        self.storage_path = registry_config.get("storage_path", "/DATA/AppData/Registry")
        self.registry_url = f"http://{self.registry_host}:{self.registry_port}/v2"
        self.lock_file = "/tmp/dockup_registry_write.lock"

        # Robust API Session with Retries
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
        self.session.mount('http://', HTTPAdapter(max_retries=retries))

    def __del__(self):
        """Cleanup session to prevent memory leak"""
        if hasattr(self, 'session'):
            try:
                self.session.close()
            except:
                pass

    def _get_config(self):
        """Internal config loader with safety defaults."""
        default_config = {
            "registry": {
                "enabled": False,
                "mode": "hybrid",  # "mirror", "proxy", or "hybrid"
                "watched_images": [], 
                "host": "127.0.0.1", 
                "port": 5500,
                "storage_path": "/DATA/AppData/Registry",
                "proxy_upstream": "https://registry-1.docker.io",  # Docker Hub
                "auto_gc_after_operations": True,
                "gc_schedule_cron": "0 4 * * 0",  # 4 AM Sundays
                "update_check_cron": "0 3 * * *"  # 3 AM daily
            }
        }
        
        if not os.path.exists(self.config_path):
            return default_config
        
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read config: {e}")
            return default_config

    def get_detailed_status(self):
        """
        Reports Registry Health vs Mutation State.
        Statuses: running, down, api_error, error.
        States: idle, busy.
        """
        status_report = {
            "status": "unknown",
            "api_ready": False,
            "write_state": "idle",
            "message": None
        }

        if os.path.exists(self.lock_file):
            status_report["write_state"] = "busy"

        try:
            container = self.client.containers.get("dockup-registry")
            status_report["status"] = container.status
            
            if container.status == "running":
                # V2 API check (allows 200 or 401)
                r = self.session.get(self.registry_url, timeout=5)
                if r.status_code in [200, 401]:
                    status_report["api_ready"] = True
                else:
                    status_report["status"] = "api_error"
                    status_report["message"] = f"HTTP {r.status_code}"
        except docker.errors.NotFound:
            status_report["status"] = "down"
        except Exception as e:
            status_report["status"] = "error"
            status_report["message"] = str(e)

        return status_report

    def _acquire_lock(self, operation_name):
        """Acquires write lock with 15-minute staleness override."""
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, 'r') as f:
                    content = f.read().split(':')
                    owner, lock_time = content[0], float(content[1])
                
                if (time.time() - lock_time) > 900:
                    logger.warning(f"Overriding stale lock from owner: {owner}")
                    self._release_lock()
                else:
                    logger.info(f"Lock requested by {operation_name} denied. Active: {owner}")
                    return False
            except Exception:
                self._release_lock()

        with open(self.lock_file, 'w') as f:
            f.write(f"{operation_name}:{time.time()}")
        return True

    def _release_lock(self):
        if os.path.exists(self.lock_file):
            os.remove(self.lock_file)

    def _smart_tag_sort(self, tags):
        """
        Sort tags intelligently:
        - Semantic versions (v1.2.3) sorted numerically
        - Date-based tags (2024-01-15) sorted chronologically
        - Other tags sorted lexicographically
        """
        def parse_tag(tag):
            # Try semantic version (v1.2.3 or 1.2.3)
            sem_match = re.match(r'^v?(\d+)\.(\d+)\.(\d+)', tag)
            if sem_match:
                return (0, int(sem_match.group(1)), int(sem_match.group(2)), int(sem_match.group(3)))
            
            # Try date format (2024-01-15, 20240115)
            date_match = re.match(r'^(\d{4})[-]?(\d{2})[-]?(\d{2})', tag)
            if date_match:
                return (1, int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            
            # Fallback to lexicographical
            return (2, tag)
        
        return sorted(tags, key=parse_tag, reverse=True)

    def list_images(self):
        """Lists images with 'Estimated Size' logic for the UI."""
        health = self.get_detailed_status()
        if not health["api_ready"]:
            return {"error": f"Registry state: {health['status']}", "images": []}

        try:
            r = self.session.get(f"{self.registry_url}/_catalog", timeout=10)
            r.raise_for_status()
            repos = r.json().get("repositories", [])
            
            results = []
            for name in repos:
                t_resp = self.session.get(f"{self.registry_url}/{name}/tags/list", timeout=5)
                if t_resp.status_code != 200: 
                    continue
                
                tags = t_resp.json().get("tags") or []
                for tag in tags:
                    headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
                    m_resp = self.session.get(f"{self.registry_url}/{name}/manifests/{tag}", headers=headers, timeout=5)
                    
                    if m_resp.status_code == 200:
                        # Note: Registry V2 API limitation - shared layers counted multiple times
                        # This is an estimate, not exact disk usage
                        size = sum(l.get("size", 0) for l in m_resp.json().get("layers", []))
                        results.append({
                            "name": name, 
                            "tag": tag, 
                            "size": size,
                            "size_mb": round(size / (1024 * 1024), 2),
                            "digest": m_resp.headers.get("Docker-Content-Digest")
                        })
            return {"error": None, "images": results}
        except Exception as e:
            logger.error(f"Error listing images: {e}")
            return {"error": str(e), "images": []}

    def run_gc(self, skip_lock=False):
        """
        Executes Garbage Collection to reclaim disk space.
        Returns accurate reclaimed space by comparing before/after disk usage.
        """
        if not skip_lock:
            logger.info("Starting garbage collection")
            if not self._acquire_lock(LOCK_GC):
                return {"status": "busy", "message": "Mutation lock active"}
        
        try:
            container = self.client.containers.get("dockup-registry")
            
            # Get disk usage BEFORE GC
            size_before = self.get_total_size()
            before_bytes = size_before.get("size_bytes", 0)
            
            # Run garbage collection
            result = container.exec_run("registry garbage-collect /etc/docker/registry/config.yml", timeout=600)
            
            # Log full output for debugging
            output = result.output.decode()
            logger.debug(f"GC Output: {output}")
            
            # Get disk usage AFTER GC
            size_after = self.get_total_size()
            after_bytes = size_after.get("size_bytes", 0)
            
            # Calculate actual reclaimed space
            reclaimed_bytes = before_bytes - after_bytes
            
            return {
                "status": "success", 
                "output": output,
                "reclaimed_bytes": reclaimed_bytes,
                "reclaimed_mb": round(reclaimed_bytes / (1024 * 1024), 2) if reclaimed_bytes > 0 else 0,
                "before_mb": size_before.get("size_mb", 0),
                "after_mb": size_after.get("size_mb", 0)
            }
        except Exception as e:
            logger.error(f"GC failed: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if not skip_lock:
                self._release_lock()

    def delete_tag(self, name, tag, skip_lock=False, auto_gc=None):
        """Standard delete-by-digest."""
        if not skip_lock:
            logger.info(f"Deleting tag: {name}:{tag}")
            if not self._acquire_lock(LOCK_DELETE):
                return {"status": "busy", "message": "Mutation lock active"}
        
        # Check if auto-GC should run (from config or parameter)
        if auto_gc is None:
            config = self._get_config()
            auto_gc = config.get("registry", {}).get("auto_gc_after_operations", True)
        
        try:
            headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
            head = self.session.head(f"{self.registry_url}/{name}/manifests/{tag}", headers=headers, timeout=5)
            digest = head.headers.get("Docker-Content-Digest")
            
            if not digest: 
                return {"status": "error", "message": "Tag not found"}
            
            r = self.session.delete(f"{self.registry_url}/{name}/manifests/{digest}", timeout=5)
            
            result = {}
            if r.status_code == 202:
                result = {"status": "success", "message": f"Deleted {name}:{tag}"}
                
                # Auto-GC after delete
                if auto_gc and not skip_lock:
                    logger.info("Running auto-GC after delete...")
                    gc_result = self.run_gc(skip_lock=True)
                    result["gc"] = gc_result
            else:
                result = {"status": "error", "message": f"Delete failed: HTTP {r.status_code}"}
            
            return result
        except Exception as e:
            logger.error(f"Delete failed for {name}:{tag}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if not skip_lock: 
                self._release_lock()

    def delete_image(self, name, auto_gc=None):
        """
        Delete all tags for an image (entire image).
        ⚠️ DESTRUCTIVE - Use with UI confirmation only
        """
        logger.warning(f"Deleting entire image: {name} (all tags)")
        if not self._acquire_lock(LOCK_DELETE):
            return {"status": "busy", "message": "Mutation lock active"}
        
        # Check if auto-GC should run (from config or parameter)
        if auto_gc is None:
            config = self._get_config()
            auto_gc = config.get("registry", {}).get("auto_gc_after_operations", True)
        
        try:
            # Get all tags for this image
            t_resp = self.session.get(f"{self.registry_url}/{name}/tags/list", timeout=5)
            if t_resp.status_code != 200:
                return {"status": "error", "message": "Image not found"}
            
            tags = t_resp.json().get("tags") or []
            deleted_count = 0
            
            for tag in tags:
                result = self.delete_tag(name, tag, skip_lock=True, auto_gc=False)
                if result["status"] == "success":
                    deleted_count += 1
            
            result = {
                "status": "success", 
                "message": f"Deleted {deleted_count} tag(s) for {name}"
            }
            
            # Auto-GC after delete
            if auto_gc:
                logger.info("Running auto-GC after image deletion...")
                gc_result = self.run_gc(skip_lock=True)
                result["gc"] = gc_result
            
            return result
        except Exception as e:
            logger.error(f"Delete image failed for {name}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self._release_lock()

    def apply_retention(self, name, policy):
        """
        N-version retention with smart tag sorting.
        Policies: latest (keep 1), keep_3, keep_5, keep_all
        """
        if policy == "keep_all": 
            return {"status": "skipped", "message": "Retention policy: keep all"}
        
        limit = 3 if policy == "keep_3" else 5 if policy == "keep_5" else 1
        
        try:
            t_resp = self.session.get(f"{self.registry_url}/{name}/tags/list", timeout=5)
            if t_resp.status_code != 200:
                return {"status": "error", "message": "Image not found"}
            
            tags = t_resp.json().get("tags") or []
            if len(tags) <= limit: 
                return {"status": "skipped", "message": f"Only {len(tags)} tag(s), within limit"}

            # Smart sort - semantic versions, dates, then lexicographical
            sorted_tags = self._smart_tag_sort(tags)
            tags_to_delete = sorted_tags[limit:]
            
            deleted_count = 0
            for tag in tags_to_delete:
                result = self.delete_tag(name, tag, skip_lock=True)
                if result["status"] == "success":
                    deleted_count += 1
            
            return {
                "status": "success",
                "message": f"Deleted {deleted_count} old tag(s), kept {limit} newest"
            }
        except Exception as e:
            logger.error(f"Retention failed for {name}: {e}")
            return {"status": "error", "message": str(e)}

    def pull_image(self, image_name, auto_gc=None):
        """
        Pull image from Docker Hub to registry using Skopeo.
        Format: linuxserver/plex:latest or ghcr.io/owner/repo:tag
        """
        logger.info(f"Manual pull requested: {image_name}")
        if not self._acquire_lock(LOCK_SYNC):
            return {"status": "busy", "message": "Mutation lock active"}
        
        # Check if auto-GC should run (from config or parameter)
        if auto_gc is None:
            config = self._get_config()
            auto_gc = config.get("registry", {}).get("auto_gc_after_operations", True)
        
        try:
            remote = f"docker://{image_name}"
            local = f"docker://{self.registry_host}:{self.registry_port}/{image_name}"
            
            logger.info(f"Pulling {image_name} to registry...")
            
            # Increased timeout for large images (15 minutes)
            result = subprocess.run(
                ["skopeo", "copy", "--dest-tls-verify=false", remote, local],
                timeout=900,
                capture_output=True,
                text=True
            )
            
            response = {}
            if result.returncode == 0:
                response = {
                    "status": "success",
                    "message": f"Successfully pulled {image_name}"
                }
                
                # Auto-GC after pull
                if auto_gc:
                    logger.info("Running auto-GC after pull...")
                    gc_result = self.run_gc(skip_lock=True)
                    response["gc"] = gc_result
            else:
                response = {
                    "status": "error",
                    "message": f"Pull failed: {result.stderr}"
                }
            
            return response
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Pull timeout (15 min) - image too large or slow connection"}
        except Exception as e:
            logger.error(f"Pull failed for {image_name}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self._release_lock()

    def auto_pull_sync(self):
        """
        Automated maintenance: enabled check -> pull -> retention -> GC.
        Called by scheduler on configured frequency.
        """
        # Cache config and check enabled guard
        config = self._get_config()
        if not config.get("registry", {}).get("enabled", False):
            logger.debug("Auto-pull skipped: Registry disabled")
            return {"status": "skipped", "message": "Registry disabled"}

        logger.info("Starting auto-pull sync")
        if not self._acquire_lock(LOCK_SYNC):
            logger.info("Auto-pull skipped: Registry busy")
            return {"status": "busy", "message": "Registry locked"}

        updates_performed = False
        results = []
        
        try:
            # Iterate through watched images
            for item in config.get("registry", {}).get("watched_images", []):
                img = item["image"]
                remote = f"docker://{img}"
                local = f"docker://{self.registry_host}:{self.registry_port}/{img}"

                try:
                    # Check remote digest
                    remote_digest = subprocess.check_output(
                        ["skopeo", "inspect", "--format", "{{.Digest}}", remote], 
                        timeout=30,
                        stderr=subprocess.DEVNULL
                    ).decode().strip()
                    
                    # Check local digest
                    try:
                        local_digest = subprocess.check_output(
                            ["skopeo", "inspect", "--format", "{{.Digest}}", "--dest-tls-verify=false", local], 
                            timeout=30,
                            stderr=subprocess.DEVNULL
                        ).decode().strip()
                    except:
                        local_digest = None

                    # Update if digests differ
                    if remote_digest != local_digest:
                        logger.info(f"Mirroring update for {img}...")
                        subprocess.check_call(
                            ["skopeo", "copy", "--dest-tls-verify=false", remote, local], 
                            timeout=900  # 15 min for large images
                        )
                        
                        # Apply retention policy
                        retention_result = self.apply_retention(img, item.get("retention", "latest"))
                        
                        updates_performed = True
                        results.append({
                            "image": img,
                            "status": "updated",
                            "retention": retention_result
                        })
                    else:
                        results.append({
                            "image": img,
                            "status": "up_to_date"
                        })
                        
                except Exception as e:
                    logger.error(f"Sync failed for {img}: {e}")
                    results.append({
                        "image": img,
                        "status": "error",
                        "message": str(e)
                    })
        finally:
            # Perform GC if space-reclaiming actions occurred
            # Important: Run GC BEFORE releasing lock to prevent race condition
            if updates_performed:
                logger.info("Running GC after updates...")
                gc_result = self.run_gc(skip_lock=True)
                results.append({"gc": gc_result})
            
            self._release_lock()
        
        return {
            "status": "success",
            "updates_performed": updates_performed,
            "results": results
        }

    def get_total_size(self):
        """Calculate total registry disk usage."""
        try:
            if not os.path.exists(self.storage_path):
                return {"size_bytes": 0, "size_gb": 0}
            
            result = subprocess.run(
                ["du", "-sb", self.storage_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                size_bytes = int(result.stdout.split()[0])
                return {
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / (1024 * 1024), 2),
                    "size_gb": round(size_bytes / (1024 * 1024 * 1024), 2)
                }
            else:
                return {"error": "Failed to calculate size"}
        except Exception as e:
            logger.error(f"Size calculation failed: {e}")
            return {"error": str(e)}

    def list_images_with_status(self):
        """
        List images with watch status and source.
        Enriches basic image list with:
        - is_watched: boolean
        - source: "proxy", "manual", or "watched"
        - retention_policy: if watched
        """
        images_result = self.list_images()
        if images_result.get("error"):
            return images_result
        
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        
        # Normalize watched images to (repository, tag) tuples using helper
        watched_set = set()
        watched_policies = {}
        for item in watched_images:
            repo, tag = _normalize_image_name(item["image"])
            watched_set.add((repo, tag))
            watched_policies[(repo, tag)] = item.get("retention", "latest")
        
        enriched_images = []
        for img in images_result["images"]:
            # Compare repository and tag separately (not concatenated strings)
            repo = img["name"]
            tag = img["tag"]
            is_watched = (repo, tag) in watched_set
            
            enriched = {
                **img,
                "is_watched": is_watched,
                "source": "watched" if is_watched else "proxy",
                "retention_policy": watched_policies.get((repo, tag)) if is_watched else None
            }
            enriched_images.append(enriched)
        
        return {"error": None, "images": enriched_images}

    def add_to_watch_list(self, image_name, retention="latest"):
        """
        Add an image to the watch list for auto-updates.
        
        Args:
            image_name: Image name (e.g., "linuxserver/plex:latest" or "nginx")
            retention: Retention policy ("latest", "keep_3", "keep_5", "keep_all")
        
        Returns:
            dict with status and message
        """
        # Normalize image name to ensure consistent format
        repo, tag = _normalize_image_name(image_name)
        normalized_name = f"{repo}:{tag}"
        
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        
        # Check if already watched (using normalized name)
        if any(item["image"] == normalized_name for item in watched_images):
            return {"status": "error", "message": f"{normalized_name} is already watched"}
        
        # Add to watch list
        watched_images.append({
            "image": normalized_name,
            "retention": retention,
            "source": "promoted"  # Indicates it was promoted from proxy cache
        })
        
        # Save config
        config["registry"]["watched_images"] = watched_images
        try:
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            logger.info(f"Added {normalized_name} to watch list with retention: {retention}")
            return {
                "status": "success",
                "message": f"Now watching {normalized_name}",
                "retention": retention
            }
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return {"status": "error", "message": f"Failed to save: {str(e)}"}

    def remove_from_watch_list(self, image_name):
        """
        Remove an image from the watch list.
        
        Args:
            image_name: Image name (e.g., "linuxserver/plex:latest" or "nginx")
        
        Returns:
            dict with status and message
        """
        # Normalize image name to ensure consistent matching
        repo, tag = _normalize_image_name(image_name)
        normalized_name = f"{repo}:{tag}"
        
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        
        # Find and remove (using normalized name)
        original_count = len(watched_images)
        watched_images = [item for item in watched_images if item["image"] != normalized_name]
        
        if len(watched_images) == original_count:
            return {"status": "error", "message": f"{normalized_name} is not in watch list"}
        
        # Save config
        config["registry"]["watched_images"] = watched_images
        try:
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            logger.info(f"Removed {normalized_name} from watch list")
            return {
                "status": "success",
                "message": f"No longer watching {normalized_name}"
            }
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return {"status": "error", "message": f"Failed to save: {str(e)}"}

    def update_watch_retention(self, image_name, retention):
        """
        Update retention policy for a watched image.
        
        Args:
            image_name: Image name (e.g., "linuxserver/plex:latest" or "nginx")
            retention: New retention policy ("latest", "keep_3", "keep_5", "keep_all")
        
        Returns:
            dict with status and message
        """
        # Normalize image name to ensure consistent matching
        repo, tag = _normalize_image_name(image_name)
        normalized_name = f"{repo}:{tag}"
        
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        
        # Find and update (using normalized name)
        found = False
        for item in watched_images:
            if item["image"] == normalized_name:
                item["retention"] = retention
                found = True
                break
        
        if not found:
            return {"status": "error", "message": f"{normalized_name} is not in watch list"}
        
        # Save config
        config["registry"]["watched_images"] = watched_images
        try:
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            logger.info(f"Updated retention for {normalized_name}: {retention}")
            return {
                "status": "success",
                "message": f"Updated retention to {retention}",
                "retention": retention
            }
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return {"status": "error", "message": f"Failed to save: {str(e)}"}

    def get_watched_images(self):
        """Get list of watched images with their settings."""
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        return {"watched_images": watched_images}

    def get_registry_mode(self):
        """Get current registry operating mode."""
        config = self._get_config()
        return {
            "mode": config.get("registry", {}).get("mode", "hybrid"),
            "proxy_upstream": config.get("registry", {}).get("proxy_upstream", "https://registry-1.docker.io")
        }

