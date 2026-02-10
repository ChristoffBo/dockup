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

class RegistryManager:
    def __init__(self, config_path="/DATA/AppData/dockup/config.json"):
        self.client = docker.from_env()
        self.config_path = config_path
        self.storage_path = "/DATA/Registry"
        self.lock_file = "/tmp/dockup_registry_write.lock"
        
        # Initial config load
        config = self._get_config()
        self.registry_host = config.get("registry", {}).get("host", "127.0.0.1")
        self.registry_port = config.get("registry", {}).get("port", 5500)
        self.registry_url = f"http://{self.registry_host}:{self.registry_port}/v2"

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
        if not os.path.exists(self.config_path):
            return {"registry": {"enabled": False, "watched_images": [], "host": "127.0.0.1", "port": 5500}}
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read config: {e}")
            return {"registry": {"enabled": False, "watched_images": []}}

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

    def run_gc(self):
        """Executes Garbage Collection to reclaim disk space."""
        if not self._acquire_lock(LOCK_GC):
            return {"status": "busy", "message": "Mutation lock active"}
        
        logger.info("Starting garbage collection")
        try:
            container = self.client.containers.get("dockup-registry")
            # Purge unreferenced blobs
            result = container.exec_run("registry garbage-collect /etc/docker/registry/config.yml", timeout=600)
            
            # Parse output to find reclaimed space
            output = result.output.decode()
            logger.debug(f"GC Output: {output}")
            
            # Try to extract reclaimed space from output
            reclaimed_bytes = 0
            for line in output.split('\n'):
                if 'deleted' in line.lower() or 'removed' in line.lower():
                    # Parse any byte counts in the line
                    numbers = re.findall(r'\d+', line)
                    if numbers:
                        reclaimed_bytes += int(numbers[0])
            
            return {
                "status": "success", 
                "output": output,
                "reclaimed_mb": round(reclaimed_bytes / (1024 * 1024), 2) if reclaimed_bytes > 0 else None,
                "reclaimed_estimate": True  # Flag for UI: GC output parsing is not authoritative
            }
        except Exception as e:
            logger.error(f"GC failed: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self._release_lock()

    def delete_tag(self, name, tag, skip_lock=False):
        """Standard delete-by-digest."""
        if not skip_lock:
            logger.info(f"Deleting tag: {name}:{tag}")
            if not self._acquire_lock(LOCK_DELETE):
                return {"status": "busy", "message": "Mutation lock active"}
        try:
            headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
            head = self.session.head(f"{self.registry_url}/{name}/manifests/{tag}", headers=headers, timeout=5)
            digest = head.headers.get("Docker-Content-Digest")
            
            if not digest: 
                return {"status": "error", "message": "Tag not found"}
            
            r = self.session.delete(f"{self.registry_url}/{name}/manifests/{digest}", timeout=5)
            
            if r.status_code == 202:
                return {"status": "success", "message": f"Deleted {name}:{tag}"}
            else:
                return {"status": "error", "message": f"Delete failed: HTTP {r.status_code}"}
        except Exception as e:
            logger.error(f"Delete failed for {name}:{tag}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if not skip_lock: 
                self._release_lock()

    def delete_image(self, name):
        """
        Delete all tags for an image (entire image).
        ⚠️ DESTRUCTIVE - Use with UI confirmation only
        """
        logger.warning(f"Deleting entire image: {name} (all tags)")
        if not self._acquire_lock(LOCK_DELETE):
            return {"status": "busy", "message": "Mutation lock active"}
        
        try:
            # Get all tags for this image
            t_resp = self.session.get(f"{self.registry_url}/{name}/tags/list", timeout=5)
            if t_resp.status_code != 200:
                return {"status": "error", "message": "Image not found"}
            
            tags = t_resp.json().get("tags") or []
            deleted_count = 0
            
            for tag in tags:
                result = self.delete_tag(name, tag, skip_lock=True)
                if result["status"] == "success":
                    deleted_count += 1
            
            return {
                "status": "success", 
                "message": f"Deleted {deleted_count} tag(s) for {name}"
            }
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

    def pull_image(self, image_name):
        """
        Pull image from Docker Hub to registry using Skopeo.
        Format: linuxserver/plex:latest or ghcr.io/owner/repo:tag
        """
        logger.info(f"Manual pull requested: {image_name}")
        if not self._acquire_lock(LOCK_SYNC):
            return {"status": "busy", "message": "Mutation lock active"}
        
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
            
            if result.returncode == 0:
                return {
                    "status": "success",
                    "message": f"Successfully pulled {image_name}"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Pull failed: {result.stderr}"
                }
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
            self._release_lock()
            
            # Perform GC if space-reclaiming actions occurred
            if updates_performed:
                logger.info("Running GC after updates...")
                gc_result = self.run_gc()
                results.append({"gc": gc_result})
        
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
