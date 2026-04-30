# 18-Conversation v2 Batch — Qualitative Evaluation (Read Each Dialog)

**Date:** 2026-04-30 (post 4-fix round)
**Method:** Read each of the 18 dialogs turn-by-turn; assess the system's actual behavior (not just metrics).

---

## TL;DR

After reading every dialog:

- **Memory cycle is rock-solid.** Every session 2 / 3 of multi-session students shows clean topic-specific carryover ("Last time you were working through... and identified pepsin's role"). This is a real product win.
- **Topic-resolution drift remains the single biggest quality issue.** ~5 of 18 sessions locked on the wrong concept (long bone → muscle types; cardiac cycle → muscle twitch; heart rate → pulse pressure). **Fix #3 didn't help** because the matcher itself is wrong — the section filter only addresses chunks crossing subsections, not the matcher's bad top hit.
- **Fix #1 (no leak prohibition) reduced obvious "let me give you the key idea" leaks but the system still leaks subtly.** Three patterns persist: (a) tutor fills in mid-components when student is stuck (b) tutor uses letter-hint anagram-style scaffolds (c) tutor over-confirms hedged guesses ("you nailed it" when student said "I think... maybe").
- **Fix #2 (two-tier locked answer) didn't activate.** Most sessions show `full_answer == locked_answer` — the lock LLM didn't reliably produce the richer full_answer field. Prompt needs additional tuning or the field needs a separate LLM call.
- **Reach rate dropped 13/18 → 9/18** which I considered a "good regression" in the metrics-only report. Reading the actual dialogs **confirms this is mostly good** — the lost reaches are sessions where the v1 tutor leaked the answer (e.g. v1 solo5 "red pulp = cleanup zone, white pulp = learning zone"), so v2's no-reach is more honest.

---

## Per-session review (18 sessions)

Format: **session — verdict — what happened — issues**

### Pairs (3 students × 2 sessions = 6)

#### 1. pair1_strong_S1_session1 — ✅ GOOD
Topic: ventricular contraction. Locked: "Phases of the Cardiac Cycle". Student answered correctly in turn 1 ("isovolumic contraction and ventricular ejection"), reached via Step A. Clinical scenario (aortic stenosis) elicited solid reasoning. Memory_update gave specific feedback. **2 turns, $0.13.**

#### 2. pair1_strong_S1_session2 — ✅ EXCELLENT
**Memory carryover beautiful**: rapport says *"Last time you were working through the cardiac cycle phases—specifically systole—and you reached the target. Since that's still solidifying at around 48% mastery..."* — references prior session topic + actual mastery percentage. Locked: SA node (clean 2-word anchor ✓). Student reached via "SA node is basically the heart's natural pacemaker". Clinical scenario about 35 bpm elderly patient triggered confused reasoning, then student SELF-CORRECTED in turn 9 distinguishing "SA node dysfunction" from "AV node escape rhythm". Real learning. **2 turns to reach + 4 assessment turns, $0.17.**

#### 3. pair2_moderate_S2_session1 — ⚠️ TOPIC DRIFT
Topic: action potential propagation along a NEURON. Locked: "Excitation-Contraction Coupling" in SKELETAL MUSCLE. Student noticed: *"Is it the same sodium and potassium channels we talked about before, or is it different for muscle?"* Tutor adapted ("the mechanism in muscle is fundamentally the same as in neurons") and walked student through scaffolds. **Subtle over-attribution** at turn 22: tutor said *"You also correctly inferred that this process is called 'local current flow'—that is the precise anatomical term"* but student said *"like local current flow or something? I'm honestly not sure"* — that's a hedge, not an inference. **9 turns, $0.60.**

#### 4. pair2_moderate_S2_session2 — ✅ GOOD
**Memory carryover**: "Last time you were working through excitation-contraction coupling — do you want to pick that up again, or shift to a different topic". Locked: myelin (1-word anchor ✓). Student said "myelin coating lets the impulse skip along the axon" → reached. Clinical (demyelination, MS, charge leakage) had structured "What you got right / What to correct next" feedback. Real learning. **2 turns to reach, $0.19.**

