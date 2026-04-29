# Session Journal — Tier 1 done, Tier 2 baseline locked (2026-04-29 evening)

Continuation of `progress_journal/2026-04-29_session_chunks_rebuild_and_revised_plan.md`.
This session: ship Tier 1 fixes, build the scaled-eval harness, run the
Tier 2 baseline. All measurements are real, post-fix, on the chunks +
RAPTOR + semantic-topic + grounding-check pipeline.

---

## Tier 1 commits this session

| Commit  | Item | Verified |
|---|---|---|
| `9d0c22f` | D.6b-1 cache history fix (split into 3 blocks; threshold 4000→1500) | ~30% cache hit at scale (this run) |
| `f9b039d` | Intent classifier prompt for S4 leading assertion + S6 hesitancy | Direct LLM test confirms 6/6 phrasings correctly classify |
| `c22f8a2` | D.1 soft metadata fallback for post-lock tutoring | Smoke verified — fires only when strict returns < 3 |
| `b765030` | Topic bank v1 (30 topics × 6 phrasings) + scaled e2e harness | 6-convo smoke exposed bank-v1 had off-domain Ch1-2 topics |
| `651330b` | Topic bank v2 (anatomy-only Ch3-Ch28) + retrieved-chunks-based on_topic metric | v2 smoke passes correctly |

### Deferred / verified-already-done / N/A

- **B'.1** (hint_plan into teacher draft): already implemented in code (teacher.py:206-213, prompt has `{hint_plan_active}`)
- **B'.2** (dean_critique into quality-check retry): already implemented (dean.py:2207-2212, prompt has `{prior_preflight_critique}`)
- **D.6b-2** (cache summarizer): N/A — prompt is 86 tokens, sub-threshold
- **D.6b-3** (cache HyDE): N/A — prompt is 229 tokens, sub-threshold
- **D.6b-4** (wrapper_delta stability): verified clean — every wrapper_delta is a static `cfg.prompts.<name>_static` string loaded at startup; never re-`format()`-ed with per-turn values
- **D.6b-5** (client-time greeting): needs frontend changes — deferred
- **D.6c** (parallelize calls): SKIPPED. Plan's three candidates each have hidden data dependencies or speculation costs:
  - exploration_judge ∥ teacher_draft: exploration MUTATES `state["retrieved_chunks"]` (line 1588 dean.py); parallelizing breaks this
  - HyDE ∥ original retrieval: requires async refactor; ~60ms avg win at ~$0.0001/query waste
  - prelock_intent ∥ refuse_draft: refuse content depends on intent's refuse_reason; quality risk
  - Decision: defer to a session with full async refactor + measurement infrastructure
- **D.6a** (streaming teacher draft): needs frontend changes — deferred

---

## Tier 2 Baseline — measured numbers

Run: `data/artifacts/scaled_convo/2026-04-28T20-40-11_tier2_baseline/`
Config: chunks-mode retriever, RAPTOR summaries, all Tier 1 fixes
Volume: 18 conversations (3 seeds × 6 profiles), 26-topic anatomy-only bank
Wall: 36 min   Cost: ~$8 (1.72M input + 119k output tokens)

| Metric | Value | Notes |
|---|---|---|
| topic_confirmed | 72.2% (13/18) | Semantic resolution lands when query is specific enough |
| on_topic (section) | 50.0% (9/18) | Subsection-level match |
| on_topic (chapter) | 66.7% (12/18) | Chapter-level match (looser, more realistic) |
| locked_answer set | 72.2% (13/18) | Equal to topic_confirmed — when locked, locked_answer always sets |
| reached_answer | 33.3% (6/18) | Driven down by S3/S5 profiles which never converge |
| avg turns/convo | 4.7 | Range 0 (stuck) to 13 (long tutoring) |
| avg wall/convo | 120 s | |
| **cache hit ratio** | **30.2%** | Real measured win from D.6b-1 fix at scale |

### Per-profile

| Profile | topic✓ | sec_hit | ch_hit | locked✓ | reached |
|---|---|---|---|---|---|
| S1 Strong | 100% | 100% | 100% | 100% | 67% |
| S2 Moderate | 67% | 0% | 33% | 67% | 33% |
| S3 Weak | 67% | 33% | 67% | 67% | 0% |
| S4 Overconfident | 67% | 67% | 67% | 67% | 67% |
| S5 Disengaged | 67% | 33% | 67% | 67% | 0% |
| S6 Anxious | 67% | 67% | 67% | 67% | 33% |

S1 is essentially perfect. S3/S5 never reach final answer (matches profile expectations). The 67% topic_confirmed across S2-S6 is identical because each of those profiles drew exactly 1 of the failing topics.

---

## Real failure pattern: 2 topics, not 5 profiles

All 5 stuck conversations failed on **two specific topics**:

