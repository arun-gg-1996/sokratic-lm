# Pre-Demo Issues — Single Source of Truth

**Goal:** decent demo quality (not perfect). Fix obvious conversation-quality
issues observed in eval runs, ship, then record demo. Stop iterating once
quality is "decent."

**Status:** v2 stack + all audit tracks shipped. 18-convo eval in flight on
v2 (concurrency=4). This doc accumulates issues observed in those runs +
fixes already promised.

---

## P0 — Demo blockers

(items that would visibly break the demo if not fixed)

| # | Issue | Source | Fix plan |
|---|---|---|---|
| **B1** | **18/18 sessions failed** in v2 eval — all stuck in pre-lock loop, 0 reached, all hit 16-turn harness cap. Cost $2.45 wasted. | `data/artifacts/eval_run_18/` 2026-05-03 | **Harness bug:** `run_eval_18_convos.py:254` picks `state.topic_options[0]` for card-pick, but topic_lock_v2's L10 confirm_and_lock sets `topic_options=[]` and puts options under `pending_user_choice.options`. Harness re-types the original topic → infinite confirm-loop until cap. **Fix:** harness should read `pending_user_choice.options` first, fall back to `topic_options`, and use `simulator.respond()` (which already handles pending_user_choice per `f78f8e1`) instead of hard-picking `opts[0]`. |

---

## P1 — Visible quality issues (fix before demo if time allows)

