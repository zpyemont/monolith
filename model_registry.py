"""
Kubernetes-native model registry for Monolith deployments.

This module provides utilities for managing model metadata and propagation
using Kubernetes ConfigMaps instead of ZooKeeper, enabling cloud-native
model lifecycle management.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Dict, Any
from threading import Thread, Event
import hashlib

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    KUBERNETES_AVAILABLE = True
except ImportError:
    logging.warning("Kubernetes client not available. Install kubernetes package for full functionality.")
    KUBERNETES_AVAILABLE = False

import tensorflow as tf


@dataclass
class ModelMetadata:
    """Metadata for a trained model."""
    model_name: str
    version: str
    export_path: str
    checkpoint_path: str
    timestamp: str
    training_type: str  # 'batch' or 'online'
    metrics: Dict[str, float]
    model_signature_hash: str
    status: str = 'available'  # 'available', 'loading', 'failed'


class ModelRegistry:
    """Kubernetes ConfigMap-based model registry."""
    
    def __init__(self, namespace: str = 'default', configmap_name: str = 'monolith-model-registry'):
        self.namespace = namespace
        self.configmap_name = configmap_name
        
        if KUBERNETES_AVAILABLE:
            try:
                # Try in-cluster config first (for pod execution)
                config.load_incluster_config()
                logging.info("Using in-cluster Kubernetes configuration")
            except config.ConfigException:
                try:
                    # Fall back to local kubeconfig
                    config.load_kube_config()
                    logging.info("Using local kubeconfig")
                except config.ConfigException as e:
                    logging.error(f"Failed to load Kubernetes configuration: {e}")
                    self._k8s_client = None
                    return
            
            self._k8s_client = client.CoreV1Api()
            self._ensure_configmap_exists()
        else:
            self._k8s_client = None
            logging.warning("Kubernetes client unavailable. Registry will operate in local mode.")

    def _ensure_configmap_exists(self):
        """Ensure the ConfigMap exists, create if not."""
        if not self._k8s_client:
            return
            
        try:
            self._k8s_client.read_namespaced_config_map(
                name=self.configmap_name, 
                namespace=self.namespace
            )
            logging.info(f"ConfigMap {self.configmap_name} exists")
        except ApiException as e:
            if e.status == 404:
                # Create the ConfigMap
                config_map = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=self.configmap_name),
                    data={"models": json.dumps({})}
                )
                try:
                    self._k8s_client.create_namespaced_config_map(
                        namespace=self.namespace,
                        body=config_map
                    )
                    logging.info(f"Created ConfigMap {self.configmap_name}")
                except ApiException as create_error:
                    logging.error(f"Failed to create ConfigMap: {create_error}")
            else:
                logging.error(f"Error checking ConfigMap: {e}")

    def _compute_model_hash(self, export_path: str) -> str:
        """Compute hash of model signature for change detection."""
        try:
            # Use the saved_model.pb file for hashing
            pb_path = os.path.join(export_path, "saved_model.pb")
            if tf.io.gfile.exists(pb_path):
                with tf.io.gfile.GFile(pb_path, 'rb') as f:
                    content = f.read()
                    return hashlib.sha256(content).hexdigest()[:16]
            else:
                # Fall back to timestamp-based hash
                return hashlib.sha256(export_path.encode()).hexdigest()[:16]
        except Exception as e:
            logging.warning(f"Failed to compute model hash: {e}")
            return hashlib.sha256(f"{export_path}-{time.time()}".encode()).hexdigest()[:16]

    def register_model(self, model_metadata: ModelMetadata) -> bool:
        """Register a new model version in the registry."""
        if not self._k8s_client:
            logging.warning("Kubernetes client not available. Skipping model registration.")
            return False

        try:
            # Read current ConfigMap
            config_map = self._k8s_client.read_namespaced_config_map(
                name=self.configmap_name,
                namespace=self.namespace
            )
            
            current_models = json.loads(config_map.data.get("models", "{}"))
            
            # Add/update model
            model_key = f"{model_metadata.model_name}"
            current_models[model_key] = asdict(model_metadata)
            
            # Update ConfigMap
            config_map.data["models"] = json.dumps(current_models, indent=2)
            config_map.data["last_updated"] = datetime.now().isoformat()
            
            self._k8s_client.patch_namespaced_config_map(
                name=self.configmap_name,
                namespace=self.namespace,
                body=config_map
            )
            
            logging.info(f"Registered model {model_metadata.model_name} v{model_metadata.version}")
            return True
            
        except ApiException as e:
            logging.error(f"Failed to register model: {e}")
            return False

    def get_model_metadata(self, model_name: str) -> Optional[ModelMetadata]:
        """Get metadata for a specific model."""
        if not self._k8s_client:
            return None
            
        try:
            config_map = self._k8s_client.read_namespaced_config_map(
                name=self.configmap_name,
                namespace=self.namespace
            )
            
            current_models = json.loads(config_map.data.get("models", "{}"))
            model_data = current_models.get(model_name)
            
            if model_data:
                return ModelMetadata(**model_data)
            return None
            
        except ApiException as e:
            logging.error(f"Failed to get model metadata: {e}")
            return None

    def list_models(self) -> Dict[str, ModelMetadata]:
        """List all registered models."""
        if not self._k8s_client:
            return {}
            
        try:
            config_map = self._k8s_client.read_namespaced_config_map(
                name=self.configmap_name,
                namespace=self.namespace
            )
            
            current_models = json.loads(config_map.data.get("models", "{}"))
            return {name: ModelMetadata(**data) for name, data in current_models.items()}
            
        except ApiException as e:
            logging.error(f"Failed to list models: {e}")
            return {}

    def watch_model_updates(self, model_name: str, callback, stop_event: Event):
        """Watch for updates to a specific model."""
        if not self._k8s_client:
            logging.warning("Cannot watch model updates without Kubernetes client")
            return
            
        logging.info(f"Starting model watch for {model_name}")
        last_version = None
        
        while not stop_event.is_set():
            try:
                current_metadata = self.get_model_metadata(model_name)
                if current_metadata and current_metadata.version != last_version:
                    logging.info(f"Model {model_name} updated to version {current_metadata.version}")
                    callback(current_metadata)
                    last_version = current_metadata.version
                    
                # Poll every 30 seconds
                stop_event.wait(30)
                
            except Exception as e:
                logging.error(f"Error in model watch: {e}")
                stop_event.wait(60)  # Wait longer on error


def create_model_metadata_from_export(
    model_name: str,
    export_path: str,
    checkpoint_path: str,
    training_type: str,
    metrics: Optional[Dict[str, float]] = None
) -> ModelMetadata:
    """Create ModelMetadata from model export information."""
    
    # Generate version from timestamp
    timestamp = datetime.now().isoformat()
    version = datetime.now().strftime("%Y%m%d-%H%M%S")
    
    # Initialize registry to compute hash
    registry = ModelRegistry()
    model_hash = registry._compute_model_hash(export_path)
    
    return ModelMetadata(
        model_name=model_name,
        version=version,
        export_path=export_path,
        checkpoint_path=checkpoint_path,
        timestamp=timestamp,
        training_type=training_type,
        metrics=metrics or {},
        model_signature_hash=model_hash,
        status='available'
    )


def validate_model_health(export_path: str) -> bool:
    """Validate that a model export is healthy and loadable."""
    try:
        # Check if saved_model.pb exists
        pb_path = os.path.join(export_path, "saved_model.pb")
        if not tf.io.gfile.exists(pb_path):
            logging.error(f"Model file not found: {pb_path}")
            return False
            
        # Try to load the model (quick validation)
        try:
            saved_model = tf.saved_model.load(export_path)
            logging.info(f"Model validation successful for {export_path}")
            return True
        except Exception as load_error:
            logging.error(f"Failed to load model {export_path}: {load_error}")
            return False
            
    except Exception as e:
        logging.error(f"Model health check failed: {e}")
        return False


class ModelUpdateMonitor:
    """Monitor for model updates and handle loading."""
    
    def __init__(self, model_name: str, registry: ModelRegistry):
        self.model_name = model_name
        self.registry = registry
        self.current_model_version = None
        self.stop_event = Event()
        self.update_callbacks = []
        self._monitor_thread = None

    def add_update_callback(self, callback):
        """Add callback to be called when model updates."""
        self.update_callbacks.append(callback)

    def start_monitoring(self):
        """Start monitoring for model updates in background thread."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            logging.warning("Monitor already running")
            return
            
        self._monitor_thread = Thread(
            target=self._monitor_loop,
            name=f"ModelMonitor-{self.model_name}"
        )
        self._monitor_thread.start()
        logging.info(f"Started model monitoring for {self.model_name}")

    def stop_monitoring(self):
        """Stop monitoring."""
        self.stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

    def _monitor_loop(self):
        """Main monitoring loop."""
        def on_model_update(metadata: ModelMetadata):
            """Handle model update notification."""
            if validate_model_health(metadata.export_path):
                logging.info(f"Model {metadata.model_name} v{metadata.version} is healthy")
                for callback in self.update_callbacks:
                    try:
                        callback(metadata)
                    except Exception as e:
                        logging.error(f"Error in update callback: {e}")
            else:
                logging.error(f"Model {metadata.model_name} v{metadata.version} failed health check")

        self.registry.watch_model_updates(
            self.model_name,
            on_model_update,
            self.stop_event
        )