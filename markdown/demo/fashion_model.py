from absl import app
from absl import flags
from absl import logging

import json
import os
import sys

import tensorflow as tf
from kafka_receiver import decode_example, to_ragged
from ml_dataset import get_preprocessed_dataset

from monolith.native_training.estimator import EstimatorSpec, Estimator, RunnerConfig, ServiceDiscoveryType
from monolith.native_training.native_model import MonolithModel
from monolith.native_training.data.datasets import create_plain_kafka_dataset

# Define training type flags: "batch", "stream", or "stdin"
flags.DEFINE_enum('training_type', 'batch', ['batch', 'stream', 'stdin'], "type of training to launch")
FLAGS = flags.FLAGS

def get_worker_count(env: dict):
  cluster = env['cluster']
  worker_count = len(cluster.get('worker', [])) + len(cluster.get('chief', []))
  assert worker_count > 0
  return worker_count

class FashionModelBase(MonolithModel):
  def __init__(self, params):
    super().__init__(params)
    self.p = params
    # Ensure that the model is exported when saving
    self.p.serving.export_when_saving = True

  def model_fn(self, features, mode):
    # Create embedding feature columns for the sparse categorical features:
    # product (prod_id), user (uid), brand, category, and device.
    for s_name in ["prod_id", "uid", "brand", "category", "device"]:
      self.create_embedding_feature_column(s_name, occurrence_threshold=0)
      
    # Lookup embeddings for the sparse features
    # Assuming self.lookup_embedding_slice can take a list of feature names in order
    feature_list = ['prod_id', 'uid', 'brand', 'category', 'device']
    embeddings = self.lookup_embedding_slice(features=feature_list, slice_name='vec', slice_dim=32)
    
    # Unpack embeddings: each will be a tensor of shape [batch_size, 32]
    prod_id_embedding      = embeddings[0]
    user_embedding     = embeddings[1]
    brand_embedding    = embeddings[2]
    category_embedding = embeddings[3]
    device_embedding   = embeddings[4]

    # Extract continuous / dense features:
    # Expand dims of scalars to form 2D tensors of shape [batch, 1]
    price = tf.expand_dims(features['price'], axis=1)             # price: [batch, 1]
    time_of_day = tf.expand_dims(features['time_of_day'], axis=1)   # time_of_day: [batch, 1]
    # Reshape precomputed dense embeddings to ensure proper dimensions:
    visual_embed = tf.reshape(features['visual_embed'], [-1, 128])  # visual features: [batch, 128]
    text_embed = tf.reshape(features['text_embed'], [-1, 64])       # text features: [batch, 64]

    # Concatenate all features into one final input vector.
    # The final vector includes:
    # user_embedding (32), prod_id_embedding (32), brand_embedding (32), category_embedding (32),
    # device_embedding (32), price (1), time_of_day (1), visual_embed (128), text_embed (64)
    concat_features = tf.concat([
         user_embedding, prod_id_embedding, brand_embedding,
         category_embedding, device_embedding, price, time_of_day,
         visual_embed, text_embed
    ], axis=1)

    # Build a deeper ranking network
    x = tf.keras.layers.Dense(256, activation='relu')(concat_features)
    x = tf.keras.layers.Dense(128, activation='relu')(x)
    x = tf.keras.layers.Dense(64, activation='relu')(x)
    rank = tf.keras.layers.Dense(1)(x)

    label = features['label']
    loss = tf.reduce_mean(tf.losses.mean_squared_error(rank, label))
    optimizer = tf.compat.v1.train.AdagradOptimizer(0.05)

    return EstimatorSpec(
      label=label,
      pred=rank,
      head_name="rank",
      loss=loss, 
      optimizer=optimizer,
      classification=False
    )
  
  def serving_input_receiver_fn(self):
    input_placeholder = tf.compat.v1.placeholder(dtype=tf.string, shape=(None,))
    receiver_tensors = {'examples': input_placeholder}
    # Update raw_feature_desc to include the additional features.
    raw_feature_desc = {
      'prod_id': tf.io.FixedLenFeature([1], tf.int64),
      'uid': tf.io.FixedLenFeature([1], tf.int64),
      'brand': tf.io.FixedLenFeature([1], tf.int64),
      'category': tf.io.FixedLenFeature([1], tf.int64),
      'price': tf.io.FixedLenFeature([], tf.float32),
      'time_of_day': tf.io.FixedLenFeature([], tf.float32),
      'device': tf.io.FixedLenFeature([1], tf.int64),
      'visual_embed': tf.io.FixedLenFeature([128], tf.float32),
      'text_embed': tf.io.FixedLenFeature([64], tf.float32),
      'label': tf.io.FixedLenFeature([], tf.float32)
    }
    examples = tf.io.parse_example(input_placeholder, raw_feature_desc)
    parsed_features = {
      'prod_id': tf.RaggedTensor.from_tensor(examples['prod_id']),
      'uid': tf.RaggedTensor.from_tensor(examples['uid']),
      'brand': tf.RaggedTensor.from_tensor(examples['brand']),
      'category': tf.RaggedTensor.from_tensor(examples['category']),
      'price': examples['price'],
      'time_of_day': examples['time_of_day'],
      'device': tf.RaggedTensor.from_tensor(examples['device']),
      'visual_embed': examples['visual_embed'],
      'text_embed': examples['text_embed'],
      'label': examples['label']
    }
    return tf.estimator.export.ServingInputReceiver(parsed_features, receiver_tensors)

