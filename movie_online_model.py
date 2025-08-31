from absl import app
from absl import flags
from absl import logging

import json
import os
import time
import sys

import tensorflow as tf
from kafka_receiver import decode_example, to_ragged
from ml_dataset import get_preprocessed_dataset
from monolith.native_training.estimator import EstimatorSpec, Estimator
from monolith.native_training.runner_utils import RunnerConfig
from monolith.native_training.service_discovery import ServiceDiscoveryType
from monolith.native_training.native_model import MonolithModel
from monolith.native_training.data.datasets import create_plain_kafka_dataset
# Import ExportMode for configuring export mode
from monolith.native_training.model_export.export_context import ExportMode
# Import Monolith operations to ensure they are loaded during training and export
from monolith.native_training.runtime.ops import gen_monolith_ops

# Import agent controller for model registration
try:
    from monolith.agent_service.backends import ZKBackend
    # Import specific functions to avoid flag conflicts
    from monolith.agent_service.agent_controller import declare_saved_model, map_model_to_layout
    AGENT_CONTROLLER_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Agent controller not available: {e}")
    AGENT_CONTROLLER_AVAILABLE = False

#kafka flags are already defined by absl
flags.DEFINE_enum('training_type', 'online', ['batch', 'online', 'stdin'],
                  'Type of training to launch')
flags.DEFINE_boolean('zk_use_ssl', False, 'Whether to use SSL for Kafka connection')
flags.DEFINE_string('zk_trust_file', '', 'Path to the ZooKeeper CA certificate file')
flags.DEFINE_integer('stream_timeout_ms', 86400000, 'Timeout for Kafka stream operations in milliseconds (24 hours for startup scenario)')
flags.DEFINE_integer('poll_batch_size', 1000, 'Number of records to poll from Kafka at once')
flags.DEFINE_boolean('skip_empty_queue', False, 'Skip training when Kafka queue is empty (False for startup scenario)')
flags.DEFINE_boolean('skip_empty_batches', False, 'Skip training steps when batch is empty (False for startup scenario)')
flags.DEFINE_integer('min_batch_size', 1, 'Minimum batch size required for training (1 for startup scenario)')
FLAGS = flags.FLAGS

# We'll track empty queue status at the object level instead of globally

def get_worker_count(env: dict):
    cluster = env.get('cluster', {})
    return len(cluster.get('worker', [])) + len(cluster.get('chief', []))


class MovieRankingModelBase(MonolithModel):
    def __init__(self, params):
        super().__init__(params)
        # enable export on checkpoint save for serving
        self.p.serving.export_when_saving = True
        # Let Monolith automatically choose the appropriate export mode

    def model_fn(self, features, mode):
        # declare embedding tables for sparse features
        for s_name in ['mov', 'uid']:
            self.create_embedding_feature_column(s_name, occurrence_threshold=0)

        mov_emb, user_emb = self.lookup_embedding_slice(
            features=['mov', 'uid'], slice_name='vec', slice_dim=32)

        # simple MLP regressor for rating
        mlp = tf.keras.Sequential([
            tf.keras.layers.Dense(256, activation='relu'),
            tf.keras.layers.Dense(64, activation='relu'),
            tf.keras.layers.Dense(1)
        ])
        concat = tf.concat([user_emb, mov_emb], axis=1)
        preds = mlp(concat)
        label = features['label']
        
        # Simplified approach: just compute loss with NaN safeguards
        # Skip complex placeholder detection for now to avoid tensor type issues
        loss = tf.reduce_mean(tf.losses.mean_squared_error(preds, label))
        
        # Add safeguard against NaN loss
        loss = tf.where(tf.math.is_nan(loss), tf.constant(0.0, dtype=tf.float32), loss)

        opt = tf.compat.v1.train.AdagradOptimizer(0.05)

        return EstimatorSpec(
            label=label,
            pred=preds,
            head_name='rank',
            loss=loss,
            optimizer=opt,
            classification=False)

    def serving_input_receiver_fn(self):
        # receive serialized tf.Example strings
        input_ph = tf.compat.v1.placeholder(dtype=tf.string, shape=[None])
        raw_desc = {
            'mov': tf.io.FixedLenFeature([1], tf.int64),
            'uid': tf.io.FixedLenFeature([1], tf.int64),
            'label': tf.io.FixedLenFeature([], tf.float32),
        }
        parsed = tf.io.parse_example(input_ph, raw_desc)
        features = {
            'mov': tf.RaggedTensor.from_tensor(parsed['mov']),
            'uid': tf.RaggedTensor.from_tensor(parsed['uid']),
            'label': parsed['label'],
        }
        return tf.estimator.export.ServingInputReceiver(features, {'examples': input_ph})


