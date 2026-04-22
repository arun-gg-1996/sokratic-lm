# Progress Journal — 2026-04-17_23-21-26_conversation_issue_clinical_hint_flow

**Author:** Arun Ghontale  
**Date:** 2026-04-17 23:21 EDT  
**Type:** Conversation issue log (no code changes in this step)

---

## Why this note
Capture the latest live conversation behavior that looked incorrect, so we can debug it systematically and include it in reporting.

---

## Conversation flow captured

1. Student selected a scoped shoulder differential topic from cards.
2. Tutoring continued for multiple turns.
3. Student provided strong clinical/differential responses.
4. System kept asking follow-up questions instead of cleanly closing.
5. Hint display appeared not to progress as expected in this run context.
6. Dean setup trace snippet showed an overly long `locked_answer` value in one path (not short canonical answer).

---

## Observed issues

### Issue A — Clinical flow continuation feels excessive
- **Observed:** The system continued asking deeper follow-up questions even after high-quality, correct responses.
- **Expected:** Clinical should be bounded and close after pass criteria (or at max clinical turns), with predictable termination.
- **Impact:** UX confusion; hard to capture a clean “end state” screenshot.

### Issue B — Hint progression ambiguity
- **Observed:** Hints appeared not to move up in this session segment.
- **Expected:** Hints should increase only on clear `incorrect` classification in tutoring, and behavior should be obvious in UI/debug.
- **Impact:** User perceives inconsistency in tutoring pressure/escalation.

### Issue C — `locked_answer` quality inconsistency
- **Observed:** `locked_answer` in trace appeared as a long sentence-like proposition in one output, not short/specific.
- **Expected:** Canonical short answer (e.g., `axillary nerve`).
- **Impact:** Can distort reach/gating logic and make explanations feel disconnected.

---

## Probable root causes (to verify)

1. Clinical pass/exit gating may be strict enough that strong but not thresholded answers continue looping.
2. Hint increment logic is working only for `incorrect`, while replies in this run may have been classified as `partial_correct`/`question`.
3. Answer-lock sanitization may still have edge cases where verbose phrase slips through.

---

## Repro marker for this issue set
Use the same profile and topic path:
- Student profile: confident-but-mixes-nerve-localization
- Topic: shoulder abduction differential (axillary vs suprascapular)
- Run through core + clinical and inspect:
  - `student_state` per turn
  - `clinical_turn_count` and pass threshold
  - `locked_answer` form
  - closeout trigger reason

---

## Immediate follow-up checklist

1. Confirm clinical close criteria hit/miss at each turn (`pass`, `confidence`, `clinical_turn_count`).
2. Add a visible closure reason in debug/export (`clinical_pass`, `clinical_max_turns`, `forced_assessment`, etc.).
3. Verify `locked_answer` remains canonical short form every turn.
4. Document one clean screenshot path with deterministic termination.

---

## Note
This entry logs behavior only. No product logic changes were made in this step.