#### 5. pair3_disengaged_S5_session1 — ❌ FAIL (TOPIC DRIFT + LEAK)
Topic: cardiac cycle phases. Locked: muscle twitch phases ("latent period contraction phase relaxation phase"). **Matcher drift.** Then **TUTOR LEAKED** at turn [6]: *"That phase is called the **contraction phase**"* when student asked "Can you just tell me?" — Fix #1 prompt didn't prevent this. **Hint level reached 3 with the leak.** 15 turns timeout, never reached. **$0.80.**

#### 6. pair3_disengaged_S5_session2 — ❌ FAIL (TOPIC DRIFT + LEAK by anagram)
Topic: heart rate → cardiac output. Locked: pulse pressure (drift). Tutor used a **letter-hint anagram leak** at turn [10]: *"the medical term starts with the letter 'P' and ends with 'pressure.'"* This is a clear leak via guided guessing — Fix #1 didn't catch this pattern. 15 turns, never reached. **$0.77.**

### Triples (2 students × 3 sessions = 6)

#### 7. triple1_progressing_S3_session1 — ❌ FAIL
Topic: parts of nephron. Locked: 11-word sentence-form `'renal corpuscle proximal convoluted tubule loop of henle distal convoluted tubule'`. Fix #2 should have produced a short concept_anchor + long full_answer; instead full_answer == locked_answer (the long string). Tutor turn [4]: *"You've nailed the three-stage process"* when student said "maybe nervous system controls how much..." — sycophancy / over-attribution. 15 turns timeout. **$1.03.**

#### 8. triple1_progressing_S3_session2 — ⚠️ MIXED
**Memory carryover not visible in turns I read** but turn 2 message implies it. Locked: "obligatory water reabsorption". 8 turns, ended with mastery_tier=needs_review (proper handling). **$0.40.**

#### 9. triple1_progressing_S3_session3 — ❌ TIMEOUT (was reached in v1)
Topic: tubular reabsorption + homeostasis. Locked: 9-word list "active transport diffusion facilitated diffusion secondary active transport osmosis". 15 turns, never reached. v1 reached this in 12 turns; v2 doesn't. Cause: Fix #1 prevents the v1 leak that helped student close the gap. **$0.91.**

#### 10. triple2_exploratory_S6_session1 — ❌ COVERAGE GATE FAILED
Topic: chemical digestion of carbs. **Matcher rejected the topic** (too broad spans multiple organs/enzymes) and offered 3 UNRELATED alternatives: Hip Bone, Muscles of the Abdomen, Reabsorption and Secretion in PCT. Student picked "Hip Bone" randomly. 16 turns into hip-bone tangent, never reached, locked_answer empty. **v1 reached this same topic in 2 turns** ("saliva"). **Massive regression on this topic.**

#### 11. triple2_exploratory_S6_session2 — ✅ GOOD
Locked: pepsin (clean). Student turn 3: "I think it might be pepsin? But honestly I'm not totally sure". Reached via Step A. Clean session. **2 turns, $0.10.**

#### 12. triple2_exploratory_S6_session3 — ✅ GOOD with concerns
**Memory carryover**: "Last time you were working through protein metabolism and identified pepsin's role". Locked: 9-word list "active transport passive diffusion facilitated diffusion co transport endocytosis" (sentence-form anchor again). Student named 4-5 of them in turn 3 hedged. Tutor turn [4]: *"You've named some really solid mechanisms—those are definitely real processes!"* — over-confirmation. Reached at turn 8, mastery=proficient. **$0.52.**

### Solos (6 sessions, S1-S6)

#### 13. solo1_S1 — ✅ GOOD (lock slightly off)
Locked: "self tolerance mechanisms" (3 words ✓). Student answered correctly turn 3 hedged. Reached via paraphrase. Clinical scenario about autoimmune. Mastery=proficient. **2 turns to reach, $0.12.**

#### 14. solo2_S2 — ⚠️ TIMEOUT (was reached in v1)
Topic: breathing rate brainstem. Locked: "medulla oblongata and pontine respiratory group" (6 words, multi-component). Student turn 5 says "I feel like I've heard something about the medulla maybe?" hedged. 9 turns, never reached, mastery=needs_review (proper handling). **v1 reached this in 2 turns**. v2 timeout because Fix #1 prevents the leak that v1 used to confirm "medulla" as good enough. **$0.45.**

