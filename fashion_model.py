import tensorflow as tf
from monolith.native_training import MonolithModel
from monolith.native_training import feature_utils
from monolith.native_training.data.parsers import sharding


class FashionRecommenderModel(MonolithModel):
    def __init__(self, params):
        super().__init__(params)
        self.embedding_dim = 128
        self.hidden_dims = [512, 256, 128, 64]
        
    def input_fn(self):
        """Define the input features for fashion recommendation"""
        feature_configs = []
        
        # === USER FEATURES ===
        # User ID - collisionless embedding (Monolith's key feature)
        feature_configs.append(
            feature_utils.make_feature_config(
                name="user_id",
                feature_type="id",
                embedding_dim=self.embedding_dim,
                vocab_size=10000000,  # Large user base
                combiner="sum"
            )
        )
        
        # User categorical features
        user_categoricals = [
            ("user_age_bucket", 6),      # 6 age buckets
            ("user_gender", 3),          # male/female/other
            ("user_style_profile", 8),   # minimalist, boho, streetwear, etc.
            ("size_range", 5),           # XS-S, M-L, XL+, etc.
        ]
        
        for name, vocab_size in user_categoricals:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="categorical",
                    embedding_dim=32,
                    vocab_size=vocab_size
                )
            )
        
        # User numerical features
        user_numericals = [
            "fashion_seasons_active",
            "avg_session_duration",
            "brand_loyalty_score",
            "trend_adoption_speed",
            "tops_affinity",
            "bottoms_affinity", 
            "dresses_affinity",
            "shoes_affinity",
            "accessories_affinity"
        ]
        
        for name in user_numericals:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="numerical",
                    normalizer="layer_norm"
                )
            )
        
        # === PRODUCT FEATURES ===
        # Product ID - main item embedding
        feature_configs.append(
            feature_utils.make_feature_config(
                name="product_id",
                feature_type="id", 
                embedding_dim=self.embedding_dim,
                vocab_size=50000000,  # Large product catalog
                combiner="sum"
            )
        )
        
        # Product categorical features
        product_categoricals = [
            ("category", 10),           # tops, bottoms, dresses, etc.
            ("subcategory", 50),        # blouse, jeans, sneakers, etc.  
            ("brand", 5000),            # thousands of fashion brands
            ("primary_color", 20),      # red, blue, black, etc.
            ("secondary_color", 20),
            ("pattern_type", 10),       # solid, stripes, floral, etc.
            ("silhouette", 12),         # fitted, loose, oversized, etc.
            ("material_primary", 15),   # cotton, denim, silk, etc.
            ("clothing_type", 8),       # casual, formal, athletic, etc.
            ("season_target", 5),       # spring, summer, fall, winter, year_round
            ("occasion", 8),            # work, party, casual, date, etc.
            ("brand_tier", 4),          # fast_fashion, mid_tier, luxury, designer
            ("price_tier", 5)           # budget to premium
        ]
        
        for name, vocab_size in product_categoricals:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="categorical",
                    embedding_dim=16,  # Smaller for product attributes
                    vocab_size=vocab_size
                )
            )
        
        # Product numerical features
        product_numericals = [
            "price",
            "product_age_days",
            "total_views_7d",
            "ctr_7d",
            "avg_dwell_time_7d", 
            "swipe_up_rate_7d",
            "trend_score",
            "season_relevance_score"
        ]
        
        for name in product_numericals:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="numerical",
                    normalizer="batch_norm"
                )
            )
        
        # === VISUAL FEATURES ===
        # Pre-computed image embeddings
        feature_configs.append(
            feature_utils.make_feature_config(
                name="image_embedding",
                feature_type="dense",
                dimension=512,  # From Vision API or custom CNN
                normalizer="layer_norm"
            )
        )
        
        # === CONTEXTUAL FEATURES ===
        context_categoricals = [
            ("hour_of_day", 24),
            ("day_of_week", 7),
            ("season", 4),
            ("device_type", 3)
        ]
        
        for name, vocab_size in context_categoricals:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="categorical",
                    embedding_dim=8,
                    vocab_size=vocab_size
                )
            )
        
        context_numericals = [
            "session_position",
            "session_duration_so_far", 
            "time_since_last_interaction"
        ]
        
        for name in context_numericals:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="numerical",
                    normalizer="batch_norm"
                )
            )
        
        # === SEQUENCE FEATURES ===
        # Recent interaction history
        sequence_features = [
            ("last_5_categories", 10, 5),     # last 5 categories viewed
            ("last_5_colors", 20, 5),         # last 5 colors engaged with  
            ("last_3_brands", 5000, 3),       # last 3 brands clicked
            ("last_3_price_tiers", 5, 3)      # last 3 price tiers
        ]
        
        for name, vocab_size, max_length in sequence_features:
            feature_configs.append(
                feature_utils.make_feature_config(
                    name=name,
                    feature_type="sequence",
                    embedding_dim=16,
                    vocab_size=vocab_size,
                    max_sequence_length=max_length,
                    combiner="mean"  # or "sum"
                )
            )
        
        return feature_configs
    
    def model_fn(self, features, mode):
        """Define the neural network architecture"""
        
        # === EMBEDDING LAYER ===
        # Get all embeddings from Monolith's collisionless tables
        user_emb = self.get_embedding("user_id", features["user_id"])
        product_emb = self.get_embedding("product_id", features["product_id"])
        
        # Category embeddings
        category_embs = []
        for cat_feature in ["category", "brand", "primary_color", "silhouette"]:
            emb = self.get_embedding(cat_feature, features[cat_feature])
            category_embs.append(emb)
        
        # Sequence embeddings
        sequence_embs = []
        for seq_feature in ["last_5_categories", "last_5_colors", "last_3_brands"]:
            seq_emb = self.get_embedding(seq_feature, features[seq_feature])
            sequence_embs.append(seq_emb)
        
        # === FEATURE INTERACTIONS ===
        # Fashion-specific interactions
        user_style_emb = self.get_embedding("user_style_profile", features["user_style_profile"])
        product_style_match = tf.reduce_sum(user_style_emb * product_emb, axis=1, keepdims=True)
        
        # Color compatibility 
        user_color_pref = self.get_embedding("user_preferred_colors", features["user_color_preferences"]) 
        product_color_emb = self.get_embedding("primary_color", features["primary_color"])
        color_match = tf.reduce_sum(user_color_pref * product_color_emb, axis=1, keepdims=True)
        
        # Price sensitivity
        price_match = tf.abs(features["user_avg_price_clicked"] - features["price"])
        price_match = tf.expand_dims(price_match, axis=1)
        
        # === CONCATENATE ALL FEATURES ===
        all_embeddings = [user_emb, product_emb] + category_embs + sequence_embs
        
        # Add numerical features
        numerical_features = [
            features["price"],
            features["session_position"],
            features["total_views_7d"],
            features["ctr_7d"],
            features["user_tops_affinity"],
            features["user_bottoms_affinity"],
            product_style_match,
            color_match,
            price_match
        ]
        
        # Stack numerical features
        numerical_tensor = tf.stack([tf.cast(f, tf.float32) for f in numerical_features], axis=1)
        
        # Concatenate embeddings and numerical features
        concat_features = tf.concat(all_embeddings + [numerical_tensor], axis=1)
        
        # === DEEP NEURAL NETWORK ===
        hidden = concat_features
        
        for i, dim in enumerate(self.hidden_dims):
            hidden = tf.layers.dense(
                hidden, 
                dim,
                activation=tf.nn.relu,
                name=f"hidden_{i}",
                kernel_regularizer=tf.contrib.layers.l2_regularizer(0.001)
            )
            hidden = tf.layers.dropout(hidden, rate=0.2, training=(mode == tf.estimator.ModeKeys.TRAIN))
        
        # === OUTPUT LAYER ===
        # Multi-task learning for fashion
        logits_engagement = tf.layers.dense(hidden, 1, name="engagement_logits")
        logits_click = tf.layers.dense(hidden, 1, name="click_logits") 
        logits_share = tf.layers.dense(hidden, 1, name="share_logits")
        
        predictions = {
            "engagement_score": tf.nn.sigmoid(logits_engagement),
            "click_probability": tf.nn.sigmoid(logits_click),
            "share_probability": tf.nn.sigmoid(logits_share),
            "user_embedding": user_emb,
            "product_embedding": product_emb
        }
        
        if mode == tf.estimator.ModeKeys.PREDICT:
            return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)
        
        # === LOSS FUNCTION ===
        # Multi-task loss for different engagement types
        engagement_loss = tf.losses.sigmoid_cross_entropy(
            features["engagement_label"], logits_engagement
        )
        click_loss = tf.losses.sigmoid_cross_entropy(
            features["click_label"], logits_click  
        )
        share_loss = tf.losses.sigmoid_cross_entropy(
            features["share_label"], logits_share
        )
        
        # Weighted combination
        total_loss = 0.5 * engagement_loss + 0.3 * click_loss + 0.2 * share_loss
        
        # === TRAINING ===
        if mode == tf.estimator.ModeKeys.TRAIN:
            optimizer = tf.train.AdamOptimizer(learning_rate=0.001)
            train_op = optimizer.minimize(total_loss, global_step=tf.train.get_global_step())
            
            return tf.estimator.EstimatorSpec(
                mode=mode,
                loss=total_loss,
                train_op=train_op
            )
        
        # === EVALUATION ===
        eval_metrics = {
            "auc_engagement": tf.metrics.auc(features["engagement_label"], predictions["engagement_score"]),
            "auc_click": tf.metrics.auc(features["click_label"], predictions["click_probability"]),
            "precision_engagement": tf.metrics.precision(features["engagement_label"], 
                                                       tf.round(predictions["engagement_score"]))
        }
        
        return tf.estimator.EstimatorSpec(
            mode=mode,
            loss=total_loss,
            eval_metric_ops=eval_metrics
        )

    def get_embedding(self, feature_name, feature_values):
        """Helper to get embeddings from Monolith's collisionless tables"""
        # This uses Monolith's internal embedding lookup
        return self.embedding_lookup(feature_name, feature_values)


