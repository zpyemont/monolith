"""
Fashion Two-Tower Model for Unified Retrieval

This implementation replaces the disconnected CLIP retrieval + pointwise ranker
with a unified two-tower architecture where:
- User tower: 32-dim user_id -> 128-dim normalized embedding (online inference)
- Item tower: product features + embeddings -> 128-dim normalized embedding (offline batch)
- Loss: In-batch negative softmax (temperature-scaled contrastive learning)
- Retrieval order = Ranking order (no separate ranking step)

Architecture:
    User Tower (32-dim) -> Dense(128, relu) -> Dropout(0.2) -> Dense(128) -> L2 normalize
    Item Tower (1667-dim) -> Dense(512) -> Dropout(0.2) -> Dense(256) -> Dropout(0.1) -> Dense(128) -> L2 normalize
    Loss: temperature-scaled softmax over similarity matrix with in-batch negatives

Input format (CSV):
    uid,pid,label,dwell_time_ms,brand,category,gender,price_tier,like_count,product_age_days,
    text_emb_0,...,text_emb_1023,image_emb_0,...,image_emb_511

Usage:
    # Batch training
    python fashion_model_two_tower.py --training_type=batch --fashion_batch_csv=data/fashion_with_embeddings.csv

    # Online training
    python fashion_model_two_tower.py --training_type=online --kafka_topics=training-sample-topic
"""

from absl import app
from absl import flags
from absl import logging

import json
import os
import sys
import numpy as np
from struct import unpack

import tensorflow as tf
from idl.matrix.proto.example_pb2 import Example
from monolith.native_training.estimator import EstimatorSpec, Estimator
from monolith.native_training.runner_utils import RunnerConfig
from monolith.native_training.service_discovery import ServiceDiscoveryType
from monolith.native_training.native_model import MonolithModel
from monolith.native_training.data.datasets import create_plain_kafka_dataset
from monolith.native_training.model_export.export_context import ExportMode
from monolith.agent_service.agent_controller import declare_saved_model, map_model_to_layout
from monolith.agent_service.backends import ZKBackend
from monolith.native_training.runtime.ops import gen_monolith_ops

# Training/stream flags
flags.DEFINE_enum('training_type', 'online', ['batch', 'online', 'stdin'], 'Type of training to launch')
flags.DEFINE_string('fashion_batch_csv', '', 'CSV file(s) pattern for batch training')
flags.DEFINE_integer('stream_timeout_ms', 86400000, 'Timeout for Kafka stream operations in ms')

# Kafka authentication
flags.DEFINE_string('kafka_username', '', 'Kafka SASL username (for Confluent Cloud)')
flags.DEFINE_string('kafka_password', '', 'Kafka SASL password (for Confluent Cloud)')
flags.DEFINE_string('kafka_security_protocol', 'SASL_SSL', 'Security protocol: PLAINTEXT, SASL_SSL')

# BigQuery options
flags.DEFINE_string('bq_project', '', 'GCP project for BigQuery table')
flags.DEFINE_string('bq_dataset', '', 'BigQuery dataset for training samples (e.g., "training")')
flags.DEFINE_string('bq_table', '', 'BigQuery table for training samples (e.g., "training_samples_raw")')
flags.DEFINE_string('bq_location', 'US', 'BigQuery dataset location')
flags.DEFINE_string('bq_products_dataset', 'catalog', 'BigQuery dataset for products table')
flags.DEFINE_string('bq_products_table', 'products', 'BigQuery table for products metadata')
flags.DEFINE_string('bq_start_date', '', 'Start date for batch training (YYYY-MM-DD format)')
flags.DEFINE_string('bq_end_date', '', 'End date for batch training (YYYY-MM-DD format)')
flags.DEFINE_integer('bq_days_back', 30, 'Days to look back from today (if dates not specified)')
flags.DEFINE_integer('bq_parallelism', 8, 'Parallel readers for BigQuery Storage API')
flags.DEFINE_bool('bq_use_sql_query', True, 'Use SQL query with JOIN instead of direct table read')
flags.DEFINE_bool('bq_filter_missing_embeddings', True, 'Filter out products without embeddings')
flags.DEFINE_integer('batch_epochs', 3, 'Number of epochs for batch training (0 = infinite)')

# Training configuration
flags.DEFINE_integer('max_steps', 10000, 'Maximum training steps for batch training')
flags.DEFINE_float('temperature', 0.05, 'Temperature for softmax loss')
flags.DEFINE_float('learning_rate', 0.05, 'Learning rate for Adagrad optimizer')

FLAGS = flags.FLAGS

# Embedding dimensions
TEXT_EMB_DIM = 1024  # Text embedding dimension (matches catalog.products)
IMAGE_EMB_DIM = 512  # CLIP model
TOWER_OUTPUT_DIM = 128  # Final embedding dimension for both towers


def get_worker_count(env: dict):
    cluster = env.get('cluster', {})
    return len(cluster.get('worker', [])) + len(cluster.get('chief', []))


def _to_ragged(features):
    """Convert embedding ID features to RaggedTensor format for Monolith.

    CRITICAL: Uses from_row_splits() instead of from_tensor().
    - from_tensor() is a Python-only op that doesn't serialize to TF graph properly
    - from_row_splits() is a graph-aware op that works in TF 2.4 graph mode
    This is the same mechanism tf.io.parse_example() uses internally.
    """
    # Get batch size dynamically - works in graph mode
    batch_size = tf.shape(features['user_id'])[0]
    # Create row_splits: [0, 1, 2, ..., batch_size] since each row has exactly 1 element
    row_splits = tf.cast(tf.range(batch_size + 1), tf.int64)

    return {
        # Embedding ID features need RaggedTensor format
        'user_id': tf.RaggedTensor.from_row_splits(
            tf.reshape(tf.cast(features['user_id'], tf.int64), [-1]),
            row_splits,
            validate=False
        ),
        'product_id': tf.RaggedTensor.from_row_splits(
            tf.reshape(tf.cast(features['product_id'], tf.int64), [-1]),
            row_splits,
            validate=False
        ),
        'brand': tf.RaggedTensor.from_row_splits(
            tf.reshape(tf.cast(features['brand'], tf.int64), [-1]),
            row_splits,
            validate=False
        ),
        'category': tf.RaggedTensor.from_row_splits(
            tf.reshape(tf.cast(features['category'], tf.int64), [-1]),
            row_splits,
            validate=False
        ),
        'gender': tf.RaggedTensor.from_row_splits(
            tf.reshape(tf.cast(features['gender'], tf.int64), [-1]),
            row_splits,
            validate=False
        ),
        # Non-embedding features stay as regular tensors
        'label': features['label'],
        'dwell_time_ms': features['dwell_time_ms'],
        'price_tier': features['price_tier'],
        'like_count': features['like_count'],
        'product_age_days': features['product_age_days'],
        'text_embedding': features['text_embedding'],
        'image_embedding': features['image_embedding'],
    }


