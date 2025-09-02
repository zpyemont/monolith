"""
Model health monitoring and versioning system for Monolith deployments.

Provides comprehensive model health checks, performance monitoring,
and rollback capabilities for production deployments.
"""

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import threading
from threading import Event, Lock
import hashlib
import os

import tensorflow as tf
from model_registry import ModelRegistry, ModelMetadata


@dataclass
class ModelHealthMetrics:
    """Health metrics for a model deployment."""
    model_name: str
    version: str
    timestamp: str
    latency_p50: float
    latency_p95: float
    latency_p99: float
    error_rate: float
    throughput_rps: float
    memory_usage_mb: float
    prediction_accuracy: Optional[float] = None
    status: str = 'healthy'  # 'healthy', 'degraded', 'unhealthy'


@dataclass
class ModelVersion:
    """Model version with rollback information."""
    version: str
    model_path: str
    deployment_time: str
    health_score: float
    is_active: bool
    rollback_reason: Optional[str] = None


class ModelHealthChecker:
    """Performs health checks on deployed models."""
    
    def __init__(self, model_name: str, health_thresholds: Optional[Dict] = None):
        self.model_name = model_name
        self.health_thresholds = health_thresholds or {
            'max_latency_p95_ms': 500,
            'max_error_rate': 0.05,
            'min_throughput_rps': 10,
            'max_memory_usage_mb': 2048
        }
        
        # Health tracking
        self._metrics_lock = Lock()
        self._recent_metrics: List[ModelHealthMetrics] = []
        self._max_metrics_history = 100
        
    def perform_health_check(self, model, test_inputs: List) -> ModelHealthMetrics:
        """Perform comprehensive health check on a model."""
        start_time = time.time()
        
        # Initialize metrics
        latencies = []
        errors = 0
        successful_predictions = 0
        
        try:
            # Run test predictions
            for test_input in test_inputs:
                pred_start = time.time()
                try:
                    _ = model.predict(test_input)
                    pred_end = time.time()
                    latencies.append((pred_end - pred_start) * 1000)  # Convert to ms
                    successful_predictions += 1
                except Exception as e:
                    logging.warning(f"Health check prediction failed: {e}")
                    errors += 1
                    
            # Calculate metrics
            total_requests = len(test_inputs)
            error_rate = errors / total_requests if total_requests > 0 else 1.0
            
            latency_p50 = self._percentile(latencies, 50) if latencies else 0
            latency_p95 = self._percentile(latencies, 95) if latencies else 0
            latency_p99 = self._percentile(latencies, 99) if latencies else 0
            
            total_time = time.time() - start_time
            throughput_rps = successful_predictions / total_time if total_time > 0 else 0
            
            # Estimate memory usage (simplified)
            memory_usage_mb = self._estimate_memory_usage()
            
            # Determine health status
            status = self._determine_health_status(
                latency_p95, error_rate, throughput_rps, memory_usage_mb
            )
            
            metrics = ModelHealthMetrics(
                model_name=self.model_name,
                version="unknown",  # Will be set by caller
                timestamp=datetime.now().isoformat(),
                latency_p50=latency_p50,
                latency_p95=latency_p95,
                latency_p99=latency_p99,
                error_rate=error_rate,
                throughput_rps=throughput_rps,
                memory_usage_mb=memory_usage_mb,
                status=status
            )
            
            # Store metrics
            with self._metrics_lock:
                self._recent_metrics.append(metrics)
                if len(self._recent_metrics) > self._max_metrics_history:
                    self._recent_metrics.pop(0)
                    
            return metrics
            
        except Exception as e:
            logging.error(f"Health check failed: {e}")
            return ModelHealthMetrics(
                model_name=self.model_name,
                version="unknown",
                timestamp=datetime.now().isoformat(),
                latency_p50=0,
                latency_p95=0,
                latency_p99=0,
                error_rate=1.0,
                throughput_rps=0,
                memory_usage_mb=0,
                status='unhealthy'
            )
    
    def _percentile(self, data: List[float], percentile: int) -> float:
        """Calculate percentile of data."""
        if not data:
            return 0
        sorted_data = sorted(data)
        index = (percentile / 100) * (len(sorted_data) - 1)
        if index.is_integer():
            return sorted_data[int(index)]
        else:
            lower_index = int(index)
            upper_index = lower_index + 1
            if upper_index >= len(sorted_data):
                return sorted_data[lower_index]
            weight = index - lower_index
            return sorted_data[lower_index] * (1 - weight) + sorted_data[upper_index] * weight
    
    def _estimate_memory_usage(self) -> float:
        """Estimate memory usage (simplified implementation)."""
        # This would need to be implemented based on your specific environment
        # For now, return a placeholder value
        return 512.0  # MB
    
    def _determine_health_status(self, latency_p95: float, error_rate: float, 
                                throughput_rps: float, memory_usage_mb: float) -> str:
        """Determine overall health status based on metrics."""
        if (latency_p95 > self.health_thresholds['max_latency_p95_ms'] or
            error_rate > self.health_thresholds['max_error_rate'] or
            throughput_rps < self.health_thresholds['min_throughput_rps'] or
            memory_usage_mb > self.health_thresholds['max_memory_usage_mb']):
            return 'unhealthy'
        elif (latency_p95 > self.health_thresholds['max_latency_p95_ms'] * 0.8 or
              error_rate > self.health_thresholds['max_error_rate'] * 0.5):
            return 'degraded'
        else:
            return 'healthy'
    
    def get_recent_metrics(self, count: int = 10) -> List[ModelHealthMetrics]:
        """Get recent health metrics."""
        with self._metrics_lock:
            return self._recent_metrics[-count:]


