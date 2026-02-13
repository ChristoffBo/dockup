import docker
import os
import logging
import yaml

logger = logging.getLogger("dockup.registry.deployment")

class RegistryDeployment:
    """
    Handles deployment and lifecycle of the Docker Registry container.
    Generates appropriate configuration based on operating mode.
    """
    
    def __init__(self, config):
        """
        Args:
            config: Registry configuration dict from config.json
        """
        self.client = docker.from_env()
        self.config = config
        self.container_name = "dockup-registry"
        
    def generate_registry_config(self, existing_secret=None):
        """
        Generate registry config.yml content based on mode.
        
        Args:
            existing_secret (str, optional): Existing HTTP secret to preserve
        
        Returns:
            dict: Registry configuration
        """
        mode = self.config.get("mode", "hybrid")
        proxy_upstream = self.config.get("proxy_upstream", "https://registry-1.docker.io")
        
        # Use existing secret or generate new one
        if existing_secret:
            http_secret = existing_secret
        else:
            import secrets
            http_secret = secrets.token_hex(32)  # 64 char hex string = 32 bytes
        
        base_config = {
            "version": "0.1",
            "log": {
                "fields": {
                    "service": "registry"
                }
            },
            "storage": {
                "delete": {
                    "enabled": True
                },
                "cache": {
                    "blobdescriptor": "inmemory"
                },
                "filesystem": {
                    "rootdirectory": "/var/lib/registry"
                }
            },
            "http": {
                "addr": ":5000",
                "secret": http_secret,  # FIX: Added HTTP secret for secure auth
                "headers": {
                    "X-Content-Type-Options": ["nosniff"]
                }
            },
            "health": {
                "storagedriver": {
                    "enabled": True,
                    "interval": "10s",
                    "threshold": 3
                }
            }
        }
        
        # Add proxy configuration for proxy or hybrid mode
        if mode in ["proxy", "hybrid"]:
            base_config["proxy"] = {
                "remoteurl": proxy_upstream
            }
        
        return base_config
    
    def deploy(self):
        """
        Deploy the registry container.
        
        Returns:
            dict: Status and message
        """
        try:
            # Check if already exists
            try:
                existing = self.client.containers.get(self.container_name)
                if existing.status == "running":
                    return {"status": "success", "message": "Registry already running"}
                else:
                    # Start existing stopped container
                    existing.start()
                    return {"status": "success", "message": "Registry started"}
            except docker.errors.NotFound:
                pass  # Container doesn't exist, create it
            
            # Create config directory if needed
            storage_path = self.config.get("storage_path", "/DATA/AppData/Registry")
            config_dir = os.path.join(storage_path, "config")
            os.makedirs(config_dir, exist_ok=True)
            
            # Generate config (preserving HTTP secret if exists)
            config_file = os.path.join(config_dir, "config.yml")
            
            # Try to preserve existing HTTP secret
            existing_secret = None
            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r') as f:
                        old_config = yaml.safe_load(f)
                        existing_secret = old_config.get('http', {}).get('secret')
                        if existing_secret:
                            logger.info("[Registry] Preserving existing HTTP secret")
                except Exception as e:
                    logger.warning(f"[Registry] Could not read old config: {e}")
            
            # Generate new config
            registry_config = self.generate_registry_config(existing_secret=existing_secret)
            
            # Write config file
            with open(config_file, 'w') as f:
                yaml.dump(registry_config, f, default_flow_style=False)
            
            if existing_secret:
                logger.info("[Registry] Config regenerated with preserved HTTP secret")
            else:
                logger.info("[Registry] Config generated with new HTTP secret")
            
            # Get settings
            port = self.config.get("port", 5500)
            version = str(self.config.get("version", "2"))  # Ensure string
            image = f"registry:{version}"
            
            logger.info(f"[Registry] Deploying - Port: {port}, Version: {version}, Image: {image}")
            
            # Create container
            container = self.client.containers.run(
                image,
                name=self.container_name,
                detach=True,
                ports={'5000/tcp': port},
                volumes={
                    storage_path: {'bind': '/var/lib/registry', 'mode': 'rw'},
                    config_file: {'bind': '/etc/docker/registry/config.yml', 'mode': 'ro'}
                },
                environment={
                    "REGISTRY_STORAGE_DELETE_ENABLED": "true"
                },
                restart_policy={"Name": "unless-stopped"},
                labels={
                    "com.docker.compose.project": "dockup-registry",
                    "dockup.system": "true",
                    "dockup.registry": "true"
                }
            )
            
            logger.info(f"[Registry] Container deployed on port {port}: {container.id[:12]}")
            return {
                "status": "success",
                "message": "Registry deployed successfully",
                "container_id": container.id[:12]
            }
            
        except Exception as e:
            logger.error(f"Registry deployment failed: {e}")
            return {"status": "error", "message": str(e)}
    
    def stop(self, remove=False):
        """
        Stop the registry container.
        
        Args:
            remove (bool): If True, also removes the container after stopping
        
        Returns:
            dict: Status and message
        """
        try:
            container = self.client.containers.get(self.container_name)
            container.stop(timeout=10)
            logger.info("Registry container stopped")
            
            if remove:
                container.remove()
                logger.info("Registry container removed")
                return {"status": "success", "message": "Registry stopped and removed"}
            else:
                return {"status": "success", "message": "Registry stopped"}
                
        except docker.errors.NotFound:
            return {"status": "error", "message": "Registry container not found"}
        except Exception as e:
            logger.error(f"Failed to stop registry: {e}")
            return {"status": "error", "message": str(e)}
    
    def remove(self):
        """
        Stop and remove the registry container.
        
        Returns:
            dict: Status and message
        """
        try:
            container = self.client.containers.get(self.container_name)
            container.stop(timeout=10)
            container.remove()
            logger.info("Registry container removed")
            return {"status": "success", "message": "Registry removed"}
        except docker.errors.NotFound:
            return {"status": "error", "message": "Registry container not found"}
        except Exception as e:
            logger.error(f"Failed to remove registry: {e}")
            return {"status": "error", "message": str(e)}
    
    def restart(self):
        """
        Restart the registry container (for config changes).
        Recreates container if port, version, or network settings changed.
        
        Returns:
            dict: Status and message
        """
        try:
            container = self.client.containers.get(self.container_name)
            
            # Get current container settings
            current_port_mapping = container.attrs['HostConfig']['PortBindings'].get('5000/tcp')
            if current_port_mapping:
                current_port = int(current_port_mapping[0]['HostPort'])
            else:
                current_port = None
            
            # Get current image - handle different formats
            if container.image.tags:
                current_image = container.image.tags[0]
            else:
                current_image = container.attrs['Config']['Image']
            
            # Extract just the version number from current image
            # Handles: registry:2, registry:3, docker.io/library/registry:2, etc.
            current_version = "2"  # default
            if ':' in current_image:
                current_version = current_image.split(':')[-1]
            
            # Get new settings from config
            new_port = self.config.get("port", 5500)
            new_version = str(self.config.get("version", "2"))  # Ensure string
            new_image = f"registry:{new_version}"
            
            logger.info(f"[Registry] Restart check - Current: {current_image} (v{current_version}), New: {new_image} (v{new_version})")
            
            # Check if recreation needed
            needs_recreation = False
            recreation_reason = []
            
            if current_port != new_port:
                needs_recreation = True
                recreation_reason.append(f"port {current_port} → {new_port}")
            
            if current_version != new_version:
                needs_recreation = True
                recreation_reason.append(f"version {current_version} → {new_version}")
            
            if needs_recreation:
                # Settings changed that require recreation
                logger.info(f"[Registry] Recreating container ({', '.join(recreation_reason)})")
                
                # Remove old container
                container.stop(timeout=10)
                container.remove()
                
                # Deploy with new config
                return self.deploy()
            else:
                # Just config changes (mode, proxy) - restart picks up new config.yml
                logger.info("[Registry] Restarting container (config changes only)")
                container.restart(timeout=10)
                return {"status": "success", "message": "Registry restarted"}
                
        except docker.errors.NotFound:
            # Container doesn't exist - deploy it
            logger.info("[Registry] Container not found, deploying")
            return self.deploy()
        except Exception as e:
            logger.error(f"Failed to restart registry: {e}")
            return {"status": "error", "message": str(e)}
    
    def get_status(self):
        """
        Get registry container status.
        
        Returns:
            dict: Container status info
        """
        try:
            container = self.client.containers.get(self.container_name)
            return {
                "status": "success",
                "container_status": container.status,
                "id": container.id[:12],
                "image": container.image.tags[0] if container.image.tags else "unknown",
                "ports": container.ports
            }
        except docker.errors.NotFound:
            return {"status": "not_found", "message": "Registry container not deployed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    def update_mode(self, new_mode, new_upstream=None):
        """
        Update registry mode (requires restart).
        
        Args:
            new_mode: "mirror", "proxy", or "hybrid"
            new_upstream: Optional new proxy upstream URL
        
        Returns:
            dict: Status and message
        """
        self.config["mode"] = new_mode
        if new_upstream:
            self.config["proxy_upstream"] = new_upstream
        
        # Regenerate config
        registry_config = self.generate_registry_config()
        storage_path = self.config.get("storage_path", "/DATA/AppData/Registry")
        config_file = os.path.join(storage_path, "config", "config.yml")
        
        try:
            with open(config_file, 'w') as f:
                yaml.dump(registry_config, f, default_flow_style=False)
            
            # Restart container to apply new config
            restart_result = self.restart()
            if restart_result["status"] == "success":
                logger.info(f"Registry mode updated to: {new_mode}")
                return {
                    "status": "success",
                    "message": f"Registry mode updated to {new_mode}",
                    "restart_required": False  # Already restarted
                }
            else:
                return restart_result
        except Exception as e:
            logger.error(f"Failed to update registry mode: {e}")
            return {"status": "error", "message": str(e)}
