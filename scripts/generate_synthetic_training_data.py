#!/usr/bin/env python3
"""
Generate synthetic training data for fashion_model_two_tower.py.

This script generates synthetic user interaction data using REAL product embeddings
from PostgreSQL. It creates diverse user profiles with fashion preferences and
simulates realistic interactions (positive and negative).

Output Format:
    CSV with 1546 columns (no header):
    uid,pid,label,dwell_time,brand,subcategory,gender,price_tier,like_count,product_age_days,
    text_emb_0,...,text_emb_1023,image_emb_0,...,image_emb_511

Usage:
    python scripts/generate_synthetic_training_data.py \
        --pg_host 127.0.0.1 --pg_port 5433 \
        --pg_user postgres --pg_password secret \
        --pg_database looksy \
        --num_users 1000 \
        --min_interactions 50 --max_interactions 200 \
        --positive_ratio 0.7 \
        --output_file synthetic_training_data.csv \
        --seed 42
"""

import argparse
import asyncio
import csv
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Set

import asyncpg
import farmhash
import numpy as np


# Category adjacency map for subcategory clustering
CATEGORY_ADJACENCY = {
    # === CLOTHING - TOPS ===
    'T-Shirts': ['Tank Tops', 'Sweatshirts', 'Hoodies', 'Blouses'],
    'Blouses': ['Shirts', 'T-Shirts', 'Tank Tops', 'Sweaters'],
    'Shirts': ['Blouses', 'Polos', 'T-Shirts', 'Sweaters'],
    'Tank Tops': ['T-Shirts', 'Sports Bras', 'Workout Tops'],
    'Sweaters': ['Cardigans', 'Turtlenecks', 'Hoodies', 'Sweatshirts'],
    'Cardigans': ['Sweaters', 'Blazers', 'Hoodies', 'Jackets'],
    'Hoodies': ['Sweatshirts', 'Sweaters', 'T-Shirts', 'Jackets'],
    'Sweatshirts': ['Hoodies', 'T-Shirts', 'Sweaters', 'Tank Tops'],
    'Polos': ['Shirts', 'T-Shirts', 'Sweaters', 'Tank Tops'],
    'Turtlenecks': ['Sweaters', 'Cardigans', 'Shirts', 'Blouses'],
    # === CLOTHING - BOTTOMS ===
    'Jeans': ['Pants', 'Shorts', 'Leggings', 'Joggers'],
    'Pants': ['Jeans', 'Shorts', 'Skirts', 'Joggers'],
    'Leggings': ['Joggers', 'Sweatpants', 'Shorts', 'Workout Bottoms'],
    'Shorts': ['Jeans', 'Pants', 'Skirts', 'Athletic Shorts'],
    'Skirts': ['Dresses', 'Shorts', 'Pants', 'Jeans'],
    'Joggers': ['Sweatpants', 'Leggings', 'Pants', 'Athletic Shorts'],
    'Sweatpants': ['Joggers', 'Leggings', 'Lounge Pants', 'Shorts'],
    # === CLOTHING - DRESSES & ONE-PIECES ===
    'Dresses': ['Jumpsuits', 'Skirts', 'Rompers', 'Cocktail Dresses'],
    'Jumpsuits': ['Dresses', 'Rompers', 'Overalls', 'Pants'],
    'Rompers': ['Dresses', 'Jumpsuits', 'Shorts', 'Overalls'],
    'Overalls': ['Jumpsuits', 'Jeans', 'Pants', 'Rompers'],
    # === CLOTHING - OUTERWEAR ===
    'Jackets': ['Coats', 'Blazers', 'Vests', 'Hoodies'],
    'Coats': ['Jackets', 'Parkas', 'Blazers', 'Vests'],
    'Blazers': ['Jackets', 'Cardigans', 'Coats', 'Suits'],
    'Vests': ['Jackets', 'Coats', 'Cardigans', 'Blazers'],
    'Parkas': ['Coats', 'Jackets', 'Vests', 'Hoodies'],
    # === CLOTHING - ACTIVEWEAR ===
    'Sports Bras': ['Workout Tops', 'Tank Tops', 'Athletic Shorts', 'Leggings'],
    'Workout Tops': ['Sports Bras', 'Tank Tops', 'T-Shirts', 'Workout Bottoms'],
    'Workout Bottoms': ['Leggings', 'Athletic Shorts', 'Joggers', 'Shorts'],
    'Tracksuits': ['Hoodies', 'Sweatpants', 'Joggers', 'Athletic Shorts'],
    'Athletic Shorts': ['Workout Bottoms', 'Shorts', 'Leggings', 'Joggers'],
    # === CLOTHING - LOUNGEWEAR & SLEEPWEAR ===
    'Pajama Sets': ['Sleep Shirts', 'Robes', 'Lounge Pants', 'Lounge Tops'],
    'Sleep Shirts': ['Pajama Sets', 'Robes', 'Tank Tops', 'T-Shirts'],
    'Robes': ['Pajama Sets', 'Sleep Shirts', 'Lounge Tops', 'Cardigans'],
    'Lounge Pants': ['Sweatpants', 'Joggers', 'Pajama Sets', 'Leggings'],
    'Lounge Tops': ['Sweatshirts', 'Hoodies', 'T-Shirts', 'Robes'],
    # === CLOTHING - SWIMWEAR & BEACHWEAR ===
    'Bikinis': ['One-Pieces', 'Cover-Ups', 'Rash Guards', 'Shorts'],
    'One-Pieces': ['Bikinis', 'Rash Guards', 'Cover-Ups', 'Dresses'],
    'Cover-Ups': ['Bikinis', 'One-Pieces', 'Dresses', 'Robes'],
    'Rash Guards': ['Bikinis', 'One-Pieces', 'Workout Tops', 'Athletic Shorts'],
    # === CLOTHING - FORMALWEAR ===
    'Evening Gowns': ['Cocktail Dresses', 'Dresses', 'Blazers', 'Heels'],
    'Cocktail Dresses': ['Evening Gowns', 'Dresses', 'Heels', 'Pumps'],
    'Suits': ['Blazers', 'Pants', 'Skirts', 'Tuxedos'],
    'Tuxedos': ['Suits', 'Shirts', 'Formal Shoes', 'Blazers'],
    # === FOOTWEAR - SNEAKERS ===
    'Lifestyle Sneakers': ['Running Shoes', 'Training Shoes', 'Skateboard Shoes', 'Slides'],
    'Running Shoes': ['Training Shoes', 'Lifestyle Sneakers', 'Athletic Shorts', 'Leggings'],
    'Training Shoes': ['Running Shoes', 'Lifestyle Sneakers', 'Basketball Shoes', 'Workout Bottoms'],
    'Basketball Shoes': ['Training Shoes', 'Lifestyle Sneakers', 'Athletic Shorts', 'Joggers'],
    'Skateboard Shoes': ['Lifestyle Sneakers', 'Slides', 'Shorts', 'Jeans'],
    # === FOOTWEAR - BOOTS ===
    'Ankle Boots': ['Knee-High Boots', 'Desert Boots', 'Jeans', 'Leggings'],
    'Knee-High Boots': ['Over-the-Knee Boots', 'Ankle Boots', 'Skirts', 'Dresses'],
    'Over-the-Knee Boots': ['Knee-High Boots', 'Ankle Boots', 'Skirts', 'Dresses'],
    'Hiking Boots': ['Work Boots', 'Desert Boots', 'Jeans', 'Pants'],
    'Work Boots': ['Hiking Boots', 'Desert Boots', 'Jeans', 'Pants'],
    'Desert Boots': ['Ankle Boots', 'Work Boots', 'Jeans', 'Pants'],
    # === FOOTWEAR - SANDALS & SLIDES ===
    'Flat Sandals': ['Heeled Sandals', 'Slides', 'Flip-Flops', 'Shorts'],
    'Heeled Sandals': ['Flat Sandals', 'Pumps', 'Wedges', 'Dresses'],
    'Slides': ['Flat Sandals', 'Flip-Flops', 'House Slippers', 'Shorts'],
    'Flip-Flops': ['Slides', 'Flat Sandals', 'Shorts', 'Cover-Ups'],
    # === FOOTWEAR - HEELS ===
    'Pumps': ['Stilettos', 'Block Heels', 'Kitten Heels', 'Dresses'],
    'Stilettos': ['Pumps', 'Heeled Sandals', 'Block Heels', 'Evening Gowns'],
    'Block Heels': ['Pumps', 'Wedges', 'Ankle Boots', 'Skirts'],
    'Wedges': ['Block Heels', 'Heeled Sandals', 'Pumps', 'Dresses'],
    'Kitten Heels': ['Pumps', 'Ballet Flats', 'Mules', 'Skirts'],
    # === FOOTWEAR - FLATS & LOAFERS ===
    'Ballet Flats': ['Loafers', 'Mules', 'Kitten Heels', 'Skirts'],
    'Loafers': ['Oxfords', 'Ballet Flats', 'Boat Shoes', 'Pants'],
    'Mules': ['Ballet Flats', 'Slides', 'Kitten Heels', 'Jeans'],
    'Oxfords': ['Loafers', 'Boat Shoes', 'Ankle Boots', 'Pants'],
    'Boat Shoes': ['Loafers', 'Oxfords', 'Shorts', 'Jeans'],
    # === FOOTWEAR - SLIPPERS ===
    'House Slippers': ['Slide Slippers', 'Robes', 'Lounge Pants', 'Pajama Sets'],
    'Slide Slippers': ['House Slippers', 'Slides', 'Robes', 'Lounge Tops'],
    # === ACCESSORIES - BAGS ===
    'Handbags': ['Crossbody Bags', 'Clutches', 'Backpacks', 'Wallets'],
    'Crossbody Bags': ['Handbags', 'Belt Bags', 'Backpacks', 'Wallets'],
    'Clutches': ['Handbags', 'Wallets', 'Evening Gowns', 'Heels'],
    'Backpacks': ['Crossbody Bags', 'Handbags', 'Belt Bags', 'Sneakers'],
    'Belt Bags': ['Crossbody Bags', 'Backpacks', 'Wallets', 'Jeans'],
    'Wallets': ['Handbags', 'Clutches', 'Belt Bags', 'Crossbody Bags'],
    # === ACCESSORIES - JEWELRY ===
    'Necklaces': ['Earrings', 'Bracelets', 'Rings', 'Dresses'],
    'Earrings': ['Necklaces', 'Bracelets', 'Rings', 'Blouses'],
    'Bracelets': ['Rings', 'Necklaces', 'Earrings', 'Watches'],
    'Rings': ['Bracelets', 'Necklaces', 'Earrings', 'Anklets'],
    'Anklets': ['Rings', 'Bracelets', 'Sandals', 'Heels'],
    # === ACCESSORIES - HATS & HEADWEAR ===
    'Hats': ['Headbands', 'Sunglasses', 'Scarves', 'Jackets'],
    'Headbands': ['Hair Clips', 'Hats', 'Earrings', 'Dresses'],
    'Hair Clips': ['Headbands', 'Earrings', 'Necklaces', 'T-Shirts'],
    # === ACCESSORIES - SCARVES & WRAPS ===
    'Scarves': ['Shawls', 'Hats', 'Gloves', 'Jackets'],
    'Shawls': ['Scarves', 'Robes', 'Evening Gowns', 'Cardigans'],
    # === ACCESSORIES - MISC ===
    'Belts': ['Jeans', 'Pants', 'Skirts', 'Dresses'],
    'Sunglasses': ['Hats', 'Scarves', 'Cover-Ups', 'Sandals'],
    'Watches': ['Bracelets', 'Rings', 'Shirts', 'Suits'],
    'Gloves': ['Scarves', 'Hats', 'Coats', 'Jackets'],
}


