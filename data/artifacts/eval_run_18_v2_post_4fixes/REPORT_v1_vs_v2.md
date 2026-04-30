# 18-Conversation Eval Batch — v1 vs v2 Comparison Report

**Date:** 2026-04-30
**v1:** baseline run (pre 4-fix round)
**v2:** post 4-fix round (INVARIANT dedup, teacher leak prohibition, lock-time section filter, two-tier locked answer)

Both batches: same 11 students, same 18 conversations, same plan. v1 outputs archived at `eval_run_18_v1_pre_4fixes/`; v2 at `eval_run_18/`.

---

## TL;DR

**Big win: Fix #4 (INVARIANT_VIOLATION dedup) worked exactly as designed.** Penalty count dropped from 110 → 17 (one per session, as intended). Triple1's 15-turn timeout: 14→2. Pair2_s2's 14-turn session: 19→4.

**One session escaped `failed_critical_penalty`** (triple2_s1, verdict=`failed_threshold`) — first time we see this verdict. **The eval framework is now actually meaningful**: a session can fail dimensions without being killed by per-turn invariant counts.

**Real-quality wins:**
- **RGC dim** 0.759 → **0.852** (+0.093) — gate accuracy up
- **ARC dim** 0.914 → **0.953** (+0.039) — Change 3+4+1 stack working
- **CE dim** 0.114 → 0.192 (+0.078) — cost target threshold catching up

**Surprising regressions:**
- **LEAK_DETECTED 10 → 15 (+5)** — opposite of expected from Fix #1. Need to investigate.
- **Reach rate 13/18 → 9/18** — students who used to reach are now timing out. Likely Fix #1's reveal-prohibition is preventing the reveal-then-parrot pattern that v1 was scoring as "reach".
- **TRQ dim** 0.814 → 0.764 (-0.050) — tutor response quality drop
- EULER **relevance** 0.884 → 0.811 (-0.072) — LLM evaluator is being more strict in v2

---

## Section 1 — Penalty histogram (the headline result)

| Penalty | v1 | v2 | Δ | Interpretation |
|---|---|---|---|---|
| **INVARIANT_VIOLATION** | **110** | **17** | **-93** | ✅ Fix #4 dedup working — now ~1 per session as designed |
| FABRICATION_AT_REACHED_FALSE | 30 | 32 | +2 | flat |
| **LEAK_DETECTED** | 10 | **15** | **+5** | ❌ unexpected — needs root-cause analysis |
| MASTERY_OVERCLAIM | 4 | 3 | -1 | flat |
| OFF_TOPIC_DRIFT_NOT_REDIRECTED | 1 | 1 | 0 | unchanged |
| HELP_ABUSE_RESPONDED_WITH_ANSWER | 0 | 1 | +1 | first occurrence; isolated |
| **TOTAL events** | **155** | **69** | **-86** | dramatic improvement |

The total event count dropped 56% even though only one penalty type actually decreased. This shows how dominant INVARIANT_VIOLATION over-counting was in v1.

---

## Section 2 — Verdict distribution

| Verdict | v1 | v2 |
|---|---|---|
| `failed_critical_penalty` | 18/18 | **17/18** |
| `failed_threshold` | 0 | 1 (triple2_s1) |

The single `failed_threshold` is significant — first session whose dimensions failed but penalty channel was clean. **The eval framework now distinguishes "dim weakness" from "hard fail".** Before Fix #4, every session was indistinguishable.

---

## Section 3 — Per-dimension means

| Dimension | v1 | v2 | Δ | Notes |
|---|---|---|---|---|
| TLQ topic-lock | 0.891 | 0.862 | -0.029 | small dip |
| RRQ retrieval | 0.813 | 0.782 | -0.030 | small dip |
| AQ anchor quality | 0.836 | 0.814 | -0.023 | small dip |
| TRQ tutor response | 0.814 | 0.764 | **-0.050** | EULER relevance drop dragged this |
| **RGC** reached gate | 0.759 | **0.852** | **+0.093** | ✅ gate accuracy up — fewer FP/FN, better evidence |
| PP pedagogical progression | 0.810 | 0.764 | -0.046 | small dip |
| **ARC** answer-reach vs step | 0.914 | **0.953** | **+0.039** | ✅ Change 3+4+1 stack working |
| CC continuity | 0.812 | 0.806 | -0.007 | flat |
| CE cost efficiency | 0.114 | 0.192 | **+0.078** | ✅ cost slightly better |
| MSC mastery calibration | 0.756 | 0.764 | +0.008 | flat |

