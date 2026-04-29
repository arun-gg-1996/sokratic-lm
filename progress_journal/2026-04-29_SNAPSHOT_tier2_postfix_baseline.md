# 📌 SNAPSHOT — Tier 2 Post-Fix Baseline

**Tag:** `tier2-postfix-baseline-2026-04-29`
**Commit:** `ca3fc98` (HEAD of `main` at snapshot time)
**Date:** 2026-04-29

This is the canonical "known good" state of the pipeline. If a future
change regresses any of the metrics below, roll back here:

```bash
git checkout tier2-postfix-baseline-2026-04-29
```

---

## Measured baseline (18-conversation scaled e2e)

Run artifact: `data/artifacts/scaled_convo/2026-04-28T22-03-06_tier2_postfix_parallel/`

| Metric | Value |
|---|---|
| topic_confirmed | **100.0%** (18/18) |
| on_topic (section) | **77.8%** (14/18) |
| on_topic (chapter) | **100.0%** (18/18) |
| locked_answer set | **100.0%** (18/18) |
| reached_answer | 38.9% (7/18) — profile-correct (S3/S5/S6 are 0% by design) |
| stuck conversations | **0** |
| cache hit ratio | **48.6%** |
| avg turns/convo | 7.4 |
| avg wall/convo | 117 s |
| total wall (18 parallel) | 15 min |

### Per-profile

| Profile | n | topic✓ | sec_hit | ch_hit | locked✓ | reached |
|---|---|---|---|---|---|---|
| S1 Strong | 3 | 100% | 100% | 100% | 100% | **100%** |
| S2 Moderate | 3 | 100% | 67% | 100% | 100% | 33% |
| S3 Weak | 3 | 100% | 67% | 100% | 100% | 0% |
| S4 Overconfident | 3 | 100% | 100% | 100% | 100% | **100%** |
| S5 Disengaged | 3 | 100% | 67% | 100% | 100% | 0% |
| S6 Anxious | 3 | 100% | 100% | 100% | 100% | 0% |

### Quality audit (manual review)

- **0 real LeakGuard violations** (5 heuristic flags, all false positives — assessment summaries / topic intros / legitimate guided reveal)
- **0 off-topic drifts**
- **0 sycophancy / wrong-confirmation cases**
- **0 stuck conversations**
- All 6 profiles handled with appropriate pedagogical strategy (embodied learning for S5, validation+analogy for S6, patient correction for S3)

See: `data/artifacts/scaled_convo/2026-04-28T22-03-06_tier2_postfix_parallel/quality_audit_writeup.md`

---

## Pipeline state

### Retrieval
- **Index:** Qdrant `sokratic_kb_chunks` collection — **8,293 points** (7,574 chunks + 549 subsection summaries + 170 section summaries)
- **BM25:** `data/indexes/bm25_chunks_openstax_anatomy.pkl` (17 MB, all 8,293 entries)
- **Source corpus:** `data/processed/chunks_openstax_anatomy.jsonl` (11 MB, 7,574 chunks)
- **Active retriever class:** `ChunkRetriever` (set via `SOKRATIC_RETRIEVER=chunks` env var in harnesses; defaults to propositions `Retriever` if unset)

### Legacy data preserved
- Qdrant `sokratic_kb` collection (65,561 proposition points) — kept for E.3 ablation; not used in production
- `data/processed/chunks_ot.jsonl`, `data/indexes/bm25_ot.pkl` — legacy artifacts on disk

### Models
- `models.teacher`: `claude-haiku-4-5-20251001`
- `models.dean`: `claude-haiku-4-5-20251001`
- `models.summarizer`: `claude-haiku-4-5-20251001`
- `models.embeddings`: `text-embedding-3-large` (3072 dim)
- `models.cross_encoder`: `ncbi/MedCPT-Cross-Encoder` (running on MPS when available)

### Retrieval thresholds (frozen at this snapshot)
```yaml
qdrant_top_k: 20
bm25_top_k: 20
rrf_k: 60
top_chunks_final: 7
window_size: 2
hyde_weak_cosine_threshold: 0.50      # was 0.65; tightened 2026-04-28
hyde_weak_topk: 5
hyde_weak_topk_mean_threshold: 0.45
ood_cosine_threshold: 0.45            # was 0.30; tightened 2026-04-28
dean_topic_gate_ce_threshold: 0.05    # added 2026-04-28; CE-scale gate
out_of_scope_threshold: 0.90
softfallback_enabled: true
softfallback_min_chunks: 3
```

### Cache config (`_cached_system` in dean.py + teacher.py)
- 3-block layout: stable (role+wrapper+chunks) [CACHED] | history [CACHED] | turn_deltas [UNCACHED]
- Threshold: `cache_min_tokens = 1500` (was 4000; lowered after empirical verification)
- Verified hit rate: **48.6%** measured at scale (vs 0% pre-fix)

