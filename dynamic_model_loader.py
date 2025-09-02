"""
Dynamic model loading utilities for Monolith online training.

Provides functionality to dynamically load and switch models during
online training without restarting the training process.
"""

import logging
import os
import threading
from typing import Optional, Callable, Any
from threading import Event, Lock
import time

import tensorflow as tf
from model_registry import ModelRegistry, ModelMetadata, ModelUpdateMonitor


class DynamicModelLoader:
    """Handles dynamic loading and switching of models during online training."""
    
    def __init__(self, 
                 model_name: str, 
                 initial_model_path: Optional[str] = None,
                 warmup_requests: int = 5,
                 model_switch_callback: Optional[Callable[[str, str], None]] = None):
        """
        Initialize dynamic model loader.
        
        Args:
            model_name: Name of the model to monitor
            initial_model_path: Path to initial model (optional)
            warmup_requests: Number of warmup requests after model load
            model_switch_callback: Called when model switches (old_path, new_path)
        """
        self.model_name = model_name
        self.warmup_requests = warmup_requests
        self.model_switch_callback = model_switch_callback
        
        # Thread safety
        self._model_lock = Lock()
        self._current_model = None
        self._current_model_path = initial_model_path
        self._current_model_version = None
        
        # Model monitoring
        self.registry = ModelRegistry()
        self.monitor = ModelUpdateMonitor(model_name, self.registry)
        self.monitor.add_update_callback(self._on_model_update)
        
        # Load initial model if provided
        if initial_model_path and tf.io.gfile.exists(initial_model_path):
            self._load_model(initial_model_path, "initial")
            
        # Start monitoring
        self.monitor.start_monitoring()
        logging.info(f"Dynamic model loader initialized for {model_name}")

    def _load_model(self, model_path: str, version: str) -> bool:
        """Load a model from path."""
        try:
            logging.info(f"Loading model from {model_path}")
            
            # Load the SavedModel
            new_model = tf.saved_model.load(model_path)
            
            # Run warmup if needed
            self._warmup_model(new_model, model_path)
            
            # Thread-safe model switch
            with self._model_lock:
                old_path = self._current_model_path
                self._current_model = new_model
                self._current_model_path = model_path
                self._current_model_version = version
                
                # Call switch callback if provided
                if self.model_switch_callback and old_path:
                    try:
                        self.model_switch_callback(old_path, model_path)
                    except Exception as callback_error:
                        logging.error(f"Model switch callback error: {callback_error}")
            
            logging.info(f"Successfully loaded model {self.model_name} v{version} from {model_path}")
            return True
            
        except Exception as e:
            logging.error(f"Failed to load model from {model_path}: {e}")
            return False

    def _warmup_model(self, model, model_path: str):
        """Perform model warmup with dummy data."""
        try:
            # Try to find a prediction function
            predict_fn = None
            if hasattr(model, 'signatures'):
                if 'serving_default' in model.signatures:
                    predict_fn = model.signatures['serving_default']
                elif len(model.signatures) > 0:
                    # Use first available signature
                    predict_fn = list(model.signatures.values())[0]
            
            if predict_fn:
                # Create dummy input matching expected signature
                try:
                    # This is a simple warmup - in production you might want 
                    # to use actual sample data that matches your model's input format
                    dummy_input = tf.constant(["dummy_example"] * 1)  # Batch of serialized examples
                    
                    for i in range(self.warmup_requests):
                        _ = predict_fn(examples=dummy_input)
                        
                    logging.info(f"Model warmup completed for {model_path}")
                except Exception as warmup_error:
                    logging.warning(f"Model warmup failed (non-critical): {warmup_error}")
            else:
                logging.warning("No suitable signature found for warmup")
                
        except Exception as e:
            logging.warning(f"Model warmup error (non-critical): {e}")

    def _on_model_update(self, metadata: ModelMetadata):
        """Handle model update notification from registry."""
        logging.info(f"Received model update notification: {metadata.model_name} v{metadata.version}")
        
        # Skip if same version
        if metadata.version == self._current_model_version:
            logging.info("Model version unchanged, skipping update")
            return
            
        # Load the new model
        success = self._load_model(metadata.export_path, metadata.version)
        if not success:
            logging.error(f"Failed to load updated model {metadata.model_name} v{metadata.version}")

    def get_model(self):
        """Get the current model (thread-safe)."""
        with self._model_lock:
            return self._current_model

    def get_model_info(self) -> tuple:
        """Get current model path and version."""
        with self._model_lock:
            return self._current_model_path, self._current_model_version

    def stop_monitoring(self):
        """Stop monitoring for model updates."""
        self.monitor.stop_monitoring()
        logging.info(f"Stopped model monitoring for {self.model_name}")

    def force_reload(self) -> bool:
        """Force reload from registry (for manual triggers)."""
        metadata = self.registry.get_model_metadata(self.model_name)
        if metadata:
            return self._load_model(metadata.export_path, metadata.version)
        else:
            logging.warning(f"No model metadata found for {self.model_name}")
            return False


