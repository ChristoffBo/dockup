import docker
import requests
import os
import subprocess
import json
import logging
import time
import re
import threading
import shutil
import math
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

# FIX: Universal headers to force Schema v2 or OCI (required for Logical Size fields)
MANIFEST_HEADERS = {
    "Accept": (
        "application/vnd.docker.distribution.manifest.v2+json, "
        "application/vnd.docker.distribution.manifest.list.v2+json, "
        "application/vnd.oci.image.manifest.v1+json, "
        "application/vnd.oci.image.index.v1+json"
    )
}

def _normalize_image_name(image_name):
    """
    Normalize image name to (repository, tag) tuple.
    """
    if ":" in image_name:
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
        
        # Cache for list_images to prevent repeated heavy processing
        self._images_cache = None
        self._images_cache_time = 0
        self._images_cache_ttl = 10  # Reduced from 60 to avoid stale data

        # Pull progress tracking
        self._pull_progress = {}  # {image_name: {status, percent, current_mb, total_mb, layer, ...}}
        self._pull_progress_lock = threading.Lock()
        
        # Thread-safe cache access
        self._cache_lock = threading.Lock()
        
        logger.info(f"[Registry] Initialized - URL: {self.registry_url}")

        # Robust API Session with Retries
        self.session = requests.Session()
        retries = Retry(
            total=5, 
            backoff_factor=1, 
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "DELETE"]
        )
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
                "mode": "hybrid",
                "watched_images": [], 
                "host": "127.0.0.1", 
                "port": 5500,
                "storage_path": "/DATA/AppData/Registry",
                "proxy_upstream": "https://registry-1.docker.io",
                "auto_gc_after_operations": True,
                "gc_schedule_cron": "0 4 * * 0",
                "update_check_cron": "0 3 * * *"
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

    def _get_blob_size(self, digest):
        """
        Helper to get exact size of a blob on disk from the v2 storage layout.
        Used for physical disk reporting, impact analysis, and orphan detection.
        """
        if not digest or not digest.startswith("sha256:"):
            return 0
        
        hash_val = digest.split(":")[1]
        path = os.path.join(
            self.storage_path, 
            "docker", "registry", "v2", 
            "blobs", "sha256", 
            hash_val[:2], 
            hash_val, 
            "data"
        )
        
        try:
            if os.path.exists(path):
                return os.path.getsize(path)
        except Exception:
            pass
            
        return 0

    def _format_size(self, size_bytes):
        """Standardizes size formatting to ensure UI consistency."""
        if size_bytes <= 0:
            return "0 B"
        size_name = ("B", "KB", "MB", "GB", "TB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"

    def _calculate_image_size(self, repo, manifest_data):
        """
        Calculates Logical Size using manifest-declared sizes.
        Uses MANIFEST_HEADERS to ensure the registry returns Schema v2/OCI.
        """
        total_bytes = 0
        seen_digests = set()
        media_type = manifest_data.get("mediaType")
        
        def process_single_manifest(m_data):
            m_total = 0
            # Add Config size
            cfg = m_data.get("config", {})
            cfg_digest = cfg.get("digest")
            if cfg_digest and cfg_digest not in seen_digests:
                m_total += cfg.get("size", 0)
                seen_digests.add(cfg_digest)
            
            # Add Layers size
            layers = m_data.get("layers") or m_data.get("fsLayers") or []
            for layer in layers:
                l_digest = layer.get("digest") or layer.get("blobSum")
                if l_digest and l_digest not in seen_digests:
                    # In Schema v2/OCI, layer.get("size") exists.
                    m_total += layer.get("size", 0)
                    seen_digests.add(l_digest)
            return m_total

        # Multi-arch Manifest List or OCI Index
        if media_type in [
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.index.v1+json"
        ]:
            for m in manifest_data.get("manifests", []):
                sub_digest = m.get("digest")
                if not sub_digest: continue
                try:
                    r = self.session.get(
                        f"{self.registry_url}/{repo}/manifests/{sub_digest}", 
                        headers=MANIFEST_HEADERS, 
                        timeout=5
                    )
                    if r.status_code == 200:
                        total_bytes += process_single_manifest(r.json())
                except:
                    continue
        else:
            total_bytes += process_single_manifest(manifest_data)
            
        return total_bytes, seen_digests

    def _cleanup_empty_dirs(self):
        """Removes empty repository folders AND blob folders from the filesystem."""
        # Clean repositories
        repo_base = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories")
        if os.path.exists(repo_base):
            for root, dirs, files in os.walk(repo_base, topdown=False):
                for name in dirs:
                    full_path = os.path.join(root, name)
                    try:
                        if not os.listdir(full_path):
                            os.rmdir(full_path)
                            logger.debug(f"Removed empty directory: {full_path}")
                    except Exception as e:
                        logger.debug(f"Could not remove {full_path}: {e}")
        
        # Clean blobs - remove empty prefix directories
        blob_base = os.path.join(self.storage_path, "docker", "registry", "v2", "blobs", "sha256")
        if os.path.exists(blob_base):
            for root, dirs, files in os.walk(blob_base, topdown=False):
                for name in dirs:
                    full_path = os.path.join(root, name)
                    try:
                        if not os.listdir(full_path):
                            os.rmdir(full_path)
                            logger.debug(f"Removed empty blob directory: {full_path}")
                    except Exception as e:
                        logger.debug(f"Could not remove {full_path}: {e}")

    def _resolve_digest(self, repo, tag):
        """Resolve the correct digest for a tag using the Registry API."""
        r = self.session.get(
            f"{self.registry_url}/{repo}/manifests/{tag}", 
            headers=MANIFEST_HEADERS, 
            timeout=10
        )
        r.raise_for_status()
        return r.headers.get("Docker-Content-Digest")

    def get_detailed_status(self):
        """Reports Registry Health vs Mutation State."""
        status_report = {"status": "unknown", "api_ready": False, "write_state": "idle", "message": None}
        if os.path.exists(self.lock_file):
            status_report["write_state"] = "busy"
        try:
            container = self.client.containers.get("dockup-registry")
            status_report["status"] = container.status
            if container.status == "running":
                r = self.session.get(self.registry_url, timeout=5)
                if r.status_code in [200, 401]:
                    status_report["api_ready"] = True
                else:
                    status_report["status"] = "api_error"
                    status_report["message"] = f"HTTP {r.status_code}"
        except Exception as e:
            status_report["status"] = "error"
            status_report["message"] = str(e)
        return status_report

    def _acquire_lock(self, operation_name):
        """Acquires write lock with staleness override."""
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, 'r') as f:
                    content = f.read().split(':')
                    owner, lock_time = content[0], float(content[1])
                if (time.time() - lock_time) > 900:
                    self._release_lock()
                else:
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
        """Sort tags intelligently based on semver, date, or alpha."""
        def parse_tag(tag):
            sem_match = re.match(r'^v?(\d+)\.(\d+)\.(\d+)', tag)
            if sem_match:
                return (0, int(sem_match.group(1)), int(sem_match.group(2)), int(sem_match.group(3)))
            date_match = re.match(r'^(\d{4})[-]?(\d{2})[-]?(\d{2})', tag)
            if date_match:
                return (1, int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            return (2, tag)
        return sorted(tags, key=parse_tag, reverse=True)

    def list_images(self, force=False):
        """
        FULL DEEP SCAN: Combines Filesystem recursion + API Catalog + Logical Size Calculation.
        Uses MANIFEST_HEADERS to fix logical size reporting.
        force: If True, bypass cache completely
        """
        if not force:
            with self._cache_lock:
                current_time = time.time()
                if self._images_cache and (current_time - self._images_cache_time) < self._images_cache_ttl:
                    return self._images_cache
        
        health = self.get_detailed_status()
        if not health["api_ready"]:
            return {"error": f"Registry state: {health['status']}", "images": []}

        discovered = {} 
        repo_fs_path = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories")
        
        if os.path.exists(repo_fs_path):
            for root, dirs, files in os.walk(repo_fs_path):
                if "_manifests" in dirs:
                    rel_repo = os.path.relpath(root, repo_fs_path).replace(os.sep, '/')
                    if rel_repo not in discovered:
                        discovered[rel_repo] = set()
                    tags_path = os.path.join(root, "_manifests", "tags")
                    if os.path.exists(tags_path):
                        for tag_name in os.listdir(tags_path):
                            if os.path.isdir(os.path.join(tags_path, tag_name)):
                                discovered[rel_repo].add(tag_name)

        try:
            r = self.session.get(f"{self.registry_url}/_catalog", timeout=10)
            if r.status_code == 200:
                for repo in r.json().get("repositories", []):
                    if repo not in discovered:
                        discovered[repo] = set()
                    if not discovered[repo]:
                        try:
                            t_resp = self.session.get(f"{self.registry_url}/{repo}/tags/list", timeout=5)
                            if t_resp.status_code == 200:
                                discovered[repo].update(t_resp.json().get("tags") or [])
                        except Exception: pass
        except Exception: pass

        results = []
        all_referenced_digests = set()

        for repo_name, tags in discovered.items():
            sorted_tags = self._smart_tag_sort(list(tags))
            for tag in sorted_tags:
                try:
                    m_resp = self.session.get(
                        f"{self.registry_url}/{repo_name}/manifests/{tag}", 
                        headers=MANIFEST_HEADERS, 
                        timeout=5
                    )
                    
                    if m_resp.status_code == 200:
                        manifest_data = m_resp.json()
                        size_bytes, digests = self._calculate_image_size(repo_name, manifest_data)
                        
                        digest = m_resp.headers.get("Docker-Content-Digest")
                        all_referenced_digests.update(digests)
                        if digest: all_referenced_digests.add(digest)

                        results.append({
                            "name": repo_name,
                            "tag": tag,
                            "size": size_bytes,
                            "size_formatted": self._format_size(size_bytes),
                            "digest": digest
                        })
                except Exception: continue

        orphan_summary = {"count": 0, "size_bytes": 0}
        blob_path = os.path.join(self.storage_path, "docker", "registry", "v2", "blobs", "sha256")
        
        if os.path.exists(blob_path):
            for prefix in os.listdir(blob_path):
                prefix_path = os.path.join(blob_path, prefix)
                if not os.path.isdir(prefix_path): continue
                for digest_hash in os.listdir(prefix_path):
                    full_digest = f"sha256:{digest_hash}"
                    if full_digest not in all_referenced_digests:
                        try:
                            data_file = os.path.join(prefix_path, digest_hash, "data")
                            if os.path.exists(data_file):
                                b_size = os.path.getsize(data_file)
                                orphan_summary["count"] += 1
                                orphan_summary["size_bytes"] += b_size
                        except Exception: pass

        result = {"error": None, "images": results, "orphans": orphan_summary, "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        with self._cache_lock:
            self._images_cache = result
            self._images_cache_time = time.time()
        return result

    def get_manifest_raw(self, name, tag):
        """Returns the raw manifest JSON using MANIFEST_HEADERS."""
        try:
            r = self.session.get(f"{self.registry_url}/{name}/manifests/{tag}", headers=MANIFEST_HEADERS, timeout=10)
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def perform_smart_update(self, image_name):
        """Checks upstream for a new digest and pulls if different."""
        repo, tag = _normalize_image_name(image_name)
        local_digest = None
        try:
            local_digest = self._resolve_digest(repo, tag)
        except: pass
        
        inspect_cmd = ["skopeo", "inspect", f"docker://{image_name}"]
        inspect_res = subprocess.run(inspect_cmd, capture_output=True, text=True)
        if inspect_res.returncode != 0:
            return {"status": "error", "message": "Failed to inspect upstream image"}
        
        upstream_data = json.loads(inspect_res.stdout)
        remote_digest = upstream_data.get("Digest")

        if local_digest == remote_digest:
            return {"status": "up_to_date", "digest": local_digest}

        logger.info(f"Update required for {image_name}. New digest: {remote_digest}")
        pull_status = self.pull_image(image_name)
        
        if pull_status["status"] == "success" and local_digest:
            try:
                delete_url = f"{self.registry_url}/{repo}/manifests/{local_digest}"
                self.session.delete(delete_url, timeout=10)
            except: pass

        # Update last_updated timestamp
        self._update_image_timestamp(image_name)
        
        return {"status": "updated", "new_digest": remote_digest}

    def check_all_watched_updates(self):
        """Check all watched images for updates and send notifications."""
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        
        if not watched_images:
            logger.info("No watched images to check")
            return {"status": "success", "message": "No watched images", "updated": [], "up_to_date": []}
        
        logger.info(f"Checking {len(watched_images)} watched images for updates")
        
        updated_images = []
        up_to_date_images = []
        failed_images = []
        
        for item in watched_images:
            image_name = item["image"]
            try:
                # Check if image exists in registry before updating
                repo, tag = _normalize_image_name(image_name)
                try:
                    local_digest = self._resolve_digest(repo, tag)
                    if not local_digest:
                        # Image doesn't exist in registry - skip it
                        logger.info(f"⊘ Skipped: {image_name} (not in registry)")
                        continue
                except:
                    # Can't resolve digest - image probably deleted
                    logger.info(f"⊘ Skipped: {image_name} (deleted from registry)")
                    continue
                
                result = self.perform_smart_update(image_name)
                if result["status"] == "updated":
                    updated_images.append(image_name)
                    logger.info(f"✓ Updated: {image_name}")
                elif result["status"] == "up_to_date":
                    up_to_date_images.append(image_name)
                    logger.info(f"  No update: {image_name}")
                else:
                    failed_images.append({"image": image_name, "error": result.get("message", "Unknown error")})
                    logger.warning(f"✗ Failed: {image_name} - {result.get('message')}")
            except Exception as e:
                failed_images.append({"image": image_name, "error": str(e)})
                logger.error(f"✗ Exception checking {image_name}: {e}")
        
        # Send notifications
        notify_on_check = config.get("notify_on_registry_check", False)
        notify_on_update = config.get("notify_on_registry_update", True)
        
        if updated_images and notify_on_update:
            # Images were updated - send success notification
            try:
                from app import notify
                message = f"Updated {len(updated_images)} image(s):\n" + "\n".join(f"✓ {img}" for img in updated_images)
                if failed_images:
                    message += f"\n\nFailed: {len(failed_images)}"
                notify("Docker Registry - Updates Applied", message, "success")
            except Exception as e:
                logger.error(f"Failed to send update notification: {e}")
        elif not updated_images and notify_on_check:
            # No updates found - send info notification
            try:
                from app import notify
                notify("Docker Registry - No Updates Found", 
                       f"Checked {len(watched_images)} image(s) - All up to date", 
                       "info")
            except Exception as e:
                logger.error(f"Failed to send check notification: {e}")
        
        # Run GC if updates were performed
        if updated_images:
            logger.info("Running garbage collection after updates")
            try:
                self.run_gc(skip_lock=False)
            except:
                pass
        
        # Cleanup orphaned watched entries (images deleted but still in watch list)
        # This prevents re-downloading deleted images
        try:
            cleanup_result = self.cleanup_watched_orphans()
            if cleanup_result.get("removed"):
                logger.info(f"Cleaned up {len(cleanup_result['removed'])} orphaned watch entries")
        except:
            pass
        
        return {
            "status": "success",
            "updated": updated_images,
            "up_to_date": up_to_date_images,
            "failed": failed_images,
            "total_checked": len(watched_images)
        }

    def _update_image_timestamp(self, image_name):
        """Update last_updated timestamp for an image."""
        try:
            config = self._get_config()
            watched_images = config.get("registry", {}).get("watched_images", [])
            
            for item in watched_images:
                if item["image"] == image_name:
                    item["last_updated"] = datetime.now().isoformat()
                    break
            
            config["registry"]["watched_images"] = watched_images
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to update timestamp for {image_name}: {e}")

    def cleanup_watched_orphans(self):
        """Remove images from watched list that no longer exist in registry."""
        try:
            config = self._get_config()
            watched_images = config.get("registry", {}).get("watched_images", [])
            
            cleaned = []
            removed = []
            
            for item in watched_images:
                image_name = item["image"]
                repo, tag = _normalize_image_name(image_name)
                
                try:
                    # Check if image exists
                    local_digest = self._resolve_digest(repo, tag)
                    if local_digest:
                        # Image exists - keep it
                        cleaned.append(item)
                    else:
                        # Image doesn't exist - remove from watch list
                        removed.append(image_name)
                        logger.info(f"Removed orphan from watch list: {image_name}")
                except:
                    # Can't resolve - assume deleted
                    removed.append(image_name)
                    logger.info(f"Removed orphan from watch list: {image_name}")
            
            # Save cleaned list
            config["registry"]["watched_images"] = cleaned
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            return {
                "status": "success",
                "removed": removed,
                "remaining": len(cleaned)
            }
            
        except Exception as e:
            logger.error(f"Failed to cleanup watched orphans: {e}")
            return {"status": "error", "message": str(e)}

    def sync_watched_images(self):
        """Pull all watched images that don't exist in registry yet."""
        try:
            config = self._get_config()
            watched_images = config.get("registry", {}).get("watched_images", [])
            
            pulled = []
            already_present = []
            failed = []
            
            for item in watched_images:
                image_name = item["image"]
                repo, tag = _normalize_image_name(image_name)
                
                try:
                    # Check if image exists
                    local_digest = self._resolve_digest(repo, tag)
                    if local_digest:
                        # Image already exists
                        already_present.append(image_name)
                        logger.info(f"[Registry] {image_name} already in registry")
                    else:
                        # Image missing - pull it
                        logger.info(f"[Registry] Pulling missing watched image: {image_name}")
                        pull_result = self.pull_image(image_name, auto_gc=False)
                        if pull_result.get("status") == "success":
                            pulled.append(image_name)
                            logger.info(f"[Registry] Successfully pulled {image_name}")
                        else:
                            failed.append({
                                "image": image_name,
                                "error": pull_result.get("message", "Unknown error")
                            })
                            logger.error(f"[Registry] Failed to pull {image_name}: {pull_result.get('message')}")
                except Exception as e:
                    failed.append({
                        "image": image_name,
                        "error": str(e)
                    })
                    logger.error(f"[Registry] Error processing {image_name}: {e}")
            
            # Clear cache after pulling
            if pulled:
                with self._cache_lock:
                    self._images_cache = None
            
            return {
                "status": "success",
                "pulled": pulled,
                "already_present": already_present,
                "failed": failed,
                "total": len(watched_images)
            }
            
        except Exception as e:
            logger.error(f"[Registry] Sync watched images failed: {e}")
            return {"status": "error", "message": str(e)}


    def get_image_impact_analysis(self):
        """Analyzes layer sharing based on physical storage footprint."""
        img_list = self.list_images()
        if img_list.get("error"): return img_list

        digest_usage = {}
        image_to_digests = {}
        
        for img in img_list["images"]:
            img_id = f"{img['name']}:{img['tag']}"
            try:
                m_resp = self.session.get(f"{self.registry_url}/{img['name']}/manifests/{img['tag']}", headers=MANIFEST_HEADERS, timeout=5)
                if m_resp.status_code == 200:
                    _, digests = self._calculate_image_size(img['name'], m_resp.json())
                    image_to_digests[img_id] = digests
                    for d in digests:
                        if d not in digest_usage:
                            digest_usage[d] = {"count": 0, "size": self._get_blob_size(d)}
                        digest_usage[d]["count"] += 1
            except Exception: continue

        impact_results = []
        for img_id, digests in image_to_digests.items():
            unique_size = 0
            shared_size = 0
            for d in digests:
                if digest_usage[d]["count"] == 1: unique_size += digest_usage[d]["size"]
                else: shared_size += digest_usage[d]["size"]
            
            impact_results.append({
                "image": img_id,
                "unique_size": unique_size,
                "unique_formatted": self._format_size(unique_size),
                "shared_size": shared_size,
                "shared_formatted": self._format_size(shared_size),
                "total_size": unique_size + shared_size,
                "total_formatted": self._format_size(unique_size + shared_size)
            })
        return sorted(impact_results, key=lambda x: x["unique_size"], reverse=True)

    def run_gc(self, skip_lock=False):
        """Executes Registry Garbage Collection."""
        if not skip_lock and not self._acquire_lock(LOCK_GC): return {"status": "busy"}
        try:
            logger.info("[Registry] Starting garbage collection")
            container = self.client.containers.get("dockup-registry")
            size_before = self.get_total_size().get("size_bytes", 0)
            logger.info(f"[Registry] Size before GC: {self._format_size(size_before)}")
            
            # Run GC with --delete-untagged to actually remove blobs
            result = container.exec_run("registry garbage-collect --delete-untagged /etc/docker/registry/config.yml")
            logger.info(f"[Registry] GC command exit code: {result.exit_code}")
            if result.exit_code != 0:
                logger.warning(f"[Registry] GC stderr: {result.output.decode()}")
            
            # Clean up empty directories after GC
            self._cleanup_empty_dirs()
            
            size_after = self.get_total_size().get("size_bytes", 0)
            reclaimed = size_before - size_after
            logger.info(f"[Registry] Size after GC: {self._format_size(size_after)}")
            logger.info(f"[Registry] Reclaimed: {self._format_size(reclaimed)}")
            
            with self._cache_lock: self._images_cache = None
            
            return {
                "status": "success", 
                "output": result.output.decode(), 
                "reclaimed_bytes": reclaimed, 
                "reclaimed_formatted": self._format_size(reclaimed),
                "size_before": size_before,
                "size_after": size_after
            }
        except Exception as e: 
            logger.error(f"[Registry] GC error: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if not skip_lock: self._release_lock()

    def delete_tag(self, name, tag, skip_lock=False, auto_gc=None):
        """Deletes a tag and cleans up physical directories."""
        if not skip_lock and not self._acquire_lock(LOCK_DELETE): return {"status": "busy"}
        if auto_gc is None: auto_gc = self._get_config().get("registry", {}).get("auto_gc_after_operations", True)
        try:
            digest = self._resolve_digest(name, tag)
            tag_path = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories", name, "_manifests", "tags", tag)
            
            # Try API delete first
            api_success = False
            if digest:
                try:
                    r = self.session.delete(f"{self.registry_url}/{name}/manifests/{digest}", timeout=10)
                    if r.status_code == 202:
                        api_success = True
                        logger.info(f"[Registry] API delete successful for {name}:{tag}")
                    else:
                        logger.warning(f"[Registry] API delete returned {r.status_code} for {name}:{tag}")
                except Exception as e:
                    logger.warning(f"[Registry] API delete failed for {name}:{tag}: {e}")
            
            # ALWAYS remove filesystem - even if API failed
            if os.path.exists(tag_path):
                shutil.rmtree(tag_path)
                logger.info(f"[Registry] Removed filesystem tag: {tag_path}")
            
            # Remove from watched list if present
            image_name = f"{name}:{tag}"
            self.remove_from_watch_list(image_name)
            
            with self._cache_lock: self._images_cache = None
            self._cleanup_empty_dirs()
            
            res = {"status": "success", "api_delete": api_success}
            if auto_gc and not skip_lock: 
                gc_result = self.run_gc(skip_lock=True)
                res["gc"] = gc_result
                logger.info(f"[Registry] GC after delete: {gc_result}")
            
            return res
            
        except Exception as e: 
            logger.error(f"[Registry] Delete tag error for {name}:{tag}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if not skip_lock: self._release_lock()

    def delete_image(self, name, auto_gc=None):
        """Deletes all tags and wipes the repository folder."""
        if not self._acquire_lock(LOCK_DELETE): return {"status": "busy"}
        if auto_gc is None: auto_gc = self._get_config().get("registry", {}).get("auto_gc_after_operations", True)
        
        try:
            logger.info(f"[Registry] Deleting image: {name}")
            
            # Get all tags
            tags = []
            try:
                t_resp = self.session.get(f"{self.registry_url}/{name}/tags/list", timeout=5)
                if t_resp.status_code == 200:
                    tags = t_resp.json().get("tags") or []
                    logger.info(f"[Registry] Found {len(tags)} tags for {name}: {tags}")
            except Exception as e:
                logger.warning(f"[Registry] Failed to get tags for {name}: {e}")
            
            # Delete each tag
            for tag in tags: 
                result = self.delete_tag(name, tag, skip_lock=True, auto_gc=False)
                logger.info(f"[Registry] Delete tag {name}:{tag} result: {result}")
            
            # Force remove entire repository folder
            repo_path = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories", name)
            if os.path.exists(repo_path):
                logger.info(f"[Registry] Removing repository folder: {repo_path}")
                shutil.rmtree(repo_path)
                logger.info(f"[Registry] Repository folder removed")
            else:
                logger.warning(f"[Registry] Repository folder not found: {repo_path}")
            
            # Cleanup empty directories
            self._cleanup_empty_dirs()
            
            # Run GC
            if auto_gc:
                logger.info(f"[Registry] Running GC after delete_image")
                gc_result = self.run_gc(skip_lock=True)
                logger.info(f"[Registry] GC result: {gc_result}")
            
            with self._cache_lock: self._images_cache = None
            
            return {"status": "success", "deleted_tags": len(tags)}
            
        except Exception as e: 
            logger.error(f"[Registry] Delete image error for {name}: {e}")
            return {"status": "error", "message": str(e)}
        finally: 
            self._release_lock()

    def pull_image(self, image_name, auto_gc=None, background=False, add_to_watch=False, retention='latest'):
        """Pulls image using skopeo with multi-arch support and optional progress tracking."""
        if not self._acquire_lock(LOCK_SYNC): return {"status": "busy"}
        if auto_gc is None: auto_gc = self._get_config().get("registry", {}).get("auto_gc_after_operations", True)
        
        if background:
            # Start pull in background thread with progress tracking
            pull_thread = threading.Thread(
                target=self._pull_image_with_progress, 
                args=(image_name, auto_gc, add_to_watch, retention)
            )
            pull_thread.daemon = True
            pull_thread.start()
            return {"status": "started", "image": image_name}
        else:
            # Original blocking behavior for backward compatibility
            try:
                remote, local = f"docker://{image_name}", f"docker://{self.registry_host}:{self.registry_port}/{image_name}"
                result = subprocess.run(["skopeo", "copy", "--dest-tls-verify=false", "--multi-arch=all", remote, local], timeout=900, capture_output=True, text=True)
                if result.returncode == 0:
                    with self._cache_lock: self._images_cache = None
                    res = {"status": "success"}
                    if auto_gc: res["gc"] = self.run_gc(skip_lock=True)
                    
                    # Add to watch if requested
                    if add_to_watch:
                        watch_result = self.add_to_watch_list(image_name, retention, auto_pull=False)
                        res["watch"] = watch_result
                    
                    return res
                return {"status": "error", "message": result.stderr}
            except Exception as e: return {"status": "error", "message": str(e)}
            finally: self._release_lock()

    def _pull_image_with_progress(self, image_name, auto_gc, add_to_watch=False, retention='latest'):
        """Internal method to pull image with progress tracking."""
        process = None  # Store process for cancellation
        try:
            # Initialize progress
            with self._pull_progress_lock:
                self._pull_progress[image_name] = {
                    "status": "starting",
                    "percent": 0,
                    "current_layer": 0,
                    "total_layers": 0,
                    "message": "Initializing pull..."
                }
            
            remote = f"docker://{image_name}"
            local = f"docker://{self.registry_host}:{self.registry_port}/{image_name}"
            
            # Run skopeo with Popen to capture output
            process = subprocess.Popen(
                ["skopeo", "copy", "--dest-tls-verify=false", "--multi-arch=all", remote, local],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            # Store process for cancellation
            with self._pull_progress_lock:
                self._pull_progress[image_name]["process"] = process
            
            layers_seen = set()
            total_layers = 0
            completed_layers = 0
            
            # Parse output for progress
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                
                logger.debug(f"[Registry Pull] {line}")
                
                # Count layers
                if "Copying blob" in line:
                    total_layers += 1
                    layers_seen.add(line)
                    with self._pull_progress_lock:
                        self._pull_progress[image_name]["total_layers"] = total_layers
                        self._pull_progress[image_name]["message"] = line
                
                if "Copying config" in line:
                    completed_layers = total_layers
                    with self._pull_progress_lock:
                        self._pull_progress[image_name]["status"] = "finalizing"
                        self._pull_progress[image_name]["percent"] = 95
                        self._pull_progress[image_name]["current_layer"] = completed_layers
                        self._pull_progress[image_name]["message"] = "Copying config..."
                
                if "Writing manifest" in line:
                    with self._pull_progress_lock:
                        self._pull_progress[image_name]["status"] = "completing"
                        self._pull_progress[image_name]["percent"] = 98
                        self._pull_progress[image_name]["message"] = "Writing manifest..."
                
                # Update progress based on layers
                if total_layers > 0:
                    completed = min(completed_layers + 1, total_layers)
                    percent = int((completed / total_layers) * 90)  # 90% for layers, rest for config/manifest
                    with self._pull_progress_lock:
                        self._pull_progress[image_name]["percent"] = percent
                        self._pull_progress[image_name]["current_layer"] = completed
            
            process.wait()
            
            if process.returncode == 0:
                with self._cache_lock:
                    self._images_cache = None
                
                # Run GC if requested
                if auto_gc:
                    self.run_gc(skip_lock=True)
                
                # Add to watch list ATOMICALLY if requested (backend handles it)
                if add_to_watch:
                    try:
                        watch_result = self.add_to_watch_list(image_name, retention, auto_pull=False)
                        logger.info(f"[Registry] Added {image_name} to watch list with retention {retention}")
                    except Exception as e:
                        logger.error(f"[Registry] Failed to add {image_name} to watch list: {e}")
                
                with self._pull_progress_lock:
                    self._pull_progress[image_name] = {
                        "status": "success",
                        "percent": 100,
                        "message": "Pull completed",
                        "watched": add_to_watch  # Let frontend know if watched
                    }
            else:
                with self._pull_progress_lock:
                    self._pull_progress[image_name] = {
                        "status": "error",
                        "percent": 0,
                        "message": "Pull failed"
                    }
        
        except Exception as e:
            logger.error(f"[Registry] Pull error for {image_name}: {e}")
            with self._pull_progress_lock:
                self._pull_progress[image_name] = {
                    "status": "error",
                    "percent": 0,
                    "message": str(e)
                }
        finally:
            self._release_lock()
    
    def get_pull_progress(self, image_name):
        """Get current pull progress for an image."""
        with self._pull_progress_lock:
            progress = self._pull_progress.get(image_name, {"status": "not_found"})
            # Don't return the process object to client
            if "process" in progress:
                progress = progress.copy()
                del progress["process"]
            return progress
    
    def cancel_pull(self, image_name):
        """Cancel an ongoing pull operation."""
        with self._pull_progress_lock:
            if image_name not in self._pull_progress:
                return {"status": "error", "message": "No pull in progress for this image"}
            
            progress = self._pull_progress[image_name]
            process = progress.get("process")
            
            if process and process.poll() is None:  # Process still running
                try:
                    process.terminate()
                    logger.info(f"[Registry] Terminated pull process for {image_name}")
                    
                    # Wait briefly for termination
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Force kill if doesn't terminate
                        process.kill()
                        logger.warning(f"[Registry] Force killed pull process for {image_name}")
                    
                    self._pull_progress[image_name] = {
                        "status": "cancelled",
                        "percent": progress.get("percent", 0),
                        "message": "Pull cancelled by user"
                    }
                    return {"status": "success", "message": "Pull cancelled"}
                except Exception as e:
                    logger.error(f"[Registry] Error cancelling pull for {image_name}: {e}")
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": "Pull already completed or not running"}
    
    def clear_pull_progress(self, image_name):
        """Clear pull progress for an image."""
        with self._pull_progress_lock:
            if image_name in self._pull_progress:
                del self._pull_progress[image_name]
                return {"status": "success"}
            return {"status": "not_found"}

    def get_total_size(self):
        """Reports total physical disk usage."""
        try:
            result = subprocess.run(["du", "-sb", self.storage_path], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                s = int(result.stdout.split()[0])
                return {"size_bytes": s, "size_formatted": self._format_size(s)}
        except Exception: pass
        return {"size_bytes": 0, "size_formatted": "0 B"}

    def get_storage_breakdown(self):
        """Returns physical disk usage breakdown."""
        path_blobs = os.path.join(self.storage_path, "docker", "registry", "v2", "blobs")
        path_repos = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories")
        def get_size(p):
            try:
                res = subprocess.run(["du", "-sb", p], capture_output=True, text=True)
                return int(res.stdout.split()[0]) if res.returncode == 0 else 0
            except: return 0
        b_size, r_size = get_size(path_blobs), get_size(path_repos)
        total = self.get_total_size()["size_bytes"]
        return {"total": self._format_size(total), "blobs": self._format_size(b_size), "metadata": self._format_size(r_size), "other": self._format_size(total - (b_size + r_size))}

    def list_images_with_status(self, force=False):
        """Returns images with metadata and watch status."""
        img_list = self.list_images(force=force)
        if img_list.get("error"): return img_list
        config = self._get_config()
        watched_images = config.get("registry", {}).get("watched_images", [])
        
        # Create lookup dictionaries for watched images
        watched_retention = {}
        watched_timestamps = {}
        for item in watched_images:
            img_name = item["image"]
            watched_retention[img_name] = item.get("retention", "latest")
            watched_timestamps[img_name] = item.get("last_updated")
        
        for img in img_list["images"]:
            nt = f"{img['name']}:{img['tag']}"
            img["is_watched"] = nt in watched_retention
            img["retention_policy"] = watched_retention.get(nt)
            img["last_updated"] = watched_timestamps.get(nt)
        return img_list

    def add_to_watch_list(self, image_name, retention="latest", auto_pull=True):
        """Adds image to retention watch list and optionally pulls it."""
        repo, tag = _normalize_image_name(image_name)
        normalized = f"{repo}:{tag}"
        config = self._get_config()
        watched = config.get("registry", {}).get("watched_images", [])
        if any(i["image"] == normalized for i in watched): 
            return {"status": "error", "message": "Already watched"}
        
        # Check if image exists in registry
        local_digest = self._resolve_digest(repo, tag)
        
        # Auto-pull if not present
        if auto_pull and not local_digest:
            logger.info(f"[Registry] Image {normalized} not in registry, pulling...")
            pull_result = self.pull_image(image_name, auto_gc=False)
            if pull_result.get("status") != "success":
                logger.error(f"[Registry] Failed to pull {normalized}: {pull_result.get('message')}")
                return {"status": "error", "message": f"Failed to pull image: {pull_result.get('message')}"}
            logger.info(f"[Registry] Successfully pulled {normalized}")
        
        # Add to watch list
        watched.append({"image": normalized, "retention": retention})
        config["registry"]["watched_images"] = watched
        with open(self.config_path, 'w') as f: json.dump(config, f, indent=2)
        with self._cache_lock: self._images_cache = None
        
        return {"status": "success", "pulled": auto_pull and not local_digest}

    def remove_from_watch_list(self, image_name):
        """Removes image from retention watch list."""
        repo, tag = _normalize_image_name(image_name)
        normalized = f"{repo}:{tag}"
        config = self._get_config()
        config["registry"]["watched_images"] = [i for i in config.get("registry", {}).get("watched_images", []) if i["image"] != normalized]
        with open(self.config_path, 'w') as f: json.dump(config, f, indent=2)
        with self._cache_lock: self._images_cache = None
        return {"status": "success"}

    def update_watch_retention(self, image_name, retention):
        """Updates retention policy for a watched image."""
        repo, tag = _normalize_image_name(image_name)
        normalized = f"{repo}:{tag}"
        config = self._get_config()
        watched = config.get("registry", {}).get("watched_images", [])
        
        found = False
        for item in watched:
            if item["image"] == normalized:
                item["retention"] = retention
                found = True
                logger.info(f"[Registry] Updated retention for {normalized} to {retention}")
                break
        
        if not found:
            return {"status": "error", "message": "Image not in watch list"}
        
        config["registry"]["watched_images"] = watched
        with open(self.config_path, 'w') as f:
            json.dump(config, f, indent=2)
        
        with self._cache_lock:
            self._images_cache = None
        
        return {"status": "success", "message": "Retention policy updated"}

    def get_watched_images(self):
        return {"watched_images": self._get_config().get("registry", {}).get("watched_images", [])}

    def get_registry_mode(self):
        cfg = self._get_config().get("registry", {})
        return {"mode": cfg.get("mode", "hybrid"), "proxy_upstream": cfg.get("proxy_upstream", "https://registry-1.docker.io")}

    def update_registry_settings(self, settings):
        """Updates the registry portion of the configuration file."""
        config = self._get_config()
        if "registry" not in config: config["registry"] = {}
        for key, value in settings.items(): config["registry"][key] = value
        with open(self.config_path, 'w') as f: json.dump(config, f, indent=2)
        with self._cache_lock: self._images_cache = None
        return {"status": "success"}

    # --- ADVANCED MAINTENANCE & ORPHAN CLEANUP LOGIC ---

    def identify_all_referenced_blobs(self):
        """Deep scan to find every blob referenced by any manifest in the registry."""
        img_list = self.list_images()
        if img_list.get("error"): return set()
        
        referenced_digests = set()
        for img in img_list["images"]:
            try:
                m_resp = self.session.get(
                    f"{self.registry_url}/{img['name']}/manifests/{img['tag']}", 
                    headers=MANIFEST_HEADERS, 
                    timeout=5
                )
                if m_resp.status_code == 200:
                    _, digests = self._calculate_image_size(img['name'], m_resp.json())
                    referenced_digests.update(digests)
                    # Include the manifest digest itself
                    m_digest = m_resp.headers.get("Docker-Content-Digest")
                    if m_digest: referenced_digests.add(m_digest)
            except Exception as e:
                logger.error(f"Error scanning references for {img['name']}: {e}")
                continue
        return referenced_digests

    def get_orphan_details(self):
        """Returns a list of every file on disk not associated with a manifest."""
        referenced = self.identify_all_referenced_blobs()
        orphans = []
        blob_path = os.path.join(self.storage_path, "docker", "registry", "v2", "blobs", "sha256")
        
        if not os.path.exists(blob_path):
            return orphans

        for prefix in os.listdir(blob_path):
            prefix_dir = os.path.join(blob_path, prefix)
            if not os.path.isdir(prefix_dir): continue
            for digest_hash in os.listdir(prefix_dir):
                full_digest = f"sha256:{digest_hash}"
                if full_digest not in referenced:
                    data_file = os.path.join(prefix_dir, digest_hash, "data")
                    if os.path.exists(data_file):
                        size = os.path.getsize(data_file)
                        orphans.append({
                            "digest": full_digest,
                            "path": data_file,
                            "size": size,
                            "size_formatted": self._format_size(size)
                        })
        return orphans

    def force_delete_orphans(self):
        """Hard-deletes blobs from disk that the registry GC might have missed."""
        if not self._acquire_lock(LOCK_GC): return {"status": "busy"}
        try:
            orphans = self.get_orphan_details()
            deleted_count = 0
            deleted_size = 0
            for o in orphans:
                try:
                    # Remove the entire hash directory
                    parent_dir = os.path.dirname(o["path"])
                    shutil.rmtree(parent_dir)
                    deleted_count += 1
                    deleted_size += o["size"]
                except Exception as e:
                    logger.error(f"Failed to delete orphan {o['digest']}: {e}")
            
            self._cleanup_empty_dirs()
            with self._cache_lock: self._images_cache = None
            
            return {
                "status": "success", 
                "deleted_count": deleted_count, 
                "deleted_size_bytes": deleted_size,
                "deleted_size_formatted": self._format_size(deleted_size)
            }
        finally:
            self._release_lock()

    def run_retention_prune(self):
        """Enforces the 'latest' retention policy: deletes all tags except the most recent for watched images."""
        config = self._get_config()
        watched = config.get("registry", {}).get("watched_images", [])
        pruned_tags = []
        
        for item in watched:
            if item.get("retention") == "latest":
                image_name = item["image"]
                repo, _ = _normalize_image_name(image_name)
                
                try:
                    t_resp = self.session.get(f"{self.registry_url}/{repo}/tags/list", timeout=5)
                    if t_resp.status_code == 200:
                        tags = t_resp.json().get("tags") or []
                        if len(tags) > 1:
                            sorted_tags = self._smart_tag_sort(tags)
                            # Keep the first (newest), delete the rest
                            to_delete = sorted_tags[1:]
                            for tag in to_delete:
                                self.delete_tag(repo, tag, skip_lock=False, auto_gc=False)
                                pruned_tags.append(f"{repo}:{tag}")
                except Exception as e:
                    logger.error(f"Retention error for {repo}: {e}")
        
        if pruned_tags:
            self.run_gc()
            
        return {"status": "success", "pruned_tags": pruned_tags}

    def check_and_fix_permissions(self):
        """Ensures the registry storage path is writable by the current process."""
        try:
            test_file = os.path.join(self.storage_path, ".perm_test")
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Permission error: {e}")
            return {"status": "error", "message": str(e)}

    def get_registry_logs(self, lines=100):
        """Fetches recent logs from the dockup-registry container."""
        try:
            container = self.client.containers.get("dockup-registry")
            logs = container.logs(tail=lines).decode("utf-8")
            return {"status": "success", "logs": logs}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ========================================================================
    # BACKUP & RESTORE FUNCTIONALITY
    # ========================================================================

    def backup_registry(self, backup_dir="/app/backups"):
        """
        Backup the entire registry data directory.
        Creates a timestamped tar.gz backup similar to stack backups.
        """
        import tarfile
        from datetime import datetime
        
        try:
            logger.info("[Registry] Starting backup")
            
            # Ensure backup directory exists
            os.makedirs(backup_dir, exist_ok=True)
            
            # Create backup filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"registry_backup_{timestamp}.tar.gz"
            backup_path = os.path.join(backup_dir, backup_filename)
            
            # Get size before backup
            size_before = self.get_total_size().get("size_bytes", 0)
            start_time = time.time()
            
            logger.info(f"[Registry] Creating backup: {backup_path}")
            logger.info(f"[Registry] Source directory: {self.storage_path}")
            
            # Create tar.gz backup
            with tarfile.open(backup_path, "w:gz") as tar:
                tar.add(self.storage_path, arcname="registry")
            
            # Get backup file size
            backup_size = os.path.getsize(backup_path)
            backup_size_mb = backup_size / (1024 * 1024)
            duration = int(time.time() - start_time)
            
            logger.info(f"[Registry] Backup completed successfully")
            logger.info(f"[Registry] Backup file: {backup_path}")
            logger.info(f"[Registry] Backup size: {backup_size_mb:.1f} MB")
            logger.info(f"[Registry] Duration: {duration}s")
            
            # Send success notification
            try:
                from app import notify
                msg_parts = [
                    "Registry backup completed",
                    f"Size: {backup_size_mb:.1f} MB",
                    f"Duration: {duration}s",
                    f"File: {backup_filename}"
                ]
                notify(
                    "✓ Registry Backup Completed",
                    "\n".join(msg_parts),
                    "success"
                )
            except Exception as notif_error:
                logger.error(f"[Registry] Failed to send notification: {notif_error}")
            
            return {
                "status": "success",
                "backup_file": backup_path,
                "backup_filename": backup_filename,
                "size_mb": backup_size_mb,
                "duration": duration,
                "timestamp": timestamp
            }
            
        except Exception as e:
            logger.error(f"[Registry] Backup failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            # Send error notification
            try:
                from app import notify
                notify(
                    "✗ Registry Backup Failed",
                    f"Error: {str(e)}",
                    "error"
                )
            except:
                pass
            
            return {"status": "error", "message": str(e)}

    def restore_registry(self, backup_path):
        """
        Restore registry from a backup file.
        Stops registry, restores data, starts registry.
        """
        import tarfile
        import tempfile
        
        try:
            logger.info(f"[Registry] Starting restore from: {backup_path}")
            
            # Verify backup file exists
            if not os.path.exists(backup_path):
                raise Exception(f"Backup file not found: {backup_path}")
            
            # Stop registry container
            logger.info("[Registry] Stopping registry container")
            stop_result = self.stop_registry()
            if stop_result.get("status") != "success":
                raise Exception(f"Failed to stop registry: {stop_result.get('message')}")
            
            # Backup current data (in case restore fails)
            backup_current = f"{self.storage_path}.pre-restore-{int(time.time())}"
            logger.info(f"[Registry] Backing up current data to: {backup_current}")
            shutil.move(self.storage_path, backup_current)
            
            try:
                # Extract backup
                logger.info(f"[Registry] Extracting backup to: {self.storage_path}")
                os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
                
                with tarfile.open(backup_path, "r:gz") as tar:
                    # Extract to parent directory
                    parent_dir = os.path.dirname(self.storage_path)
                    tar.extractall(path=parent_dir)
                    
                    # If extracted as "registry" folder, rename to actual storage path name
                    extracted_path = os.path.join(parent_dir, "registry")
                    if os.path.exists(extracted_path) and extracted_path != self.storage_path:
                        if os.path.exists(self.storage_path):
                            shutil.rmtree(self.storage_path)
                        shutil.move(extracted_path, self.storage_path)
                
                logger.info("[Registry] Restore completed successfully")
                
                # Remove backup of old data
                if os.path.exists(backup_current):
                    logger.info(f"[Registry] Removing old backup: {backup_current}")
                    shutil.rmtree(backup_current)
                
            except Exception as extract_error:
                # Restore failed - put back original data
                logger.error(f"[Registry] Restore failed, reverting: {extract_error}")
                if os.path.exists(self.storage_path):
                    shutil.rmtree(self.storage_path)
                if os.path.exists(backup_current):
                    shutil.move(backup_current, self.storage_path)
                raise
            
            # Start registry container
            logger.info("[Registry] Starting registry container")
            start_result = self.start_registry()
            if start_result.get("status") != "success":
                logger.warning(f"[Registry] Failed to start registry: {start_result.get('message')}")
            
            # Clear cache
            with self._cache_lock:
                self._images_cache = None
            
            # Send success notification
            try:
                from app import notify
                notify(
                    "✓ Registry Restore Completed",
                    f"Registry restored from backup\nFile: {os.path.basename(backup_path)}",
                    "success"
                )
            except:
                pass
            
            return {
                "status": "success",
                "message": "Registry restored successfully",
                "backup_file": backup_path
            }
            
        except Exception as e:
            logger.error(f"[Registry] Restore failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            # Send error notification
            try:
                from app import notify
                notify(
                    "✗ Registry Restore Failed",
                    f"Error: {str(e)}",
                    "error"
                )
            except:
                pass
            
            return {"status": "error", "message": str(e)}

    def list_registry_backups(self, backup_dir="/app/backups"):
        """List all registry backup files"""
        try:
            if not os.path.exists(backup_dir):
                return {"status": "success", "backups": []}
            
            backups = []
            for filename in os.listdir(backup_dir):
                if filename.startswith("registry_backup_") and filename.endswith(".tar.gz"):
                    filepath = os.path.join(backup_dir, filename)
                    stat = os.stat(filepath)
                    backups.append({
                        "filename": filename,
                        "path": filepath,
                        "size": stat.st_size,
                        "size_mb": stat.st_size / (1024 * 1024),
                        "size_formatted": self._format_size(stat.st_size),
                        "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "timestamp": stat.st_mtime
                    })
            
            # Sort by timestamp (newest first)
            backups.sort(key=lambda x: x["timestamp"], reverse=True)
            
            return {"status": "success", "backups": backups}
            
        except Exception as e:
            logger.error(f"[Registry] Failed to list backups: {e}")
            return {"status": "error", "message": str(e)}

    def delete_registry_backup(self, backup_path):
        """Delete a registry backup file"""
        try:
            if not os.path.exists(backup_path):
                return {"status": "error", "message": "Backup file not found"}
            
            # Security check - ensure it's a registry backup file
            if not os.path.basename(backup_path).startswith("registry_backup_"):
                return {"status": "error", "message": "Invalid backup file"}
            
            os.remove(backup_path)
            logger.info(f"[Registry] Deleted backup: {backup_path}")
            
            return {"status": "success", "message": "Backup deleted"}
            
        except Exception as e:
            logger.error(f"[Registry] Failed to delete backup: {e}")
            return {"status": "error", "message": str(e)}