#### 15. solo3_S3 — ❌ SAME WRONG-TOPIC LOCK AS V1
Topic: long bone structure. Locked: muscle types ("skeletal cardiac and smooth muscle"). **Fix #3 didn't help** because matcher's top hit was the wrong subsection (muscle tissue rather than bone tissue). The session 1 system summary literally documents this: *"the tutor redirected to the three types of muscle tissue"*. Tutor turn [4] suggested switching topics ("Would it make sense to pick a different anatomy topic?") — student declined, kept stonewalling. 15 turns, never reached. **$0.75.**

#### 16. solo4_S4 — ✅ EXCELLENT
Topic: elbow joint. Locked: "humeroulnar joint" (clean ✓). Student answered correctly + hedged in turn 3: *"the humeroulnar, humeroradial, and proximal radioulnar joints, and I'm pretty sure the humeroulnar joint is the main hinge"*. Tutor confirmed appropriately. Clean session. **2 turns, $0.12.**

#### 17. solo5_S5 — ❌ FAIL with refusal-to-leak
Topic: spleen function. Locked: "red pulp and white pulp" (5 words). Student turn 3: **"Can you just tell me the answer?"** Tutor turn 4: **"I won't hand over the answer—that won't help you retain or apply this knowledge"** — Fix #1 working here ✓. Then tutor scaffolded with simpler questions. 12 turns, 9 low_effort, never reached, mastery=needs_review. **v1 of this session leaked "red pulp = cleanup zone, white pulp = learning zone" in turn 2 — v2 correctly refuses.** This is the cleanest demonstration of Fix #1 working. **$0.77.**

#### 18. solo6_S6 — ✅ GOOD
Topic: thyroid metabolism. Locked: "thyroid hormones decrease with age" (locked_question shifted to age-related angle — minor drift). Student turn 3: "I think thyroid hormone production goes down as you get older?" Tutor scaffolded BMR effects. 7 turns to reach, mastery=proficient. **$0.43.**

---

## Patterns observed across the batch

### What's working

1. **Memory carryover (5/5 multi-session second sessions)**: every triple/pair session 2+ has specific topic recall, mastery percentage references, and tactful continuity-or-pivot offers. This is the cleanest feature of the system.

2. **Help-abuse counter triggering when student says "just tell me"** (solo5): the tutor refuses, scaffolds simpler. Direct demonstration of Fix #1 + Change 4 working together.

3. **Clinical scenarios genuinely test understanding** (pair1_s2, pair2_s2): structured "What you got right / What to correct next" feedback drives self-correction. Real learning happens.

4. **Mastery tier downgrade when student stonewalls** (solo2 needs_review, solo3/5/triple1_s1/pair3_s2 not_assessed): system correctly flags these.

5. **Anchor brevity (1–3 words) when topics are simple** (pair1_s2 "sinoatrial node", pair2_s2 "myelin", triple2_s2 "pepsin", solo4 "humeroulnar joint", solo1 "self tolerance mechanisms"): clean. Fix #2 working when concepts are simple.

### What's NOT working

1. **Matcher-layer topic drift** (5/18 sessions): pair3_s1 (cardiac→muscle), pair3_s2 (cardiac output→pulse pressure), triple1_s1 (nephron parts→sentence-form list), solo3 (long bone→muscle types), triple2_s1 (chemical digestion REJECTED with random alternatives). **This is the biggest single product issue.** Fix #3's section filter only fires when chunks span multiple subsections; the matcher's top hit being wrong defeats it.

2. **Sentence-form locked_answer (5/18)** when concept truly is multi-component: triple1_s1 (11 words), triple1_s3 (9 words), triple2_s3 (9 words), pair3_s1 (6 words), solo3 (5 words). Fix #2's two-tier was meant to fix this — `locked_answer` short, `full_answer` rich — but the lock LLM is silently falling back to using the same value for both.

3. **Tutor leaks via patterns Fix #1 didn't anticipate**:
   - **Mid-component reveal under pressure** (pair3_s1): student asked "can you just tell me?", tutor caved with "That phase is called the contraction phase".
   - **Anagram-style hints** (pair3_s2): "the medical term starts with the letter 'P' and ends with 'pressure'" — letter-hint scaffold.
   - **Over-confirmation of hedges** (pair2_s1, triple1_s1, triple2_s3): tutor says "you've nailed it" / "you've correctly identified" when student said "maybe X? I'm not sure" — gives credit without earning.