class FashionBatchTraining(FashionModelBase):
  def input_fn(self, mode):
    env = json.loads(os.environ['TF_CONFIG'])
    dataset = get_preprocessed_dataset('1m')
    dataset = dataset.shard(get_worker_count(env), env['task']['index'])
    # We assume the dataset now provides the additional features.
    return dataset.batch(512, drop_remainder=True)\
      .map(to_ragged).prefetch(tf.data.AUTOTUNE)

class FashionStreamTraining(FashionModelBase):
  def input_fn(self, mode):
    dataset = create_plain_kafka_dataset(
        topics=["training-sample-topic"],
        group_id="cgonline",
        servers="127.0.0.1:9092",
        stream_timeout=10000,
        poll_batch_size=16,
        configuration=[
          "session.timeout.ms=7000",
          "max.poll.interval.ms=8000"
        ],
    )
    return dataset.map(lambda x: decode_example(x.message))
  
def read_stdin():
  while True:
    line = sys.stdin.readline()
    if line:
      tokens = line.strip().split(',')
      # Expect tokens in order:
      # prod_id, uid, brand, category, price, time_of_day, device, (visual_embed: 128 floats),
      # (text_embed: 64 floats), label
      # For this example, we assume a simple comma-delimited format for demonstration.
      # In practice you would need a more robust parser, especially for vector inputs.
      num_expected = 1 + 1 + 1 + 1 + 1 + 1 + 1 + 128 + 64 + 1
      if len(tokens) < num_expected:
          continue
      # Parse simple scalar integer features
      yield {
        'prod_id': [int(tokens[0])],
        'uid': [int(tokens[1])],
        'brand': [int(tokens[2])],
        'category': [int(tokens[3])],
        'price': float(tokens[4]),
        'time_of_day': float(tokens[5]),
        'device': [int(tokens[6])],
        # For vector features, we convert the following tokens appropriately.
        'visual_embed': [float(x) for x in tokens[7:7+128]],
        'text_embed': [float(x) for x in tokens[7+128:7+128+64]],
        'label': float(tokens[-1])
      }
    else:
      return
  
class FashionBatchTrainingStdin(FashionModelBase):
  def input_fn(self, mode):
    return tf.data.Dataset.from_generator(
      read_stdin,
      output_signature={
        'prod_id': tf.TensorSpec(shape=(1,), dtype=tf.int64),
        'uid': tf.TensorSpec(shape=(1,), dtype=tf.int64),
        'brand': tf.TensorSpec(shape=(1,), dtype=tf.int64),
        'category': tf.TensorSpec(shape=(1,), dtype=tf.int64),
        'price': tf.TensorSpec(shape=(), dtype=tf.float32),
        'time_of_day': tf.TensorSpec(shape=(), dtype=tf.float32),
        'device': tf.TensorSpec(shape=(1,), dtype=tf.int64),
        'visual_embed': tf.TensorSpec(shape=(128,), dtype=tf.float32),
        'text_embed': tf.TensorSpec(shape=(64,), dtype=tf.float32),
        'label': tf.TensorSpec(shape=(), dtype=tf.float32),
      }
    ).batch(512, drop_remainder=True)\
      .map(to_ragged).prefetch(tf.data.AUTOTUNE)

FLAGS = flags.FLAGS

def main(_):
  tf.compat.v1.disable_eager_execution()
  # Read the TF_CONFIG from the environment
  raw_tf_conf = os.environ.get('TF_CONFIG', '{}')
  try:
    tf_conf = json.loads(raw_tf_conf)
  except json.JSONDecodeError:
    tf_conf = {}

  # If POD_NAME is set, override the task index using the pod's ordinal
  pod_name = os.environ.get('POD_NAME')
  if pod_name:
    try:
      # Assuming pod name format is something like "monolith-batch-ps-0"
      ordinal = int(pod_name.split('-')[-1])
      tf_conf['task']['index'] = ordinal
      # Update raw_tf_conf to reflect the override
      raw_tf_conf = json.dumps(tf_conf)
      logging.info("Overriding TF_CONFIG task index with ordinal %d from POD_NAME %s", ordinal, pod_name)
    except Exception as e:
      logging.error("Error parsing POD_NAME %s: %s", pod_name, e)
  
  config = RunnerConfig(
      discovery_type=ServiceDiscoveryType.PRIMUS,
      tf_config=raw_tf_conf,
      save_checkpoints_steps=10000,
      enable_model_ckpt_info=True,
      num_ps=len(tf_conf.get('cluster', {}).get('ps', [])),
      num_workers=get_worker_count(tf_conf),
      server_type=tf_conf.get('task', {}).get('type', ''),
      index=tf_conf.get('task', {}).get('index', 0)
  )

  # Instantiate parameters based on the chosen training type
  if FLAGS.training_type == "batch":
    params = FashionBatchTraining.params().instantiate()
  elif FLAGS.training_type == "stdin":
    params = FashionBatchTrainingStdin.params().instantiate()
  else:
    params = FashionStreamTraining.params().instantiate()

  estimator = Estimator(params, config)
  estimator.train(max_steps=1000000)

if __name__ == '__main__':
  logging.set_verbosity(logging.INFO)
  app.run(main)