class ModelServingWrapper:
    """Wrapper that provides serving functionality with dynamic model loading."""
    
    def __init__(self, model_name: str, initial_model_path: Optional[str] = None):
        self.model_name = model_name
        self.loader = DynamicModelLoader(
            model_name=model_name,
            initial_model_path=initial_model_path,
            model_switch_callback=self._on_model_switch
        )
        self._serving_stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'model_switches': 0,
            'current_model_version': None
        }
        
    def _on_model_switch(self, old_path: str, new_path: str):
        """Handle model switch event."""
        self._serving_stats['model_switches'] += 1
        self._serving_stats['current_model_version'] = self.loader._current_model_version
        logging.info(f"Model switch completed: {old_path} -> {new_path}")

    def predict(self, inputs):
        """Make prediction with current model."""
        self._serving_stats['total_requests'] += 1
        
        try:
            model = self.loader.get_model()
            if model is None:
                raise RuntimeError("No model loaded")
                
            # Use the serving signature
            if hasattr(model, 'signatures'):
                if 'serving_default' in model.signatures:
                    predict_fn = model.signatures['serving_default']
                    result = predict_fn(**inputs)
                    self._serving_stats['successful_requests'] += 1
                    return result
                else:
                    raise RuntimeError("No serving signature found")
            else:
                raise RuntimeError("Model has no signatures")
                
        except Exception as e:
            logging.error(f"Prediction failed: {e}")
            raise

    def get_stats(self) -> dict:
        """Get serving statistics."""
        model_path, model_version = self.loader.get_model_info()
        return {
            **self._serving_stats,
            'current_model_path': model_path,
            'current_model_version': model_version
        }

    def stop(self):
        """Stop the serving wrapper."""
        self.loader.stop_monitoring()


def create_model_serving_hook(model_name: str, 
                            estimator,
                            serving_wrapper: ModelServingWrapper) -> Any:
    """Create a training hook that integrates with dynamic model loading."""
    
    class DynamicModelServingHook(tf.estimator.SessionRunHook):
        """Training hook that enables dynamic model serving during training."""
        
        def __init__(self, model_name: str, serving_wrapper: ModelServingWrapper):
            self.model_name = model_name
            self.serving_wrapper = serving_wrapper
            
        def begin(self):
            """Called once before using the session."""
            logging.info(f"Dynamic model serving hook initialized for {self.model_name}")
            
        def after_create_session(self, session, coord):
            """Called after session is created."""
            pass
            
        def before_run(self, run_context):
            """Called before each run."""
            return None
            
        def after_run(self, run_context, run_values):
            """Called after each run."""
            pass
            
        def end(self, session):
            """Called at the end of session."""
            self.serving_wrapper.stop()
            logging.info(f"Dynamic model serving hook ended for {self.model_name}")
    
    return DynamicModelServingHook(model_name, serving_wrapper)