| # | Issue | Source | Fix plan |
|---|---|---|---|
| Q1 | `_classify_opt_in` is rule-based (token regex) — violates "no rule-based logic" stance. Currently inside `conversation/assessment_v2.py`. | Bug-fix `f78f8e1` (post-sanity-check) | Replace with `haiku_opt_in_intent_check` Haiku call. Returns `{intent: "yes"\|"no"\|"ambiguous", confidence, evidence}`. ~$0.0003/call. ~30 min. |
| Q2 | Dean's planning hint_text leaked into a tutor message in clinical phase. Observed once during S1 sanity-check + reproduced in solo3_S3 eval (turn 3 assess: *"The student is clearly lost on the clinical scenario, so let me step back entirely and return to basic anatomy per the hint."*). | 2026-05-03 evals | Designed: Dean verifies Teacher's draft. Loop: Teacher drafts → Dean reviews (does it leak my hint_text? does it reveal the answer?) → if clean ship, else Dean rewrites. Adds 1 LLM call per turn but only when hint_text non-empty. Teacher should also have the actual answer in context with explicit "do not reveal" rule (currently Teacher doesn't see the answer at all — only forbidden noun phrases). ~1-2 hrs. |
| **Q3** | **Three hardcoded close-phase prompts (`honest_close`, `reach_close`, `clinical_natural_close`) is a band-aid, not a design.** The right thing: ONE close prompt that reads state (`student_reached_answer`, `clinical_engaged`, `disengage_strikes`) and reacts accordingly. Today's 3-prompt split is the demo-fix for B2. | B2 fix 2026-05-03 | Replace 3 modes with 1 `close` mode. Pass state flags into the prompt. Prompt branches on flags rather than caller picking which prompt to invoke. Reduces drift, removes the "wrong mode picked" failure class entirely. ~1 hr. |
| **Q4** | **Q2 verify-loop not implemented + no comparative v1-vs-v2 eval.** Dean-reviews-Teacher loop was designed (Teacher drafts → Dean reviews for hint_text leak / answer reveal → if clean ship, else Dean rewrites). Skipped pre-demo to keep the punch list short. ALSO: the existing evaluator harness (Hit@k, EULER scores) was never run on the v2 conversation flow — so we have no apples-to-apples quality number for v1 vs v2 today. | Designed but unshipped 2026-05-03 | Two parts: (a) Implement the verify-loop in `assessment_v2._tutoring_v2` — ~1-2 hrs. After implementing, re-run conversations where Q2 leaked (solo3_S3) and confirm it's caught. (b) Update the evaluator scoring scripts (`scripts/score_euler.py`, `scripts/euler_eval.py`) to consume v2 conversation JSON format and run on the existing `data/artifacts/eval_run_18/` runs. ~30 min. Output: a real v1-vs-v2 quality comparison table. |

---

## P2 — Polish (not on critical path)

| # | Item | Plan |
|---|---|---|
| P1 | Per-site A/B for **`classifiers.haiku_off_domain`** compact TOC injection. Build fixture (30-50 queries), add `use_compact_toc` kwarg, run, compute agreement %. Ship compact if ≥95% agreement. | ~1.5 hrs + $0.50 |
| P2 | Per-site A/B for **`dean._exploration_judge`** compact TOC injection. Same harness pattern as P1 but with tangent-detect fixtures. | ~1.5 hrs + $0.50 |
| P3 | `OPT_IN_REASK_CAP=2` hard cap exists as a safety net. Once Q1 lands (Haiku classifier), the hard cap is still useful as defense-in-depth but stops mattering in practice. | n/a — leave |
| P4 | `_render_reach_close` / `_render_reveal_close` have templated fallback strings (`_build_reach_close_fallback`, `_build_reveal_close_fallback`). They only fire on Teacher LLM error, but they are templated. If we want zero-template guarantee, replace with deterministic short Haiku call. | Low priority — fallback only |

---

## Already shipped fixes

| When | What | Commit |
|---|---|---|
| 2026-05-03 | `_classify_opt_in` permissive token-matching + `OPT_IN_REASK_CAP=2` (rule-based — to be replaced per Q1) | `f78f8e1` |
| 2026-05-03 | Simulator now mimics UI button clicks on `pending_user_choice` (returns "Yes"/"No"/first-option). Not a production-side change. | `f78f8e1` |
| 2026-05-03 | VLM `cfg.models.vision` lookup + curated test image set with HERO tags | `7909334` |
| 2026-05-03 | All audit tracks (v2 + Track 5 + L77 + L78 + L79 + L80 + L39 + prompt-opt-1) | 16 commits since `e5fb2e9` |

---

## Observations from 18-convo eval

### Run #1 (pre-B1-fix) — 0/18 reached, $2.45 wasted

| Outcome | Count |
|---|---|
| reached_answer=True | 0/18 |
| stuck pre-lock | 18/18 |

**Cause:** harness bug B1 (fixed). Re-ran after fix.

### Run #2 (post-B1-fix) — 0/18 reached, $3.65 wasted, NEW failure mode

| Outcome | Count |
|---|---|
| reached_answer=True | 0/18 |
| Topic locked first attempt | 18/18 (lock succeeded — verified in trace) |
| Retrieval returned 0 chunks → coverage gate refused → topic_confirmed flipped back to False | 18/18 |
| Then card-pick loop: pick card → 0 chunks → refuse → next card → ... → 16-turn harness cap | 18/18 |

**Root cause: ChunkRetriever returns 0 chunks under concurrency=4.**

Same `dean._retrieve_on_topic_lock(query)` call that returned chunks in
the single-conversation sanity check returns 0 chunks when 4 chains run
concurrently. ChunkRetriever (Qdrant + BM25) is likely thread-unsafe in
the way the eval harness uses it (`asyncio.to_thread` × 4 parallel
chains sharing one ChunkRetriever instance).

**Trace excerpt (same query, B Cell Differentiation):**
```
Sanity check (1 chain):       chunks returned, reached=True, $0.016
18-convo run (4 chains):       0 chunks, refuse loop, $0.20/session
```

**This is a test-infrastructure issue, NOT a v2 stack issue.** The v2
flow itself is verified by:
  * 313+ unit/integration tests passing
  * Single-user sanity check (B Cell Differentiation) — reached=True
  * VLM end-to-end (Process of Breathing) — locks at 0.95 confidence
  * e2e regression: legacy 7/8, v2 8/8 after Track 4.7g fix
  * The DEMO IS SINGLE-USER — concurrency=4 isn't the production path.

### Decision

For demo prep we need **conversation-quality observations**, not
concurrency-stress data. Two paths:

  **A. Re-run with `CONCURRENCY=1`** — sequential, no thread contention,
     real per-profile quality data. ~30 min wall, ~$2.50.
  **B. Skip the 18-convo eval entirely** — single-user verification
     already gives sufficient confidence for the demo. Saves time + cost.

Recommended: **A** if there's time, **B** if not. The thread-safety
issue itself is a separate cleanup item (after demo).

---

## Strategy

1. **Watch the 18-convo eval roll in.** Skim transcripts for the most
   obvious quality issues per profile (S1 strong, S2 moderate, S3 weak,
   S4 overconfident, S5 disengaged, S6 anxious).
2. **Add the worst-2-or-3 patterns to the P0/P1 sections above** — only
   the most demo-visible ones, not perfectionism.
3. **Fix that short list** (probably ~1-3 hours total).
4. **Stop iterating.** Ship, record demo.
5. Anything else (P2 items, deeper polish, prompt-opt rollout) can land
   after the demo.

This doc is the single source of truth. New issues land here, fixes get
checked off, no stale tracking spread across multiple files.
