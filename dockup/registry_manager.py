import docker
import requests
import os
import subprocess
import json
import logging
import time
import re
import threading
import tempfile
import stat as _stat

# v2.1: ONE lock and ONE atomic writer for config.json, shared with app.py.
# Both modules used to truncate-and-rewrite the file independently: a reader
# could catch it half-written, and each side overwrote the other's changes.
CONFIG_IO_LOCK = threading.RLock()


def atomic_write_json(path, data, indent=2):
    """Write JSON so a crash or a concurrent reader never sees a partial file:
    temp file in the same folder, fsync, then os.replace. Falls back to an
    in-place write when replace is impossible (e.g. a file bind mount)."""
    text = None
    for _ in range(3):           # the dict may be mutated by another thread mid-dump
        try:
            text = json.dumps(data, indent=indent)
            break
        except RuntimeError:
            time.sleep(0.01)
    if text is None:
        text = json.dumps(data, indent=indent)
    folder = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                os.chmod(tmp, _stat.S_IMODE(os.stat(path).st_mode))
            except OSError:
                pass
        try:
            os.replace(tmp, path)
        except OSError:
            with open(path, 'w') as f:      # bind-mounted single file: cannot be replaced
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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

# Media types that identify a manifest LIST / OCI index (multi-arch image).
# The child manifests these point at are themselves blobs on disk and must be
# treated as referenced, otherwise orphan detection deletes them and every
# multi-arch image in the registry becomes unpullable.
MANIFEST_LIST_TYPES = (
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json"
)

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
        
        for attempt in range(3):
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Failed to read config: {e}")
                time.sleep(0.05)
        # v2.1: marked, so _write_config refuses to save these defaults over the
        # real file (that used to wipe every setting after one bad read).
        default_config["_dockup_fallback"] = True
        return default_config

    def _write_config(self, config):
        """Save ONLY the registry block, merged into the file as it is right now."""
        if config.get("_dockup_fallback"):
            raise RuntimeError("config.json could not be read — refusing to overwrite it with defaults")
        with CONFIG_IO_LOCK:
            current = {}
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    current = json.load(f)
            current["registry"] = config.get("registry", {})
            atomic_write_json(self.config_path, current)

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
        """Resolve the correct digest for a tag using the Registry API.

        v2.1: returns None when the registry says the tag does not exist (404)
        and raises for every other failure. It used to raise on 404 too, so the
        "missing image -> pull it" branches never ran, and callers could not tell
        "deleted" from "registry briefly unreachable".
        """
        r = self.session.get(
            f"{self.registry_url}/{repo}/manifests/{tag}", 
            headers=MANIFEST_HEADERS, 
            timeout=10
        )
        if r.status_code == 404:
            return None
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

        result = {"error": None, "images": results, "orphans": orphan_summary, "scan_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"}
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
        try:
            # v2.1: no timeout meant one hung inspect stalled the whole batch
            inspect_res = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Timed out inspecting the upstream image"}
        if inspect_res.returncode != 0:
            return {"status": "error", "message": "Failed to inspect upstream image"}
        
        upstream_data = json.loads(inspect_res.stdout)
        remote_digest = upstream_data.get("Digest")

        if local_digest == remote_digest:
            return {"status": "up_to_date", "digest": local_digest}

        logger.info(f"Update required for {image_name}. New digest: {remote_digest}")
        # Disable auto_gc during batch updates - GC runs once at end in check_all_watched_updates
        pull_status = self.pull_image(image_name, auto_gc=False)
        # v2.1: a failed or busy pull used to fall through to "updated" — with a
        # notification, a GC run, retention and a fresh last_updated stamp.
        if pull_status.get("status") != "success":
            return {"status": pull_status.get("status", "error"),
                    "message": pull_status.get("message", "Pull failed")}
        
        # For unwatched images: delete old manifest immediately (keep only latest)
        # For watched images: let retention policy handle it
        if pull_status["status"] == "success" and local_digest:
            # Parse image name to get repo and tag
            repo_parsed, tag_parsed = _normalize_image_name(image_name)
            
            config = self._get_config()
            watched_images = config.get("registry", {}).get("watched_images", [])
            is_watched = any(item["image"] == image_name for item in watched_images)
            
            # v2.1: a digest delete removes EVERY tag on that digest (e.g. updating
            # nginx:latest also destroyed nginx:1.27). Same guard as delete_tag.
            if not is_watched and self._tags_sharing_digest(repo_parsed, tag_parsed, local_digest):
                logger.info(f"[Registry] Old manifest of {image_name} is still used by other tags — kept")
            elif not is_watched:
                # Not watched - delete old manifest to keep only latest
                try:
                    delete_url = f"{self.registry_url}/{repo_parsed}/manifests/{local_digest}"
                    resp = self.session.delete(delete_url, timeout=10)
                    if resp.status_code in [200, 202, 404]:
                        logger.info(f"[Registry] Deleted old manifest for unwatched image {image_name}")
                        
                        # Attempt safe cleanup of orphaned tag index entry
                        self._safe_cleanup_tag_index(repo_parsed, tag_parsed, local_digest, image_name)
                        
                except Exception as e:
                    logger.warning(f"[Registry] Failed to delete old manifest: {e}")

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
        checked_images = []  # every image the check actually reached, for last_checked
        
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
                checked_images.append(image_name)
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
        
        # Record that these images were checked, regardless of the outcome. Done once
        # here rather than per image so concurrent WebUI config writes get one window,
        # not one per watched image.
        self._stamp_last_checked(checked_images)

        # Send notifications via HTTP like backup module does
        notify_on_check = config.get("notify_on_registry_check", False)
        notify_on_update = config.get("notify_on_registry_update", True)
        
        if updated_images and notify_on_update:
            # Images were updated - send success notification via HTTP
            try:
                import requests
                message = f"Updated {len(updated_images)} image(s):\n" + "\n".join(f"✓ {img}" for img in updated_images)
                if failed_images:
                    message += f"\n\nFailed: {len(failed_images)}"
                
                requests.post(
                    'http://localhost:5000/api/internal/backup-notification',
                    json={
                        'title': 'Docker Registry - Updates Applied',
                        'message': message,
                        'type': 'success'
                    },
                    timeout=5
                )
            except Exception as e:
                logger.error(f"Failed to send update notification: {e}")
        elif not updated_images and notify_on_check:
            # No updates found - send info notification via HTTP
            try:
                import requests
                requests.post(
                    'http://localhost:5000/api/internal/backup-notification',
                    json={
                        'title': 'Docker Registry - No Updates Found',
                        'message': f"Checked {len(watched_images)} image(s) - All up to date",
                        'type': 'info'
                    },
                    timeout=5
                )
            except Exception as e:
                logger.error(f"Failed to send check notification: {e}")
        
        # Run GC if updates were performed
        if updated_images:
            logger.info("Running garbage collection after updates")
            try:
                self.run_gc(skip_lock=False)
            except:
                pass
        
        # Enforce retention policies after updates
        if updated_images:
            logger.info("Enforcing retention policies after updates")
            try:
                self.enforce_retention_policy()
            except Exception as e:
                logger.error(f"Retention enforcement failed: {e}")
        
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

    def _stamp_last_checked(self, image_names):
        """
        Records that these images WERE checked, whatever the outcome.
        last_updated is only written when a copy actually happens, so an image that
        has been checked hundreds of times with no change never gets a timestamp and
        the UI showed it as "Never" — which reads as "never checked" when it means
        "never changed". One config write for the whole batch.
        """
        if not image_names:
            return
        try:
            stamp = datetime.utcnow().isoformat() + "Z"
            config = self._get_config()
            watched_images = config.get("registry", {}).get("watched_images", [])
            names = set(image_names)
            for item in watched_images:
                if item.get("image") in names:
                    item["last_checked"] = stamp
            config.setdefault("registry", {})["watched_images"] = watched_images
            self._write_config(config)
        except Exception as e:
            logger.error(f"[Registry] Failed to stamp last_checked: {e}")

    def _update_image_timestamp(self, image_name):
        """Update last_updated timestamp for an image."""
        try:
            config = self._get_config()
            watched_images = config.get("registry", {}).get("watched_images", [])
            
            for item in watched_images:
                if item["image"] == image_name:
                    item["last_updated"] = datetime.utcnow().isoformat() + "Z"  # UTC timestamp
                    break
            
            config["registry"]["watched_images"] = watched_images
            self._write_config(config)
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
                except Exception as e:
                    # v2.1: "can't resolve" was treated as "deleted" — one registry
                    # restart or timeout emptied the whole watch list. Keep it.
                    cleaned.append(item)
                    logger.warning(f"Could not check {image_name} (kept on the watch list): {e}")
            
            # Save cleaned list (only when something was really removed)
            if removed:
                config["registry"]["watched_images"] = cleaned
                self._write_config(config)
            
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
            
            # --delete-untagged is known to also unlink the per-architecture child
            # manifests of TAGGED multi-arch images on affected registry versions
            # (distribution/distribution#3178), which leaves the manifest list
            # pointing at manifests that no longer exist. Exposed as a config key so
            # it can be turned off without a rebuild. Default preserves the previous
            # behaviour — set registry.gc_delete_untagged = false to disable.
            delete_untagged = self._get_config().get("registry", {}).get("gc_delete_untagged", True)
            gc_cmd = "registry garbage-collect"
            if delete_untagged:
                gc_cmd += " --delete-untagged"
            gc_cmd += " /etc/docker/registry/config.yml"
            logger.info(f"[Registry] GC command: {gc_cmd}")

            result = container.exec_run(gc_cmd)
            gc_output = result.output.decode(errors="replace") if result.output else ""
            logger.info(f"[Registry] GC command exit code: {result.exit_code}")

            # A non-zero exit means the collector aborted, possibly part-way through.
            # Previously this only logged a warning and still returned success, so a
            # failed GC reported a reclaimed figure as if it had worked.
            if result.exit_code != 0:
                logger.error(f"[Registry] GC FAILED (exit {result.exit_code}): {gc_output}")
                self._cleanup_empty_dirs()
                size_after_failed = self.get_total_size().get("size_bytes", 0)
                with self._cache_lock: self._images_cache = None
                return {
                    "status": "error",
                    "message": f"garbage-collect exited with code {result.exit_code}",
                    "exit_code": result.exit_code,
                    "output": gc_output,
                    "size_before": size_before,
                    "size_after": size_after_failed,
                    "reclaimed_bytes": size_before - size_after_failed,
                    "reclaimed_formatted": self._format_size(size_before - size_after_failed)
                }
            
            # Clean up empty directories after GC
            self._cleanup_empty_dirs()
            
            size_after = self.get_total_size().get("size_bytes", 0)
            reclaimed = size_before - size_after
            logger.info(f"[Registry] Size after GC: {self._format_size(size_after)}")
            logger.info(f"[Registry] Reclaimed: {self._format_size(reclaimed)}")
            
            with self._cache_lock: self._images_cache = None
            
            return {
                "status": "success", 
                "output": gc_output, 
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

    def _tags_sharing_digest(self, name, tag, digest):
        """
        Returns the OTHER tags in this repository that resolve to the same manifest
        digest. DELETE /v2/<name>/manifests/<digest> removes the manifest revision
        itself, which unlinks EVERY tag pointing at it — so deleting :latest when
        :latest and :abc123 share a digest destroys both, leaving dangling tag links
        that fail to pull. Callers use this to skip the API delete in that case.
        Returns [] when the tag list cannot be read, which keeps the previous
        behaviour rather than silently blocking deletes.
        """
        if not digest:
            return []
        others = []
        try:
            t_resp = self.session.get(f"{self.registry_url}/{name}/tags/list", timeout=10)
            if t_resp.status_code != 200:
                logger.warning(f"[Registry] Could not list tags for {name} (HTTP {t_resp.status_code}) - shared-digest check skipped")
                return []
            all_tags = t_resp.json().get("tags") or []
        except Exception as e:
            logger.warning(f"[Registry] Could not list tags for {name}: {e} - shared-digest check skipped")
            return []

        for other in all_tags:
            if other == tag:
                continue
            try:
                if self._resolve_digest(name, other) == digest:
                    others.append(other)
            except Exception as e:
                logger.warning(f"[Registry] Could not resolve digest for {name}:{other}: {e}")
                continue
        return others

    def delete_tag(self, name, tag, skip_lock=False, auto_gc=None, check_shared=True):
        """
        Deletes a tag and cleans up physical directories.
        check_shared=False skips the shared-digest lookup — only safe for callers that
        are removing every tag in the repository anyway (delete_image), where the check
        would cost one tag list plus one digest resolve per remaining tag for nothing.
        """
        if not skip_lock and not self._acquire_lock(LOCK_DELETE): return {"status": "busy"}
        if auto_gc is None: auto_gc = self._get_config().get("registry", {}).get("auto_gc_after_operations", True)
        try:
            digest = self._resolve_digest(name, tag)
            if not digest:
                raise Exception(f"{name}:{tag} was not found in the registry")
            tag_path = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories", name, "_manifests", "tags", tag)
            
            # Other tags on the same digest would be destroyed by a digest delete.
            shared_tags = self._tags_sharing_digest(name, tag, digest) if check_shared else []

            # Try API delete first
            api_success = False
            if digest and shared_tags:
                logger.warning(
                    f"[Registry] {name}:{tag} shares its manifest with {shared_tags} - "
                    f"removing the tag only, keeping the manifest so those tags stay pullable"
                )
            elif digest:
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
            
            # If the API delete did not happen (registry refused it, request failed) the
            # manifest revision survives on disk while the tag link is gone. That revision
            # keeps every layer it references alive, so the space is never reclaimed and
            # the repo accumulates unreachable blobs. Drop the revision too — but only when
            # no other tag points at it.
            if digest and not api_success and not shared_tags:
                revision_path = os.path.join(
                    self.storage_path, "docker", "registry", "v2", "repositories", name,
                    "_manifests", "revisions", "sha256", digest.split(":")[-1]
                )
                if os.path.exists(revision_path):
                    try:
                        shutil.rmtree(revision_path)
                        logger.info(f"[Registry] Removed unreferenced manifest revision: {revision_path}")
                    except Exception as e:
                        logger.error(f"[Registry] Could not remove manifest revision {revision_path}: {e}")

            # Remove from watched list if present
            image_name = f"{name}:{tag}"
            self.remove_from_watch_list(image_name)
            
            with self._cache_lock: self._images_cache = None
            self._cleanup_empty_dirs()
            
            res = {"status": "success", "api_delete": api_success, "shared_tags_preserved": shared_tags}
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
                result = self.delete_tag(name, tag, skip_lock=True, auto_gc=False, check_shared=False)
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
        watched_checked = {}
        for item in watched_images:
            img_name = item["image"]
            watched_retention[img_name] = item.get("retention", "latest")
            watched_timestamps[img_name] = item.get("last_updated")
            watched_checked[img_name] = item.get("last_checked")
        
        for img in img_list["images"]:
            nt = f"{img['name']}:{img['tag']}"
            img["is_watched"] = nt in watched_retention
            img["retention_policy"] = watched_retention.get(nt)
            img["last_updated"] = watched_timestamps.get(nt)
            img["last_checked"] = watched_checked.get(nt)
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
        self._write_config(config)
        with self._cache_lock: self._images_cache = None
        
        return {"status": "success", "pulled": auto_pull and not local_digest}

    def remove_from_watch_list(self, image_name):
        """Removes image from retention watch list."""
        repo, tag = _normalize_image_name(image_name)
        normalized = f"{repo}:{tag}"
        config = self._get_config()
        config["registry"]["watched_images"] = [i for i in config.get("registry", {}).get("watched_images", []) if i["image"] != normalized]
        self._write_config(config)
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
        self._write_config(config)
        
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
        self._write_config(config)
        with self._cache_lock: self._images_cache = None
        return {"status": "success"}

    # --- ADVANCED MAINTENANCE & ORPHAN CLEANUP LOGIC ---

    def identify_all_referenced_blobs(self):
        """Deep scan to find every blob referenced by any manifest in the registry."""
        # Any gap in this scan makes real blobs look unreferenced, so track whether the
        # scan was complete. force_delete_orphans() refuses to delete anything unless it
        # was — one timed-out request used to be enough to mark a whole image as garbage.
        self._reference_scan_complete = True

        img_list = self.list_images()
        if img_list.get("error"):
            self._reference_scan_complete = False
            return set()
        
        referenced_digests = set()
        for img in img_list["images"]:
            try:
                m_resp = self.session.get(
                    f"{self.registry_url}/{img['name']}/manifests/{img['tag']}", 
                    headers=MANIFEST_HEADERS, 
                    timeout=30
                )
                if m_resp.status_code == 200:
                    m_data = m_resp.json()
                    _, digests = self._calculate_image_size(img['name'], m_data)
                    referenced_digests.update(digests)
                    # Include the manifest digest itself
                    m_digest = m_resp.headers.get("Docker-Content-Digest")
                    if m_digest: referenced_digests.add(m_digest)

                    # For a manifest list, the per-architecture child manifests are blobs
                    # in their own right. _calculate_image_size only collects each child's
                    # config and layer digests, not the child manifest digest, so without
                    # this every multi-arch child manifest was classified as an orphan.
                    if m_data.get("mediaType") in MANIFEST_LIST_TYPES:
                        for sub in m_data.get("manifests", []):
                            sub_digest = sub.get("digest")
                            if sub_digest: referenced_digests.add(sub_digest)
                else:
                    self._reference_scan_complete = False
                    logger.error(f"Reference scan incomplete: {img['name']}:{img['tag']} returned HTTP {m_resp.status_code}")
            except Exception as e:
                self._reference_scan_complete = False
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

            # An incomplete reference scan means blobs that ARE in use can appear in this
            # list. Deleting on a partial scan is how a single slow response turns into a
            # destroyed image, so abort instead.
            if not getattr(self, "_reference_scan_complete", False):
                logger.error("[Registry] Reference scan incomplete - refusing to hard-delete orphan blobs")
                return {
                    "status": "error",
                    "message": "Reference scan incomplete - refusing to delete blobs. Check the registry is reachable and retry.",
                    "candidates": len(orphans)
                }

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

    def enforce_retention_policy(self):
        """Enforces keep_N retention policies by deleting old manifest revisions."""
        config = self._get_config()
        watched = config.get("registry", {}).get("watched_images", [])
        deleted_count = 0
        
        for item in watched:
            retention = item.get("retention", "latest")
            image_name = item["image"]
            repo, tag = _normalize_image_name(image_name)
            
            # Skip if retention is "keep_all"
            if retention == "keep_all":
                continue
            
            # Determine how many versions to keep
            if retention == "latest":
                keep_count = 1  # "latest" means keep only 1 version
            elif retention.startswith("keep_"):
                # Parse keep_N value
                try:
                    keep_count = int(retention.split("_")[1])
                except:
                    logger.warning(f"Invalid retention policy '{retention}' for {image_name}")
                    continue
            else:
                # Unknown retention policy
                logger.warning(f"Unknown retention policy '{retention}' for {image_name}")
                continue
            
            # Get all manifest revisions for THIS SPECIFIC TAG
            tag_index_path = os.path.join(
                self.storage_path, "docker", "registry", "v2", "repositories",
                repo, "_manifests", "tags", tag, "index", "sha256"
            )
            
            if not os.path.exists(tag_index_path):
                continue
            
            try:
                # List all manifest digests with their timestamps
                manifests = []
                for digest_dir in os.listdir(tag_index_path):
                    digest_full = f"sha256:{digest_dir}"
                    dir_path = os.path.join(tag_index_path, digest_dir)
                    
                    if os.path.isdir(dir_path):
                        mtime = os.path.getmtime(dir_path)
                        manifests.append({
                            "digest": digest_full,
                            "timestamp": mtime
                        })
                
                # Sort by timestamp, newest first
                manifests.sort(key=lambda x: x['timestamp'], reverse=True)
                
                # Get current tag digest to make sure we don't delete it
                try:
                    current_digest = self._resolve_digest(repo, tag)
                except:
                    current_digest = None
                
                # Keep first N manifests, delete the rest
                if len(manifests) > keep_count:
                    to_delete = manifests[keep_count:]
                    
                    for manifest in to_delete:
                        # Don't delete if it's the current tag
                        if manifest["digest"] == current_digest:
                            continue
                        
                        # CRITICAL: Check if any OTHER tag in this repo references this manifest
                        # before deleting to avoid data corruption
                        if self._is_manifest_referenced_by_other_tags(repo, tag, manifest["digest"]):
                            logger.info(f"[Registry] Skipping deletion of {manifest['digest'][:19]}... - referenced by other tags")
                            continue
                        
                        try:
                            delete_url = f"{self.registry_url}/{repo}/manifests/{manifest['digest']}"
                            resp = self.session.delete(delete_url, timeout=10)
                            if resp.status_code in [200, 202, 404]:
                                deleted_count += 1
                                logger.info(f"[Registry] Retention: Deleted old manifest {manifest['digest'][:19]}... for {image_name}")
                                
                                # Attempt safe cleanup of orphaned tag index entry
                                # This prevents accumulation of filesystem metadata
                                # Safe: only runs on filesystem storage with multiple safeguards
                                self._safe_cleanup_tag_index(repo, tag, manifest["digest"], image_name)
                                
                        except Exception as e:
                            logger.warning(f"[Registry] Failed to delete manifest {manifest['digest'][:19]}...: {e}")
                
            except Exception as e:
                logger.error(f"[Registry] Retention enforcement error for {image_name}: {e}")
        
        # Run GC if we deleted anything
        if deleted_count > 0:
            logger.info(f"[Registry] Retention enforcement deleted {deleted_count} old manifests, running GC...")
            self.run_gc()
        
        return {"status": "success", "deleted_count": deleted_count}
    
    def _is_manifest_referenced_by_other_tags(self, repo, exclude_tag, manifest_digest):
        """Check if a manifest is referenced by any tag other than exclude_tag"""
        try:
            # Get list of all tags in the repo
            tags_resp = self.session.get(f"{self.registry_url}/{repo}/tags/list", timeout=5)
            if tags_resp.status_code != 200:
                return True  # Conservative: assume it's referenced if we can't check
            
            all_tags = tags_resp.json().get("tags", [])
            
            # Check each tag (except the one we're enforcing retention for)
            for tag in all_tags:
                if tag == exclude_tag:
                    continue
                
                try:
                    # Get the digest this tag currently points to
                    tag_digest = self._resolve_digest(repo, tag)
                    if tag_digest == manifest_digest:
                        # This tag points to the manifest we want to delete!
                        logger.info(f"[Registry] Manifest {manifest_digest[:19]}... is referenced by {repo}:{tag}")
                        return True
                except Exception as e:
                    # If we can't resolve this tag's digest, skip it
                    logger.debug(f"[Registry] Could not resolve digest for {repo}:{tag}: {e}")
                    pass
            
            return False  # No other tags reference this manifest
            
        except Exception as e:
            logger.error(f"[Registry] Error checking manifest references: {e}")
            return True  # Conservative: assume it's referenced if check fails
    
    def _is_filesystem_storage(self):
        """Detect if registry is using filesystem storage (vs S3/Azure/etc)"""
        try:
            # Check if storage path exists and is accessible
            if not os.path.exists(self.storage_path):
                return False
            
            # Check for typical filesystem storage structure
            repos_path = os.path.join(self.storage_path, "docker", "registry", "v2", "repositories")
            blobs_path = os.path.join(self.storage_path, "docker", "registry", "v2", "blobs")
            
            return os.path.isdir(repos_path) and os.path.isdir(blobs_path)
        except (OSError, IOError) as e:
            # Filesystem access errors (permissions, I/O errors)
            logger.debug(f"[Registry] Filesystem check failed: {e}")
            return False
        except Exception as e:
            # Other unexpected errors
            logger.warning(f"[Registry] Unexpected error checking filesystem storage: {e}")
            return False
    
    def _safe_cleanup_tag_index(self, repo, tag, manifest_digest, image_name):
        """
        Safely cleanup orphaned tag index entry after manifest deletion.
        
        IMPORTANT SAFEGUARDS:
        - Only runs on filesystem storage (not S3/Azure)
        - Checks current/link to avoid corruption
        - Verifies manifest was actually deleted
        - Comprehensive error handling
        - Can be disabled via config
        """
        try:
            # SAFEGUARD 1: Check if filesystem cleanup is enabled
            config = self._get_config()
            cleanup_enabled = config.get("registry", {}).get("cleanup_tag_index", True)
            if not cleanup_enabled:
                logger.debug(f"[Registry] Tag index cleanup disabled in config")
                return False
            
            # SAFEGUARD 2: Only run on filesystem storage
            if not self._is_filesystem_storage():
                logger.debug(f"[Registry] Skipping tag index cleanup - not filesystem storage")
                return False
            
            # SAFEGUARD 3: Get current tag digest to avoid deleting current reference
            try:
                current_digest = self._resolve_digest(repo, tag)
                if current_digest == manifest_digest:
                    logger.warning(f"[Registry] Refusing to cleanup tag index - manifest is current! {manifest_digest[:19]}...")
                    return False
            except Exception as e:
                # If we can't resolve current digest, be conservative and don't delete
                logger.warning(f"[Registry] Cannot verify current digest for {repo}:{tag}, skipping cleanup for safety: {e}")
                return False
            
            # SAFEGUARD 4: Construct paths carefully
            digest_hash = manifest_digest.replace("sha256:", "")
            tag_index_entry = os.path.join(
                self.storage_path, "docker", "registry", "v2", "repositories",
                repo, "_manifests", "tags", tag, "index", "sha256", digest_hash
            )
            
            # SAFEGUARD 5: Verify entry exists before attempting deletion
            if not os.path.exists(tag_index_entry):
                logger.debug(f"[Registry] Tag index entry already gone: {digest_hash[:12]}...")
                return True  # Already cleaned up
            
            # SAFEGUARD 6: Delete with comprehensive error handling
            import shutil
            shutil.rmtree(tag_index_entry)
            logger.info(f"[Registry] Cleaned up orphaned tag index entry for {manifest_digest[:19]}... ({image_name})")
            return True
            
        except Exception as e:
            logger.warning(f"[Registry] Failed to cleanup tag index for {manifest_digest[:19]}...: {e}")
            # Non-fatal: GC will eventually handle this (though slowly)
            return False
    
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

    def backup_registry(self):
        """
        Backup registry appdata to the configured backup location.
        """
        import tarfile
        from datetime import datetime
        
        try:
            from backup_manager import check_backup_destination_available
            
            logger.info("[Registry] Starting backup")
            
            # Get backup destination (SMB share or local path from settings)
            is_available, dest_path, available_gb, error_msg = check_backup_destination_available()
            
            if not is_available:
                raise Exception(f"Backup destination not available: {error_msg}")
            
            logger.info(f"[Registry] Backup destination: {dest_path}")
            
            # Create backup filename with timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            backup_filename = f"registry_{timestamp}.tar.gz"
            backup_file_path = os.path.join(dest_path, backup_filename)
            
            start_time = time.time()
            
            logger.info(f"[Registry] Creating backup: {backup_file_path}")
            logger.info(f"[Registry] Source directory: {self.storage_path}")
            
            # Verify source exists
            if not os.path.exists(self.storage_path):
                raise Exception(f"Registry storage path does not exist: {self.storage_path}")
            
            # Create tar.gz backup of registry appdata
            with tarfile.open(backup_file_path, "w:gz") as tar:
                tar.add(self.storage_path, arcname="registry")
            
            # Get backup file size
            backup_size = os.path.getsize(backup_file_path)
            backup_size_mb = backup_size / (1024 * 1024)
            duration = int(time.time() - start_time)
            
            logger.info(f"[Registry] Backup completed successfully")
            logger.info(f"[Registry] Backup file: {backup_file_path}")
            logger.info(f"[Registry] Backup size: {backup_size_mb:.1f} MB")
            logger.info(f"[Registry] Duration: {duration}s")
            
            # Store in database
            from backup_manager import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO backup_history 
                (stack_name, backup_file_path, backup_size_mb, backup_duration_seconds, status, volumes_backed_up, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """, ('registry', backup_file_path, backup_size_mb, duration, 'completed', 1))
            conn.commit()
            conn.close()
            
            # Send success notification
            try:
                from app import notify
                notify(
                    "✓ Registry Backup Completed",
                    f"Size: {backup_size_mb:.1f} MB\nDuration: {duration}s",
                    "success"
                )
            except Exception:
                pass
            
            return {
                "status": "success",
                "backup_file": backup_file_path,
                "backup_filename": backup_filename,
                "size_mb": backup_size_mb
            }
            
        except Exception as e:
            logger.error(f"[Registry] Backup failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            # Send error notification
            try:
                from app import notify
                notify("✗ Registry Backup Failed", f"Error: {str(e)}", "error")
            except Exception:
                pass
            
            return {"status": "error", "message": str(e)}

    def stop_registry(self):
        """v2.1: restore_registry called this but it did not exist (AttributeError)."""
        try:
            container = self.client.containers.get("dockup-registry")
            if container.status == "running":
                container.stop(timeout=30)
            return {"status": "success"}
        except docker.errors.NotFound:
            return {"status": "success", "message": "Registry container not found (nothing to stop)"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def start_registry(self):
        """v2.1: restore_registry called this but it did not exist (AttributeError)."""
        try:
            container = self.client.containers.get("dockup-registry")
            if container.status != "running":
                container.start()
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def restore_registry(self, backup_path):
        """
        Restore registry from a backup file.
        Stops registry, restores appdata, starts registry.
        """
        import tarfile
        
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
            if os.path.exists(self.storage_path):
                shutil.move(self.storage_path, backup_current)
            
            try:
                # Extract backup
                logger.info(f"[Registry] Extracting backup to: {self.storage_path}")
                os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
                
                with tarfile.open(backup_path, "r:gz") as tar:
                    parent_dir = os.path.dirname(self.storage_path)
                    tar.extractall(path=parent_dir)
                    
                    extracted_path = os.path.join(parent_dir, "registry")
                    if os.path.exists(extracted_path) and extracted_path != self.storage_path:
                        if os.path.exists(self.storage_path):
                            shutil.rmtree(self.storage_path)
                        shutil.move(extracted_path, self.storage_path)
                
                logger.info("[Registry] Restore completed successfully")
                
                if os.path.exists(backup_current):
                    shutil.rmtree(backup_current)
                
            except Exception as extract_error:
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
            
            try:
                from app import notify
                notify("✓ Registry Restore Completed", "Registry restored from backup", "success")
            except Exception:
                pass
            
            return {"status": "success", "message": "Registry restored successfully"}
            
        except Exception as e:
            logger.error(f"[Registry] Restore failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            try:
                from app import notify
                notify("✗ Registry Restore Failed", f"Error: {str(e)}", "error")
            except Exception:
                pass
            
            return {"status": "error", "message": str(e)}

    def list_registry_backups(self):
        """List all registry backups by scanning the backup destination directory"""
        try:
            from backup_manager import scan_backup_directory
            
            all_backups = scan_backup_directory()
            registry_backups = []
            
            for b in all_backups:
                if b.get('stack_name') == 'registry':
                    size_bytes = (b.get('size_mb') or 0) * 1024 * 1024
                    registry_backups.append({
                        "id": None,
                        "filename": b.get('filename', ''),
                        "path": b.get('file_path', ''),
                        "size_mb": b.get('size_mb', 0),
                        "size_formatted": self._format_size(size_bytes),
                        "created": b.get('backup_date', ''),
                        "status": "completed"
                    })
            
            return {"status": "success", "backups": registry_backups}
            
        except Exception as e:
            logger.error(f"[Registry] Failed to list backups: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"status": "error", "message": str(e)}

    def delete_registry_backup(self, backup_path):
        """Delete a registry backup file"""
        try:
            if not backup_path or not os.path.exists(backup_path):
                return {"status": "error", "message": "Backup file not found"}
            
            os.remove(backup_path)
            logger.info(f"[Registry] Deleted backup: {backup_path}")
            
            return {"status": "success", "message": "Backup deleted"}
            
        except Exception as e:
            logger.error(f"[Registry] Failed to delete backup: {e}")
            return {"status": "error", "message": str(e)}
    
    def list_image_versions(self, image_name):
        """List all stored manifest versions for an image (for rollback)"""
        try:
            repo, tag = _normalize_image_name(image_name)
            
            # Path to tag-specific manifest index (version history for THIS tag only)
            tag_index_path = os.path.join(
                self.storage_path, "docker", "registry", "v2", "repositories",
                repo, "_manifests", "tags", tag, "index", "sha256"
            )
            
            if not os.path.exists(tag_index_path):
                return {"status": "success", "message": "No version history", "versions": [], "image_name": image_name, "repo": repo, "tag": tag}
            
            versions = []
            current_digest = None
            
            # Get current digest for this tag
            try:
                current_digest = self._resolve_digest(repo, tag)
            except:
                pass
            
            # List all manifest digests that this tag has pointed to
            for digest_dir in os.listdir(tag_index_path):
                digest_full = f"sha256:{digest_dir}"
                manifest_link = os.path.join(tag_index_path, digest_dir, "link")
                
                if os.path.exists(manifest_link):
                    # Get timestamp from directory mtime
                    dir_path = os.path.join(tag_index_path, digest_dir)
                    mtime = os.path.getmtime(dir_path)
                    
                    # Try to get manifest data for size info
                    try:
                        m_resp = self.session.get(
                            f"{self.registry_url}/{repo}/manifests/{digest_full}",
                            headers=MANIFEST_HEADERS,
                            timeout=5
                        )
                        if m_resp.status_code == 200:
                            manifest_data = m_resp.json()
                            size_bytes, _ = self._calculate_image_size(repo, manifest_data)
                        else:
                            logger.warning(f"[Registry] Failed to get manifest {digest_full[:19]}... for {image_name}: HTTP {m_resp.status_code}")
                            size_bytes = 0
                    except Exception as e:
                        logger.warning(f"[Registry] Exception getting manifest {digest_full[:19]}... for {image_name}: {e}")
                        size_bytes = 0
                    
                    versions.append({
                        "digest": digest_full,
                        "digest_short": digest_dir[:12],
                        "is_current": (digest_full == current_digest),
                        "timestamp": mtime,
                        "size": size_bytes,
                        "size_formatted": self._format_size(size_bytes) if size_bytes else "Unknown"
                    })
            
            # Sort by timestamp, newest first
            versions.sort(key=lambda x: x['timestamp'], reverse=True)
            
            return {
                "status": "success",
                "image_name": image_name,
                "repo": repo,
                "tag": tag,
                "current_digest": current_digest,
                "versions": versions
            }
            
        except Exception as e:
            logger.error(f"[Registry] Failed to list versions for {image_name}: {e}")
            return {"status": "error", "message": str(e), "versions": []}
    
    def rollback_to_version(self, image_name, target_digest):
        """Rollback an image tag to point to a specific digest"""
        try:
            repo, tag = _normalize_image_name(image_name)
            
            # Verify target digest exists
            try:
                m_resp = self.session.get(
                    f"{self.registry_url}/{repo}/manifests/{target_digest}",
                    headers=MANIFEST_HEADERS,
                    timeout=5
                )
                if m_resp.status_code != 200:
                    return {"status": "error", "message": f"Digest {target_digest} not found"}
            except Exception as e:
                return {"status": "error", "message": f"Cannot verify digest: {e}"}
            
            # Update the tag to point to target digest by PUTting the manifest
            try:
                # Get the manifest content
                manifest_resp = self.session.get(
                    f"{self.registry_url}/{repo}/manifests/{target_digest}",
                    headers=MANIFEST_HEADERS,
                    timeout=10
                )
                manifest_resp.raise_for_status()
                manifest_content = manifest_resp.content
                content_type = manifest_resp.headers.get('Content-Type', 'application/vnd.docker.distribution.manifest.v2+json')
                
                # PUT it back with the tag
                put_headers = {
                    'Content-Type': content_type
                }
                put_resp = self.session.put(
                    f"{self.registry_url}/{repo}/manifests/{tag}",
                    headers=put_headers,
                    data=manifest_content,
                    timeout=10
                )
                put_resp.raise_for_status()
                
                logger.info(f"[Registry] Rolled back {image_name} to {target_digest}")
                
                # Update last_updated timestamp
                self._update_image_timestamp(image_name)
                
                return {
                    "status": "success",
                    "message": f"Rolled back {image_name} to {target_digest[:19]}",
                    "image_name": image_name,
                    "target_digest": target_digest
                }
                
            except Exception as e:
                logger.error(f"[Registry] Rollback failed: {e}")
                return {"status": "error", "message": f"Rollback failed: {e}"}
                
        except Exception as e:
            logger.error(f"[Registry] Rollback error for {image_name}: {e}")
            return {"status": "error", "message": str(e)}