def _parse_csv_line(line):
    """
    Parse CSV line with embeddings

    Format:
    uid,pid,label,dwell_time_ms,brand,category,gender,price_tier,like_count,product_age_days,
    text_emb_0,...,text_emb_1023,image_emb_0,...,image_emb_511

    Total columns: 10 + 1024 + 512 = 1546
    """
    parts = tf.strings.split(line, ',')

    # Basic features
    uid = tf.strings.to_number(parts[0], out_type=tf.int64)
    pid = tf.strings.to_number(parts[1], out_type=tf.int64)
    label = tf.strings.to_number(parts[2], out_type=tf.float32)
    dwell_time = tf.strings.to_number(parts[3], out_type=tf.float32)
    # Hash string features to int64 for embedding lookup
    brand = tf.strings.to_hash_bucket_fast(parts[4], 2**63 - 1)
    category = tf.strings.to_hash_bucket_fast(parts[5], 2**63 - 1)
    gender = tf.strings.to_hash_bucket_fast(parts[6], 2**63 - 1)
    price_tier = tf.strings.to_number(parts[7], out_type=tf.int64)
    like_count = tf.strings.to_number(parts[8], out_type=tf.int64)
    product_age_days = tf.strings.to_number(parts[9], out_type=tf.int64)

    # Text embedding (1024 dimensions)
    text_emb_parts = parts[10:10+TEXT_EMB_DIM]
    text_embedding = tf.strings.to_number(text_emb_parts, out_type=tf.float32)

    # Image embedding (512 dimensions)
    image_emb_parts = parts[10+TEXT_EMB_DIM:10+TEXT_EMB_DIM+IMAGE_EMB_DIM]
    image_embedding = tf.strings.to_number(image_emb_parts, out_type=tf.float32)

    return {
        'user_id': tf.reshape(uid, [1]),
        'product_id': tf.reshape(pid, [1]),
        'label': label,
        'dwell_time_ms': dwell_time,
        'brand': tf.reshape(brand, [1]),
        'category': tf.reshape(category, [1]),
        'gender': tf.reshape(gender, [1]),
        'price_tier': price_tier,
        'like_count': like_count,
        'product_age_days': product_age_days,
        'text_embedding': text_embedding,
        'image_embedding': image_embedding,
    }


def _decode_fashion_example(example_bytes):
    """Decode TFRecord example with embeddings"""
    raw_desc = {
        'user_id': tf.io.FixedLenFeature([1], tf.int64),
        'product_id': tf.io.FixedLenFeature([1], tf.int64),
        'label': tf.io.FixedLenFeature([], tf.float32),
        'dwell_time_ms': tf.io.FixedLenFeature([], tf.float32, default_value=0.0),
        'brand': tf.io.FixedLenFeature([], tf.string, default_value=''),
        'category': tf.io.FixedLenFeature([], tf.string, default_value=''),
        'gender': tf.io.FixedLenFeature([], tf.string, default_value=''),
        'price_tier': tf.io.FixedLenFeature([], tf.int64, default_value=0),
        'like_count': tf.io.FixedLenFeature([], tf.int64, default_value=0),
        'product_age_days': tf.io.FixedLenFeature([], tf.int64, default_value=0),
        'text_embedding': tf.io.FixedLenFeature([TEXT_EMB_DIM], tf.float32),
        'image_embedding': tf.io.FixedLenFeature([IMAGE_EMB_DIM], tf.float32),
    }
    parsed = tf.io.parse_example(example_bytes, raw_desc)
    # Hash string features to int64 for embedding lookup
    parsed['brand'] = tf.reshape(tf.strings.to_hash_bucket_fast(parsed['brand'], 2**63 - 1), [-1, 1])
    parsed['category'] = tf.reshape(tf.strings.to_hash_bucket_fast(parsed['category'], 2**63 - 1), [-1, 1])
    parsed['gender'] = tf.reshape(tf.strings.to_hash_bucket_fast(parsed['gender'], 2**63 - 1), [-1, 1])
    return parsed


