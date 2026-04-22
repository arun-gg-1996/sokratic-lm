# Sokratic Final Execution Plan (4 Phases)

Date: 2026-04-19  
Status: Approved for execution after gate checks

## Objective
Deliver a stable milestone covering:
1. Cache reliability with verified real-session cache hits.
2. Robust anchor locking (`locked_question`, `locked_answer`) and cleaner topic scoping.
3. De-hardcoded, config-driven behavior.
4. UX/debug/export parity for reliable evaluation workflows.

## Global Guardrails
1. Execute strictly phase-by-phase; do not interleave.
2. Commit after each successful phase.
3. If a phase gate fails, stop immediately.
4. `conversation/nodes.py` edits are allowed only for:
   - Phase 2: invariant logging in `dean_node` for tutoring-without-anchors.
   - Phase 3: `_compute_mastery_tiers` and `_tier_from_score` reading from `cfg.mastery.*`.
   - Any other `nodes.py` edit requires stop/report.

---

## Phase 1: Cache Activation (Non-Negotiable Proof)

### Implement
1. Prompt block structure per Teacher/Dean call:
   - Block 1 (cached): `role_base + wrapper_delta`.
   - Block 2 (cached): retrieved propositions (stable post topic-lock).
   - Block 3 (cached): append-only rendered history.
   - Block 4 (uncached): turn deltas (`hint_level`, `student_state`, critique, current-turn specifics).
2. Real prompt factoring (`teacher_base`, `dean_base`) with true deltas (no alias no-op).
3. Keep append-only no-window history rendering.
4. Add per-call cache observability:
   - `cache_read`, `cache_write`.
   - cached block token-size estimates.

### Gate (must pass before Phase 2)
1. First eligible repeated wrapper call shows `cache_write > 0`.
2. Second invocation of repeated wrapper shows `cache_read > 0`.
3. Comparable ~9-turn session is measurably cheaper than Apr 20 baseline (`~$0.21`, `~49K input tokens`), target around 20%+ reduction.
4. Must be proven from a real exported trace (not summary text).

### Reviewer Reminder (Phase 1)
1. Open export JSON yourself.
2. Inspect `all_turn_traces` turn 2/3 for repeated wrappers (`teacher.draft_socratic`, `dean._setup_call`, etc.).
3. `cache_read` should be non-trivial (hundreds/thousands), not tiny values.
4. Tiny `cache_read` indicates partial failure (only a small block caching). Treat as fail.

---

## Phase 2: Anchors + Proposition Alignment

### Implement
1. Add `locked_question` to state; lock once together with `locked_answer` immediately after topic-lock.
2. Add a Dean anchor extraction call returning:
   - `locked_question`, `locked_answer`, `rationale`.
3. Remove per-turn locked-answer re-proposal from setup.
4. Reached-answer gate requires:
   - non-empty locked anchors,
   - student naming/matching the locked answer signal.
5. Make sanitizer transparent:
   - return `(value, action_reason)` and log the action to trace.
6. Prompt text uses “propositions” (prompt-level only).
7. Add invariant logging when tutoring proceeds without anchors.
8. If anchor extraction fails, route back to topic-scoping with explicit narrowing prompt; do not proceed with normal tutoring.

### Mandatory Pre-step (no skip)
1. Run live retrieval diagnostic and log returned keys.
2. Verify schema before sanitizer field usage updates.
3. Expected keys from prior verified run:
   - `chunk_id, text, score, chapter_num, chapter_title, section_title, subsection_title, page, element_type, image_filename`.
4. If keys differ, stop and resolve mapping before proceeding.

### Gate
1. Narrow-topic session locks both anchors on topic-lock turn.
2. Broad-topic session triggers explicit re-scoping, not silent drift.
3. Sanitizer decisions are visible in trace.
4. No tutoring progression without anchors.

---

## Phase 3: De-hardcode + Config-driven Rules

### Implement
1. Remove domain-specific hardcoded guess lists from Dean path.
2. Remove heuristic confidence blending/caps; rely on LLM confidence + prompt calibration.
3. Move mastery weights/thresholds to `cfg.mastery.*`.
4. Keep memory persistence intentionally stubbed for now.
5. Prevent pseudo-memory effects while stubbed:
   - no prior-session weak-topic injection in new sessions unless explicitly loaded.
6. Add strict embedding-dimension startup check (fail fast on mismatch).

### Gate
1. Grep confirms targeted hardcoded constants/functions removed.
2. 3 replay sessions show no major pedagogy regressions.
3. Mastery behavior remains sensible after config migration.
4. Embedding mismatch fails with clear startup error.

---

## Phase 4: UX + Debug + Export Parity

### Implement
1. Clamp hint UI to max and show explicit “hints exhausted” state.
2. Render topic/choice cards only after tutor streaming completes.
3. Clear pending-choice immediately on card click to prevent duplicates.
4. Add “Something else” escape-hatch behavior:
   - dismiss cards,
   - show composer,
   - next text is treated as fresh topic statement and re-enters topic scoping.
5. Disable composer on terminal phase; show session-complete/new-chat action.
6. Upgrade debug panel to Streamlit-like depth:
   - per-message expandable trace,
   - per-call full details (prompt, messages sent, response, tools, tokens, cache, latency, cost).
7. Export upgrades:
   - `input_hash` per API call,
   - `decision_effect` per API call,
   - consistent turn envelope,
   - strip underscore-prefixed runtime fields from export.
8. Closeout tone fix is prompt-only (`dean_close_session_static`); no code-level tone filter.

### Gate
1. No duplicate or premature cards.
2. No invalid hint display (e.g., `4/3`).
3. Terminal phase blocks input as expected.
4. Debug panel supports full per-call inspection.
5. Export is evaluation-ready for replay and scoring.

---

## Hard Stop Review Checklist

### Phase 1 Pass/Fail
- Export reviewed directly by reviewer: `YES/NO`
- Repeated wrapper has `cache_write > 0`: `YES/NO`
- Repeated wrapper has non-trivial `cache_read > 0`: `YES/NO`
- Comparable run cost lower than baseline: `YES/NO`
- Decision: `PASS/FAIL`

### Phase 2 Pre-step Pass/Fail
- Retrieval diagnostic run/logged: `YES/NO`
- Returned key schema captured: `YES/NO`
- Sanitizer mapping verified or updated: `YES/NO`
- Decision: `PASS/FAIL`

## Execution Rule
Do not move to the next phase until the current gate has concrete evidence (real export + logs), not narrative summaries.

## Sticky Reviewer Reminders
1. Do not accept Phase 1 based on narration. Open the real exported JSON and verify `cache_read > 0` on repeated wrapper calls yourself.
2. Treat tiny cache reads as partial failure. Repeated wrappers should show non-trivial cache-read volume once warm.
3. Do not skip Phase 2 pre-step. Run and log live retrieval key diagnostics before assuming sanitizer field mapping.