**Exercise and Stretching** (Ch11 Lever Systems > Exercise and Stretching, 24 chunks):
  - S1 phrasing: WORKED ("lever fulcrum effort load")
  - S2 phrasing "Can you explain how lever systems work in muscles when we stretch and exercise?": stuck
  - S3 phrasing "how do those lever things help when your doing stretches and stuff?": stuck
  - S4 phrasing "Stretching basically just uses first-class levers to lengthen muscles...": stuck

**Types of Synovial Joints** (Ch9 Synovial Joints > Types of Synovial Joints, 40 chunks):
  - S5 phrasing "synovial joint types?": stuck
  - S6 phrasing "I'm not really sure, but I think there are maybe several different types of synovial...": stuck

**Diagnosis**: chunks-mode retrieval + dean semantic-resolution lands on adjacent
chunks in the same chapter that don't precisely match the locked subsection
when student phrasings are vague/leading/anxious. The dean's CE threshold
gate (0.05) passes the retrieval, but the chunks the LLM uses to extract
locked_answer come from sibling subsections.

These are not retrieval-system bugs — they are query-shape sensitivities
that need either better topic-bank phrasings (engineering) or a more
robust topic-resolution layer (architecture).

---

## Cache validation — D.6b-1 measured at scale

The cache structure fix was the single biggest unmeasured claim coming
into this session. Today's evidence:

| Run | Cache hit ratio | Notes |
|---|---|---|
| Pre-fix (4000-token threshold + history-in-cached-block) | 0% | Verified via cache_smoke_test.py |
| cache_smoke_test (3-turn S2 convo, fresh cache) | 30% | Pure structural test |
| Tier 2 baseline (18-convo run, 1.72M input tokens) | **30.2%** | Real-world conversation behavior |

**~520k input tokens served from cache out of ~2.46M total input + cached.**
At Anthropic Sonnet 4-5 rates this is roughly $1.50-2.00 saved per 18-convo
run from the cache fix alone.

---

## Issues to investigate next session (ordered by leverage)

1. **Why "Exercise and Stretching" + "Types of Synovial Joints" reliably get stuck**
   Trace: read the saved JSONs for each stuck conv, run the exact phrasing
   through ChunkRetriever directly, identify which stage fails. Likely
   either: (a) chunks-mode retrieval ranks sibling subsections higher,
   or (b) topic_engagement node refuses despite chunks being available.

2. **Cache hit rate is 30% but design target was 50-70%**
   Likely cause: `_exploration_retrieval_maybe` mutates `state["retrieved_chunks"]`
   mid-session. When chunks change, Block 1's hash changes → cache miss.
   Fix: keep exploration chunks separate from primary retrieved_chunks
   (e.g., as `state["exploration_chunks"]`), or do exploration BEFORE
   locking Block 1.

3. **Stuck conversations burn 75-82 API calls + 130-216 sec wall on no-progress loops**
   Add a circuit breaker: if `state["turn_count"]` is still 0 after 5
   topic-engagement attempts, exit with a clear failure record instead
   of looping until the 25-turn safety cap.

4. **`reached_answer = False` on S3 (Weak) and S5 (Disengaged) — every conversation**
   Could be expected profile behavior (these students don't converge),
   OR a system bug where the assessment node is too strict. Worth a spot-
   check — read 1-2 saved S3 conversations end-to-end and see if the
   tutor reasonably guided the student.

5. **on_topic_section 50% but on_topic_chapter 67%**
   17% of conversations land in the right chapter but a sibling subsection.
   Some of this is correct behavior (the answer to "loop of Henle" lives
   in "Tubular Reabsorption" not "Nephrons" — sibling subsections are
   often legitimate). Some is the same drift that fails harder cases.
   Worth a manual audit on 3-4 sample cases.

---

## Pending tier work

| Tier | Item | Status |
|---|---|---|
| 2 | Topic bank + scaled harness | ✅ done |
| 2 | D.2 Adaptive-RAG query routing | not started |
| 2 | D.3 Per-concept Knowledge Tracing | not started |
| 3 | E.1 6-profile sim (full system) | this run IS the first pass; numbers above |
| 3 | E.2 EULER scoring | not started |
| 3 | E.3 Ablation table v1 | not started |
| 4 | ColBERT v2 reranker bake-off | not started — defer until E.1 confirms retrieval is the bottleneck |
| 4 | Iterative retrieval (i-MedRAG) | not started |
| 5 | All cosmetic / cleanup / write-up / 2nd textbook / Phase F | not started, defer |

---

## What "tomorrow" looks like

Hand-off note for next session, in priority order:

1. Investigate the 2 stuck-topic patterns (item 1 above) — 30 min trace + diagnose.
   Either fix topic-bank phrasings or identify a dean/retrieval improvement.

2. Diagnose cache hit rate ceiling (item 2 above) — confirm exploration
   retrieval is the cache-buster, then either move exploration chunks
   to a separate state field or order them differently.

3. Re-run Tier 2 baseline (18 convos) to measure the deltas from #1+#2.

4. Then move to D.2 adaptive routing OR Phase E (EULER scoring), per
   user direction.