@dataclass
class Product:
    """Product with embeddings from PostgreSQL."""
    product_id: str
    brand: str
    subcategory: str
    gender: str
    price_tier: int
    like_count: int
    product_age_days: int
    text_embedding: List[float]
    image_embedding: List[float]


@dataclass
class UserProfile:
    """Synthetic user profile with fashion preferences."""
    user_id: str
    preferred_brands: List[str]
    preferred_subcats: List[str]
    gender_pref: str
    price_tier_center: int
    num_interactions: int


class ProductIndexes:
    """Fast lookup indexes for sampling products."""
    def __init__(self):
        self.by_brand: Dict[str, List[Product]] = defaultdict(list)
        self.by_subcategory: Dict[str, List[Product]] = defaultdict(list)
        self.by_gender: Dict[str, List[Product]] = defaultdict(list)
        self.by_price_tier: Dict[int, List[Product]] = defaultdict(list)
        self.all_products: List[Product] = []
        self.by_id: Dict[str, Product] = {}

    def add(self, product: Product):
        """Add a product to all indexes."""
        self.all_products.append(product)
        self.by_id[product.product_id] = product
        self.by_brand[product.brand].append(product)
        self.by_subcategory[product.subcategory].append(product)
        self.by_gender[product.gender].append(product)
        self.by_price_tier[product.price_tier].append(product)

    def get_brand_distribution(self) -> Dict[str, int]:
        """Get brand counts for weighted sampling."""
        return {brand: len(products) for brand, products in self.by_brand.items()}


