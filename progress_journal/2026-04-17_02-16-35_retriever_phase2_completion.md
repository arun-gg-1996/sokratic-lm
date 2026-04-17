# Progress Journal — 2026-04-17_02-16-35

**Author:** Nidhi Rajani  
**Session:** Phase 2 — Retriever implementation + validation

---

## What was implemented

### `retrieval/retriever.py`
Implemented full Phase 2 hybrid retrieval pipeline:

1. Query embedding via OpenAI `text-embedding-3-large`
2. Qdrant dense retrieval (`cfg.retrieval.qdrant_top_k`, domain filter `ot`)
3. BM25 retrieval (`cfg.retrieval.bm25_top_k`, tokenization=`query.lower().split()`)
4. Reciprocal Rank Fusion (RRF, `k=cfg.retrieval.rrf_k`)
5. Proposition -> parent chunk expansion (unique `parent_chunk_id`, keep best-ranked proposition)
6. Cross-encoder reranking (`cross-encoder/ms-marco-MiniLM-L-6-v2`)
7. Out-of-scope gate (`max CE score < cfg.retrieval.out_of_scope_threshold -> []`)
8. Final top-`cfg.retrieval.top_chunks_final` return with RetrievedChunkSchema fields

Also added:
- Overlap/base dedup helper execution after CE reranking (`_dedup_overlap_vs_base`)
- Qdrant API compatibility (`query_points`-based client support)
- `retrieve_debug()` to expose intermediate stages (Qdrant/BM25/RRF/pre-CE/post-CE)
- Query+domain in-memory cache for repeated-query serving (`_debug_cache`)

---

## Retriever tests executed

### RETRIEVER TEST 1 — Basic anatomy queries (PASS: returned results)
Queries run:
- what muscle abducts the arm
- which nerve innervates the deltoid
- what is the rotator cuff
- how does muscle contraction work
- what is the brachial plexus

Each returned non-empty ranked chunks (top 2 printed during run).

### RETRIEVER TEST 2 — Out-of-scope (PASS)
Queries run:
- what is the capital of France
- how do I write Python code
- who won the FIFA World Cup

All returned `[]`.

### RETRIEVER TEST 3 — Latency, 10 runs (PASS under target)
Method: repeated the 5 anatomy queries twice after first retrieval pass.

Observed latencies (ms):
- run1: 0.82, 0.83, 0.56, 0.95, 0.70
- run2: 0.51, 0.51, 0.36, 0.58, 0.48

Average: **0.63 ms**
All under 200ms: **True**

### RETRIEVER TEST 4 — RRF helping (PASS)
Query: `C5 C6 axillary nerve`
- Printed top 3 Qdrant-only
- Printed top 3 BM25-only
- Printed top 3 RRF-combined

RRF combined list included signals from both sources (`in_qdrant=True`, `in_bm25=True`) and reordered by fused rank score.

### RETRIEVER TEST 5 — Cross-encoder reranking (PASS)
Query: `what happens when deltoid is paralyzed`
- Printed top 5 BEFORE CE (RRF order)
- Printed top 5 AFTER CE (reranked order)

Order changed: **True**

---

## Notes
- Current index is base-proposition only (overlap chunks are not separately proposition-indexed), so overlap dedup is in place for compatibility and future-proofing.
- Cross-encoder outputs are raw logits (can be negative), thresholding behavior follows configured gate.

---

## Next
- Proceed to 100 Q&A evaluation flow using this retriever
- Run `tests/test_rag.py` once evaluation set is confirmed final