class ModelVersionManager:
    """Manages model versions and rollback capabilities."""
    
    def __init__(self, model_name: str, registry: ModelRegistry):
        self.model_name = model_name
        self.registry = registry
        self.versions: Dict[str, ModelVersion] = {}
        self._version_lock = Lock()
        
    def register_version(self, metadata: ModelMetadata, health_score: float = 0.0) -> bool:
        """Register a new model version."""
        try:
            version_info = ModelVersion(
                version=metadata.version,
                model_path=metadata.export_path,
                deployment_time=metadata.timestamp,
                health_score=health_score,
                is_active=False  # Will be activated separately
            )
            
            with self._version_lock:
                self.versions[metadata.version] = version_info
                
            logging.info(f"Registered version {metadata.version} for model {self.model_name}")
            return True
            
        except Exception as e:
            logging.error(f"Failed to register version: {e}")
            return False
    
    def activate_version(self, version: str) -> bool:
        """Activate a specific model version."""
        with self._version_lock:
            if version not in self.versions:
                logging.error(f"Version {version} not found")
                return False
                
            # Deactivate all other versions
            for v in self.versions.values():
                v.is_active = False
                
            # Activate the requested version
            self.versions[version].is_active = True
            logging.info(f"Activated version {version} for model {self.model_name}")
            return True
    
    def get_active_version(self) -> Optional[ModelVersion]:
        """Get the currently active version."""
        with self._version_lock:
            for version_info in self.versions.values():
                if version_info.is_active:
                    return version_info
            return None
    
    def rollback_to_version(self, version: str, reason: str) -> bool:
        """Rollback to a specific version."""
        with self._version_lock:
            if version not in self.versions:
                logging.error(f"Cannot rollback to version {version}: not found")
                return False
                
            # Deactivate current version and mark rollback reason
            current_active = self.get_active_version()
            if current_active:
                current_active.is_active = False
                current_active.rollback_reason = reason
                
            # Activate rollback version
            self.versions[version].is_active = True
            logging.info(f"Rolled back to version {version}: {reason}")
            return True
    
    def get_rollback_candidates(self, max_age_hours: int = 168) -> List[ModelVersion]:
        """Get versions that are viable rollback candidates."""
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        candidates = []
        
        with self._version_lock:
            for version_info in self.versions.values():
                deployment_time = datetime.fromisoformat(version_info.deployment_time.replace('Z', '+00:00'))
                if (deployment_time > cutoff_time and 
                    version_info.health_score > 0.7 and  # Arbitrary threshold
                    not version_info.is_active):
                    candidates.append(version_info)
                    
        return sorted(candidates, key=lambda x: x.health_score, reverse=True)