def hash_id(id_str: str) -> int:
    """Hash an ID string to int64 using FarmHash (matches training)."""
    return farmhash.fingerprint64(id_str) % (2**63 - 1)


def sanitize_csv_field(value: str) -> str:
    """Replace commas with spaces (CSV parser splits on comma)."""
    return value.replace(',', ' ')


async def fetch_products(
    pg_host: str,
    pg_port: int,
    pg_user: str,
    pg_password: str,
    pg_database: str
) -> ProductIndexes:
    """Fetch all products with embeddings from PostgreSQL."""

    print("Connecting to PostgreSQL...", file=sys.stderr)
    conn = await asyncpg.connect(
        host=pg_host,
        port=pg_port,
        user=pg_user,
        password=pg_password,
        database=pg_database
    )

    try:
        query = """
        SELECT p.product_id, COALESCE(p.brand, '') as brand,
            COALESCE(p.subcategory, p.category, '') as subcategory,
            COALESCE(p.gender, 'unisex') as gender,
            CASE WHEN pr.price < 15 THEN 0 WHEN pr.price < 30 THEN 1
                 WHEN pr.price < 60 THEN 2 WHEN pr.price < 100 THEN 3
                 WHEN pr.price < 200 THEN 4 ELSE 5 END as price_tier,
            COALESCE(pr.like_count, 0) as like_count,
            COALESCE(EXTRACT(DAY FROM (NOW() - p.created_at))::int, 0) as product_age_days,
            e.text_embedding::real[], e.image_embedding::real[]
        FROM catalog.products p
        JOIN catalog.product_pricing pr ON p.product_id = pr.product_id
        JOIN embeddings.product_vectors e ON p.product_id = e.product_id
        WHERE pr.is_active = true
          AND e.text_embedding IS NOT NULL AND e.image_embedding IS NOT NULL
        """

        print("Fetching products from database...", file=sys.stderr)
        rows = await conn.fetch(query)

        indexes = ProductIndexes()
        skipped = 0
        for row in rows:
            text_emb = list(row['text_embedding'])
            image_emb = list(row['image_embedding'])
            if len(text_emb) != 1024 or len(image_emb) != 512:
                skipped += 1
                continue
            product = Product(
                product_id=row['product_id'],
                brand=row['brand'],
                subcategory=row['subcategory'],
                gender=row['gender'],
                price_tier=row['price_tier'],
                like_count=row['like_count'],
                product_age_days=row['product_age_days'],
                text_embedding=text_emb,
                image_embedding=image_emb
            )
            indexes.add(product)

        if skipped:
            print(f"  Skipped {skipped} products with wrong embedding dimensions", file=sys.stderr)

        print(f"Fetched {len(indexes.all_products)} products", file=sys.stderr)
        print(f"  Brands: {len(indexes.by_brand)}", file=sys.stderr)
        print(f"  Subcategories: {len(indexes.by_subcategory)}", file=sys.stderr)
        print(f"  Genders: {len(indexes.by_gender)}", file=sys.stderr)

        return indexes

    finally:
        await conn.close()


