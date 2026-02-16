# Two-Tower Model Progress

**Date:** 2026-02-16
**Status:** LIVE. Two-tower model is serving and integrated into rec_sys.

---

## Completed

### Training (Phase 2)
- **Final loss: 3.4–3.6** (down from 6.238 random baseline)
- Positive-only training data: 125,883 samples, 50 epochs
- 4 hyperparameter improvements applied:
  - Adam LR 0.001 (was 0.05), sparse LR 0.01
  - Learnable CLIP-style temperature (logit_scale)
  - 500-step linear LR warmup
  - BatchNormalization before L2 norm in both towers
- Model exported to PVC: `/checkpoints/fashion_two_tower/exported_models/entry/1771204201/`
- Training completed at 01:10 UTC, ~3 hours, ~$1.25 on spot

### Infrastructure
- ZooKeeper: running, healthy (`monolith-zookeeper-0`)
- Checkpoint PVC: bound, 10Gi
- Docker image: `gcr.io/looksyuk/monolith-fashion-two-tower:latest`
- Serving StatefulSet: 2 replicas running, models loaded and AVAILABLE
- Model registered in ZK with `version_policy: "latest"`

### Model Serving (Phase 3)
- ZK model registration complete (entry + ps_0 nodes)
- Both models loaded: version 1771204201, state AVAILABLE
- Signatures available: `item_tower`, `user_tower`, `features_for_join`, `serving_default`

### Pre-compute Item Embeddings (Phase 3)
- Rewrote `precompute_item_embeddings.py` to call TF Serving via gRPC (not local model load)
- **13,788 products embedded** in 4.3s (3,205 products/sec)
- All embeddings written to `embeddings.product_vectors.learned_embedding` (128-dim)
- HNSW index built: `idx_learned_embedding` with `vector_cosine_ops`, m=16, ef_construction=64
- DB permission: `GRANT INSERT, UPDATE ON embeddings.product_vectors TO rec_sys;`

### Integration with rec_sys (Phase 5)
- `MONOLITH_ENABLED=true`
- `USE_LEARNED_EMBEDDINGS=true`
- `MONOLITH_MODEL_NAME=fashion_two_tower:entry`
- Feed endpoint returning results:
  - Anonymous: 10 items, 291ms
  - New user: 10 items, 261ms
  - Warm request: 10 items, 411ms

---

## Architecture

```
User Request → rec_sys (FastAPI)
  ├─ Anonymous/New user → get_candidates_for_anonymous (fresh/trending/random)
  ├─ Browsing user → session embedding → pgvector cosine search on learned_embedding
  └─ User with likes → Monolith user_tower gRPC → pgvector cosine search on learned_embedding
       └─ Retrieval order = ranking order (no separate ranker yet)
```

## Key Files

| What | Where |
|------|-------|
| Two-tower model code | `~/code/monolith/fashion_model_two_tower.py` |
| Precompute script (gRPC) | `~/code/monolith/scripts/precompute_item_embeddings.py` |
| Precompute K8s Job | `~/code/monolith/kubernetes/monolith/precompute-item-embeddings-job.yaml` |
| Serving manifest | `~/code/monolith/kubernetes/monolith/fashion-two-tower-serving.yaml` |
| Serving config | `~/code/monolith/two_tower.conf` |
| TF Serving client | `~/workspace/startup/rec_sys/app/connectors/tfs_client.py` |
| Feed endpoint | `~/workspace/startup/rec_sys/app/main.py` |
| Ranking model plan | `~/workspace/startup/rec_sys/RANKING_MODEL_PLAN.md` |

## Future Work

1. **Cross-network ranker** — Separate ranking model for scoring (see RANKING_MODEL_PLAN.md)
2. **Online training** — Kafka FeatureEvent pipeline for continuous model updates
3. **CronJob for precompute** — Run nightly to capture new products
4. **Cold-start improvements** — Content-based bootstrapping for new users
5. **Redis caching** — Cache user tower embeddings (currently Redis not configured)
