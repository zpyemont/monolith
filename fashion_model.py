from absl import app
from absl import flags
from absl import logging

import json
import os
import sys

import tensorflow as tf
from monolith.native_training.estimator import EstimatorSpec, Estimator
from monolith.native_training.runner_utils import RunnerConfig
from monolith.native_training.service_discovery import ServiceDiscoveryType
from monolith.native_training.native_model import MonolithModel
from monolith.native_training.data.datasets import create_plain_kafka_dataset
from monolith.native_training.model_export.export_context import ExportMode
from monolith.native_training.runtime.ops import gen_monolith_ops

## ZooKeeper agent controller removed (using TF_CONFIG discovery)


# Training/stream flags (Kafka flags are provided by Monolith)
flags.DEFINE_enum('training_type', 'online', ['batch', 'online', 'stdin'], 'Type of training to launch')
flags.DEFINE_string('fashion_batch_csv', '', 'CSV file(s) pattern for batch training: lines as uid,pid,label')
flags.DEFINE_integer('stream_timeout_ms', 86400000, 'Timeout for Kafka stream operations in ms')

# Confluent Cloud Kafka authentication
flags.DEFINE_string('kafka_username', '', 'Kafka SASL username (for Confluent Cloud)')
flags.DEFINE_string('kafka_password', '', 'Kafka SASL password (for Confluent Cloud)')
flags.DEFINE_string('kafka_security_protocol', 'SASL_SSL', 'Security protocol: PLAINTEXT, SASL_SSL')
# BigQuery options (optional; requires tensorflow-io)
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
FLAGS = flags.FLAGS


def get_worker_count(env: dict):
    cluster = env.get('cluster', {})
    return len(cluster.get('worker', [])) + len(cluster.get('chief', []))


def _to_ragged(features):
    return {
        'user_id': tf.RaggedTensor.from_tensor(features['user_id']),
        'product_id': tf.RaggedTensor.from_tensor(features['product_id']),
        'label': features['label'],
        'dwell_time_ms': features['dwell_time_ms'],
        'brand': features['brand'],
        'category': features['category'],
        'gender': features['gender'],
        'price_tier': features['price_tier'],
        'like_count': features['like_count'],
        'product_age_days': features['product_age_days'],
    }


def _parse_csv_line(line):
    """
    Parse CSV line with format:
    uid,pid,label,dwell_time_ms,brand,category,gender,price_tier,like_count,product_age_days

    Example: 1,100,1.0,3500.0,nike,shoes,mens,2,150,30
    """
    parts = tf.strings.split(line, ',')
    uid = tf.strings.to_number(parts[0], out_type=tf.int64)
    pid = tf.strings.to_number(parts[1], out_type=tf.int64)
    label = tf.strings.to_number(parts[2], out_type=tf.float32)
    dwell_time = tf.strings.to_number(parts[3], out_type=tf.float32)
    brand = parts[4]  # String
    category = parts[5]  # String
    gender = parts[6]  # String
    price_tier = tf.strings.to_number(parts[7], out_type=tf.int64)
    like_count = tf.strings.to_number(parts[8], out_type=tf.int64)
    product_age_days = tf.strings.to_number(parts[9], out_type=tf.int64)

    return {
        'user_id': tf.reshape(uid, [1]),
        'product_id': tf.reshape(pid, [1]),
        'label': label,
        'dwell_time_ms': dwell_time,
        'brand': brand,
        'category': category,
        'gender': gender,
        'price_tier': price_tier,
        'like_count': like_count,
        'product_age_days': product_age_days,
    }


def _decode_fashion_example(example_bytes):
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
    }
    parsed = tf.io.parse_example(example_bytes, raw_desc)
    return parsed