class MovieRankingBatchTraining(MovieRankingModelBase):
    def input_fn(self, mode):
        env = json.loads(os.environ.get('TF_CONFIG', '{}'))
        dataset = get_preprocessed_dataset('1m')
        # shard across workers for distributed training
        dataset = dataset.shard(get_worker_count(env), env.get('task', {}).get('index', 0))
        return dataset.batch(512, drop_remainder=True).map(to_ragged).prefetch(tf.data.AUTOTUNE)


class MovieRankingOnlineTraining(MovieRankingModelBase):
    def __init__(self, params):
        super().__init__(params)
        self._empty_queue_count = 0
        
    def input_fn(self, mode):
        # consume real-time training examples from Kafka
        # Build Kafka configuration with SSL settings
        kafka_config = [
            f"security.protocol={os.environ.get('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT')}",
        ]
        
        # Add SSL config if using SSL
        if os.environ.get('KAFKA_SECURITY_PROTOCOL') == 'SSL':
            kafka_config.extend([
                f"ssl.ca.location={os.environ.get('KAFKA_SSL_CA_LOCATION', '')}",
                f"ssl.certificate.location={os.environ.get('KAFKA_SSL_CA_LOCATION', '')}",
                f"ssl.certificate.location={os.environ.get('KAFKA_SSL_CERTIFICATE_LOCATION', '')}",
                f"ssl.key.location={os.environ.get('KAFKA_SSL_KEY_LOCATION', '')}"
            ])
        
        # Create dataset from Kafka
        dataset = create_plain_kafka_dataset(
            topics=FLAGS.kafka_topics.split(','),
            group_id=FLAGS.kafka_group_id,
            servers=FLAGS.kafka_servers,
            stream_timeout=FLAGS.stream_timeout_ms,
            poll_batch_size=16,  # Match demo size
            configuration=kafka_config
        )
        
        # Kafka provides correct format - use original decode_example  
        return dataset.map(lambda x: decode_example(x.message)).map(to_ragged)


class MovieRankingBatchStdin(MovieRankingModelBase):
    def input_fn(self, mode):
        def gen():
            for line in sys.stdin:
                mov, uid, label = line.strip().split(',')
                yield {'mov': [int(mov)], 'uid': [int(uid)], 'label': float(label)}
        return tf.data.Dataset.from_generator(
            gen,
            output_signature={
                'mov': tf.TensorSpec([1], tf.int64),
                'uid': tf.TensorSpec([1], tf.int64),
                'label': tf.TensorSpec([], tf.float32)
            }
        ).batch(512, drop_remainder=True).map(to_ragged).prefetch(tf.data.AUTOTUNE)


def register_model_after_training(model_name="movie_lens_tutorial", 
                                  export_base="/checkpoints/movie_lens_tutorial/exported_models", 
                                  layout="test", 
                                  bzid="monolith_serving_test"):
    """Register the trained model with ZooKeeper for serving."""
    if not AGENT_CONTROLLER_AVAILABLE:
        logging.warning("Agent controller not available - skipping model registration")
        return False
        
    try:
        # Get ZooKeeper server from FLAGS
        zk_servers = FLAGS.zk_server
        logging.info(f"Registering model {model_name} with ZooKeeper at {zk_servers}")
        
        # Create ZK backend with proper TLS configuration
        bd = ZKBackend(bzid, zk_servers)
        bd.start()
        
        try:
            # Declare the saved models
            logging.info(f"Declaring saved models from {export_base}")
            declare_saved_model(
                bd, 
                export_base, 
                model_name,
                overwrite=True,
                arch="entry_ps"
            )
            
            # List declared models for verification
            saved_models = bd.list_saved_models(model_name)
            logging.info(f"Found saved models: {saved_models}")
            
            # Publish models to layout
            layout_path = f"/{bzid}/layouts/{layout}"
            logging.info(f"Publishing models to layout {layout_path}")
            
            map_model_to_layout(
                bd, 
                f"{model_name}:*", 
                layout_path, 
                action="pub"
            )
            
            logging.info("✅ Model registration completed successfully!")
            return True
            
        finally:
            bd.stop()
            
    except Exception as e:
        logging.error(f"❌ Failed to register model: {e}")
        import traceback
        traceback.print_exc()
        return False


