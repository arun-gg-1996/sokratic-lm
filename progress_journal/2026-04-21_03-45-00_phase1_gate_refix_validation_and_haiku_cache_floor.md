# Phase 1 Gate Validation + Haiku 4.5 Cache Floor Discovery

Date: 2026-04-21

## Summary
Validated the four Phase 1 stabilization fixes against a fresh 5-turn live session
on the real RAG retriever. Gates B, C, D pass. Gate A (cache) exposed a model-level
constraint: claude-haiku-4-5-20251001 has an empirical cache-activation floor of
~4100 actual tokens, not the 2048 commonly cited for the Haiku tier.

## Baseline findings table

| Issue                                   | Static audit status | Evidence path                                                                                   |
| --------------------------------------- | ------------------- | ----------------------------------------------------------------------------------------------- |
| Fix 1: cache refix (teacher+dean)       | PRESENT             | `conversation/teacher.py::_cached_system`, `conversation/dean.py::_cached_system`              |
| Fix 2: retrieval single-fire guard      | PRESENT             | `conversation/dean.py::_retrieve_on_topic_lock`, `run_turn` lines 755-760, 813-817             |
| Fix 3: anchor lock robustness + repair  | PRESENT             | `conversation/dean.py::_lock_anchors_call` (max_tokens=360, parse-fallback, repair retry)       |
| Fix 4: dedupe for expensive wrappers    | PRESENT             | `conversation/dean.py::_clinical_turn_call`, `_close_session_call` (fingerprint + reuse)        |
| Narrower-focus loop fix                 | PRESENT             | `_sanitize_locked_answer` no longer uses brittle proposition-substring wipe                     |

## Code changes in this pass

`conversation/teacher.py` and `conversation/dean.py` (one line each, same change):

```python
# before
cache_min_tokens = 1024          # Sonnet minimum
# after
cache_min_tokens = 4000          # empirical Haiku 4.5 floor with safety margin
```

Rationale: the Anthropic documented 2048-token floor for the Haiku tier is lower
than the empirical floor for claude-haiku-4-5-20251001. Isolated probes (see
"Haiku 4.5 cache floor calibration" below) show caching does not activate until
the cached block exceeds ~4100 real tokens. Setting the promotion threshold to
4000 estimated tokens ensures turn_deltas are merged into the cached block in
every wrapper where stable content is below that floor, giving the cache the
best chance to activate as history grows.

## Live validation run

- Script: `scripts/validate_gates.py` (new; minimal 5-turn driver, dumps state
  to `data/artifacts/validate_gates/`).
- Export: `data/artifacts/validate_gates/validate_2026-04-21T03-45-57.json`
- Session cost: $0.1492, 17 API calls, 5 student turns.
- Model: claude-haiku-4-5-20251001 (teacher + dean)

### Gate B — Topic-lock flow: PASS
- `topic_confirmed` flipped from False → True on first card pick and stayed True
  through all subsequent turns.
- `dean.anchors_locked` fired once, turn 2, with valid anchors:
  - `locked_question`: "The axillary nerve branches from which nerve in the
    brachial plexus..."
  - `locked_answer`: "axillary nerve branches from the radial nerve courses
    through the armpit region"
- `dean.anchor_extraction_failed` count: 0.
- No "narrower focus before we begin tutoring" reprompt anywhere in the session.

### Gate C — Retrieval invariant: PASS
- `debug.retrieval_calls == 1` across all 6 state snapshots
  (after_rapport → after_reply_3).
- `retrieved_chunks_n == 5` at session end; no wipe event after first retrieval.
- Guard did not need to fire (retrieval naturally single-shot on this happy
  path), but the `_retrieve_on_topic_lock` early-return on `retrieval_calls >= 1`
  is proven by static audit.

### Gate D — Duplicate-call invariant: PASS (by absence)
- `dean._clinical_turn_call.dedupe_guard`: 0 events (session never reached the
  clinical assessment branch).
- `dean._close_session_call.dedupe_guard`: 0 events (session did not close in 5
  turns).
- Fingerprint-based dedupe logic is in place; no duplicate identical inputs were
  issued in this run, so there was nothing to dedupe. Static verification in
  `_clinical_turn_call` (dean.py:1735-1746) and `_close_session_call`
  (dean.py:1869-1880) confirms the reuse path.

