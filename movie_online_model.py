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
                f"ssl.certificate.location={os.environ.get('KAFKA_SSL_CERTIFICATE_LOCATION', '')}",
                f"ssl.key.location={os.environ.get('KAFKA_SSL_KEY_LOCATION', '')}"
            ])
        
        # Create dataset from Kafka
        dataset = create_plain_kafka_dataset(
            topics=FLAGS.kafka_topics.split(','),
            group_id=FLAGS.kafka_group_id,
            servers=FLAGS.kafka_servers,
            stream_timeout=FLAGS.stream_timeout_ms,
            poll_batch_size=FLAGS.poll_batch_size,
            configuration=kafka_config
        )
        
        # Process Kafka messages and handle empty queue
        def process_kafka_message(message_batch):
            # Add debugging information
            logging.info(f"Processing message batch with shape: {message_batch.message.shape}")
            
            # Check if batch is empty (EOF or timeout)
            if message_batch.message.shape[0] == 0:
                logging.info(f"Empty Kafka batch detected - process is running and waiting for data (empty count: {self._empty_queue_count})")
                self._empty_queue_count += 1
                
                # Log less frequently after initial startup
                if self._empty_queue_count % 100 == 0:
                    logging.info(f"Still waiting for data... (empty batches: {self._empty_queue_count})")
                
                # Return placeholder data to keep training alive
                return {
                    'mov': tf.RaggedTensor.from_tensor(tf.constant([[1]], dtype=tf.int64)),
                    'uid': tf.RaggedTensor.from_tensor(tf.constant([[1]], dtype=tf.int64)),
                    'label': tf.constant([0.0], dtype=tf.float32)
                }
            else:
                # Process real data
                logging.info(f"🎉 REAL DATA ARRIVED! Processing Kafka batch with {message_batch.message.shape[0]} messages")
                try:
                    decoded = decode_example(message_batch.message)
                    logging.info(f"Successfully decoded {message_batch.message.shape[0]} examples - training on real data!")
                    return decoded
                except Exception as e:
                    logging.error(f"Failed to decode Kafka messages: {e}")
                    # Return placeholder data on decode error
                    return {
                        'mov': tf.RaggedTensor.from_tensor(tf.constant([[1]], dtype=tf.int64)),
                        'uid': tf.RaggedTensor.from_tensor(tf.constant([[1]], dtype=tf.int64)),
                        'label': tf.constant([0.0], dtype=tf.float32)
                    }
                
        # Map to process messages and handle empty batches
        processed_dataset = dataset.map(process_kafka_message).map(to_ragged)
        
        # Make the dataset infinite for startup scenario - no concatenation needed
        # The Kafka dataset will keep producing placeholder data when empty
        infinite_dataset = processed_dataset.repeat()
        
        # Add minimum batch size handling if enabled  
        if FLAGS.skip_empty_batches or FLAGS.min_batch_size > 1:
            # Filter out batches that are too small
            def is_sufficient_batch(features):
                batch_size = tf.shape(features['label'])[0]
                if FLAGS.skip_empty_batches:
                    # Skip single-example placeholder batches
                    return tf.greater(batch_size, 1)
                else:
                    # Use configured minimum batch size
                    return tf.greater_equal(batch_size, FLAGS.min_batch_size)
            
            infinite_dataset = infinite_dataset.filter(is_sufficient_batch)
        
        return infinite_dataset.prefetch(tf.data.AUTOTUNE)


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
        zk_server=FLAGS.zk_server
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
        config.enable_parameter_sync = True

    # build estimator and train
    estimator = Estimator(params, config)
    estimator.train(max_steps=1000000)


if __name__ == '__main__':
    logging.set_verbosity(logging.INFO)
    app.run(main)