def main(_argv):
    tf.compat.v1.disable_eager_execution()

    # Set environment variables from flags for zk_utils
    if FLAGS.zk_use_ssl:
        os.environ['ZK_USE_SSL'] = 'true'
    if FLAGS.zk_trust_file:
        os.environ['ZK_TRUST_FILE'] = FLAGS.zk_trust_file
        logging.info(f"ZooKeeper trust file path: {FLAGS.zk_trust_file}")
        try:
            with open(FLAGS.zk_trust_file, 'r') as f:
                cert_content = f.read()
                logging.info(f"ZooKeeper trust file content:\n{cert_content}")
        except Exception as e:
            logging.error(f"Failed to read ZooKeeper trust file: {e}")

    # load TF_CONFIG for cluster setup
    raw_tf_conf = os.environ.get('TF_CONFIG', '{}')
    try:
        tf_conf = json.loads(raw_tf_conf)
    except json.JSONDecodeError:
        tf_conf = {}

    # override index if running in k8s pod by POD_NAME
    pod = os.environ.get('POD_NAME')
    if pod:
        try:
            idx = int(pod.rsplit('-', 1)[-1])
            tf_conf.setdefault('task', {})['index'] = idx
            raw_tf_conf = json.dumps(tf_conf)
            logging.info('Overriding TF_CONFIG index with %d', idx)
        except ValueError:
            pass

    # build runner config
    logging.info(f"FLAGS.training_type: {FLAGS.training_type}")
    config = RunnerConfig(
        discovery_type=ServiceDiscoveryType.ZK,
        tf_config=raw_tf_conf,
        save_checkpoints_steps=10000,
        enable_model_ckpt_info=True,
        num_ps=len(tf_conf.get('cluster', {}).get('ps', [])),
        num_workers=get_worker_count(tf_conf),
        server_type=tf_conf.get('task', {}).get('type', ''),
        index=tf_conf.get('task', {}).get('index', 0), 
        zk_server=FLAGS.zk_server, 
        base_name="movie_lens",
        bzid="monolith_serving_test"
    )

    # instantiate model params based on training type
    if FLAGS.training_type == 'batch':
        params = MovieRankingBatchTraining.params().instantiate()
    elif FLAGS.training_type == 'stdin':
        params = MovieRankingBatchStdin.params().instantiate()
    else:
        # online streaming mode
        params = MovieRankingOnlineTraining.params().instantiate()
        # enable real-time parameter sync to serving PS
        config.enable_realtime_training = True

    # build estimator and train
    estimator = Estimator(params, config)
    estimator.train(max_steps=1000000)

    # Register model after batch training completes (only for primary worker)
    if FLAGS.training_type == 'batch':
        # Only register from one worker to avoid conflicts
        task_type = tf_conf.get('task', {}).get('type', '')
        task_index = tf_conf.get('task', {}).get('index', 0)
        
        # Register models from worker:0 or if no distributed setup
        if task_type == 'worker' and task_index == 0:
            logging.info("Primary worker - registering model with ZooKeeper")
            register_model_after_training()
        elif not task_type:  # single-node training
            logging.info("Single-node training - registering model with ZooKeeper")
            register_model_after_training()
        else:
            logging.info(f"Worker {task_type}:{task_index} - skipping model registration (primary worker will handle it)")
    else:
        logging.info(f"Training type {FLAGS.training_type} - skipping model registration")


if __name__ == '__main__':
    logging.set_verbosity(logging.INFO)
    app.run(main)
