# 2026-05-01 — Tier 1 #1.1: Bedrock + Sonnet 4.6 prompt caching verified

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_08-35-36_handoff_takeover_setup_and_round1_fixes.md`

---

## Question this entry answers

The `ingestion/run.py:109` comment warns:

> Sonnet 4-6 silently ignores cache_control in our environment;
> Sonnet 4-5 caches correctly.

Could that warning apply to the conversation runtime too — and would
that mean every Bedrock+Sonnet 4.6 session is paying full uncached
input price (~$1.69/session × 120 planned sessions = $203 → over the
$100 AWS budget)?

**Answer: No. Caching works fine on the conversation runtime.**

---

## What was run

```bash
cd /Users/nidhirajani/Desktop/sokratic-lm
SOKRATIC_USE_BEDROCK=1   # already set in .env
SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/cache_smoke_test.py
```

`scripts/cache_smoke_test.py` patches the Anthropic SDK
(`Messages.create` at the class level) before importing dean/teacher,
then drives a 3-turn S2 conversation through the real graph. Per call
it logs `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `output_tokens` to stdout.

Total cost: ~$0.10. Wall: ~3 min.

---

## Result — full per-call log

```
=== Cache smoke test (chunks-mode retriever) ===

--- Turn 0: rapport ---
  [api call # 1] input=  440  cache_read=    0  cache_write=    0  output= 43

--- Turn 1: topic_input ---
  [api call # 2] input= 1229  cache_read=    0  cache_write=    0  output= 77
  [api call # 3] input= 2840  cache_read=    0  cache_write= 3175  output=116
  [api call # 4] input=  247  cache_read=    0  cache_write= 3104  output=135
  [api call # 5] input=  609  cache_read=    0  cache_write=    0  output= 79
  [api call # 6] input=  567  cache_read=    0  cache_write=    0  output= 69

--- Turn 2: tutoring_1 ---
  [api call # 7] input=  169  cache_read=    0  cache_write=    0  output= 43
  [api call # 8] input=  394  cache_read=    0  cache_write= 3302  output=110
  [api call # 9] input= 1052  cache_read=    0  cache_write=    0  output= 64
  [api call #10] input=  731  cache_read=    0  cache_write=    0  output= 68
  [api call #11] input=  753  cache_read=    0  cache_write=    0  output= 84
  [api call #12] input=  743  cache_read=    0  cache_write= 3447  output= 81
  [api call #13] input=  766  cache_read=    0  cache_write= 3164  output= 36

--- Turn 3: tutoring_2 ---
  [api call #14] input=  202  cache_read=    0  cache_write=    0  output= 49
  [api call #15] input=  658  cache_read= 3302  cache_write=    0  output=116
  [api call #16] input= 1322  cache_read=    0  cache_write=    0  output= 53
  [api call #17] input=  869  cache_read=    0  cache_write=    0  output= 72
  [api call #18] input= 1017  cache_read=    0  cache_write=    0  output= 93
  [api call #19] input= 1013  cache_read= 3447  cache_write=    0  output= 85
  [api call #20] input= 1040  cache_read= 3164  cache_write=    0  output= 41

CACHE SUMMARY
Total API calls:                          20
Total uncached input tokens:           16661
Total cache_read_input_tokens:          9913   (savings)
Total cache_creation_input_tokens:     16192   (1.25x writes)

Per-turn:
  rapport     ( 1 calls): input=   440  read=    0   write=    0
  topic_input ( 5 calls): input=  5492  read=    0   write= 6279
  tutoring_1  ( 7 calls): input=  4608  read=    0   write= 9913
  tutoring_2  ( 7 calls): input=  6121  read= 9913   write=    0

cache_read > 0 (9913 tokens, 37.3% of input).
```

Full log archived at
`data/artifacts/cache_smoke/2026-05-01_bedrock_sonnet46.log`.

---

## What this proves

1. **Bedrock honours `cache_control: {type: "ephemeral"}` markers via the
   Anthropic SDK on Sonnet 4.6.** Calls 15, 19, and 20 read 3302, 3447,
   and 3164 cached tokens respectively — exactly matching the cache
   writes from calls 8, 12, and 13.
2. **The `ingestion/run.py:109` warning is specific to the batch path.**
   That comment was written for `messages.batches.create` on the
   ingestion dual-task. It does NOT apply to `messages.create` on the
   conversation runtime.
3. **No code change needed.** The 3-block `_cached_system()` in
   `conversation/dean.py:101–210` and `conversation/teacher.py:266–272`
   already fires correctly on Bedrock — it does not need a Bedrock-
   specific code path.
4. **Per-session cost projection holds.** At 37.3% cache hit rate on a
   3-turn smoke (caching builds with longer sessions; baseline at scale
   was 48.6% on Apr 29), per-session cost lands near the $0.78 estimate
   from the prior journal — fits the $100 AWS budget with 120 sessions
   planned.

---

## What remains an open question (not blocking)

- The smoke is 3 turns; longer (10–15 turn) sessions should hit the
  48–55% cache ratio observed at scale. Confirm during the 50-conv
  eval (Tier 1 #1.5).
- Cache hit ceiling 48.6% on Apr 29 was diagnosed as
  `_exploration_retrieval_maybe` mutating `state["retrieved_chunks"]`
  mid-session. Whether to fix this (Tier 2 #2.2) depends on whether the
  ceiling actually limits the production budget. Current evidence says
  no.

---

## Pytest baseline taken before the smoke (for reference)

```
136 passed, 9 failed, 9 errors in 313.23s
```

The 18 non-passing items match the pre-existing failure inventory from
the `2026-04-22_01-30-00` and `2026-04-29_session_chunks_rebuild` journal
entries (test_ingestion stale chunk-count expectations from the
propositions era; test_rag fixtures pointing at a retriever path that
needs `SOKRATIC_RETRIEVER=chunks` env var; test_conversation requiring
ANTHROPIC_API_KEY in shell). None are regressions from the takeover or
Bedrock work. Documented for the eventual cleanup pass; not blocking
Tier 1.

---

## Files touched

- New: `scripts/cache_smoke_test.py` already existed — no code change.
- New: `data/artifacts/cache_smoke/2026-05-01_bedrock_sonnet46.log`
  (the smoke output; archived for the paper's methodology section).
- Modified: `.gitignore` — un-ignore `data/artifacts/cache_smoke/*.log`
  (small, durable, paper-relevant; not a regenerable bloat artifact).
- Modified: `progress_journal/_tracker.json` (last_prompt bump).

---

## Cost ledger update

| Entry | Cost |
|---|---|
| Prior cumulative (Anthropic Direct) | ~$3.20 |
| Prior cumulative (AWS Bedrock) | ~$2.00 |
| **This session** (cache smoke on Bedrock) | **~$0.10** |
| Cumulative AWS Bedrock | ~$2.10 |
| AWS budget remaining | ~$97.90 / $100 |

---

## Next

Tier 1 #1.2 — the four deferred fixes from the prior round:

a. Letter-hint regex extensions (two-letter abbrev, "X stands for Y", "first letters of …")
b. `_is_distinctive_anchor` for short ALL-CAPS abbreviations (Fc, SA, RCA, LCA, ATP)
c. `sample_related` cold-start investigation (random results on "coronary circulation")
d. Sonnet-specific sycophancy patterns ("on an interesting track", "partly right", "both key concepts in hand")

Estimated 2.5 hr + ~$0.10. Each fix gets its own commit + journal entry per the auto-push cadence.