class FashionTwoTowerModelBase(MonolithModel):
    """
    Fashion Two-Tower Model for Unified Retrieval

    This architecture uses separate user and item towers optimized for retrieval:

    User Tower:
        - user_id embedding (32-dim)
        - Network: Dense(128, relu) -> Dropout(0.2) -> Dense(128, relu)
        - L2 normalization
        - Output: 128-dim normalized user embedding

    Item Tower:
        - Categorical embeddings: product_id, brand, category, gender (4 x 32 = 128-dim)
        - Numerical features: price_tier, like_count, product_age_days (3-dim)
        - Pre-computed embeddings: text (1024-dim) + image (512-dim)
        - Total input: 1667-dim
        - Network: Dense(512, relu) -> Dropout(0.2) -> Dense(256, relu) -> Dropout(0.1) -> Dense(128, relu)
        - L2 normalization
        - Output: 128-dim normalized item embedding

    Training Loss:
        - Temperature-scaled in-batch negative softmax
        - Similarity matrix: user_vec @ item_vec.T / temperature
        - Labels: diagonal (user i matches item i)
        - Cross-entropy loss over similarity matrix
    """

    def __init__(self, params):
        super().__init__(params)
        self.p.serving.export_when_saving = True

    def model_fn(self, features, mode):
        # === 1. Create learned embedding features using Monolith ===
        embedding_features = ['product_id', 'user_id', 'brand', 'category', 'gender']
        for s_name in embedding_features:
            self.create_embedding_feature_column(s_name, occurrence_threshold=0)

        # Lookup all embeddings (32-dim each)
        prod_emb, user_emb, brand_emb, category_emb, gender_emb = self.lookup_embedding_slice(
            features=embedding_features, slice_name='vec', slice_dim=32)

        # === 2. USER TOWER (Simple - runs online per request) ===
        # Input: user_id embedding (32-dim)
        # Future: add liked_categories, session_product_ids for richer user representation
        user_features = user_emb  # 32-dim

        user_tower = tf.keras.Sequential([
            tf.keras.layers.Dense(128, activation='relu', name='user_dense_1'),
            tf.keras.layers.Dropout(0.2, name='user_dropout'),
            tf.keras.layers.Dense(128, activation='relu', name='user_dense_2'),
        ], name='user_tower')

        user_vec = user_tower(user_features)  # (batch, 128)
        user_vec = tf.nn.l2_normalize(user_vec, axis=1, name='user_vec_norm')

        # === 3. ITEM TOWER (Complex - runs offline in batch) ===
        # Numerical features (normalized, 3-dim)
        price_tier_norm = tf.expand_dims(tf.cast(features['price_tier'], tf.float32) / 5.0, axis=-1)
        log_like_count = tf.expand_dims(tf.math.log1p(tf.cast(features['like_count'], tf.float32)) / 10.0, axis=-1)
        product_age_norm = tf.expand_dims(tf.cast(features['product_age_days'], tf.float32) / 365.0, axis=-1)

        numerical_features = tf.concat([
            price_tier_norm,
            log_like_count,
            product_age_norm,
        ], axis=1)  # 3-dim

        # Pre-computed embeddings
        text_emb = features['text_embedding']  # 1024-dim
        image_emb = features['image_embedding']  # 512-dim

        # Concatenate all item features
        item_features = tf.concat([
            prod_emb,            # 32-dim
            brand_emb,           # 32-dim
            category_emb,        # 32-dim
            gender_emb,          # 32-dim
            numerical_features,  # 3-dim
            text_emb,            # 1024-dim
            image_emb,           # 512-dim
        ], axis=1)  # Total: 1667-dim

        item_tower = tf.keras.Sequential([
            tf.keras.layers.Dense(512, activation='relu', name='item_dense_1'),
            tf.keras.layers.Dropout(0.2, name='item_dropout_1'),
            tf.keras.layers.Dense(256, activation='relu', name='item_dense_2'),
            tf.keras.layers.Dropout(0.1, name='item_dropout_2'),
            tf.keras.layers.Dense(128, activation='relu', name='item_dense_3'),
        ], name='item_tower')

        item_vec = item_tower(item_features)  # (batch, 128)
        item_vec = tf.nn.l2_normalize(item_vec, axis=1, name='item_vec_norm')

        # === 4. Two-Tower Loss: In-Batch Negative Softmax ===
        # Compute similarity matrix: (batch, batch)
        # similarity[i, j] = cosine similarity between user i and item j
        similarity_matrix = tf.matmul(user_vec, item_vec, transpose_b=True)  # (batch, batch)
        similarity_matrix = similarity_matrix / FLAGS.temperature

        # Labels: diagonal matrix (user i matches item i)
        batch_size = tf.shape(user_vec)[0]
        labels = tf.range(batch_size)  # [0, 1, 2, ..., batch_size-1]

        # Cross-entropy loss: each row is a softmax over all items
        retrieval_loss = tf.reduce_mean(
            tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=labels, logits=similarity_matrix))

        # Compute pairwise similarity for monitoring (diagonal elements)
        similarity = tf.reduce_sum(user_vec * item_vec, axis=1)  # (batch,)

        # Log metrics
        tf.summary.scalar('losses/retrieval_loss', retrieval_loss)
        tf.summary.scalar('metrics/avg_similarity', tf.reduce_mean(similarity))
        tf.summary.scalar('metrics/user_vec_norm', tf.reduce_mean(tf.norm(user_vec, axis=1)))
        tf.summary.scalar('metrics/item_vec_norm', tf.reduce_mean(tf.norm(item_vec, axis=1)))

        # Console logging
        tf.print("[TWO-TOWER LOSS] retrieval_loss:", retrieval_loss,
                 "avg_similarity:", tf.reduce_mean(similarity),
                 output_stream=sys.stderr)

        # Handle NaN
        retrieval_loss = tf.where(tf.math.is_nan(retrieval_loss),
                                 tf.constant(0.0, dtype=tf.float32),
                                 retrieval_loss)

        # === 5. Export named outputs for serving ===
        # User tower serving signature (online inference per request)
        self.add_extra_output(
            name="user_tower",
            outputs={
                "user_vec": tf.identity(user_vec, name="user_vec"),
                "user_embedding": tf.identity(user_emb, name="user_embedding"),
            },
        )

        # Item tower serving signature (offline batch pre-computation)
        self.add_extra_output(
            name="item_tower",
            outputs={
                "item_vec": tf.identity(item_vec, name="item_vec"),
                "product_embedding": tf.identity(prod_emb, name="product_embedding"),
            },
        )

        # Debug outputs
        self.add_extra_output(
            name="features_for_join",
            outputs={
                "user_embedding": tf.identity(user_emb, name="user_embedding"),
                "product_embedding": tf.identity(prod_emb, name="product_embedding"),
                "brand_embedding": tf.identity(brand_emb, name="brand_embedding"),
                "category_embedding": tf.identity(category_emb, name="category_embedding"),
                "user_vec": tf.identity(user_vec, name="user_vec"),
                "item_vec": tf.identity(item_vec, name="item_vec"),
                "similarity": tf.identity(similarity, name="similarity"),
            },
        )

        opt = tf.compat.v1.train.AdagradOptimizer(FLAGS.learning_rate)

        return EstimatorSpec(
            label=features['label'],
            pred=similarity,
            head_name='two_tower',
            loss=retrieval_loss,
            optimizer=opt,
            classification=False)

    @staticmethod
    def serving_input_receiver_fn():
        """Default serving input - returns both towers' outputs"""
        input_ph = tf.compat.v1.placeholder(dtype=tf.string, shape=[None])
        raw_desc = {
            'user_id': tf.io.FixedLenFeature([1], tf.int64, default_value=[0]),
            'product_id': tf.io.FixedLenFeature([1], tf.int64, default_value=[0]),
            'label': tf.io.FixedLenFeature([], tf.float32, default_value=0.0),
            'dwell_time_ms': tf.io.FixedLenFeature([], tf.float32, default_value=0.0),
            'brand': tf.io.FixedLenFeature([], tf.string, default_value=''),
            'category': tf.io.FixedLenFeature([], tf.string, default_value=''),
            'gender': tf.io.FixedLenFeature([], tf.string, default_value=''),
            'price_tier': tf.io.FixedLenFeature([], tf.int64, default_value=0),
            'like_count': tf.io.FixedLenFeature([], tf.int64, default_value=0),
            'product_age_days': tf.io.FixedLenFeature([], tf.int64, default_value=0),
            'text_embedding': tf.io.FixedLenFeature([TEXT_EMB_DIM], tf.float32, default_value=[0.0] * TEXT_EMB_DIM),
            'image_embedding': tf.io.FixedLenFeature([IMAGE_EMB_DIM], tf.float32, default_value=[0.0] * IMAGE_EMB_DIM),
        }
        parsed = tf.io.parse_example(input_ph, raw_desc)
        # Create RaggedTensors using from_row_splits (graph-mode compatible)
        batch_size = tf.shape(parsed['user_id'])[0]
        row_splits = tf.cast(tf.range(batch_size + 1), tf.int64)

        # Hash string features to int64 for embedding lookup
        brand_hashed = tf.strings.to_hash_bucket_fast(parsed['brand'], 2**63 - 1)
        category_hashed = tf.strings.to_hash_bucket_fast(parsed['category'], 2**63 - 1)
        gender_hashed = tf.strings.to_hash_bucket_fast(parsed['gender'], 2**63 - 1)

        features = {
            'user_id': tf.RaggedTensor.from_row_splits(
                tf.reshape(parsed['user_id'], [-1]),
                row_splits,
                validate=False
            ),
            'product_id': tf.RaggedTensor.from_row_splits(
                tf.reshape(parsed['product_id'], [-1]),
                row_splits,
                validate=False
            ),
            'brand': tf.RaggedTensor.from_row_splits(
                tf.reshape(brand_hashed, [-1]),
                row_splits,
                validate=False
            ),
            'category': tf.RaggedTensor.from_row_splits(
                tf.reshape(category_hashed, [-1]),
                row_splits,
                validate=False
            ),
            'gender': tf.RaggedTensor.from_row_splits(
                tf.reshape(gender_hashed, [-1]),
                row_splits,
                validate=False
            ),
            'label': parsed['label'],
            'dwell_time_ms': parsed['dwell_time_ms'],
            'price_tier': parsed['price_tier'],
            'like_count': parsed['like_count'],
            'product_age_days': parsed['product_age_days'],
            'text_embedding': parsed['text_embedding'],
            'image_embedding': parsed['image_embedding'],
        }
        return tf.estimator.export.ServingInputReceiver(features, {'examples': input_ph})

    @staticmethod
    def serving_input_receiver_fn_user():
        """User tower serving input - called online per request"""
        input_ph = tf.compat.v1.placeholder(dtype=tf.string, shape=[None])
        raw_desc = {
            'user_id': tf.io.FixedLenFeature([1], tf.int64),
            # Future: add liked_category_ids, session_product_ids
        }
        parsed = tf.io.parse_example(input_ph, raw_desc)

        # Create RaggedTensors using from_row_splits (graph-mode compatible)
        batch_size = tf.shape(parsed['user_id'])[0]
        row_splits = tf.cast(tf.range(batch_size + 1), tf.int64)

        features = {
            'user_id': tf.RaggedTensor.from_row_splits(
                tf.reshape(parsed['user_id'], [-1]),
                row_splits,
                validate=False
            ),
            # Dummy values for item features (not used by user tower)
            'product_id': tf.RaggedTensor.from_row_splits(
                tf.zeros([batch_size], dtype=tf.int64),
                row_splits,
                validate=False
            ),
            'brand': tf.RaggedTensor.from_row_splits(
                tf.zeros([batch_size], dtype=tf.int64),
                row_splits,
                validate=False
            ),
            'category': tf.RaggedTensor.from_row_splits(
                tf.zeros([batch_size], dtype=tf.int64),
                row_splits,
                validate=False
            ),
            'gender': tf.RaggedTensor.from_row_splits(
                tf.zeros([batch_size], dtype=tf.int64),
                row_splits,
                validate=False
            ),
            'label': tf.zeros([batch_size], dtype=tf.float32),
            'dwell_time_ms': tf.zeros([batch_size], dtype=tf.float32),
            'price_tier': tf.zeros([batch_size], dtype=tf.int64),
            'like_count': tf.zeros([batch_size], dtype=tf.int64),
            'product_age_days': tf.zeros([batch_size], dtype=tf.int64),
            'text_embedding': tf.zeros([batch_size, TEXT_EMB_DIM], dtype=tf.float32),
            'image_embedding': tf.zeros([batch_size, IMAGE_EMB_DIM], dtype=tf.float32),
        }
        return tf.estimator.export.ServingInputReceiver(features, {'examples': input_ph})

    @staticmethod
    def serving_input_receiver_fn_item():
        """Item tower serving input - called offline in batch for pre-computation"""
        input_ph = tf.compat.v1.placeholder(dtype=tf.string, shape=[None])
        raw_desc = {
            'product_id': tf.io.FixedLenFeature([1], tf.int64),
            'brand': tf.io.FixedLenFeature([], tf.string, default_value=''),
            'category': tf.io.FixedLenFeature([], tf.string, default_value=''),
            'gender': tf.io.FixedLenFeature([], tf.string, default_value=''),
            'price_tier': tf.io.FixedLenFeature([], tf.int64, default_value=0),
            'like_count': tf.io.FixedLenFeature([], tf.int64, default_value=0),
            'product_age_days': tf.io.FixedLenFeature([], tf.int64, default_value=0),
            'text_embedding': tf.io.FixedLenFeature([TEXT_EMB_DIM], tf.float32),
            'image_embedding': tf.io.FixedLenFeature([IMAGE_EMB_DIM], tf.float32),
        }
        parsed = tf.io.parse_example(input_ph, raw_desc)

        # Create RaggedTensors using from_row_splits (graph-mode compatible)
        batch_size = tf.shape(parsed['product_id'])[0]
        row_splits = tf.cast(tf.range(batch_size + 1), tf.int64)

        # Hash string features to int64 for embedding lookup
        brand_hashed = tf.strings.to_hash_bucket_fast(parsed['brand'], 2**63 - 1)
        category_hashed = tf.strings.to_hash_bucket_fast(parsed['category'], 2**63 - 1)
        gender_hashed = tf.strings.to_hash_bucket_fast(parsed['gender'], 2**63 - 1)

        features = {
            'product_id': tf.RaggedTensor.from_row_splits(
                tf.reshape(parsed['product_id'], [-1]),
                row_splits,
                validate=False
            ),
            'brand': tf.RaggedTensor.from_row_splits(
                tf.reshape(brand_hashed, [-1]),
                row_splits,
                validate=False
            ),
            'category': tf.RaggedTensor.from_row_splits(
                tf.reshape(category_hashed, [-1]),
                row_splits,
                validate=False
            ),
            'gender': tf.RaggedTensor.from_row_splits(
                tf.reshape(gender_hashed, [-1]),
                row_splits,
                validate=False
            ),
            'price_tier': parsed['price_tier'],
            'like_count': parsed['like_count'],
            'product_age_days': parsed['product_age_days'],
            'text_embedding': parsed['text_embedding'],
            'image_embedding': parsed['image_embedding'],
            # Dummy values for user features (not used by item tower)
            'user_id': tf.RaggedTensor.from_row_splits(
                tf.zeros([batch_size], dtype=tf.int64),
                row_splits,
                validate=False
            ),
            'label': tf.zeros([batch_size], dtype=tf.float32),
            'dwell_time_ms': tf.zeros([batch_size], dtype=tf.float32),
        }
        return tf.estimator.export.ServingInputReceiver(features, {'examples': input_ph})


