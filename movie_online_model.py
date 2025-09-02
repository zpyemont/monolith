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
# Import ZooKeeper model registration utilities
from monolith.agent_service.agent_controller import declare_saved_model
from monolith.agent_service.backends import ZKBackend

# Kafka flags - using existing Monolith framework flags where available
# kafka_topics is already defined in monolith.native_training.gflags_utils
flags.DEFINE_string('kafka_group_id', 'movie-training-group', 'Kafka consumer group ID')
flags.DEFINE_string('kafka_servers', 'localhost:9092', 'Kafka broker servers')
flags.DEFINE_string('kafka_username', '', 'Kafka SASL username (for Confluent Cloud)')
flags.DEFINE_string('kafka_password', '', 'Kafka SASL password (for Confluent Cloud)')
flags.DEFINE_string('kafka_security_protocol', 'PLAINTEXT', 'Security protocol: PLAINTEXT, SASL_SSL, etc.')
flags.DEFINE_enum('training_type', 'online', ['batch', 'online', 'stdin'],
                  'Type of training to launch')
flags.DEFINE_integer('stream_timeout_ms', 86400000, 'Timeout for Kafka stream operations in milliseconds (24 hours for startup scenario)')
flags.DEFINE_integer('poll_batch_size', 1000, 'Number of records to poll from Kafka at once')
flags.DEFINE_boolean('skip_empty_queue', False, 'Skip training when Kafka queue is empty (False for startup scenario)')
flags.DEFINE_boolean('skip_empty_batches', False, 'Skip training steps when batch is empty (False for startup scenario)')
flags.DEFINE_integer('min_batch_size', 1, 'Minimum batch size required for training (1 for startup scenario)')
# BigQuery options for batch mode (optional; requires tensorflow-io)
flags.DEFINE_string('bq_project', '', 'GCP project for BigQuery table')
flags.DEFINE_string('bq_dataset', '', 'BigQuery dataset')
flags.DEFINE_string('bq_table', '', 'BigQuery table')
flags.DEFINE_string('bq_location', 'US', 'BigQuery dataset location')
flags.DEFINE_string('bq_selected_fields', 'mov,uid,label', 'Comma-separated columns to read')
flags.DEFINE_string('bq_row_restriction', '', 'Optional SQL row restriction (WHERE clause without WHERE)')
flags.DEFINE_integer('bq_parallelism', 4, 'Parallel readers for BigQuery Storage API')
flags.DEFINE_string('bq_mov_col', 'mov', 'Column name for movie id')
flags.DEFINE_string('bq_uid_col', 'uid', 'Column name for user id')
flags.DEFINE_string('bq_label_col', 'label', 'Column name for label')
FLAGS = flags.FLAGS

# We'll track empty queue status at the object level instead of globally

def get_worker_count(env: dict):
    cluster = env.get('cluster', {})
    return len(cluster.get('worker', [])) + len(cluster.get('chief', []))


def register_model_after_training():
    """Register the trained model with ZooKeeper for serving."""
    try:
        # Initialize ZooKeeper backend
        zk_servers = os.environ.get('ZK_SERVERS', 'monolith-zookeeper-client.default.svc.cluster.local:2181')
        bd = ZKBackend("monolith_serving_test", zk_servers)
        bd.start()
        
        try:
            # Find the exported model directory
            export_base = "/checkpoints/movie_lens_tutorial/exported_models"
            if tf.io.gfile.exists(export_base):
                # Declare the saved model in ZooKeeper
                model_name = declare_saved_model(
                    bd=bd,
                    export_base=export_base,
                    model_name="movie_lens_tutorial",
                    overwrite=True,
                    arch="entry_ps"
                )
                logging.info(f"Successfully registered model {model_name} in ZooKeeper")
            else:
                logging.error(f"Export directory {export_base} does not exist")
                
        finally:
            bd.stop()
            
    except Exception as e:
        logging.error(f"Failed to register model in ZooKeeper: {e}")
        # Don't fail the training job if registration fails


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
        # Expose features used for decision to a separate serving signature for Flink join
        # Call signature_name="features_for_join" from your Python proxy to fetch these
        user_embedding_out = tf.identity(user_emb, name="user_embedding")
        product_embedding_out = tf.identity(mov_emb, name="product_embedding")
        score_out = tf.identity(preds, name="score")
        self.add_extra_output(
            name="features_for_join",
            outputs={
                "user_embedding": user_embedding_out,
                "product_embedding": product_embedding_out,
                "score": score_out,
            },
        )
        
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
        # Prefer BigQuery if configured
        if FLAGS.bq_project and FLAGS.bq_dataset and FLAGS.bq_table:
            try:
                from tensorflow_io.bigquery import BigQueryClient
                client = BigQueryClient()
                parent = f"projects/{FLAGS.bq_project}/locations/{FLAGS.bq_location}"
                selected_fields = [s.strip() for s in FLAGS.bq_selected_fields.split(',') if s.strip()]
                output_types = []
                for col in selected_fields:
                    if col == FLAGS.bq_mov_col or col == FLAGS.bq_uid_col:
                        output_types.append(tf.int64)
                    elif col == FLAGS.bq_label_col:
                        output_types.append(tf.float32)
                    else:
                        output_types.append(tf.float32)

                read_session = client.read_session(
                    parent=parent,
                    project_id=FLAGS.bq_project,
                    table_id=FLAGS.bq_table,
                    dataset_id=FLAGS.bq_dataset,
                    selected_fields=selected_fields,
                    row_restriction=FLAGS.bq_row_restriction or None,
                    output_types=output_types,
                    requested_streams=FLAGS.bq_parallelism,
                )
                ds = read_session.parallel_read_rows()

                def _row_to_example(*cols):
                    row = dict(zip(selected_fields, cols))
                    mov = tf.cast(row[FLAGS.bq_mov_col], tf.int64)
                    uid = tf.cast(row[FLAGS.bq_uid_col], tf.int64)
                    label = tf.cast(row[FLAGS.bq_label_col], tf.float32)
                    return {'mov': tf.reshape(mov, [1]), 'uid': tf.reshape(uid, [1]), 'label': label}

                return ds.map(_row_to_example).batch(512, drop_remainder=True).map(to_ragged).prefetch(tf.data.AUTOTUNE)
            except Exception as e:
                logging.warning(f"BigQuery input requested but unavailable ({e}); falling back to demo dataset")

        env = json.loads(os.environ.get('TF_CONFIG', '{}'))
        dataset = get_preprocessed_dataset('1m')
        dataset = dataset.shard(get_worker_count(env), env.get('task', {}).get('index', 0))
        return dataset.batch(512, drop_remainder=True).map(to_ragged).prefetch(tf.data.AUTOTUNE)


