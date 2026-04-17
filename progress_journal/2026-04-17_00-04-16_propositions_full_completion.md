# Progress Journal — 2026-04-17_00-04-16 (propositions full completion)

**Author:** Nidhi Rajani  
**Session:** Phase 1 Step 4 — Full propositions run with parallel workers + resume cleanup

---

## What was implemented

Updated `ingestion/propositions.py` for full-scale resilient processing:
- Parallel extraction using `ThreadPoolExecutor(max_workers=5)`
- Shared thread-safe sliding-window limiter:
  - request threshold: 45/min
  - output token threshold: 7000/min
- `RateLimitError` handling: wait 60s + retry same chunk
- Other API errors: log + skip
- Thread-safe append checkpoint writer after every completed chunk
- Resume behavior using existing `parent_chunk_id` set from output JSONL
- Base chunk-only processing (`is_overlap=false`; skip `paragraph_overlap`)

## Runs executed

### Run A (full resume pass)
Command:
```bash
./venv/bin/python -m ingestion.propositions --all --workers 5 --output data/processed/propositions_ot.jsonl
```

Result:
- Chunks total target: 3944
- Already processed before run: 2852
- Processed in run: 1092
- Total propositions after run: 40140
- Avg props/chunk: 10.18
- API-error chunks: 45
- Time: 32.70 minutes

### Run B (cleanup resume for failed 45)
Command:
```bash
./venv/bin/python -m ingestion.propositions --all --workers 5 --output data/processed/propositions_ot.jsonl
```

Result:
- Pending chunks at start: 45
- Processed in run: 45
- API-error chunks: 0
- Time: 1.19 minutes
- Total propositions after cleanup: 40614
- Avg props/chunk: 10.30

## Final completeness check

- Base chunks: 3944
- Propositions total: 40614
- Base chunks with >=1 proposition: 3944
- Missing base chunks: 0

## Final chapter distribution (props)

Ch 1: 777 | Ch 2: 1319 | Ch 3: 1316 | Ch 4: 1020 | Ch 5: 837 | Ch 6: 751 | Ch 7: 1233 | Ch 8: 1104 | Ch 9: 1308 | Ch 10: 1112 | Ch 11: 993 | Ch 12: 1244 | Ch 13: 1366 | Ch 14: 1576 | Ch 15: 987 | Ch 16: 1490 | Ch 17: 1754 | Ch 18: 1469 | Ch 19: 1970 | Ch 20: 2551 | Ch 21: 1661 | Ch 22: 1479 | Ch 23: 2244 | Ch 24: 1253 | Ch 25: 1541 | Ch 26: 951 | Ch 27: 1288 | Ch 28: 4020

Top 5: Ch28 (4020), Ch20 (2551), Ch23 (2244), Ch19 (1970), Ch17 (1754)  
Bottom 5: Ch6 (751), Ch1 (777), Ch5 (837), Ch26 (951), Ch15 (987)

## Output artifact

- `data/processed/propositions_ot.jsonl` (40,614 propositions)

## Next

- Run ingestion gate:
  - `python -m pytest tests/test_ingestion.py -v`
- Hold on `ingestion/index.py` until explicit approval.