# === TRAINING DATA FORMAT ===
def create_training_example():
    """Example of what your training data should look like"""
    return {
        # User features
        "user_id": "alice_123",
        "user_age_bucket": 2,  # 23-29
        "user_gender": 1,      # female  
        "user_style_profile": 3,  # streetwear
        "size_range": 1,       # M-L
        
        # User behavior
        "fashion_seasons_active": 4,
        "avg_session_duration": 8.5,
        "brand_loyalty_score": 0.3,
        "tops_affinity": 0.8,
        "bottoms_affinity": 0.6,
        "shoes_affinity": 0.9,
        
        # Product features  
        "product_id": "nike_red_sneaker_456",
        "category": 4,         # shoes
        "subcategory": 12,     # sneakers
        "brand": 245,          # nike
        "primary_color": 5,    # red
        "silhouette": 2,       # fitted
        "clothing_type": 0,    # casual
        "occasion": 2,         # casual
        "season_target": 4,    # year_round
        
        # Product metrics
        "price": 89.99,
        "product_age_days": 30,
        "total_views_7d": 15420,
        "ctr_7d": 0.067,
        "avg_dwell_time_7d": 3.2,
        "swipe_up_rate_7d": 0.23,
        
        # Visual features
        "image_embedding": [0.1, 0.8, -0.3, ...],  # 512-dim from Vision API
        
        # Context
        "hour_of_day": 14,     # 2 PM
        "day_of_week": 2,      # Tuesday  
        "season": 1,           # summer
        "session_position": 3,
        "session_duration_so_far": 5.2,
        
        # Sequence features
        "last_5_categories": [4, 1, 4, 2, 1],     # shoes, tops, shoes, bottoms, tops
        "last_5_colors": [5, 8, 5, 12, 1],        # red, black, red, white, blue
        "last_3_brands": [245, 156, 892],         # nike, adidas, zara
        
        # Labels (what actually happened)
        "engagement_label": 0,   # swipe up = negative
        "click_label": 0,        # didn't click
        "share_label": 0,        # didn't share
        "dwell_time": 1.2       # only viewed for 1.2 seconds
    }