def expand_subcategory_cluster(
    seed_subcat: str,
    catalog_subcats: Set[str],
    num_neighbors: int = 2
) -> List[str]:
    """
    Expand a seed subcategory to include adjacent subcategories.
    Only return subcategories that exist in the catalog.
    """
    cluster = {seed_subcat}

    # Add neighbors from adjacency map
    if seed_subcat in CATEGORY_ADJACENCY:
        neighbors = CATEGORY_ADJACENCY[seed_subcat]
        # Filter to subcategories that exist in catalog
        valid_neighbors = [n for n in neighbors if n in catalog_subcats]
        # Take up to num_neighbors
        selected = random.sample(valid_neighbors, min(num_neighbors, len(valid_neighbors)))
        cluster.update(selected)

    return list(cluster)


def generate_user_profiles(
    num_users: int,
    min_interactions: int,
    max_interactions: int,
    indexes: ProductIndexes
) -> List[UserProfile]:
    """Generate synthetic user profiles with realistic preferences."""

    print(f"Generating {num_users} user profiles...", file=sys.stderr)

    # Get brand distribution for weighted sampling
    brand_counts = indexes.get_brand_distribution()
    brands = list(brand_counts.keys())
    brand_weights = [brand_counts[b] for b in brands]

    # Get available subcategories from catalog
    catalog_subcats = set(indexes.by_subcategory.keys())
    # Filter to subcategories that are keys in adjacency map
    seed_subcats = [s for s in catalog_subcats if s in CATEGORY_ADJACENCY]

    # Gender distribution: Women 65%, Men 16%, Unisex 19%
    gender_choices = ['Women', 'Men', 'Unisex']
    gender_weights = [0.65, 0.16, 0.19]

    # Price tier distribution: center-weighted toward 1-3
    price_tier_choices = [0, 1, 2, 3, 4, 5]
    price_tier_weights = [0.10, 0.25, 0.30, 0.25, 0.08, 0.02]

    profiles = []
    for i in range(num_users):
        user_id = f"synthetic_user_{i}"

        # Select 1-2 preferred brands (weighted by catalog distribution)
        num_brands = random.choices([1, 2], weights=[0.6, 0.4])[0]
        preferred_brands = random.choices(brands, weights=brand_weights, k=num_brands)

        # Select 2-3 preferred subcategories (adjacent cluster)
        if seed_subcats:
            seed = random.choice(seed_subcats)
            num_subcats = random.choices([2, 3], weights=[0.5, 0.5])[0]
            preferred_subcats = expand_subcategory_cluster(seed, catalog_subcats, num_subcats - 1)
        else:
            # Fallback if no adjacency mapping available
            preferred_subcats = random.sample(list(catalog_subcats), min(3, len(catalog_subcats)))

        # Gender preference
        gender_pref = random.choices(gender_choices, weights=gender_weights)[0]

        # Price tier center
        price_tier_center = random.choices(price_tier_choices, weights=price_tier_weights)[0]

        # Number of interactions
        num_interactions = random.randint(min_interactions, max_interactions)

        profile = UserProfile(
            user_id=user_id,
            preferred_brands=preferred_brands,
            preferred_subcats=preferred_subcats,
            gender_pref=gender_pref,
            price_tier_center=price_tier_center,
            num_interactions=num_interactions
        )
        profiles.append(profile)

    print(f"Generated {len(profiles)} user profiles", file=sys.stderr)
    return profiles


