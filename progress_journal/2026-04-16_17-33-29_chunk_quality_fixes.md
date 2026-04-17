# Progress Journal — 2026-04-16_17-33-29 (Chunk Quality Fixes)

**Author:** Nidhi Rajani  
**Session:** Pre-propositions quality gate fixes (Issues 1–4)

---

## What changed

### 1) `ingestion/extract.py` table-noise filter hardened
- Added robust figure/table metadata pattern:
  - from: `figure\s+\d+\.\d+`
  - to: `\b(figure|table)\s*\d+\.\d+\b`
- This now catches both `Figure 1.1` and `Figure1.1` style OCR/text artifacts.
- Existing quality rules retained (`short_text`, `learning objective`, `review question`, dense-pipe low-content).

### 2) `ingestion/chunk.py` boundary-quality postprocessing added
- Added token-aware refinement stage applied after semantic splitting:
  - `_split_overlong_chunk(...)`: sentence-first splitting for chunks over 450 tokens.
  - `_merge_pronoun_starts(...)`: merges pronoun-start paragraph chunks into previous chunk when safe (`<=450` tokens, same chapter/section).
  - `_is_noise_only_chunk(...)`: drops strict noise-only remnants.
- Added helpers:
  - `_get_token_encoder()` (`cl100k_base`)
  - `_token_len(...)` with char-based fallback
- `chunk_elements(...)` now returns refined chunks (`_refine_chunks(...)`).

### 3) Applied refinement to existing chunk artifact
- Input backup created: `data/processed/chunks_ot_pre_refine_backup.jsonl`
- Updated active artifact: `data/processed/chunks_ot.jsonl`
- Before refine: 4,759 chunks, 52 chunks >450 tokens
- After refine: 4,396 chunks, 0 chunks >450 tokens

---

## Diagnostics and test results

### ISSUE 1 — axillary nerve traceability
- Raw PDF (`"axillary nerve"` exact phrase): 3 pages `[541, 544, 1296]`
- Raw elements exact phrase: 1 match (content page 541)
- Chunks containing `"axillary"`: 26
- Chunks containing exact `"axillary nerve"`: 1

Note: Exact phrase appears only 3 times in full PDF (2 are glossary/index-style pages).

### ISSUE 2 — table metadata garbage
- Table-candidate audit (raw table extraction before conversion):
  - total candidates: 111
  - filtered out: 52
  - passed: 59
  - reasons: `figure_metadata=26`, `short_text=26`
- Current processed table elements in `raw_elements_ot.jsonl` contain 0 figure/table-label garbage by pattern check.

### ISSUE 3 — threshold comparison on Chapter 11 only
(semantic splitter, `buffer_size=1`)

- Threshold 70:
  - chunk count: 97
  - token stats: mean 112.1, P50 98, P90 206, P95 248, max 482
  - pronoun-start: 10

- Threshold 75:
  - chunk count: 85
  - token stats: mean 129.4, P50 108, P90 239, P95 265, max 482
  - pronoun-start: 10

### ISSUE 4 — pronoun-start boundary fixes
- Option A (post-processing merge) on Chapter 11 baseline backup:
  - merges: 8
  - pronoun-start: 10 -> 2
  - P95 tokens: 265 -> 355
- Option B (`buffer_size=2`) on Chapter 11, threshold 75:
  - chunk count: 86
  - pronoun-start: 10
  - P95 tokens: 312

Conclusion: Option A improves pronoun boundaries more reliably than Option B in this corpus.

---

## Re-run of chunk validation gates (post-fix)

Using `data/processed/chunks_ot.jsonl` after refinement:
- Total chunks: 4,396
- Token distribution (`cl100k_base`):
  - mean 121.23, P50 90, P90 261, P95 336, P99 432, max 450
- Chunks under 15 tokens: 0
- Chunks over 450 tokens: 0
- Pronoun-start chunks: 135 (3.07%)
- Noise-only chunks: 0
- Chapter coverage: all chapters 1–28 present
- Critical keyword counts:
  - `deltoid`: 8
  - `biceps`: 18
  - `rotator cuff`: 6
  - `axillary nerve`: 1

---

## Status vs requested targets

- `deltoid > 5` ✅
- `rotator cuff > 2` ✅
- valid table chunks only ✅
- topic coherence >= 7/10 GOOD (manual sample check) ✅
- pronoun-start < 5% ✅
- chunks > 450 tokens < 20 ✅ (actual 0)
- zero noise chunks ✅
- `axillary nerve > 3` ❌ (exact phrase occurrences are limited in source text)

---

## Next recommended action (before propositions)
1. Confirm whether `axillary nerve > 3` should be treated as:
   - exact phrase requirement, or
   - concept-level requirement (`axillary` + nerve-context chunks).
2. If exact phrase is strict, extract/include glossary/index pages in controlled mode; otherwise proceed with current quality-passed chunk set.