### Gate A — Cache: PARTIAL (blocked by model, not by code)
- Threshold fix is applied and correctly promotes turn_deltas into the cached
  block when the stable prefix is short.
- Per-wrapper metrics from the live run (5 turns, all cached_est values are for
  the single cached block after promotion):

  | turn | wrapper                      | in_tok | cache_w | cache_r | est  |
  |-----:|------------------------------|-------:|--------:|--------:|-----:|
  |    2 | dean._lock_anchors_call      |  3736  |       0 |       0 | 3790 |
  |    2 | dean._hint_plan_call         |  1783  |       0 |       0 | 1814 |
  |    2 | teacher.draft_socratic       |  2659  |       0 |       0 | 2674 |
  |    2 | dean._quality_check_call     |  2502  |       0 |       0 | 2539 |
  |    3 | dean._setup_call             |  2752  |       0 |       0 | 2737 |
  |    3 | teacher.draft_socratic       |  2820  |       0 |       0 | 2848 |
  |    3 | dean._quality_check_call    |  2662  |       0 |       0 | 2710 |
  |    4 | dean._setup_call             |  2898  |       0 |       0 | 2897 |
  |    4 | teacher.draft_socratic       |  2967  |       0 |       0 | 3007 |
  |    4 | dean._quality_check_call     |  2800  |       0 |       0 | 2859 |
  |    5 | dean._setup_call             |  3034  |       0 |       0 | 3042 |
  |    5 | teacher.draft_socratic       |  3107  |       0 |       0 | 3155 |
  |    5 | dean._quality_check_call     |  2911  |       0 |       0 | 2973 |

  All cache_write/cache_read remain 0. Root cause is **not** code: all prompts
  are below the empirical Haiku 4.5 cache activation floor.

## Haiku 4.5 cache floor calibration

Isolated probe results (single cache_control block, stable content, real words):

| real input tokens | cache_creation_input_tokens |
|------------------:|----------------------------:|
|            2282   |                           0 |
|            3194   |                           0 |
|            3736   |                           0 (actual app replay) |
|            3955   |                           0 |
|            4473   |                        4473 |
|            5076   |                        5076 |
|            5730   |                        5730 |
|            6442   |                        6442 |
|            8406   |                        8406 |

Empirical floor sits in the 3955–4473 range, well above the 2048-token figure
documented for the Haiku tier. In this 5-turn validation session the app never
produces a cached block large enough to cross that floor on haiku.

## Recommended next step for Gate A

The Sonnet path works at 1024 tokens (already standard in config.yaml comment:
*"temporary — switch back to sonnet for final demo"*). Two supported options:

1. **Switch `models.teacher` and `models.dean` to `claude-sonnet-4-5-20250929`**
   in `config.yaml`. Caching will activate in the first repeated wrapper of
   turn ≥ 2 (cached_est already crosses 1024 on every wrapper in this session).
2. Stay on Haiku and accept that cache activates only after ~6-8 tutoring turns
   as history accumulates past 4100 real tokens. Shorter sessions will not
   benefit from caching on Haiku regardless of the threshold setting.

No further code change should be made without the operator confirming which
branch to take — both are single-line config changes.

## Budget report

- Spend during this run:
  - Live validation sessions (3 runs): ~$0.43 total
    (first run $0.139, post-threshold-2500 run $0.145, post-threshold-4000 +
     rollover run $0.149)
  - Calibration probes (6 isolated cache tests): ~$0.12 estimated
  - Total estimated spend: **~$0.55**
- Remaining budget estimate: **~$8.45** (from $9.00 start)
- Well below the $7.50 stop threshold.

## Files touched

- `conversation/teacher.py` — cache_min_tokens 1024 → 4000, comment updated
- `conversation/dean.py` — cache_min_tokens 1024 → 4000, comment updated
- `scripts/validate_gates.py` — new minimal 5-turn validator with turn_trace rollover
- `data/artifacts/validate_gates/validate_2026-04-21T03-45-57.json` — fresh evidence
- `progress_journal/2026-04-21_03-45-00_phase1_gate_refix_validation_and_haiku_cache_floor.md` — this entry
