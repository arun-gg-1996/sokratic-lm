# Progress Journal — 2026-04-17_02-03-36

**Author:** Nidhi Rajani  
**Session:** Phase 1 — Step 5 (Indexing) completion for OT corpus

---

## What was implemented

### `ingestion/index.py`
Implemented/verified end-to-end indexing pipeline with the current parse_pdf + overlap architecture:

- Loads propositions from `data/processed/propositions_ot.jsonl`
- Loads base chunk ids from `data/processed/chunks_ot.jsonl`
- Filters to base-source propositions only (`parent_chunk_id` in base ids)
- Enforces `domain="ot"` in every payload
- Embeds with OpenAI `text-embedding-3-large` (3072 dims)
- Upserts vectors into Qdrant collection `sokratic_kb`
- Builds BM25Okapi and saves to `data/indexes/bm25_ot.pkl`
- Keeps full PropositionSchema payload in Qdrant (including `parent_chunk_text`)
- Progress output at 500-step cadence: `Embedded X/40614 | upserted Y/40614`

### Compatibility patch applied
`qdrant_client` in this environment does not expose `search()` (uses `query_points()`).
Added a compatibility wrapper in `ingestion/index.py` so verification queries work across client versions.

---

## Run summary

- Raw propositions loaded: **40,614**
- Base chunk ids loaded: **3,944**
- Indexed propositions (base-only source): **40,614**
- Embedding model: `text-embedding-3-large`
- Vector size: **3072**
- Qdrant collection: `sokratic_kb`

Progress checkpoints were emitted every 500 embeddings until completion.
Final line reached:

- `Embedded 40614/40614 | upserted 40614/40614`

---

## Index verification results

### INDEX TEST 1 — Qdrant health
- Collection exists: **True** (`sokratic_kb`)
- Vector count: **40,614**
- Expected: **40,614**
- Sample payload fields include:
  - `proposition_id`, `text`, `parent_chunk_id`, `parent_chunk_text`,
    `chapter_num`, `chapter_title`, `section_num`, `section_title`,
    `subsection_title`, `page`, `element_type`, `image_filename`, `domain`
- `parent_chunk_text` present in sampled points: **Yes**

### INDEX TEST 2 — BM25 health
- File exists: **True** (`data/indexes/bm25_ot.pkl`)
- BM25 loads successfully: **True**
- BM25 proposition count: **40,614**

### INDEX TEST 3 — Manual semantic search (Qdrant, top-2)
- `deltoid muscle shoulder` → deltoid/shoulder-relevant hits
- `axillary nerve C5 C6` → axillary-nerve hits
- `muscle contraction ATP` → ATP/muscle-contraction hits
- `brachial plexus posterior cord` → brachial plexus/spinal context hits
- `joint range of motion` → ROM/joint hits

### INDEX TEST 4 — Manual BM25 search (top-2)
- `axillary` → axillary vein/artery region propositions
- `supraspinatus` → supraspinatus function/location propositions
- `sarcomere` → sarcomere mechanics propositions

### INDEX TEST 5 — Domain filter
- Qdrant search with `domain="physics"` returned: **0 results**

---

## Gate outcome for this step

Index build and health checks completed successfully.
No retriever implementation started in this step.

---

## Next

- Start/validate `retrieval/retriever.py` against this indexed corpus
- Ensure overlap-aware dedup logic is implemented during post-retrieval ranking
- Run retrieval gate tests once retriever is wired