def sample_positive_product(
    user: UserProfile,
    indexes: ProductIndexes,
    used_products: Set[str]
) -> Product:
    """
    Sample a positive product matching user preferences.
    Multi-match products are sampled with higher probability.
    """
    # Build ID sets per criterion for efficient scoring
    brand_ids = set()
    for brand in user.preferred_brands:
        brand_ids.update(p.product_id for p in indexes.by_brand.get(brand, []))

    subcat_ids = set()
    for subcat in user.preferred_subcats:
        subcat_ids.update(p.product_id for p in indexes.by_subcategory.get(subcat, []))

    gender_ids = set(p.product_id for p in indexes.by_gender.get(user.gender_pref, []))
    if user.gender_pref != 'Unisex':
        gender_ids.update(p.product_id for p in indexes.by_gender.get('Unisex', []))

    price_ids = set()
    for tier in [user.price_tier_center - 1, user.price_tier_center, user.price_tier_center + 1]:
        if 0 <= tier <= 5:
            price_ids.update(p.product_id for p in indexes.by_price_tier.get(tier, []))

    # Score unique product IDs (skip used)
    match_scores: Dict[str, int] = {}
    for pid in (brand_ids | subcat_ids | gender_ids | price_ids) - used_products:
        score = 0
        if pid in brand_ids:
            score += 2
        if pid in subcat_ids:
            score += 2
        if pid in gender_ids:
            score += 1
        if pid in price_ids:
            score += 1
        match_scores[pid] = score

    if not match_scores:
        # Fallback: random product
        available = [p for p in indexes.all_products if p.product_id not in used_products]
        if not available:
            raise ValueError("No more unused products available")
        return random.choice(available)

    # Sample weighted by match score
    product_ids = list(match_scores.keys())
    scores = [match_scores[pid] for pid in product_ids]
    selected_id = random.choices(product_ids, weights=scores)[0]

    return indexes.by_id[selected_id]