class FashionTwoTowerBatchTraining(FashionTwoTowerModelBase):
    """Batch training class for Fashion Two-Tower Model."""

    # Class-level variables to persist across Monolith's deepcopy operations
    _train_data_cache = None
    _val_data_cache = None
    _data_loaded_cache = False
    _force_eval_mode = False  # Flag to force returning validation data

    def _get_batch_size(self):
        """Get batch size from params with default fallback"""
        batch_size = getattr(self.p.train, 'per_replica_batch_size', None)
        return batch_size if batch_size is not None else 512

    def input_fn(self, mode):
        # Try BigQuery first if configured
        if FLAGS.bq_project and FLAGS.bq_dataset and FLAGS.bq_table:
            ds = self._try_bigquery_input(mode)
            if ds is not None:
                # Apply _to_ragged AFTER batching - creates graph-mode RaggedTensors
                return ds.map(_to_ragged).prefetch(tf.data.AUTOTUNE)

        # Fallback to CSV or stdin
        return self._csv_or_stdin_input()

    def _try_bigquery_input(self, mode):
        """Load training data from BigQuery with JOIN to products table (including embeddings)"""
        try:
            from google.cloud import bigquery
            import time

            logging.info("Loading training data from BigQuery with embeddings...")

            # Build date filter
            date_filter = self._build_date_filter()

            # Build SQL query with JOIN to products table
            query = self._build_training_query_with_embeddings(date_filter)

            logging.info(f"BigQuery query: {query[:500]}...")  # Log first 500 chars

            # Load data once and cache train/val splits (use class-level vars to survive deepcopy)
            if not FashionTwoTowerBatchTraining._data_loaded_cache:
                # Execute query and write to temp table
                temp_table_id = self._execute_query_to_temp_table(query)

                # Read from temp table and split into train/val
                self._load_and_split_data(temp_table_id)
                FashionTwoTowerBatchTraining._data_loaded_cache = True

            # Return appropriate split based on mode or _force_eval_mode flag
            # Monolith doesn't properly pass mode=EVAL, so we use a class-level flag
            is_eval = FashionTwoTowerBatchTraining._force_eval_mode
            is_training = not is_eval
            data = FashionTwoTowerBatchTraining._train_data_cache if is_training else FashionTwoTowerBatchTraining._val_data_cache
            split_name = "train" if is_training else "validation"
            logging.info(f"Returning {split_name} dataset with {len(data['user_id'])} samples (force_eval={is_eval})")

            # Convert numpy arrays to TF constants for graph mode compatibility
            # This is required because Monolith runs in graph mode (disable_eager_execution)
            # and from_tensor_slices needs proper graph tensors, not numpy arrays
            data_tensors = {k: tf.constant(v) for k, v in data.items()}
            ds = tf.data.Dataset.from_tensor_slices(data_tensors)
            if is_training:
                # For batch training: repeat for specified epochs, then stop
                # For online training: repeat indefinitely
                if FLAGS.training_type == 'batch':
                    epochs = FLAGS.batch_epochs if FLAGS.batch_epochs > 0 else None
                    logging.info(f"Batch training: {epochs if epochs else 'infinite'} epochs over {len(next(iter(data.values())))} samples")
                    return ds.shuffle(10000).repeat(epochs).batch(self._get_batch_size(), drop_remainder=True)
                else:
                    # Online training: repeat indefinitely
                    return ds.repeat().shuffle(10000).batch(self._get_batch_size(), drop_remainder=True)
            else:
                return ds.batch(self._get_batch_size(), drop_remainder=True)

        except Exception as e:
            logging.warning(f"BigQuery input failed ({e}); falling back to CSV/stdin")
            import traceback
            logging.warning(traceback.format_exc())
            return None

    def _build_date_filter(self):
        """Build date filter for BigQuery query"""
        if FLAGS.bq_start_date and FLAGS.bq_end_date:
            return f"DATE(TIMESTAMP_MILLIS(feature_event_time)) BETWEEN '{FLAGS.bq_start_date}' AND '{FLAGS.bq_end_date}'"
        else:
            return f"TIMESTAMP_MILLIS(feature_event_time) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {FLAGS.bq_days_back} DAY)"

    def _build_training_query_with_embeddings(self, date_filter):
        """Build SQL query to JOIN training_samples with products table (including embeddings)"""

        # Optional: Filter out products without embeddings
        embedding_filter = ""
        if FLAGS.bq_filter_missing_embeddings:
            embedding_filter = """
            AND p.text_embedding IS NOT NULL
            AND p.image_embedding IS NOT NULL
            AND ARRAY_LENGTH(p.text_embedding) = 1024
            AND ARRAY_LENGTH(p.image_embedding) = 512
            """

        return f"""
        SELECT
            -- Extract from feature_data JSON
            JSON_VALUE(t.feature_data, '$.user_id') as user_id,

            -- Extract from action_data JSON
            JSON_VALUE(t.action_data, '$.product_id') as product_id,
            CAST(JSON_VALUE(t.action_data, '$.label') AS FLOAT64) as label,
            CAST(COALESCE(JSON_VALUE(t.action_data, '$.dwell_time'), '0') AS FLOAT64) * 1000 as dwell_time_ms,

            -- Join product metadata from products table
            COALESCE(p.brand, '') as brand,
            COALESCE(p.category, '') as category,
            COALESCE(p.gender, '') as gender,

            -- Calculate price tier (0-5 buckets)
            CASE
                WHEN p.price IS NULL THEN 0
                WHEN p.price < 50 THEN 1
                WHEN p.price < 100 THEN 2
                WHEN p.price < 200 THEN 3
                WHEN p.price < 500 THEN 4
                ELSE 5
            END as price_tier,

            -- like_count (not available in products table yet, default to 0)
            0 as like_count,

            -- Calculate product age in days
            CAST(COALESCE(
                DATE_DIFF(CURRENT_DATE(), DATE(p.parsed_at), DAY),
                0
            ) AS INT64) as product_age_days,

            -- Embeddings (REPEATED FLOAT arrays)
            p.text_embedding as text_embedding,
            p.image_embedding as image_embedding

        FROM `{FLAGS.bq_project}.{FLAGS.bq_dataset}.{FLAGS.bq_table}` t
        LEFT JOIN `{FLAGS.bq_project}.{FLAGS.bq_products_dataset}.{FLAGS.bq_products_table}` p
            ON JSON_VALUE(t.action_data, '$.product_id') = p.product_id
        WHERE {date_filter}
            AND JSON_VALUE(t.action_data, '$.label') IS NOT NULL
            AND JSON_VALUE(t.feature_data, '$.user_id') IS NOT NULL
            AND JSON_VALUE(t.action_data, '$.product_id') IS NOT NULL
            {embedding_filter}
        ORDER BY t.feature_event_time
        """

    def _execute_query_to_temp_table(self, query):
        """Execute query and write results to temp table"""
        from google.cloud import bigquery
        import time

        bq_client = bigquery.Client(project=FLAGS.bq_project, location=FLAGS.bq_location)
        temp_table_id = f"{FLAGS.bq_project}.{FLAGS.bq_dataset}.temp_batch_training_two_tower_{int(time.time())}"

        logging.info(f"Creating temp table: {temp_table_id} (location: {FLAGS.bq_location})")
        job_config = bigquery.QueryJobConfig(destination=temp_table_id)
        query_job = bq_client.query(query, job_config=job_config, location=FLAGS.bq_location)
        query_job.result()  # Wait for completion

        # Get row count
        result = bq_client.query(f"SELECT COUNT(*) as cnt FROM `{temp_table_id}`").result()
        row_count = list(result)[0]['cnt']
        logging.info(f"Temp table created with {row_count:,} training samples")

        if row_count == 0:
            raise ValueError("No training samples found in BigQuery. Check date range and filters.")

        return temp_table_id

    def _load_and_split_data(self, temp_table_id, val_ratio=0.2):
        """Load data from temp table and split into train/validation sets.

        Args:
            temp_table_id: BigQuery temp table ID
            val_ratio: Fraction of data for validation (default 20%)
        """
        from google.cloud import bigquery
        import numpy as np

        bq_client = bigquery.Client(project=FLAGS.bq_project, location=FLAGS.bq_location)

        # Query all rows from temp table
        query = f"""
        SELECT
            user_id, product_id, label, dwell_time_ms,
            brand, category, gender, price_tier,
            like_count, product_age_days,
            text_embedding, image_embedding
        FROM `{temp_table_id}`
        """

        logging.info(f"Reading training data from temp table: {temp_table_id}")

        # Batch load all data using to_dataframe() - much faster than row-by-row iteration
        query_job = bq_client.query(query)
        df = query_job.to_dataframe()
        logging.info(f"Loaded {len(df)} rows via to_dataframe()")

        # Vectorized hash operations using pandas apply
        user_ids = df['user_id'].apply(lambda x: [hash(x) % (2**63)]).tolist()
        product_ids = df['product_id'].apply(lambda x: [hash(x) % (2**63)]).tolist()
        labels = df['label'].astype(np.float32).tolist()
        dwell_times = df['dwell_time_ms'].astype(np.float32).tolist()
        brands = df['brand'].fillna('unknown').apply(lambda x: [hash(x) % (2**63)]).tolist()
        categories = df['category'].fillna('unknown').apply(lambda x: [hash(x) % (2**63)]).tolist()
        genders = df['gender'].fillna('unknown').apply(lambda x: [hash(x) % (2**63)]).tolist()
        price_tiers = df['price_tier'].astype(np.int64).tolist()
        like_counts = df['like_count'].astype(np.int64).tolist()
        product_ages = df['product_age_days'].astype(np.int64).tolist()

        # Process embeddings with vectorized padding
        def pad_embedding(emb, target_dim):
            if emb is None:
                return [0.0] * target_dim
            emb = list(emb)
            if len(emb) < target_dim:
                return emb + [0.0] * (target_dim - len(emb))
            return emb[:target_dim]

        text_embeddings = df['text_embedding'].apply(lambda x: pad_embedding(x, TEXT_EMB_DIM)).tolist()
        image_embeddings = df['image_embedding'].apply(lambda x: pad_embedding(x, IMAGE_EMB_DIM)).tolist()

        num_samples = len(df)
        logging.info(f"Loaded {num_samples} samples into memory")

        # Shuffle indices for random split
        indices = np.random.permutation(num_samples)
        val_size = int(num_samples * val_ratio)
        train_indices = indices[val_size:]
        val_indices = indices[:val_size]

        logging.info(f"Split: {len(train_indices)} train, {len(val_indices)} validation samples")

        # Convert to numpy arrays
        all_data = {
            'user_id': np.array(user_ids, dtype=np.int64),
            'product_id': np.array(product_ids, dtype=np.int64),
            'label': np.array(labels, dtype=np.float32),
            'dwell_time_ms': np.array(dwell_times, dtype=np.float32),
            'brand': np.array(brands, dtype=np.int64),
            'category': np.array(categories, dtype=np.int64),
            'gender': np.array(genders, dtype=np.int64),
            'price_tier': np.array(price_tiers, dtype=np.int64),
            'like_count': np.array(like_counts, dtype=np.int64),
            'product_age_days': np.array(product_ages, dtype=np.int64),
            'text_embedding': np.array(text_embeddings, dtype=np.float32),
            'image_embedding': np.array(image_embeddings, dtype=np.float32),
        }

        # Split into train and validation (use class-level vars to survive deepcopy)
        FashionTwoTowerBatchTraining._train_data_cache = {k: v[train_indices] for k, v in all_data.items()}
        FashionTwoTowerBatchTraining._val_data_cache = {k: v[val_indices] for k, v in all_data.items()}

    def eval_input_fn(self, mode=None):
        """Input function that always returns validation data.

        Monolith's estimator doesn't properly pass mode=EVAL, so we need this
        separate function to explicitly return validation data during evaluation.
        """
        # Ensure data is loaded (reuse logic from input_fn)
        if FLAGS.bq_project and FLAGS.bq_dataset and FLAGS.bq_table:
            if not FashionTwoTowerBatchTraining._data_loaded_cache:
                # This shouldn't happen if input_fn was called first for training
                logging.warning("eval_input_fn called before data was loaded - calling input_fn first")
                self.input_fn(tf.estimator.ModeKeys.TRAIN)

        data = FashionTwoTowerBatchTraining._val_data_cache
        if data is None:
            logging.warning("No validation data available - returning empty dataset")
            return None

        logging.info(f"eval_input_fn: Returning validation dataset with {len(data['user_id'])} samples")
        # Convert numpy arrays to TF constants for graph mode compatibility
        data_tensors = {k: tf.constant(v) for k, v in data.items()}
        ds = tf.data.Dataset.from_tensor_slices(data_tensors)
        ds = ds.batch(self._get_batch_size(), drop_remainder=False)  # No shuffle, keep all samples
        return ds.map(_to_ragged).prefetch(tf.data.AUTOTUNE)

    def _csv_or_stdin_input(self):
        """Fallback to CSV or stdin input"""
        if FLAGS.fashion_batch_csv:
            files = tf.io.gfile.glob(FLAGS.fashion_batch_csv)
            ds = tf.data.TextLineDataset(files).map(_parse_csv_line)
        else:
            def gen():
                for line in sys.stdin:
                    parts = line.strip().split(',')
                    yield {
                        'user_id': [int(parts[0])],
                        'product_id': [int(parts[1])],
                        'label': float(parts[2]),
                        'dwell_time_ms': float(parts[3]),
                        # Hash string features to int64 for embedding lookup
                        'brand': [hash(parts[4] or 'unknown') % (2**63)],
                        'category': [hash(parts[5] or 'unknown') % (2**63)],
                        'gender': [hash(parts[6] or 'unknown') % (2**63)],
                        'price_tier': int(parts[7]),
                        'like_count': int(parts[8]),
                        'product_age_days': int(parts[9]),
                        'text_embedding': [float(x) for x in parts[10:10+TEXT_EMB_DIM]],
                        'image_embedding': [float(x) for x in parts[10+TEXT_EMB_DIM:10+TEXT_EMB_DIM+IMAGE_EMB_DIM]],
                    }
            ds = tf.data.Dataset.from_generator(
                gen,
                output_signature={
                    'user_id': tf.TensorSpec([1], tf.int64),
                    'product_id': tf.TensorSpec([1], tf.int64),
                    'label': tf.TensorSpec([], tf.float32),
                    'dwell_time_ms': tf.TensorSpec([], tf.float32),
                    'brand': tf.TensorSpec([1], tf.int64),
                    'category': tf.TensorSpec([1], tf.int64),
                    'gender': tf.TensorSpec([1], tf.int64),
                    'price_tier': tf.TensorSpec([], tf.int64),
                    'like_count': tf.TensorSpec([], tf.int64),
                    'product_age_days': tf.TensorSpec([], tf.int64),
                    'text_embedding': tf.TensorSpec([TEXT_EMB_DIM], tf.float32),
                    'image_embedding': tf.TensorSpec([IMAGE_EMB_DIM], tf.float32),
                }
            )
        # Apply _to_ragged AFTER batching - creates graph-mode RaggedTensors
        return ds.batch(self._get_batch_size(), drop_remainder=True).map(_to_ragged).prefetch(tf.data.AUTOTUNE)