class MovieRankingOnlineTraining(MovieRankingModelBase):
    def __init__(self, params):
        super().__init__(params)
        self._empty_queue_count = 0
        
    def input_fn(self, mode):
        # For MVP: Fall back to demo dataset if Kafka not available
        # This allows testing model propagation without setting up Kafka
        try:
            # Get Confluent Kafka configuration from environment (set by Kubernetes)
            kafka_bootstrap_servers = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', FLAGS.kafka_servers)
            confluent_api_key = os.environ.get('CONFLUENT_API_KEY', FLAGS.kafka_username)
            confluent_api_secret = os.environ.get('CONFLUENT_API_SECRET', FLAGS.kafka_password)
            kafka_topic = os.environ.get('KAFKA_TOPIC', FLAGS.kafka_topics or 'movie-training')
            kafka_group_id = os.environ.get('KAFKA_GROUP_ID', FLAGS.kafka_group_id)
            
            # Try Kafka first if configured
            if kafka_bootstrap_servers and kafka_bootstrap_servers != 'localhost:9092' and confluent_api_key:
                kafka_config = [
                    "security.protocol=SASL_SSL",
                    "sasl.mechanism=PLAIN",
                    f"sasl.username={confluent_api_key}",
                    f"sasl.password={confluent_api_secret}",
                    "ssl.endpoint.identification.algorithm=https"
                ]
                
                logging.info(f"Connecting to Confluent Kafka: {kafka_bootstrap_servers}, topic: {kafka_topic}")
                
                # Create dataset from Kafka
                dataset = create_plain_kafka_dataset(
                    topics=[kafka_topic],
                    group_id=kafka_group_id,
                    servers=kafka_bootstrap_servers,
                    stream_timeout=FLAGS.stream_timeout_ms,
                    poll_batch_size=16,  # Match demo size
                    configuration=kafka_config
                )
                
                return dataset.map(lambda x: decode_example(x.message)).map(to_ragged)
            else:
                raise Exception("Using demo dataset fallback")
                
        except Exception as e:
            logging.warning(f"Kafka not available ({e}); using demo dataset for online training")
            
            # Fall back to demo dataset (same as batch training)
            env = json.loads(os.environ.get('TF_CONFIG', '{}'))
            dataset = get_preprocessed_dataset('1m')
            dataset = dataset.shard(get_worker_count(env), env.get('task', {}).get('index', 0))
            return dataset.repeat().batch(16, drop_remainder=True).map(to_ragged).prefetch(tf.data.AUTOTUNE)


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


## ZooKeeper-based registration removed (switching to TF_CONFIG/Kubernetes discovery)


def main(_argv):
    tf.compat.v1.disable_eager_execution()

    # ZooKeeper-specific environment handling removed

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
        base_name="movie_lens",
        bzid="monolith_serving_test",
        # Use in-cluster ZooKeeper for model registration and realtime training
        zk_server=os.environ.get('ZK_SERVERS', 'monolith-zookeeper-client.default.svc.cluster.local:2181')
    )

    # instantiate model params based on training type
    if FLAGS.training_type == 'batch':
        params = MovieRankingBatchTraining.params().instantiate()
    elif FLAGS.training_type == 'stdin':
        params = MovieRankingBatchStdin.params().instantiate()
    else:
        # online streaming mode
        params = MovieRankingOnlineTraining.params().instantiate()
        # Enable real-time parameter sync to serving PS
        config.enable_realtime_training = True

    # build estimator and train
    estimator = Estimator(params, config)
    
    estimator.train(max_steps=1000000)

    # Export the final model for serving
    if FLAGS.training_type == 'batch':
        logging.info("Batch training completed. Exporting model for serving...")
        estimator.export_saved_model(
            batch_size=64,
            name="movie_lens_model",
            dense_only=False
        )
        logging.info("Model export completed.")
        
        # Register model with ZooKeeper - only primary worker should register
        tf_conf = json.loads(os.environ.get('TF_CONFIG', '{}'))
        task_type = tf_conf.get('task', {}).get('type', '')
        task_index = tf_conf.get('task', {}).get('index', 0)
        
        if task_type == 'worker' and task_index == 0:
            logging.info("Primary worker - registering model with ZooKeeper")
            register_model_after_training()
        elif not task_type:
            logging.info("Single-node training - registering model with ZooKeeper")
            register_model_after_training()
        else:
            logging.info(f"Worker {task_type}:{task_index} - skipping model registration")


if __name__ == '__main__':
    logging.set_verbosity(logging.INFO)
    app.run(main)