def sample_negative_product(
    user: UserProfile,
    indexes: ProductIndexes,
    used_products: Set[str]
) -> Product:
    """
    Sample a negative product NOT matching user preferences.
    """
    # Get products NOT in preferred brands or subcategories
    excluded_ids = set()
    for brand in user.preferred_brands:
        for p in indexes.by_brand.get(brand, []):
            excluded_ids.add(p.product_id)
    for subcat in user.preferred_subcats:
        for p in indexes.by_subcategory.get(subcat, []):
            excluded_ids.add(p.product_id)

    # Candidate pool: not in preferred brands/subcats and not used
    candidates = [
        p for p in indexes.all_products
        if p.product_id not in excluded_ids and p.product_id not in used_products
    ]

    if not candidates:
        # Fallback: any unused product
        candidates = [p for p in indexes.all_products if p.product_id not in used_products]
        if not candidates:
            raise ValueError("No more unused products available")

    return random.choice(candidates)


def generate_interactions(
    user: UserProfile,
    indexes: ProductIndexes,
    positive_ratio: float
) -> List[tuple]:
    """
    Generate interactions for a user.
    Returns list of (product, label, dwell_time_ms) tuples.
    """
    num_positive = int(user.num_interactions * positive_ratio)
    num_negative = user.num_interactions - num_positive

    interactions = []
    used_products = set()

    # Generate positive interactions
    for _ in range(num_positive):
        try:
            product = sample_positive_product(user, indexes, used_products)
            used_products.add(product.product_id)
            label = 1.0
            dwell_time_ms = random.uniform(2000, 8000)
            interactions.append((product, label, dwell_time_ms))
        except ValueError:
            break  # No more products available

    # Generate negative interactions
    for _ in range(num_negative):
        try:
            product = sample_negative_product(user, indexes, used_products)
            used_products.add(product.product_id)
            label = 0.0
            dwell_time_ms = random.uniform(200, 1500)
            interactions.append((product, label, dwell_time_ms))
        except ValueError:
            break  # No more products available

    # Shuffle to mix positive and negative
    random.shuffle(interactions)
    return interactions