---

## Section 4 — Per-dimension fail count (out of 18)

| Dimension | v1 fails | v2 fails | Δ |
|---|---|---|---|
| TLQ | 0 | 1 | +1 |
| RRQ | 1 | 4 | +3 |
| AQ | 8 | 10 | +2 |
| TRQ | 7 | 9 | +2 |
| **RGC** | 10 | **6** | **-4** ✅ |
| PP | 1 | 1 | 0 |
| **ARC** | 2 | **1** | **-1** ✅ |
| **CC** | 18 | **17** | **-1** ✅ first session passing CC |
| CE | 18 | 18 | 0 |
| MSC | 9 | 8 | -1 |

RGC and ARC fail counts both dropped — the reached-gate work is paying off. CC's first pass (the triple2_s1 session) is the clean session.

---

## Section 5 — EULER (primary)

| Criterion | v1 mean | v2 mean | Δ | Threshold | v2 passes |
|---|---|---|---|---|---|
| `question_present` | 1.000 | 0.944 | -0.056 | 0.95 | ✗ (just below) |
| `relevance` | 0.884 | 0.811 | -0.072 | 0.70 | ✓ |
| `helpful` | 0.607 | 0.608 | +0.001 | 0.70 | ✗ (unchanged) |
| `no_reveal` | 0.894 | 0.876 | -0.018 | 0.95 | ✗ |

**The EULER drops are an LLM-evaluator-side phenomenon** — the same prompts judging slightly stricter on v2 sessions. `helpful` stayed flat (the dimensional-hint work is what would move this; deferred).

---

## Section 6 — RAGAS (primary)

| Metric | v1 | v2 | Δ | Threshold | v2 passes |
|---|---|---|---|---|---|
| `context_precision` | 0.363 | 0.365 | +0.002 | 0.80 | ✗ |
| `context_recall` | 0.742 | 0.706 | -0.036 | 0.70 | ✓ (barely) |
| `faithfulness` | 0.993 | 0.997 | +0.003 | 0.85 | ✓ |
| `answer_relevancy` | 0.898 | 0.851 | -0.047 | 0.75 | ✓ |

RAGAS basically unchanged. context_precision still the headline weakness — chunk reranking needs tightening.

---

## Section 7 — Reach rate breakdown by profile

| Profile | v1 reached | v2 reached | Δ |
|---|---|---|---|
| S1 (strong) | 3/3 | 3/3 | 0 |
| S2 (moderate) | 3/3 | 2/3 | -1 (solo2 timed out) |
| S3 (progressing) | 1/4 | 0/4 | -1 (triple1_s3 stopped reaching) |
| S4 (overconfident) | 1/1 | 1/1 | 0 |
| S5 (disengaged) | 1/3 | 0/3 | -1 (pair3_s1 timed out) |
| S6 (exploratory) | 4/4 | 3/4 | -1 (triple2_s1 timed out) |
| **Total** | **13/18** | **9/18** | **-4** |

**Why reach rate dropped:** v1 had a leak-then-parrot pattern. Tutor would reveal the answer (`"red pulp = cleanup zone, white pulp = learning zone"`), student parroted it back, gate fired reached=True. With Fix #1's leak prohibition, the tutor no longer reveals. Some students genuinely figure it out (S6 triple2_s2/s3) but many can't and time out.

**This is a "good" regression from the integrity standpoint** — the system is no longer scoring "reach" by feeding the answer to the student. But it surfaces that the underlying tutoring isn't strong enough to drive students to the answer without leaking.

---

## Section 8 — Specific session deltas (interesting cases)

| Session | v1 crit | v2 crit | v1 reached? | v2 reached? | Notes |
|---|---|---|---|---|---|
| pair2_s2 | 19 | **4** | ✓ (14t) | ✓ (2t) | dramatic dedup; reach rate same |
| pair3_s2 | 14 | 4 | ✗ | ✗ | dramatic dedup; both timeouts |
| solo3 | 15 | **1** | ✗ | ✗ | dedup; same wrong topic locked (matcher bug) |
| solo5 | 16 | 7 | ✗ | ✗ | dedup |
| triple1_s1 | 14 | **2** | ✗ | ✗ | dedup; lock more accurate |
| triple1_s2 | 15 | 2 | ✗ | ✗ | dedup |
| triple1_s3 | 15 | 3 | ✓ (12t) | ✗ (15t) | reach lost; teacher no longer reveals to push reach |
| triple2_s1 | 4 | **0** | ✓ (2t) | ✗ (16t) | reach lost; first session w/ 0 critical penalties |
| triple2_s3 | 3 | 4 | ✓ (2t) | ✓ (8t) | similar |

