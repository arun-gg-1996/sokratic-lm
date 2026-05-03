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
| — | _none open_ | | |

---

## P1 — Visible quality issues (fix before demo if time allows)

| # | Issue | Source | Fix plan |
|---|---|---|---|
| Q1 | `_classify_opt_in` is rule-based (token regex) — violates "no rule-based logic" stance. Currently inside `conversation/assessment_v2.py`. | Bug-fix `f78f8e1` (post-sanity-check) | Replace with `haiku_opt_in_intent_check` Haiku call. Returns `{intent: "yes"\|"no"\|"ambiguous", confidence, evidence}`. ~$0.0003/call. ~30 min. |
| Q2 | Dean's planning hint_text leaked into a tutor message in clinical phase. Observed once during S1 sanity-check: the tutor message read `"The student keeps retreating to a mechanism name instead of reasoning through the logic, so redirect sharply..."` — that's Dean's internal hint, not student-facing prose. | Sanity-check transcript 2026-05-03 turn t1 assess | Investigate: likely the Teacher prompt is surfacing `hint_text` directly when it should be the scaffolding INPUT, not the OUTPUT. Check `_PROMPT_HINT_BLOCK` in `teacher_v2.py`. May need to add a Haiku check that catches "this looks like internal planning prose." |

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

(populated as the run completes)

| Profile | student_id | observed | priority |
|---|---|---|---|
| _pending_ | | | |

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