class FashionRankingModelBase(MonolithModel):
    def __init__(self, params):
        super().__init__(params)
        self.p.serving.export_when_saving = True

    def model_fn(self, features, mode):
        # === Create embedding features ===
        embedding_features = ['product_id', 'user_id', 'brand', 'category', 'gender']
        for s_name in embedding_features:
            self.create_embedding_feature_column(s_name, occurrence_threshold=0)

        # Lookup all embeddings (32-dim each)
        prod_emb, user_emb, brand_emb, category_emb, gender_emb = self.lookup_embedding_slice(
            features=embedding_features, slice_name='vec', slice_dim=32)

        # === Numerical features (normalized) ===
        # Price tier (already bucketed, normalized to 0-1 range assuming 0-5 tiers)
        price_tier_norm = tf.cast(features['price_tier'], tf.float32) / 5.0

        # Log-normalized like count (popularity signal)
        log_like_count = tf.math.log1p(tf.cast(features['like_count'], tf.float32)) / 10.0

        # Product age (freshness signal, normalized by year)
        product_age_norm = tf.cast(features['product_age_days'], tf.float32) / 365.0

        # Stack numerical features into a single tensor
        numerical_features = tf.stack([
            price_tier_norm,
            log_like_count,
            product_age_norm,
        ], axis=1)

        # === Concatenate all features ===
        # Total dimensions: 32*5 (embeddings) + 3 (numerical) = 163-dim
        concat = tf.concat([
            user_emb,          # 32-dim
            prod_emb,          # 32-dim
            brand_emb,         # 32-dim
            category_emb,      # 32-dim
            gender_emb,        # 32-dim
            numerical_features # 3-dim
        ], axis=1)

        # === Shared embedding tower ===
        shared_tower = tf.keras.Sequential([
            tf.keras.layers.Dense(256, activation='relu', name='shared_dense_1'),
            tf.keras.layers.Dropout(0.2, name='shared_dropout_1'),
            tf.keras.layers.Dense(128, activation='relu', name='shared_dense_2'),
            tf.keras.layers.Dropout(0.2, name='shared_dropout_2'),
        ], name='shared_tower')

        shared_embedding = shared_tower(concat)

        # === Task-specific prediction heads ===
        # Head 1: Like prediction (binary classification)
        like_tower = tf.keras.Sequential([
            tf.keras.layers.Dense(64, activation='relu', name='like_dense_1'),
            tf.keras.layers.Dense(1, activation='sigmoid', name='like_pred')
        ], name='like_tower')
        like_pred = like_tower(shared_embedding)

        # Head 2: Dwell time prediction (regression)
        dwell_tower = tf.keras.Sequential([
            tf.keras.layers.Dense(64, activation='relu', name='dwell_dense_1'),
            tf.keras.layers.Dense(1, activation='relu', name='dwell_pred')  # ReLU ensures positive
        ], name='dwell_tower')
        dwell_pred = dwell_tower(shared_embedding)

        # === Extract labels ===
        label_binary = tf.cast(features['label'] > 0.5, tf.float32)
        dwell_time_actual = features['dwell_time_ms']

        # === Multi-objective loss ===
        # Like loss (binary cross-entropy)
        like_pred_squeezed = tf.squeeze(like_pred, axis=-1)
        like_loss = tf.reduce_mean(
            tf.keras.losses.binary_crossentropy(label_binary, like_pred_squeezed)
        )

        # Dwell time loss (MSE, normalized to be comparable to like_loss)
        dwell_pred_squeezed = tf.squeeze(dwell_pred, axis=-1)
        dwell_loss = tf.reduce_mean(
            tf.square(dwell_pred_squeezed - dwell_time_actual) / 10000.0  # Normalize by ~10 seconds
        )

        # Combined loss with weights (0.7 for like, 0.3 for dwell time)
        total_loss = 0.7 * like_loss + 0.3 * dwell_loss

        # Handle NaN
        total_loss = tf.where(tf.math.is_nan(total_loss),
                             tf.constant(0.0, dtype=tf.float32),
                             total_loss)

        # === Create combined score for ranking ===
        # Combine like probability with expected dwell time
        combined_score = 0.7 * like_pred_squeezed + 0.3 * (dwell_pred_squeezed / 10000.0)

        # === Export named outputs for serving ===
        user_embedding_out = tf.identity(user_emb, name="user_embedding")
        product_embedding_out = tf.identity(prod_emb, name="product_embedding")
        brand_embedding_out = tf.identity(brand_emb, name="brand_embedding")
        category_embedding_out = tf.identity(category_emb, name="category_embedding")
        like_score_out = tf.identity(like_pred_squeezed, name="like_score")
        dwell_score_out = tf.identity(dwell_pred_squeezed, name="expected_dwell_time")
        combined_score_out = tf.identity(combined_score, name="combined_score")

        self.add_extra_output(
            name="features_for_join",
            outputs={
                "user_embedding": user_embedding_out,
                "product_embedding": product_embedding_out,
                "brand_embedding": brand_embedding_out,
                "category_embedding": category_embedding_out,
                "like_score": like_score_out,
                "expected_dwell_time": dwell_score_out,
                "combined_score": combined_score_out,
            },
        )

        opt = tf.compat.v1.train.AdagradOptimizer(0.05)

        return EstimatorSpec(
            label=label_binary,
            pred=combined_score,  # Use combined score as main prediction
            head_name='rank',
            loss=total_loss,  # Multi-task loss
            optimizer=opt,
            classification=False)

    def serving_input_receiver_fn(self):
        input_ph = tf.compat.v1.placeholder(dtype=tf.string, shape=[None])
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
        }
        parsed = tf.io.parse_example(input_ph, raw_desc)
        features = {
            'user_id': tf.RaggedTensor.from_tensor(parsed['user_id']),
            'product_id': tf.RaggedTensor.from_tensor(parsed['product_id']),
            'label': parsed['label'],
            'dwell_time_ms': parsed['dwell_time_ms'],
            'brand': parsed['brand'],
            'category': parsed['category'],
            'gender': parsed['gender'],
            'price_tier': parsed['price_tier'],
            'like_count': parsed['like_count'],
            'product_age_days': parsed['product_age_days'],
        }
        return tf.estimator.export.ServingInputReceiver(features, {'examples': input_ph})


