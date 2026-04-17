# Progress Journal — 2026-04-16_14-16-05 (threshold decision)

**Author:** Nidhi Rajani
**Session:** Phase 1 Step 3 — Final threshold decision after 75/85/95 comparison

---

## Decision

Adopt **`breakpoint_percentile_threshold=75`** as the active/default chunking strategy.

Keep **`85`** documented as the fallback candidate for future tuning if proposition/index load becomes a practical bottleneck.

---

## Side-by-side comparison (from completed runs)

| Metric | threshold=75 | threshold=85 | threshold=95 |
|---|---:|---:|---:|
| Total chunks | 4,759 | 3,415 | 1,886 |
| Paragraph chunks | 4,032 | 2,688 | 1,159 |
| Table chunks | 110 | 110 | 110 |
| Figure caption chunks | 617 | 617 | 617 |
| Chapters detected | 28 | 28 | 28 |
| Gate 1 pass at old max=3,000 | ✗ | ✗ | ✓ |
| Min length (chars) | 100 | 100 | 100 |
| Mean length (chars) | 531 | 749 | 1,366 |
| Median length (chars) | 394 | 504 | 653 |
| Max length (chars) | 5,199 | 6,022 | 16,649 |
| 100–600 chars | 3,318 (70%) | 1,952 (57%) | 909 (48%) |
| 600–900 chars | 718 (15%) | 514 (15%) | 171 (9%) |
| >900 chars | 723 (15%) | 948 (28%) | 806 (43%) |
| Estimated propositions | ~12,000 | ~10,752 | ~4,600 |

---

## Post-change verification reruns (after making 75 default)

- `python -m ingestion.chunk` (default threshold=75):
  - Total chunks: 4,759
  - Paragraph/Table/Caption: 4,032 / 110 / 617
  - Length stats: min=100, mean=531, median=394, max=5,199
  - Buckets: 100–600=3,317, 600–900=718, >900=724, <100=0
- `python -m ingestion.chunk --threshold 85 --output data/processed/chunks_ot_85.jsonl`:
  - Total chunks: 3,414
  - Paragraph/Table/Caption: 2,687 / 110 / 617
  - Length stats: min=100, mean=749, median=504, max=6,022
  - Buckets: 100–600=1,951, 600–900=516, >900=946, <100=0
- Both runs passed in-script verification with the updated chunk count bound `[1000, 5000]`.
- Small ±1 deltas versus earlier runs are expected from semantic splitter boundary behavior.

---

## Rationale for choosing 75

- Quality is the top priority.
- Threshold 75 yields the most retrieval-friendly chunk profile (short, focused chunks).
- It avoids the very long chunk tail seen at 95 (max 16,649 chars), which can reduce proposition extraction quality.
- Qdrant scale is not expected to be a blocker at this volume.

---

## Policy changes made to support this decision

1. `ingestion/chunk.py`
   - default threshold set to **75** (function + CLI default)
   - chunk count assert updated from `[1000, 3000]` to `[1000, 5000]`

2. `tests/test_ingestion.py`
   - `CHUNK_COUNT_MAX` updated to **5000** (min remains 1000)

3. Reminder cadence (manual in-chat)
   - Every 10 assistant turns, prompt for:
     - quick summary of what was done
     - whether to track it in `progress_journal` (yes/no)

---

## Next

Proceed to `ingestion/propositions.py` using the threshold-75 chunking baseline.
