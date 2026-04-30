# 18-Conversation Eval Batch — v1 → v2 → v3 Comparison

**Date:** 2026-04-30
**Sessions per round:** 18 (6 single + 6 paired + 6 triple, S1–S6 profiles)
**Run cost (each round):** ~$8–9 in conversation gen; ~$1 in scoring

| Round | What landed | Marker |
|---|---|---|
| **v1** | Pre-4-fix-round baseline | `data/artifacts/eval_run_18_v1_pre_4fixes/` |
| **v2** | Round 4 fixes: INVARIANT dedup, strict teacher leak prohibition, two-tier locked answer (`locked_answer` + `full_answer`), lock-time section filter | `data/artifacts/eval_run_18_v2_post_4fixes/` |
| **v3** | Round 5 fixes: explicit forbidden patterns in teacher prompt (anagram leaks, mid-component reveals, "just tell me" caves), 1–5 word hard cap on `locked_answer`, matcher `STRONG_MIN` 65→78 + `STRONG_GAP` 5→10 | `data/artifacts/eval_run_18/` (this round) |

---

## Headline metrics

| Metric | v1 | v2 | v3 | v1→v3 | v2→v3 |
|---|---:|---:|---:|---:|---:|
| **Penalties (total)** | 155 | 69 | **49** | -106 | **-20** |
| Penalties (critical) | 150 | 64 | 47 | -103 | -17 |
| Penalties (major) | 5 | 5 | 2 | -3 | -3 |
| Sessions failing critical-channel | 18/18 | 17/18 | 17/18 | -1 | 0 |
| INVARIANT_VIOLATION events | 110 | 17 | **17** | -93 | 0 (dedup holds) |
| LEAK_DETECTED events | 10 | 15 | **10** | 0 | **-5** |
| FABRICATION_AT_REACHED_FALSE | 30 | 32 | **20** | -10 | **-12** |
| MASTERY_OVERCLAIM | 4 | 3 | 2 | -2 | -1 |
| Reach rate | 13/18 | 9/18 | 9/18 | -4 | 0 |

**Read:** v3 is the cleanest run yet. The Round 5 prompt strengthening landed — both leaks (-5) and fabrications (-12) dropped meaningfully, and the dedup from Round 4 holds steady at 17 INVARIANT events (one per session, as designed).

The reach rate stayed at 9/18 — same as v2, well below v1's 13/18. This is the **good regression** noted in the v1→v2 report: the leak-then-parrot pattern that inflated v1's reach rate is gone, so reach now reflects genuine student understanding.

---

## Secondary dimensions (10-dim diagnostic suite)

| Dim | v1 | v2 | v3 | v2→v3 |
|---|---:|---:|---:|---:|
| TLQ (topic-lock quality) | 0.891 | 0.862 | 0.859 | -0.003 |
| RRQ (retrieval-relevance) | 0.813 | 0.782 | 0.782 | 0.000 |
| AQ (anchor quality) | 0.836 | 0.814 | 0.822 | +0.008 |
| TRQ (tutor-response quality) | 0.814 | 0.764 | 0.784 | +0.020 |
| **RGC (response-grounding-correctness)** | 0.759 | 0.852 | **0.889** | **+0.037** |
| PP (pedagogical posture) | 0.810 | 0.764 | 0.788 | +0.024 |
| ARC (anchor-relevance to corpus) | 0.914 | 0.953 | 0.944 | -0.009 |
| CC (clinical correctness) | 0.812 | 0.806 | 0.819 | +0.013 |
| CE (cost-efficiency) | 0.114 | 0.192 | 0.160 | -0.032 |
| MSC (mastery-scoring correctness) | 0.756 | 0.764 | 0.778 | +0.014 |

**Read:** RGC is the standout — up another +0.037 in v3, totaling +0.130 since v1. This is the dim most directly affected by both the Round 4 lock-time section filter and the Round 5 leak-prevention work. PP and TRQ also tick up, indicating the tutor's pedagogical posture stabilized (less "caving" under stonewalling, more grounded scaffolding).

CE dipped slightly (-0.032). The CE target was already flagged as unrealistic in `README.md` open issues — needs target retune to $0.50/turn.

---

## Primary metrics (EULER + RAGAS)

| Metric | v1 | v2 | v3 | v2→v3 |
|---|---:|---:|---:|---:|
| EULER question_present | 1.000 | 0.944 | 0.948 | +0.004 |
| EULER relevance | 0.884 | 0.811 | 0.733 | **-0.078** ⚠ |
| EULER helpful | 0.607 | 0.608 | 0.558 | **-0.050** ⚠ |
| EULER no_reveal | 0.894 | 0.876 | **0.914** | **+0.038** ✓ |
| RAGAS context_precision | 0.363 | 0.365 | 0.379 | +0.014 |
| RAGAS context_recall | 0.742 | 0.706 | 0.724 | +0.018 |
| RAGAS context_relevancy | 0.354 | 0.365 | 0.367 | +0.002 |
| RAGAS faithfulness | 0.993 | 0.997 | 0.988 | -0.009 |
| RAGAS answer_relevancy | 0.898 | 0.851 | 0.828 | -0.023 |
| RAGAS answer_correctness | 0.722 | 0.500 | 0.500 | 0.000 |