class FashionRankingBatchTraining(FashionRankingModelBase):
    def input_fn(self, mode):
        # Try BigQuery first if configured
        if FLAGS.bq_project and FLAGS.bq_dataset and FLAGS.bq_table:
            ds = self._try_bigquery_input()
            if ds is not None:
                return ds.batch(512, drop_remainder=True).map(_to_ragged).prefetch(tf.data.AUTOTUNE)

        # Fallback to CSV or stdin
        return self._csv_or_stdin_input()

    def _try_bigquery_input(self):
        """Load training data from BigQuery with JOIN to products table"""
        try:
            from google.cloud import bigquery
            import time

            logging.info("Loading training data from BigQuery...")

            # Build date filter
            date_filter = self._build_date_filter()

            # Build SQL query with JOIN to products table
            query = self._build_training_query(date_filter)

            logging.info(f"BigQuery query: {query}")

            # Execute query and write to temp table
            temp_table_id = self._execute_query_to_temp_table(query)

            # Read from temp table using TensorFlow I/O
            ds = self._read_temp_table_to_dataset(temp_table_id)

            logging.info("Successfully loaded training data from BigQuery")
            return ds

        except Exception as e:
            logging.warning(f"BigQuery input failed ({e}); falling back to CSV/stdin")
            return None

    def _build_date_filter(self):
        """Build date filter for BigQuery query"""
        if FLAGS.bq_start_date and FLAGS.bq_end_date:
            return f"DATE(TIMESTAMP_MILLIS(feature_event_time)) BETWEEN '{FLAGS.bq_start_date}' AND '{FLAGS.bq_end_date}'"
        else:
            return f"TIMESTAMP_MILLIS(feature_event_time) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {FLAGS.bq_days_back} DAY)"

    def _build_training_query(self, date_filter):
        """Build SQL query to JOIN training_samples with products table"""
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

            -- Use like_count from products table (not available in training samples yet)
            CAST(COALESCE(p.like_count, 0) AS INT64) as like_count,

            -- Calculate product age in days
            CAST(DATE_DIFF(
                CURRENT_DATE(),
                DATE(p.parsed_at),
                DAY
            ) AS INT64) as product_age_days

        FROM `{FLAGS.bq_project}.{FLAGS.bq_dataset}.{FLAGS.bq_table}` t
        LEFT JOIN `{FLAGS.bq_project}.{FLAGS.bq_products_dataset}.{FLAGS.bq_products_table}` p
            ON JSON_VALUE(t.action_data, '$.product_id') = p.product_id
        WHERE {date_filter}
            AND JSON_VALUE(t.action_data, '$.label') IS NOT NULL
            AND JSON_VALUE(t.feature_data, '$.user_id') IS NOT NULL
            AND JSON_VALUE(t.action_data, '$.product_id') IS NOT NULL
        ORDER BY t.feature_event_time
        """

    def _execute_query_to_temp_table(self, query):
        """Execute query and write results to temp table"""
        from google.cloud import bigquery
        import time

        bq_client = bigquery.Client(project=FLAGS.bq_project)
        temp_table_id = f"{FLAGS.bq_project}.{FLAGS.bq_dataset}.temp_batch_training_{int(time.time())}"

        logging.info(f"Creating temp table: {temp_table_id}")
        job_config = bigquery.QueryJobConfig(destination=temp_table_id)
        query_job = bq_client.query(query, job_config=job_config)
        query_job.result()  # Wait for completion

        # Get row count
        result = bq_client.query(f"SELECT COUNT(*) as cnt FROM `{temp_table_id}`").result()
        row_count = list(result)[0]['cnt']
        logging.info(f"Temp table created with {row_count:,} training samples")

        return temp_table_id

    def _read_temp_table_to_dataset(self, temp_table_id):
        """Read temp table into TensorFlow dataset using BigQuery Storage API"""
        from tensorflow_io.bigquery import BigQueryClient

        # Extract dataset and table from full table ID
        parts = temp_table_id.split('.')
        dataset_id = parts[1]
        table_id = parts[2]

        client = BigQueryClient()
        read_session = client.read_session(
            parent=f"projects/{FLAGS.bq_project}/locations/{FLAGS.bq_location}",
            project_id=FLAGS.bq_project,
            dataset_id=dataset_id,
            table_id=table_id,
            selected_fields=[
                'user_id', 'product_id', 'label', 'dwell_time_ms',
                'brand', 'category', 'gender', 'price_tier',
                'like_count', 'product_age_days'
            ],
            output_types=[
                tf.string, tf.string, tf.float64, tf.float64,
                tf.string, tf.string, tf.string, tf.int64,
                tf.int64, tf.int64
            ],
            requested_streams=FLAGS.bq_parallelism,
        )

        ds = read_session.parallel_read_rows()
        ds = ds.map(self._parse_bigquery_row)

        return ds

    def _parse_bigquery_row(self, user_id_str, product_id_str, label, dwell_time,
                            brand, category, gender, price_tier, like_count, product_age_days):
        """Parse BigQuery row into training example format"""
        # Hash string IDs to int64 (same as online training)
        user_id = tf.py_function(
            lambda x: hash(x.numpy().decode('utf-8')) % (2**63),
            [user_id_str],
            tf.int64
        )
        product_id = tf.py_function(
            lambda x: hash(x.numpy().decode('utf-8')) % (2**63),
            [product_id_str],
            tf.int64
        )

        return {
            'user_id': tf.reshape(user_id, [1]),
            'product_id': tf.reshape(product_id, [1]),
            'label': tf.cast(label, tf.float32),
            'dwell_time_ms': tf.cast(dwell_time, tf.float32),
            'brand': brand,
            'category': category,
            'gender': gender,
            'price_tier': price_tier,
            'like_count': like_count,
            'product_age_days': product_age_days,
        }

    def _csv_or_stdin_input(self):
        """Fallback to CSV or stdin input"""
        if FLAGS.fashion_batch_csv:
            files = tf.io.gfile.glob(FLAGS.fashion_batch_csv)
            ds = tf.data.TextLineDataset(files).map(_parse_csv_line)
        else:
            def gen():
                for line in sys.stdin:
                    parts = line.strip().split(',')
                    uid, pid, label, dwell_time = parts[0], parts[1], parts[2], parts[3]
                    brand, category, gender = parts[4], parts[5], parts[6]
                    price_tier, like_count, product_age_days = parts[7], parts[8], parts[9]
                    yield {
                        'user_id': [int(uid)],
                        'product_id': [int(pid)],
                        'label': float(label),
                        'dwell_time_ms': float(dwell_time),
                        'brand': brand,
                        'category': category,
                        'gender': gender,
                        'price_tier': int(price_tier),
                        'like_count': int(like_count),
                        'product_age_days': int(product_age_days),
                    }
            ds = tf.data.Dataset.from_generator(
                gen,
                output_signature={
                    'user_id': tf.TensorSpec([1], tf.int64),
                    'product_id': tf.TensorSpec([1], tf.int64),
                    'label': tf.TensorSpec([], tf.float32),
                    'dwell_time_ms': tf.TensorSpec([], tf.float32),
                    'brand': tf.TensorSpec([], tf.string),
                    'category': tf.TensorSpec([], tf.string),
                    'gender': tf.TensorSpec([], tf.string),
                    'price_tier': tf.TensorSpec([], tf.int64),
                    'like_count': tf.TensorSpec([], tf.int64),
                    'product_age_days': tf.TensorSpec([], tf.int64),
                }
            )
        return ds.batch(512, drop_remainder=True).map(_to_ragged).prefetch(tf.data.AUTOTUNE)


class FashionRankingOnlineTraining(FashionRankingModelBase):
    def input_fn(self, mode):
        # Configure Kafka authentication based on security protocol
        kafka_config = [
            f"security.protocol={FLAGS.kafka_security_protocol}",
        ]

        # Add SASL authentication for Confluent Cloud
        if FLAGS.kafka_security_protocol == 'SASL_SSL':
            kafka_config.extend([
                "sasl.mechanisms=PLAIN",
                f"sasl.username={FLAGS.kafka_username}",
                f"sasl.password={FLAGS.kafka_password}",
            ])
        # Support legacy SSL configuration via env vars
        elif os.environ.get('KAFKA_SECURITY_PROTOCOL') == 'SSL':
            kafka_config.extend([
                f"ssl.ca.location={os.environ.get('KAFKA_SSL_CA_LOCATION', '')}",
                f"ssl.certificate.location={os.environ.get('KAFKA_SSL_CERTIFICATE_LOCATION', '')}",
                f"ssl.key.location={os.environ.get('KAFKA_SSL_KEY_LOCATION', '')}",
            ])

        # Create Kafka dataset
        dataset = create_plain_kafka_dataset(
            topics=FLAGS.kafka_topics.split(','),  # Should be: training-sample-topic
            group_id=FLAGS.kafka_group_id,
            servers=FLAGS.kafka_servers,
            stream_timeout=FLAGS.stream_timeout_ms,
            poll_batch_size=16,
            configuration=kafka_config,
        )

        # Parse JSON training samples from Flink joiner
        def decode_json_training_sample(kafka_message):
            """
            Decode JSON training sample from Flink joiner output

            Expected JSON format:
            {
                "request_id": 1234567890,
                "feature_data": "{...}",  # JSON string containing user_id, embeddings, etc.
                "action_data": "{...}",   # JSON string containing product_id, action, label, dwell_time_ms
                "product_data": "{...}",  # JSON string containing brand, category, gender, etc.
                "feature_event_time": 1704067200000,
                "action_event_time": 1704067202500
            }
            """
            try:
                # Decode Kafka message bytes to string
                message_str = kafka_message.message.numpy().decode('utf-8')

                # Parse outer JSON
                sample = json.loads(message_str)

                # Parse nested JSON strings
                feature_data = json.loads(sample['feature_data'])
                action_data = json.loads(sample['action_data'])
                product_data = json.loads(sample.get('product_data', '{}'))

                # Extract IDs and label
                user_id_str = feature_data['user_id']
                product_id_str = action_data['product_id']
                label = action_data['label']
                dwell_time_ms = action_data.get('dwell_time_ms', 0.0)

                # Extract product features
                brand = product_data.get('brand', '')
                category = product_data.get('category', '')
                gender = product_data.get('gender', '')
                price_tier = product_data.get('price_tier', 0)
                like_count = product_data.get('like_count', 0)
                product_age_days = product_data.get('product_age_days', 0)

                # Hash string IDs to int64 (same as TFS client)
                user_id = hash(user_id_str) % (2**63)
                product_id = hash(product_id_str) % (2**63)

                return {
                    'user_id': [user_id],
                    'product_id': [product_id],
                    'label': label,
                    'dwell_time_ms': dwell_time_ms,
                    'brand': brand,
                    'category': category,
                    'gender': gender,
                    'price_tier': price_tier,
                    'like_count': like_count,
                    'product_age_days': product_age_days,
                }
            except (json.JSONDecodeError, KeyError, AttributeError) as e:
                logging.warning(f"Failed to decode training sample: {e}")
                # Return dummy example to avoid breaking training
                return {
                    'user_id': [0],
                    'product_id': [0],
                    'label': 0.0,
                    'dwell_time_ms': 0.0,
                    'brand': '',
                    'category': '',
                    'gender': '',
                    'price_tier': 0,
                    'like_count': 0,
                    'product_age_days': 0,
                }

        return dataset.map(decode_json_training_sample).map(_to_ragged)


class FashionRankingBatchStdin(FashionRankingModelBase):
    def input_fn(self, mode):
        def gen():
            for line in sys.stdin:
                parts = line.strip().split(',')
                uid, pid, label, dwell_time = parts[0], parts[1], parts[2], parts[3]
                brand, category, gender = parts[4], parts[5], parts[6]
                price_tier, like_count, product_age_days = parts[7], parts[8], parts[9]
                yield {
                    'user_id': [int(uid)],
                    'product_id': [int(pid)],
                    'label': float(label),
                    'dwell_time_ms': float(dwell_time),
                    'brand': brand,
                    'category': category,
                    'gender': gender,
                    'price_tier': int(price_tier),
                    'like_count': int(like_count),
                    'product_age_days': int(product_age_days),
                }
        return tf.data.Dataset.from_generator(
            gen,
            output_signature={
                'user_id': tf.TensorSpec([1], tf.int64),
                'product_id': tf.TensorSpec([1], tf.int64),
                'label': tf.TensorSpec([], tf.float32),
                'dwell_time_ms': tf.TensorSpec([], tf.float32),
                'brand': tf.TensorSpec([], tf.string),
                'category': tf.TensorSpec([], tf.string),
                'gender': tf.TensorSpec([], tf.string),
                'price_tier': tf.TensorSpec([], tf.int64),
                'like_count': tf.TensorSpec([], tf.int64),
                'product_age_days': tf.TensorSpec([], tf.int64),
            }
        ).batch(512, drop_remainder=True).map(_to_ragged).prefetch(tf.data.AUTOTUNE)


## ZooKeeper-based registration removed


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
    config = RunnerConfig(
        discovery_type=ServiceDiscoveryType.PRIMUS,
        tf_config=raw_tf_conf,
        save_checkpoints_steps=10000,
        enable_model_ckpt_info=True,
        num_ps=len(tf_conf.get('cluster', {}).get('ps', [])),
        num_workers=get_worker_count(tf_conf),
        server_type=tf_conf.get('task', {}).get('type', ''),
        index=tf_conf.get('task', {}).get('index', 0),
        zk_server=FLAGS.zk_server,
        base_name="fashion",
        bzid="monolith_serving_test",
    )

    if FLAGS.training_type == 'batch':
        params = FashionRankingBatchTraining.params().instantiate()
    elif FLAGS.training_type == 'stdin':
        params = FashionRankingBatchStdin.params().instantiate()
    else:
        params = FashionRankingOnlineTraining.params().instantiate()
        config.enable_realtime_training = True

    estimator = Estimator(params, config)
    estimator.train(max_steps=1000000)

    if FLAGS.training_type == 'batch':
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
    else:
        logging.info(f"Training type {FLAGS.training_type} - skipping model registration")


if __name__ == '__main__':
    logging.set_verbosity(logging.INFO)
    app.run(main)