# === SERVING CONFIGURATION ===
class FashionServingConfig:
    """Configuration for serving the trained model"""
    
    @staticmethod
    def serving_input_receiver_fn():
        """Define what the serving endpoint expects"""
        feature_placeholders = {
            # Required for real-time prediction
            "user_id": tf.placeholder(tf.string, [None], name="user_id"),
            "candidate_product_ids": tf.placeholder(tf.string, [None, None], name="candidate_products"),
            
            # Context (provided by Next.js)
            "hour_of_day": tf.placeholder(tf.int32, [None], name="hour"),
            "session_position": tf.placeholder(tf.int32, [None], name="session_pos"),
            "session_duration_so_far": tf.placeholder(tf.float32, [None], name="session_duration"),
            
            # Recent history
            "last_5_categories": tf.placeholder(tf.int32, [None, 5], name="recent_categories"),
            "last_5_colors": tf.placeholder(tf.int32, [None, 5], name="recent_colors")
        }
        
        return tf.estimator.export.ServingInputReceiver(
            features=feature_placeholders,
            receiver_tensors=feature_placeholders
        )


# === USAGE EXAMPLE ===
def train_fashion_model():
    """How you would train this model"""
    
    params = {
        "model_dir": "/path/to/fashion_model",
        "save_checkpoints_steps": 1000,
        "real_time_training": True,  # Monolith's key feature
        "embedding_update_freq": 100  # Update embeddings every 100 steps
    }
    
    model = FashionRecommenderModel(params)
    
    # Training data from BigQuery via Kafka
    train_input_fn = lambda: create_training_dataset_from_kafka()
    
    model.train(input_fn=train_input_fn, steps=100000)


def create_training_dataset_from_kafka():
    """Read training examples from Kafka (after Flink joining)"""
    
    # This would connect to your joined training examples topic
    dataset = tf.data.experimental.make_csv_dataset(
        file_pattern="gs://your-bucket/fashion-training-data/*.csv",
        batch_size=512,
        label_name=["engagement_label", "click_label", "share_label"],
        num_epochs=None,  # Real-time training
        shuffle=True,
        shuffle_buffer_size=10000
    )
    
    return dataset


# === PREDICTION EXAMPLE ===
def get_fashion_recommendations(user_id, candidate_products, context):
    """How your enhancement service would call this"""
    
    model = FashionRecommenderModel.load("/path/to/trained/model")
    
    input_data = {
        "user_id": [user_id],
        "candidate_product_ids": [candidate_products],
        "hour_of_day": [context["hour"]],
        "session_position": [context["position"]],
        "last_5_categories": [context["recent_categories"]]
    }
    
    predictions = model.predict(input_data)
    
    # Returns engagement scores for each candidate product
    return {
        "product_scores": predictions["engagement_score"],
        "user_embedding": predictions["user_embedding"],  # For feature publishing
        "product_embeddings": predictions["product_embedding"]  # For feature publishing
    }