**Read — wins:**
- **EULER no_reveal: 0.914** — highest of all three rounds. Round 5's anti-leak prompt is quantitatively visible. The qualitative read confirms it: pair3_s2's anagram-leak pattern from v2 (*"starts with 'P' and ends with 'pressure'"*) did NOT recur in v3.

**Read — regressions to investigate:**
- **EULER relevance dropped -0.078** in v3. Likely cause: the matcher `STRONG_MIN` 65→78 bump is now driving more questions to "borderline" tier where the dean's question selection is less confidently topical. The answer_relevancy dip (-0.023) is consistent with this.
- **EULER helpful dropped -0.050** to 0.558. The qualitative review (below) shows the same pattern as v1/v2: tutor scaffolding sometimes over-Socratic without advancing reasoning. This is the open issue tracked for the deferred dimensional-hints work (Change 6 in the README).

---

## Qualitative read — all 18 v3 dialogs

I read each dialog end-to-end (per the user's earlier instruction "did you look at each conversation properly").

### Wins (Round 5 fixes that held)

1. **`pair3_s2` (heart rate → pulse pressure):** The v2 anagram leak (*"starts with 'P' and ends with 'pressure'"*) is **gone**. Student asked "Can you just tell me?" three times (turns 7, 9). Tutor refused each time:
   - T8: *"I won't hand over the answer. But here's what I need from you: look at 120 and 80—just tell me which number is larger…"*
   - T10: *"I hear the frustration—and I get that this feels slow. But here's the thing: I'm asking you to compare two numbers…"*
   Eventually scaffolded to "pulse pressure" via a money/pressure-difference path. Some leakiness in the final stretch (T18 *"we combine the words for 'difference' and 'pressure'"*) but no anagram pattern.

2. **`triple1_s1` (nephron → renal corpuscle):** Locked correctly as `"renal corpuscle"` (3 words) — fixes v2's 11-word run-on `"What are the three main structural parts that make up a nephron?"`. Round 4's `locked_answer`/`full_answer` split + Round 5's 1–5 word cap held for this case.

3. **Strong-student paths clean (S1–S2):** `solo1`, `solo4`, `solo6`, `pair1_s1`, `pair1_s2`, `pair2_s1`, `pair2_s2`, `triple2_s2` — all reach in 2–3 turns, then run optional clinical assessment with mastery=proficient. No leaks, no fabrications in the tutoring phase. Critical penalties on these are clinical-phase fabrication false-positives (see "Penalty noise" below).

### Persistent issues (carried over from v2)

4. **Lock drift on free-text questions** — `STRONG_MIN` 78 didn't fix these:
   - `solo3` ("structure of long bone") → locked as **"three types of muscle tissue"**
   - `pair3_s1` ("phases of cardiac cycle") → locked as **"three phases of muscle twitch"**
   - `pair3_s2` ("heart rate / cardiac output") → locked as **"pulse pressure"** (subsection)
   The fuzzy matcher's `token_set_ratio` lands these in the >78 strong-tier band even though they're conceptually wrong. Threshold tuning has reached its limit — this needs **semantic similarity** (embedding-based), not stricter fuzzy thresholds.

5. **Multi-word lock cap breach** — Round 5's 1–5 word hard cap held in **most** cases but failed in `triple1_s3`:
   - `locked_answer = "active transport diffusion facilitated diffusion secondary active transport and osmosis"` (12 words)
   - `pair3_s1`: `"latent period contraction phase relaxation phase"` (6 words — borderline)
   The lock prompt's cap isn't being strictly enforced when the answer is genuinely a list of N items. Worth a follow-up: either restructure these as multi-anchor questions, or relax the cap for list-style answers and use a per-item match.

6. **Tutor caves under stonewalling** — Round 5's "ABSOLUTE LEAK PROHIBITION" prompt block reduced this but didn't eliminate it:
   - `solo3` T4: tutor names skeletal muscle's defining feature ("attached to bones, voluntary movement") — student parrots back "is it muscle? Like skeletal muscle with all those striations?"
   - `solo3` T6 + T8: tutor introduces cardiac AND smooth muscle by name (the locked answer's three components) — student never independently named them
   - `triple1_s2` T10: *"Sodium is actively pumped out of the tubule into the tissue, creating a concentration gradient. Water then moves passively across the membrane to dilute that high sodium concentration—this is called osmosis."* The mechanism the question was probing.
   - `triple1_s1` T10: *"The textbook's first paragraph says the nephron accomplishes 'filtration, reabsorption, and secretion.'"* Reveals the question.

7. **`triple2_s1` coverage-gate runaway** — **new severity escalation, not in v2**: 32 turns of card-loop hell. 14 topics rejected by the coverage gate, including ones that should clearly be in the OT corpus: *"The Liver"*, *"Atoms and Subatomic Particles"*, *"Mendel's Theory of Inheritance"*, *"Conduction System of the Heart"*, *"Cognitive Abilities"*. `locked_question` ended up empty; `low_effort=0`, `off_topic=0` (no penalties triggered because student was cooperative). This is open-issue #3 (over-strict coverage gate) escalating from "minor" to a hard fail mode. **Highest-priority fix for next round.**

### Penalty noise

The 47 critical penalties are concentrated in two kinds:

- **FABRICATION_AT_REACHED_FALSE: 20** — fires on clinical-phase turns even after `final_student_reached_answer=True`. Example: `pair1_s1` reached on T3 then continued into clinical assessment; tutor said *"What you got right: You correctly identified isovolumic contraction"* in turn 8, but the per-turn reach flag for that turn was False (legitimately — the student admitted uncertainty about the clinical extension). The penalty fires because the per-turn flag is False even though the session-level reach is True. **This is a scorer false-positive**, partially fixed in v2 by parsing the result string into a per-turn flag, but the fix is incomplete for clinical-phase turns. → see Phase 3 task list.

- **INVARIANT_VIOLATION: 17 (one per session, except triple2_s1)** — the dedup is working as designed. The remaining 17 are real envelope/state-shape violations the eval framework expects to surface — not regressions, but worth re-validating the schema once Phase 3 settles.

### Engagement-system check

- `pair3_s2`: 5 low-effort turns → strike warnings fired → student eventually engaged after money-analogy reset → reached the answer. ✓
- `solo5`: 10 low-effort turns → hint advanced to 4 → terminate via memory_update. ✓
- `solo3`: 5 low-effort + 0 off-topic → hints advanced (final hint=3) → did NOT terminate but tutor leaked. Counter system kept the session alive but the tutor's prompt-time discipline cracked. ⚠ (this is the issue in #6 above)
- No off-topic terminations triggered in v3 (none needed).

---

## What changed substantively v2 → v3

### Quantitative
- **-20 penalties** (69→49), **-12 fabrications**, **-5 leaks**, **-3 majors**.
- **EULER no_reveal +0.038**, **RGC +0.037**, **PP +0.024**, **TRQ +0.020** — all directionally consistent with the Round 5 prompt-strengthening hypothesis.
- INVARIANT count flat at 17 (dedup invariant).

### Qualitative
- Anagram-leak pattern eliminated. ✓
- Lock-cap fix worked in some cases (`triple1_s1` 3-word lock) but failed in others (`triple1_s3` 12-word lock).
- Coverage gate failure mode worsened (`triple2_s1` 32-turn loop).
- Tutor still occasionally caves under sustained stonewalling (`solo3`, `triple1_s2`, `triple1_s1` post-T10).

---

## Recommended next round (Round 6 / Phase 3 input)

| Priority | Item | Rationale |
|---|---|---|
| **P0** | **Coverage-gate over-rejection fix** (`triple2_s1`-style 32-turn loops) | Open issue #3, now a hard fail mode. Tune threshold or change rejection UX (e.g., a one-shot "we couldn't lock this — pick a related topic") |
| **P0** | **Semantic-similarity matcher** (replace fuzzy STRONG_MIN tuning) | Lock-drift cases (`solo3`, `pair3_s1`, `pair3_s2`) are unreachable by threshold tuning — fuzzy `token_set_ratio` lands wrong matches in the strong band |
| **P1** | **Tighten teacher prompt against "scaffolded leak"** | Round 5 caught the obvious anagram pattern but not the subtler "name-the-feature → student-parrots" pattern (`solo3` T4–T6, `triple1_s2` T10) |
| **P1** | **Multi-anchor support for list-style answers** | `triple1_s3`'s 5-mechanism question shouldn't be force-fit into a 1–5 word `locked_answer`. Either allow a list anchor or split into N sub-questions |
| **P2** | **Scorer fix: `FABRICATION_AT_REACHED_FALSE` should respect session-level reach flag for clinical-phase turns** | Eliminates the 20-event false-positive contributing to all "failed_critical_penalty" verdicts |
| **P2** | **CE target retune** to $0.50/turn | Existing $0.20/turn target is unreachable; CE dim is meaningless until retuned |
| **P3** | **EULER `helpful` 0.558** — Socratic drafts not advancing reasoning | The deferred dimensional-hints work (Change 6) targets this |

---

## Files
- v3 raw: `data/artifacts/eval_run_18/eval18_*.json` (18 sessions, $8.59 total cost)
- v3 scored: `data/artifacts/eval_run_18/scored/eval18_*_eval.json` (18 scored, $1.04 scoring cost)
- v3 dialogs (markdown): `/tmp/v3_dialogs/eval18_*.md`
- v1 baseline: `data/artifacts/eval_run_18_v1_pre_4fixes/`
- v2 baseline: `data/artifacts/eval_run_18_v2_post_4fixes/`
- Prior reports: `data/artifacts/eval_run_18_v2_post_4fixes/REPORT_v1_vs_v2.md`, `…/REPORT_qualitative_v2.md`