class ModelMonitoringService:
    """Comprehensive model monitoring service."""
    
    def __init__(self, model_name: str, 
                 monitoring_interval_seconds: int = 300,
                 auto_rollback_enabled: bool = True):
        self.model_name = model_name
        self.monitoring_interval = monitoring_interval_seconds
        self.auto_rollback_enabled = auto_rollback_enabled
        
        # Components
        self.registry = ModelRegistry()
        self.health_checker = ModelHealthChecker(model_name)
        self.version_manager = ModelVersionManager(model_name, self.registry)
        
        # Monitoring state
        self.monitoring_active = False
        self.stop_event = Event()
        self.monitoring_thread = None
        
        # Test data for health checks (should be configured per model)
        self.test_inputs = []  # Will need to be populated with actual test data
        
    def start_monitoring(self):
        """Start continuous monitoring."""
        if self.monitoring_active:
            logging.warning("Monitoring already active")
            return
            
        self.monitoring_active = True
        self.stop_event.clear()
        
        self.monitoring_thread = threading.Thread(
            target=self._monitoring_loop,
            name=f"ModelMonitor-{self.model_name}"
        )
        self.monitoring_thread.start()
        logging.info(f"Started model monitoring for {self.model_name}")
    
    def stop_monitoring(self):
        """Stop monitoring."""
        self.stop_event.set()
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=10)
        self.monitoring_active = False
        logging.info(f"Stopped model monitoring for {self.model_name}")
    
    def _monitoring_loop(self):
        """Main monitoring loop."""
        while not self.stop_event.wait(self.monitoring_interval):
            try:
                self._perform_monitoring_cycle()
            except Exception as e:
                logging.error(f"Error in monitoring cycle: {e}")
                # Continue monitoring despite errors
    
    def _perform_monitoring_cycle(self):
        """Perform one monitoring cycle."""
        # Get current model metadata
        metadata = self.registry.get_model_metadata(self.model_name)
        if not metadata:
            logging.warning(f"No metadata found for model {self.model_name}")
            return
            
        # Register version if not already registered
        if metadata.version not in self.version_manager.versions:
            self.version_manager.register_version(metadata)
            
        # Note: Actual health checking would require access to the loaded model
        # This is a placeholder for the monitoring logic
        logging.info(f"Monitoring cycle completed for {self.model_name}")
    
    def force_rollback(self, target_version: Optional[str] = None, reason: str = "Manual rollback"):
        """Force rollback to a specific version or best available."""
        if target_version:
            success = self.version_manager.rollback_to_version(target_version, reason)
            if success:
                logging.info(f"Successfully rolled back to version {target_version}")
            else:
                logging.error(f"Failed to rollback to version {target_version}")
        else:
            # Find best rollback candidate
            candidates = self.version_manager.get_rollback_candidates()
            if candidates:
                best_candidate = candidates[0]
                success = self.version_manager.rollback_to_version(best_candidate.version, reason)
                if success:
                    logging.info(f"Successfully rolled back to best candidate: {best_candidate.version}")
            else:
                logging.error("No rollback candidates available")


def create_test_inputs_for_movie_model() -> List[Dict]:
    """Create test inputs for movie recommendation model health checks."""
    # This should be customized based on your model's input format
    test_examples = []
    
    # Create some dummy test examples
    for i in range(5):
        example = tf.train.Example()
        example.features.feature['mov'].int64_list.value.append(i + 1)
        example.features.feature['uid'].int64_list.value.append(i + 100)
        example.features.feature['label'].float_list.value.append(4.0)
        
        serialized = example.SerializeToString()
        test_examples.append({"examples": tf.constant([serialized])})
    
    return test_examples