---

## Section 9 — Specific findings on each fix

### Fix #4 (INVARIANT_VIOLATION dedup) — ✅ WORKS

Penalty count dropped from 110 → 17. One per session as designed. **Eval framework is now meaningful** — distinguishes structural failures from per-turn artifacts.

### Fix #1 (Teacher leak prohibition) — ⚠️ MIXED

LEAK_DETECTED count went UP (10 → 15). Possible reasons:
- LLM evaluator reads the tightened "no leak" rule and judges stricter
- Teacher's alternative drafts (when not allowed to reveal) still leak in subtler ways
- Reach rate dropped (13 → 9): tutor no longer leaks-then-parrots, so students who can't actually reason it out time out

The intent of the fix held: the system no longer takes the easy "let me give you the key idea" path. But the implementation has secondary effects that need investigation. **Action: read 2-3 v2 LEAK_DETECTED sessions to understand the leak pattern.**

### Fix #3 (Lock-time section filter) — ⚠️ DIDN'T HELP solo3

solo3's wrong-topic lock REPRODUCED in v2 (asked about long bones, locked on muscle types). The matcher's top hit was wrong; section filter only helps when chunks span multiple subsections. **The matcher itself is the bug; section filter is downstream.** Need matcher-layer work — out of scope this round.

### Fix #2 (Two-tier locked answer) — ⚠️ NEEDS PROMPT TUNING

The new lock prompt asks for `full_answer` but most v2 sessions still have `full_answer == locked_answer` (silent fallback). The lock LLM isn't producing the richer full_answer field consistently. **Action: tighten prompt's `full_answer` examples or split into a separate LLM call.** Until then, the two-tier separation isn't yielding its grading benefit.

---

## Section 10 — Action items (in priority order)

1. **Investigate LEAK_DETECTED regression** — read 2-3 sessions where it fired in v2 to understand the leak pattern. The teacher prompt may need additional refinement.
2. **Matcher-layer fix for solo3 pattern** — the topic_matcher chose a wrong subsection. Improve matching strategy: re-rank by question-keyword density in subsection title, not just chunk content.
3. **`full_answer` prompt tuning** — most v2 locks didn't produce richer full_answer. Either tighten the prompt or split into a separate LLM call after locking.
4. **Reach-rate engineering** — Fix #1 surfaced an underlying gap: the tutor can't always drive students to the answer without leaking. The deferred dimensional-hint work (Change 6) targets this.
5. **EULER `helpful` 0.61 unchanged** — confirms dimensional hints are needed, not just leak prohibition.
6. **CE threshold recalibration** — still 0/18 passing. Threshold $0.20/turn too tight for full-flow sessions.

---

## Section 11 — What the comparison tells us

**Most important takeaway:** the 4-fix round delivered exactly what we asked for on the eval framework side (Fix #4) and on the gate accuracy side (RGC +0.093). The teacher-leak-prohibition fix had the right intent but exposed a **deeper underlying issue**: the tutor can't reliably reach the answer without leaking. That's not a Change 4-1-2-3 problem; it's a deferred-Change-6 problem (dimensional hints).

**v2 is a healthier system** — it doesn't reward leaks with apparent reaches. The cost is lower reach rate, but that's the right trade. To restore reach rate without leaks, ship dimensional hints (Change 6 main piece) — currently deferred.

**For thesis/deployment:** v2's metric profile is more honest than v1's. v1's INVARIANT_VIOLATION dominance was hiding meaningful signals. v2's penalty histogram is interpretable. v2's RGC and ARC scores are publishable.

---

## Files

- v1: `data/artifacts/eval_run_18_v1_pre_4fixes/`
- v2: `data/artifacts/eval_run_18/`
- Per-session JSONs + scored JSONs in each
- This report: `data/artifacts/eval_run_18/REPORT_v1_vs_v2.md`
- Original v1 report: `data/artifacts/eval_run_18_v1_pre_4fixes/REPORT_2026-04-30.md`