### Locked-answer sanitizer (`_sanitize_locked_answer` in dean.py)
- Word cap: **15** (was 6; bumped after multi-component answers were being wiped)
- Verb-marker sentence detection: still firing (catches actual prose)
- Grounding check: ≥60% content-token overlap with retrieved chunks

---

## Code components introduced this session

| Component | File | Purpose |
|---|---|---|
| `ChunkRetriever` class | `retrieval/retriever.py` | chunk-level retrieval (replaces propositions in production) |
| `scripts/reindex_chunks.py` | — | re-embeds chunks into Qdrant `sokratic_kb_chunks` |
| `scripts/build_raptor_summaries.py` | — | generates 549 subsection + 170 section summaries via Haiku, embeds + upserts |
| `scripts/run_scaled_convos.py` | — | parallel e2e harness (asyncio.gather + Semaphore), produces summary.json + summary.txt + per-convo JSONs |
| `scripts/audit_convos.py` | — | conversation quality audit; produces `audit.md` per run dir |
| `scripts/cache_smoke_test.py` | — | 3-turn cache verification harness |
| `scripts/eval_realistic.py` | — | 112-row realistic-student-profile retrieval eval |
| `scripts/eval_legacy_compare.py` | — | 231-row legacy-set comparison with pipeline-invariant scoring |
| `scripts/diagnose_misses.py` | — | bucket failures into WRONG_CHAPTER / WRONG_SECTION / SPLIT_ANSWER / NOT_IN_CORPUS |
| `scripts/trace_pipeline.py` | — | per-query candidate-pool tracer |
| `data/eval/topic_bank_v2.jsonl` | — | 26 anatomy-only topics × 6 profile phrasings = 156 queries |

---

## Reproducibility — verifying this snapshot

To reproduce the 18-conversation baseline numbers:

```bash
git checkout tier2-postfix-baseline-2026-04-29
SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_scaled_convos.py \
    --seeds 3 --concurrency 3 --label verify
# Expect: ~15 min wall, topic_confirmed=100%, ch_hit=100%, cache≈48%
```

To reproduce the cache-fix verification:

```bash
git checkout tier2-postfix-baseline-2026-04-29
SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/cache_smoke_test.py
# Expect: cache_read>0 across calls, cache hit ratio ≥30% on 3-turn convo
```

---

## Rollback procedure

If a later change regresses any of:
- topic_confirmed below 95%
- chapter_hit below 95%
- cache hit ratio below 35%
- stuck conversations > 0
- quality audit raises real (manually-confirmed) flags

…roll back:

```bash
git checkout tier2-postfix-baseline-2026-04-29
# Re-verify with: scripts/run_scaled_convos.py --seeds 3 --concurrency 3
```

The Qdrant collection `sokratic_kb_chunks` is what the code points at;
that's preserved on the local Qdrant instance. If it gets wiped, rebuild via:

```bash
.venv/bin/python scripts/reindex_chunks.py     # ~$0.10, ~3 min
.venv/bin/python scripts/build_raptor_summaries.py  # ~$1, ~5 min
```

---

## Known-issue inventory at this snapshot (NOT regressions, just open)

These are documented as Tier 1/2 followups but were intentionally left
in place because they're not blocking:

1. `audit_convos.py` `answer_reveal_pre_student` heuristic over-flags assessment-phase summaries (false positives)
2. `reached_answer` boolean undercounts S3/S5/S6 partial convergence
3. Cache hit ratio ceiling at ~48.6% (target was 50-70%); likely caused by `_exploration_retrieval_maybe` mutating `state["retrieved_chunks"]`
4. TopicMatcher fuzzy fallback in dean.py is now redundant (semantic resolution handles all cases) but kept for safety
5. Locked-answer formatting is lowercase/punctuation-stripped (cosmetic; functional content correct)
6. Source coverage gap: OpenStax 2e doesn't cover OT-clinical syndromes (wrist drop / claw hand / etc.) — addressed by adding 2nd corpus in Phase C (Tier 5)

---

## What's NOT in this snapshot (deliberately)

- D.2 Adaptive-RAG query routing (Tier 2)
- D.3 Per-concept Knowledge Tracing (Tier 2)
- ColBERT v2 reranker (Tier 4)
- Iterative retrieval (Tier 4)
- EULER scoring run (Tier 3)
- Ablation table v1 (Tier 3)
- Phase C 2nd textbook (Tier 5)
- Phase F write-up (Tier 5)

These are the next planned changes. Each will be measured against THIS snapshot.