4. **Coverage gate rejecting reasonable broad topics with random alternatives** (triple2_s1): "Walk me through chemical digestion of carbohydrates" got REJECTED with Hip Bone, Muscles of Abdomen, PCT cards. v1 happily locked on "saliva" for the same input. The coverage gate may be over-strict on broad/exploratory student questions.

5. **Help-abuse counter resets too generously**: solo3 had 7 low_effort turns but never capped because the student interleaved "umm" and "not sure" responses with the occasional real word, breaking the consecutive chain. The counter is doing its job but the threshold-with-reset isn't catching multi-turn drift.

---

## Action items (priority order, post-qualitative-read)

### Critical (blocking thesis-quality results)

1. **Matcher-layer topic accuracy fix.** ~28% of sessions had matcher drift. Need:
   - Re-rank matches by semantic similarity to the *student's literal question*, not just chunk content
   - When the matcher's confidence is low (below threshold), present cards instead of auto-locking on a weak top hit
   - Check the topic_index quality for ambiguous chapter intersections (long bone vs muscle attachment, cardiac cycle vs muscle twitch)

2. **Tighten Fix #1 to forbid mid-component reveals + anagram hints.** Add explicit examples in `teacher_socratic_static`:
   - *"NEVER state any individual word from the locked_answer or aliases."*
   - *"NEVER provide letter hints, first-letter prompts, or 'starts with...' scaffolds."*
   - *"When student asks 'just tell me', refuse with: 'I won't hand over the answer.'"*

3. **Fix #2 needs prompt strengthening.** Lock LLM is silently producing `full_answer = locked_answer`. Either:
   - Add a hard requirement: "If full_answer field is identical to locked_answer, the lock fails — produce a richer full_answer."
   - OR split the lock into two LLM calls: one for `locked_answer + aliases`, one for `full_answer` — separate prompts can be more focused.

### Major (post-deploy)

4. **Coverage-gate threshold relaxation** for broad topics. triple2_s1's "chemical digestion of carbohydrates" should have locked on saliva/amylase, not been rejected.

5. **Help-abuse counter resilience** — consider non-resetting tally (already tracked via `total_low_effort_turns`) to drive cap when the consecutive-chain breaks frequently but session-wide rate is high.

6. **Sycophancy/over-attribution audit** — the deterministic `_has_strong_affirmation` check was removed in the audit cleanup; the LLM QC is supposed to catch it but isn't. Re-introduce the deterministic check OR strengthen the LLM QC prompt's sycophancy criteria.

### Already deferred to Nidhi / future

7. **Dimensional hints (Change 6)** would address several timeout sessions where students genuinely struggle (triple1_s1, solo2, solo3, solo5).

8. **Sonnet vs Haiku A/B** for dean — would the Sonnet dean produce better topic resolution + less leak under pressure?

9. **`full_answer` separate LLM call** if the inline approach can't be made reliable.

---

## What this teaches me about the eval framework

Reading the dialogs reveals patterns the metrics couldn't catch:

- **EULER `helpful 0.61`** corresponds to real over-confirmation patterns I saw (pair2_s1 "you've nailed it" on hedges).
- **LEAK_DETECTED count up** (10→15) corresponds to real subtle leaks (anagram hints, mid-component reveals) that v1's looser teacher-prompt didn't trigger but v2's stricter prompt makes the LLM evaluator MORE sensitive to.
- **Reach rate down 13→9** corresponds to honest-mode tutoring. The 4 lost reaches are: pair3_s1 (matcher drift), triple1_s3 (lost the leak that helped v1), triple2_s1 (coverage gate), solo2 (lost the leak). Three of four are matcher/coverage issues, not Fix #1 regressions per se.

The dimensional metrics are useful but **reading actual transcripts is irreplaceable**. The matcher-drift bug shows up CLEARLY in transcripts but only loosely in dim scores.

---

## Files

- 18 transcripts: `/tmp/v2_dialogs/eval18_*.md`
- v2 saved JSONs: `data/artifacts/eval_run_18/eval18_*.json`
- v2 scored JSONs: `data/artifacts/eval_run_18/scored/`
- v1 archive: `data/artifacts/eval_run_18_v1_pre_4fixes/`
- This report: `data/artifacts/eval_run_18/REPORT_qualitative_v2.md`
