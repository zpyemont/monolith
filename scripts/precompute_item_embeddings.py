"""
Pre-compute item tower embeddings for all active products.

Calls the two-tower model's item_tower signature via gRPC on
TF Serving, runs inference on all active products, and writes
the 128-dim learned embeddings to PostgreSQL embeddings.product_vectors.

Usage:
    python precompute_item_embeddings.py \
        --serving_address monolith-fashion-two-tower-serving:2223 \
        --pg_host localhost \
        --pg_port 5432 \
        --pg_database looksy \
        --pg_user postgres \
        --pg_password secret \
        --batch_size 256
"""

import argparse
import asyncio
import logging
import time

import asyncpg
import grpc
import numpy as np
import tensorflow as tf
from tensorflow_serving.apis import predict_pb2, prediction_service_pb2_grpc

logger = logging.getLogger(__name__)


def build_example_proto(row: dict) -> bytes:
    """Build a serialized tf.train.Example from a product row."""
    import farmhash

    example = tf.train.Example()
    feat = example.features.feature

    # Hash product_id to int64 (matching training)
    pid_hash = farmhash.fingerprint64(str(row['product_id'])) % (2**63 - 1)
    feat['product_id'].int64_list.value.append(pid_hash)

    # Write categorical features as raw strings — the serving graph
    # hashes them via tf.strings.to_hash_bucket_fast to match training.
    # Do NOT pre-hash here or they get double-hashed.
    for col in ['brand', 'category', 'gender']:
        val = str(row.get(col, ''))
        feat[col].bytes_list.value.append(val.encode('utf-8'))

    # Numerical features
    feat['price_tier'].int64_list.value.append(int(row.get('price_tier', 3)))
    feat['like_count'].int64_list.value.append(int(row.get('like_count', 0)))
    feat['product_age_days'].int64_list.value.append(int(row.get('product_age_days', 0)))

    # Dense embeddings
    text_emb = row.get('text_embedding')
    if text_emb is not None:
        feat['text_embedding'].float_list.value.extend(text_emb)
    else:
        feat['text_embedding'].float_list.value.extend([0.0] * 1024)

    image_emb = row.get('image_embedding')
    if image_emb is not None:
        feat['image_embedding'].float_list.value.extend(image_emb)
    else:
        feat['image_embedding'].float_list.value.extend([0.0] * 512)

    return example.SerializeToString()


def predict_item_tower(stub, examples: list, timeout: float = 30.0) -> np.ndarray:
    """Call item_tower signature via gRPC and return item_vec embeddings."""
    request = predict_pb2.PredictRequest()
    request.model_spec.name = 'fashion_two_tower:entry'
    request.model_spec.signature_name = 'item_tower'
    request.inputs['examples'].CopyFrom(
        tf.make_tensor_proto(examples, dtype=tf.string)
    )

    response = stub.Predict(request, timeout=timeout)
    item_vec_proto = response.outputs['item_vec']
    shape = [d.size for d in item_vec_proto.tensor_shape.dim]
    item_vecs = np.array(item_vec_proto.float_val).reshape(shape)
    return item_vecs


async def fetch_active_products(conn: asyncpg.Connection) -> list:
    """Fetch all active products with their features."""
    rows = await conn.fetch("""
        SELECT
            p.product_id,
            COALESCE(p.brand, '') as brand,
            COALESCE(p.subcategory, p.category, '') as category,
            COALESCE(p.gender, 'unisex') as gender,
            CASE
                WHEN pr.price < 15 THEN 0
                WHEN pr.price < 30 THEN 1
                WHEN pr.price < 60 THEN 2
                WHEN pr.price < 100 THEN 3
                WHEN pr.price < 200 THEN 4
                ELSE 5
            END as price_tier,
            COALESCE(pr.like_count, 0) as like_count,
            COALESCE(
                EXTRACT(DAY FROM (CURRENT_TIMESTAMP - p.created_at))::int,
                0
            ) as product_age_days,
            e.text_embedding::real[] as text_embedding,
            e.image_embedding::real[] as image_embedding
        FROM catalog.products p
        JOIN catalog.product_pricing pr ON p.product_id = pr.product_id
        LEFT JOIN embeddings.product_vectors e ON p.product_id = e.product_id
        WHERE pr.is_active = true
          AND pr.availability NOT IN ('out of stock', 'sold out')
    """)
    return [dict(row) for row in rows]