class FashionTwoTowerOnlineTraining(FashionTwoTowerModelBase):
    """Online training from Kafka with product embedding lookup"""

    _product_embeddings_cache = None  # Class-level cache

    def _get_batch_size(self):
        """Get batch size from params with default fallback"""
        batch_size = getattr(self.p.train, 'per_replica_batch_size', None)
        return batch_size if batch_size is not None else 512

    @classmethod
    def _load_product_embeddings(cls):
        """Load product embeddings from BigQuery into memory (called once)"""
        if cls._product_embeddings_cache is not None:
            logging.info("Using cached product embeddings")
            return cls._product_embeddings_cache

        if not (FLAGS.bq_project and FLAGS.bq_products_dataset and FLAGS.bq_products_table):
            logging.warning("No BigQuery products config - online training will use zero embeddings")
            cls._product_embeddings_cache = {}
            return {}

        try:
            logging.info("Loading product embeddings from BigQuery for online training...")
            from google.cloud import bigquery

            client = bigquery.Client(project=FLAGS.bq_project)
            query = f"""
                SELECT
                    product_id,
                    text_embedding,
                    image_embedding,
                    COALESCE(brand, '') as brand,
                    COALESCE(category, '') as category,
                    COALESCE(gender, '') as gender,
                    COALESCE(price, 0) as price
                FROM `{FLAGS.bq_project}.{FLAGS.bq_products_dataset}.{FLAGS.bq_products_table}`
                WHERE text_embedding IS NOT NULL
                  AND image_embedding IS NOT NULL
                  AND ARRAY_LENGTH(text_embedding) = {TEXT_EMB_DIM}
                  AND ARRAY_LENGTH(image_embedding) = {IMAGE_EMB_DIM}
            """

            df = client.query(query).to_dataframe()
            logging.info(f"Loaded {len(df)} products with embeddings from BigQuery")

            # Build cache: product_id -> {embeddings + metadata}
            cache = {}
            for _, row in df.iterrows():
                cache[row['product_id']] = {
                    'text_embedding': list(row['text_embedding']),
                    'image_embedding': list(row['image_embedding']),
                    'brand': row['brand'],
                    'category': row['category'],
                    'gender': row['gender'],
                    'price': float(row['price']),
                }

            cls._product_embeddings_cache = cache
            logging.info(f"Product embedding cache built: {len(cache)} products")
            return cache

        except Exception as e:
            logging.error(f"Failed to load product embeddings: {e}")
            logging.error("Online training will continue with zero embeddings (degraded mode)")
            cls._product_embeddings_cache = {}
            return {}

    def input_fn(self, mode):
        # Load product embeddings once at startup
        product_cache = self._load_product_embeddings()

        # Configure Kafka authentication
        kafka_config = [
            f"security.protocol={FLAGS.kafka_security_protocol}",
            "auto.offset.reset=latest",  # Skip old messages on first consumer group startup
        ]

        if FLAGS.kafka_security_protocol == 'SASL_SSL':
            kafka_config.extend([
                "sasl.mechanisms=PLAIN",
                f"sasl.username={FLAGS.kafka_username}",
                f"sasl.password={FLAGS.kafka_password}",
            ])

        dataset = create_plain_kafka_dataset(
            topics=FLAGS.kafka_topics.split(','),
            group_id=FLAGS.kafka_group_id,
            servers=FLAGS.kafka_servers,
            stream_timeout=FLAGS.stream_timeout_ms,
            poll_batch_size=1,  # Return individual messages for JSON decoding (use .batch() after map)
            configuration=kafka_config,
        )

        def decode_protobuf_training_sample(kafka_message):
            """Decode protobuf training sample using tf.py_function for graph compatibility"""

            def decode_pb(message_bytes):
                """Python function to decode protobuf - executed in eager mode"""
                try:
                    # tf.py_function passes EagerTensor - convert to numpy array
                    if hasattr(message_bytes, 'numpy'):
                        message_bytes = message_bytes.numpy()

                    # Handle empty messages (EOF markers from Kafka)
                    if isinstance(message_bytes, np.ndarray) and message_bytes.shape[0] == 0:
                        # Return zeros for empty messages
                        return (
                            np.array([0], dtype=np.int64), np.array([0], dtype=np.int64),
                            np.float32(0.0), np.float32(0.0), np.array([0], dtype=np.int64),
                            np.array([0], dtype=np.int64), np.array([0], dtype=np.int64),
                            np.int64(0), np.int64(0), np.int64(0),
                            np.array([0.0] * TEXT_EMB_DIM, dtype=np.float32),
                            np.array([0.0] * IMAGE_EMB_DIM, dtype=np.float32),
                        )

                    # Extract bytes from numpy array (shape should be (1,))
                    if isinstance(message_bytes, np.ndarray):
                        message_bytes = message_bytes.item()

                    # Ensure we have Python bytes
                    if not isinstance(message_bytes, bytes):
                        logging.warning(f"Unexpected message type: {type(message_bytes)}")
                        return (
                            np.array([0], dtype=np.int64), np.array([0], dtype=np.int64),
                            np.float32(0.0), np.float32(0.0), np.array([0], dtype=np.int64),
                            np.array([0], dtype=np.int64), np.array([0], dtype=np.int64),
                            np.int64(0), np.int64(0), np.int64(0),
                            np.array([0.0] * TEXT_EMB_DIM, dtype=np.float32),
                            np.array([0.0] * IMAGE_EMB_DIM, dtype=np.float32),
                        )

                    # Parse 8-byte length prefix (little-endian unsigned long long)
                    length = unpack('<Q', message_bytes[:8])[0]
                    pb_bytes = message_bytes[8:]

                    if len(pb_bytes) != length:
                        logging.warning(f"Length mismatch: expected {length}, got {len(pb_bytes)}")

                    # Deserialize Example protobuf
                    example = Example()
                    example.ParseFromString(pb_bytes)

                    # Extract features by name from named_feature list
                    features = {nf.name: nf.feature for nf in example.named_feature}

                    # Extract IDs (fid_v2_list contains fixed64 values)
                    user_id = features['user_id'].fid_v2_list.value[0] if 'user_id' in features else 0
                    product_id = features['product_id'].fid_v2_list.value[0] if 'product_id' in features else 0

                    # Extract embeddings (float_list contains float values)
                    text_embedding = list(features['text_embedding'].float_list.value) if 'text_embedding' in features else [0.0] * TEXT_EMB_DIM
                    image_embedding = list(features['image_embedding'].float_list.value) if 'image_embedding' in features else [0.0] * IMAGE_EMB_DIM

                    # Extract other features
                    brand = features['brand'].fid_v2_list.value[0] if 'brand' in features else 0
                    category = features['category'].fid_v2_list.value[0] if 'category' in features else 0
                    gender = features['gender'].fid_v2_list.value[0] if 'gender' in features else 0
                    price_tier = features['price_tier'].int64_list.value[0] if 'price_tier' in features else 0
                    dwell_time_ms = features['dwell_time_ms'].float_list.value[0] if 'dwell_time_ms' in features else 0.0

                    # Extract label from Example.label
                    label = example.label[0] if len(example.label) > 0 else 0.0

                    # Return as numpy arrays (same format as JSON decoder)
                    return (
                        np.array([user_id], dtype=np.int64),
                        np.array([product_id], dtype=np.int64),
                        np.float32(label),
                        np.float32(dwell_time_ms),
                        np.array([brand], dtype=np.int64),
                        np.array([category], dtype=np.int64),
                        np.array([gender], dtype=np.int64),
                        np.int64(price_tier),
                        np.int64(0),  # like_count (placeholder)
                        np.int64(0),  # product_age_days (placeholder)
                        np.array(text_embedding, dtype=np.float32),
                        np.array(image_embedding, dtype=np.float32),
                    )
                except Exception as e:
                    logging.warning(f"Failed to decode protobuf training sample: {e}")
                    # Return zeros for failed decode
                    return (
                        np.array([0], dtype=np.int64),
                        np.array([0], dtype=np.int64),
                        np.float32(0.0),
                        np.float32(0.0),
                        np.array([0], dtype=np.int64),
                        np.array([0], dtype=np.int64),
                        np.array([0], dtype=np.int64),
                        np.int64(0),
                        np.int64(0),
                        np.int64(0),
                        np.array([0.0] * TEXT_EMB_DIM, dtype=np.float32),
                        np.array([0.0] * IMAGE_EMB_DIM, dtype=np.float32),
                    )

            # Wrap in tf.py_function - executed in eager mode within graph context
            (user_id, product_id, label, dwell_time_ms, brand, category, gender,
             price_tier, like_count, product_age_days, text_embedding, image_embedding) = tf.py_function(
                decode_pb,
                [kafka_message.message],
                [tf.int64, tf.int64, tf.float32, tf.float32, tf.int64, tf.int64, tf.int64,
                 tf.int64, tf.int64, tf.int64, tf.float32, tf.float32]
            )

            # Set shapes explicitly (required for .batch() to work correctly)
            user_id.set_shape([1])
            product_id.set_shape([1])
            label.set_shape([])
            dwell_time_ms.set_shape([])
            brand.set_shape([1])
            category.set_shape([1])
            gender.set_shape([1])
            price_tier.set_shape([])
            like_count.set_shape([])
            product_age_days.set_shape([])
            text_embedding.set_shape([TEXT_EMB_DIM])
            image_embedding.set_shape([IMAGE_EMB_DIM])

            return {
                'user_id': user_id,
                'product_id': product_id,
                'label': label,
                'dwell_time_ms': dwell_time_ms,
                'brand': brand,
                'category': category,
                'gender': gender,
                'price_tier': price_tier,
                'like_count': like_count,
                'product_age_days': product_age_days,
                'text_embedding': text_embedding,
                'image_embedding': image_embedding,
            }

        # poll_batch_size=1 returns individual messages for protobuf decoding
        # Then .batch() creates TensorFlow batches for training
        # .repeat() allows continuous cycling through messages for streaming training
        # drop_remainder=False processes all messages (last batch may be partial)
        return dataset.map(decode_protobuf_training_sample).batch(self._get_batch_size(), drop_remainder=False).repeat().map(_to_ragged).prefetch(tf.data.AUTOTUNE)