def write_csv_row(
    writer,
    user_id: str,
    product: Product,
    label: float,
    dwell_time_ms: float
):
    """
    Write a single CSV row with 1546 columns.
    Format: uid,pid,label,dwell_time,brand,category,gender,price_tier,like_count,
            product_age_days,text_emb_0,...,text_emb_1023,image_emb_0,...,image_emb_511
    """
    row = [
        hash_id(user_id),
        hash_id(product.product_id),
        label,
        dwell_time_ms,
        sanitize_csv_field(product.brand),
        sanitize_csv_field(product.subcategory),
        sanitize_csv_field(product.gender),
        product.price_tier,
        product.like_count,
        product.product_age_days
    ]
    # Add text embedding (1024 floats)
    row.extend(product.text_embedding)
    # Add image embedding (512 floats)
    row.extend(product.image_embedding)

    writer.writerow(row)


async def main():
    parser = argparse.ArgumentParser(
        description='Generate synthetic training data for fashion_model_two_tower.py'
    )
    parser.add_argument('--pg_host', required=True, help='PostgreSQL host')
    parser.add_argument('--pg_port', type=int, required=True, help='PostgreSQL port')
    parser.add_argument('--pg_user', required=True, help='PostgreSQL user')
    parser.add_argument('--pg_password', required=True, help='PostgreSQL password')
    parser.add_argument('--pg_database', required=True, help='PostgreSQL database')
    parser.add_argument('--num_users', type=int, default=1000, help='Number of synthetic users')
    parser.add_argument('--min_interactions', type=int, default=50, help='Min interactions per user')
    parser.add_argument('--max_interactions', type=int, default=200, help='Max interactions per user')
    parser.add_argument('--positive_ratio', type=float, default=0.7, help='Ratio of positive samples (0-1)')
    parser.add_argument('--output_file', required=True, help='Output CSV file path')
    parser.add_argument('--seed', type=int, default=42, help='Random seed')

    args = parser.parse_args()

    # Set random seed
    random.seed(args.seed)
    np.random.seed(args.seed)

    print("=" * 60, file=sys.stderr)
    print("Synthetic Training Data Generator", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Configuration:", file=sys.stderr)
    print(f"  PostgreSQL: {args.pg_host}:{args.pg_port}/{args.pg_database}", file=sys.stderr)
    print(f"  Users: {args.num_users}", file=sys.stderr)
    print(f"  Interactions per user: {args.min_interactions}-{args.max_interactions}", file=sys.stderr)
    print(f"  Positive ratio: {args.positive_ratio:.1%}", file=sys.stderr)
    print(f"  Output: {args.output_file}", file=sys.stderr)
    print(f"  Seed: {args.seed}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Fetch products
    indexes = await fetch_products(
        args.pg_host,
        args.pg_port,
        args.pg_user,
        args.pg_password,
        args.pg_database
    )

    if not indexes.all_products:
        print("ERROR: No products found in database", file=sys.stderr)
        return 1

    # Generate user profiles
    users = generate_user_profiles(
        args.num_users,
        args.min_interactions,
        args.max_interactions,
        indexes
    )

    # Generate interactions and write to CSV
    print(f"Generating interactions and writing to {args.output_file}...", file=sys.stderr)

    total_samples = 0
    positive_samples = 0

    with open(args.output_file, 'w', newline='') as f:
        writer = csv.writer(f)

        for i, user in enumerate(users):
            interactions = generate_interactions(user, indexes, args.positive_ratio)

            for product, label, dwell_time_ms in interactions:
                write_csv_row(writer, user.user_id, product, label, dwell_time_ms)
                total_samples += 1
                if label == 1.0:
                    positive_samples += 1

            if (i + 1) % 100 == 0:
                print(f"  Processed {i + 1}/{len(users)} users ({total_samples} samples)", file=sys.stderr)

    print("=" * 60, file=sys.stderr)
    if total_samples == 0:
        print("WARNING: No samples generated", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return 1

    print("SUCCESS", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Total samples: {total_samples:,}", file=sys.stderr)
    print(f"Positive samples: {positive_samples:,} ({100*positive_samples/total_samples:.1f}%)", file=sys.stderr)
    print(f"Negative samples: {total_samples - positive_samples:,} ({100*(total_samples - positive_samples)/total_samples:.1f}%)", file=sys.stderr)
    print(f"Output file: {args.output_file}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    return 0


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