async def write_embeddings(
    conn: asyncpg.Connection,
    embeddings: dict,
    batch_size: int = 100,
):
    """Write learned embeddings to PostgreSQL."""
    product_ids = list(embeddings.keys())
    total = len(product_ids)
    written = 0

    for i in range(0, total, batch_size):
        batch_ids = product_ids[i:i + batch_size]
        # Use a single batch upsert
        values = []
        for pid in batch_ids:
            emb = embeddings[pid]
            # pgvector expects string format: '[0.1,0.2,...]'
            emb_str = '[' + ','.join(str(float(v)) for v in emb) + ']'
            values.append((pid, emb_str))

        await conn.executemany("""
            INSERT INTO embeddings.product_vectors (product_id, learned_embedding)
            VALUES ($1, $2::vector)
            ON CONFLICT (product_id)
            DO UPDATE SET learned_embedding = $2::vector
        """, values)

        written += len(batch_ids)
        if written % 1000 == 0:
            logger.info(f"Written {written}/{total} embeddings")

    logger.info(f"Finished writing {total} embeddings")


async def rebuild_index(conn: asyncpg.Connection):
    """Rebuild HNSW index on learned_embedding column."""
    logger.info("Rebuilding HNSW index on learned_embedding...")
    await conn.execute("""
        DROP INDEX IF EXISTS idx_learned_embedding
    """)
    await conn.execute("""
        CREATE INDEX idx_learned_embedding ON embeddings.product_vectors
        USING hnsw (learned_embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    logger.info("HNSW index rebuilt successfully")


async def main():
    parser = argparse.ArgumentParser(description="Pre-compute item tower embeddings")
    parser.add_argument("--serving_address", required=True,
                        help="TF Serving gRPC address (host:port)")
    parser.add_argument("--pg_host", default="localhost")
    parser.add_argument("--pg_port", type=int, default=5432)
    parser.add_argument("--pg_database", default="looksy")
    parser.add_argument("--pg_user", default="postgres")
    parser.add_argument("--pg_password", default="")
    parser.add_argument("--batch_size", type=int, default=256)
    parser.add_argument("--rebuild_index", action="store_true", default=True)
    args = parser.parse_args()

    # Connect to TF Serving via gRPC
    logger.info(f"Connecting to TF Serving at {args.serving_address}")
    channel = grpc.insecure_channel(
        args.serving_address,
        options=[('grpc.max_receive_message_length', 100 * 1024 * 1024)],
    )
    stub = prediction_service_pb2_grpc.PredictionServiceStub(channel)

    # Connect to PostgreSQL
    dsn = f"postgresql://{args.pg_user}:{args.pg_password}@{args.pg_host}:{args.pg_port}/{args.pg_database}"
    conn = await asyncpg.connect(dsn)

    try:
        # Fetch all active products
        logger.info("Fetching active products...")
        products = await fetch_active_products(conn)
        logger.info(f"Found {len(products)} active products")

        if not products:
            logger.warning("No active products found, exiting")
            return

        # Run item tower inference in batches via gRPC
        all_embeddings = {}
        total_batches = (len(products) + args.batch_size - 1) // args.batch_size
        start_time = time.time()

        for batch_idx in range(total_batches):
            start = batch_idx * args.batch_size
            end = min(start + args.batch_size, len(products))
            batch = products[start:end]

            # Build TF Example protos
            examples = [build_example_proto(row) for row in batch]

            # Call item_tower via gRPC
            item_vecs = predict_item_tower(stub, examples)

            for j, row in enumerate(batch):
                all_embeddings[row['product_id']] = item_vecs[j]

            if (batch_idx + 1) % 10 == 0:
                elapsed = time.time() - start_time
                rate = (batch_idx + 1) / elapsed
                logger.info(
                    f"Batch {batch_idx + 1}/{total_batches} "
                    f"({rate:.1f} batches/sec, "
                    f"{len(all_embeddings)} products processed)"
                )

        elapsed = time.time() - start_time
        logger.info(
            f"Inference complete: {len(all_embeddings)} products in {elapsed:.1f}s "
            f"({len(all_embeddings)/elapsed:.0f} products/sec)"
        )

        # Write to PostgreSQL
        logger.info("Writing embeddings to PostgreSQL...")
        await write_embeddings(conn, all_embeddings)

        # Rebuild HNSW index
        if args.rebuild_index:
            await rebuild_index(conn)

        logger.info("Pre-computation pipeline complete!")

    finally:
        await conn.close()
        channel.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