def export_and_register_model(estimator, tf_conf):
    """Export trained model for TensorFlow Serving and register with ZooKeeper"""

    # Export SavedModel
    try:
        logging.info(f"Exporting model '{FLAGS.model_name}' for TensorFlow Serving...")
        estimator.export_saved_model(
            batch_size=64,
            name=FLAGS.model_name,
            dense_only=False
        )
        logging.info("Model exported successfully")
    except Exception as e:
        logging.error(f"Model export failed: {e}")
        raise

    # Only primary worker registers with ZooKeeper
    task_type = tf_conf.get('task', {}).get('type', '')
    task_index = tf_conf.get('task', {}).get('index', 0)

    should_register = (
        (task_type == 'worker' and task_index == 0) or  # Distributed: primary worker
        (not task_type)  # Single-node: always register
    )

    if should_register and FLAGS.zk_server:
        try:
            # Register in ZooKeeper for inference discovery
            export_base = f"{FLAGS.model_dir}/exported_models"
            bzid = "monolith_serving_test"
            layout_name = "test"

            logging.info(f"Registering model in ZooKeeper at /{bzid}/layouts/{layout_name}")

            # Create ZooKeeper backend
            bd = ZKBackend(bzid, FLAGS.zk_server)
            bd.start()

            try:
                # Step 1: Declare saved models (creates saved_models registry entries)
                if tf.io.gfile.exists(export_base):
                    model_name = declare_saved_model(
                        bd=bd,
                        export_base=export_base,
                        model_name=FLAGS.model_name,
                        overwrite=True,
                        arch="entry_ps"
                    )
                    logging.info(f"Declared saved model: {model_name}")

                    # Step 2: Publish to layout (makes models discoverable by inference)
                    layout_path = f"/{bzid}/layouts/{layout_name}"
                    map_model_to_layout(
                        bd=bd,
                        model_pattern=f"{model_name}:*",  # Publish all subgraphs (entry, ps_0, etc.)
                        layout_path=layout_path,
                        action='pub'
                    )
                    logging.info(f"Published model to layout: {layout_path}")
                    logging.info(f"Model registration complete - inference servers should discover model")
                else:
                    logging.error(f"Export base does not exist: {export_base}")

            finally:
                bd.stop()

        except Exception as e:
            logging.warning(f"ZooKeeper registration failed (non-fatal): {e}")
    elif not FLAGS.zk_server:
        logging.info("No ZK server - skipping model registration")
    else:
        logging.info(f"Worker {task_type}:{task_index} - skipping registration")


def main(_argv):
    tf.compat.v1.disable_eager_execution()

    raw_tf_conf = os.environ.get('TF_CONFIG', '{}')
    try:
        tf_conf = json.loads(raw_tf_conf)
    except json.JSONDecodeError:
        tf_conf = {}

    pod = os.environ.get('POD_NAME')
    if pod:
        try:
            idx = int(pod.rsplit('-', 1)[-1])
            tf_conf.setdefault('task', {})['index'] = idx
            raw_tf_conf = json.dumps(tf_conf)
            logging.info('Overriding TF_CONFIG index with %d', idx)
        except ValueError:
            pass

    logging.info(f"FLAGS.training_type: {FLAGS.training_type}")
    logging.info("Architecture: Two-Tower Unified Retrieval (User Tower + Item Tower)")
    logging.info(f"Model checkpoints will be saved to: {FLAGS.model_dir}")
    logging.info(f"Temperature: {FLAGS.temperature}, Learning rate: {FLAGS.learning_rate}")

    # Determine if running locally (no PS/workers in TF_CONFIG)
    is_local = len(tf_conf.get('cluster', {}).get('ps', [])) == 0 and get_worker_count(tf_conf) <= 1

    config = RunnerConfig(
        discovery_type=ServiceDiscoveryType.PRIMUS,  # Use PRIMUS for TF_CONFIG-based PS/Worker discovery
        unified_serving=True,  # CRITICAL: Use ZKBackend for parameter sync (watches /binding/ path)
        model_name="fashion_two_tower",  # Must match model registered in ZK by batch training
        tf_config=raw_tf_conf,
        model_dir=FLAGS.model_dir,
        save_checkpoints_steps=500,  # Checkpoint frequently for preemptible VMs
        checkpoints_max_to_keep=5,   # Keep only 5 most recent checkpoints
        enable_model_ckpt_info=True,
        continue_training=True,       # Auto-resume from latest checkpoint
        # For batch training: force num_ps=1 to get distributed export (entry + ps_0)
        # For online training: use TF_CONFIG cluster definition
        num_ps=1 if FLAGS.training_type == 'batch' else len(tf_conf.get('cluster', {}).get('ps', [])),
        num_workers=max(1, get_worker_count(tf_conf)),
        server_type=tf_conf.get('task', {}).get('type', '') or 'worker',
        index=tf_conf.get('task', {}).get('index', 0),
        zk_server=FLAGS.zk_server or os.environ.get('ZK_SERVERS', ''),
        base_name="fashion_two_tower",
        bzid="monolith_serving_test",
        is_local=is_local,
    )

    if FLAGS.training_type == 'batch':
        params = FashionTwoTowerBatchTraining.params().instantiate()
    else:
        params = FashionTwoTowerOnlineTraining.params().instantiate()
        config.enable_realtime_training = True

    estimator = Estimator(params, config)

    # Setup graceful shutdown handler for preemptible VMs
    def create_shutdown_handler():
        """Graceful shutdown for K8s preemption (SIGTERM)"""
        def handler(signum, frame):
            logging.warning(f"Received signal {signum} - initiating graceful shutdown")
            logging.info("Latest checkpoint saved - training will auto-resume")
            import time
            time.sleep(3)  # Allow final checkpoint write
            sys.exit(0)
        return handler

    import signal
    signal.signal(signal.SIGTERM, create_shutdown_handler())  # K8s preemption
    signal.signal(signal.SIGINT, create_shutdown_handler())   # Ctrl+C
    logging.info("Graceful shutdown handlers registered for preemptible VMs")

    if FLAGS.training_type == 'batch':
        logging.info("=" * 60)
        logging.info("Starting batch training")
        logging.info(f"Max steps: {FLAGS.max_steps}")
        logging.info(f"Checkpoint frequency: every {config.save_checkpoints_steps} steps")
        logging.info("=" * 60)

        # Pass hooks explicitly - updated estimator.py supports hooks parameter
        estimator.train(max_steps=FLAGS.max_steps, hooks=params._training_hooks)

        logging.info("=" * 60)
        logging.info("BATCH TRAINING COMPLETE")
        logging.info("=" * 60)

        # Export model and register with ZooKeeper
        export_and_register_model(estimator, tf_conf)

    else:
        # Online training - pass hooks explicitly
        estimator.train(hooks=params._training_hooks)


if __name__ == '__main__':
    logging.set_verbosity(logging.INFO)
    app.run(main)
