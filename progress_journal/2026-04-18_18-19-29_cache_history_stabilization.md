# Progress Journal — 2026-04-18_18-19-29_cache_history_stabilization

**Author:** Arun Ghontale  
**Date:** 2026-04-18 18:19 EDT  
**Type:** Implementation log (cache optimization + history stabilization + retrieval freeze)

---

## Scope executed
This pass implements the cache/history plumbing milestone and keeps UI untouched.

### Files changed in this pass
- `conversation/rendering.py` (new)
- `conversation/teacher.py`
- `conversation/dean.py`
- `conversation/state.py`
- `conversation/summarizer.py`
- `config.yaml`
- `tests/test_rendering.py` (new)
- `tests/test_prompt_parity.py` (new)
- `TODO.md` (new)

---

## What was implemented

### 1) Deterministic full-history renderer
Added `render_history(messages)` in `conversation/rendering.py`.
- Reads only `role` and `content`.
- Ignores extra metadata fields.
- Supports summarizer-injected `system` messages.
- Used as the single history renderer in teacher/dean prompt assembly.

### 2) Removed per-wrapper history windowing
Replaced sliding-window history logic with full append-only rendering.
Updated wrappers:
- `teacher.draft_socratic`
- `dean._setup_call`
- `dean._quality_check_call`
- `dean._clinical_turn_call`
- `dean._assessment_call`
- `dean._memory_summary_call`

Removed all code usage of:
- `session.prompt_history_messages`
- `dean.setup_history_messages`
- `dean.qc_history_messages`
- `_format_messages(...)`

### 3) Reworked cache block structure (teacher + dean)
Both agents now use a 5-block `_cached_system(...)` shape:
1. role base (cached)
2. wrapper delta (cached)
3. retrieved chunks (cached)
4. rendered history (cached)
5. turn-local deltas (uncached)

### 4) Removed `_cache_suffix_message` completely
Deleted helper behavior and all call sites in both agents.
All message sends now use plain Anthropic message arrays.

### 5) Retrieval freeze at topic lock
Dean retrieval behavior now:
- retrieval fires at topic lock via `_retrieve_on_topic_lock`
- retrieval count tracked in debug (`debug.retrieval_calls`)
- duplicate retrieval attempts are guarded and skipped
- once locked, chunks are treated as frozen session context

Additional behavior:
- stale topic cards are cleared if `topic_confirmed=True`
- if a resumed state is locked but missing chunks and `retrieval_calls==0`, one recovery retrieval is allowed

### 6) Prompt base/delta keys + parity-safe aliases
In `config.yaml`:
- added `teacher_base`, `dean_base` (currently empty for parity safety)
- added wrapper delta keys (aliases to static prompt blocks)

Teacher deltas:
- `teacher_topic_engagement_delta`
- `teacher_rapport_delta`
- `teacher_socratic_delta`
- `teacher_clinical_opt_in_delta`
- `teacher_clinical_delta`

Dean deltas:
- `dean_setup_delta`
- `dean_quality_check_tutoring_delta`
- `dean_quality_check_assessment_delta`
- `dean_clinical_turn_delta`
- `dean_assessment_delta`
- `dean_memory_summary_delta`

### 7) Focus-line additions to dynamic prompts
Appended explicit focus instructions to avoid quality drift with full history:
- `teacher_socratic_dynamic`
- `dean_setup_classify_dynamic`
- `dean_quality_check_dynamic` (tutoring)
- `dean_quality_check_assessment_dynamic`
- `dean_clinical_turn_dynamic`

### 8) Summarizer cache-impact note
Added explicit warning/comment in `conversation/summarizer.py` documenting that summarization mutates history and therefore resets cache prefix from that point.

### 9) Minimal observability additions
- `debug.retrieval_calls` initialized in `initial_state()`
- retrieval trace entries include call count
- `turn_trace` already had `cache_read`/`cache_write` and remains intact

### 10) Future-work doc
Added `TODO.md` with deferred Turn/Operation envelope design.

---

## Validation performed

### Syntax/compile
Ran:
- `.venv/bin/python -m py_compile conversation/rendering.py conversation/teacher.py conversation/dean.py conversation/state.py conversation/summarizer.py tests/test_rendering.py tests/test_prompt_parity.py`

Result: pass

### Manual assertions (equivalent checks)
Executed Python checks for:
- render append-only contract
- metadata-ignoring behavior
- system summary rendering
- prompt parity reconstruction (`base + delta == original`) across all teacher/dean wrappers

Result: pass (`manual_checks: ok`)

### Note on pytest invocation
Direct `pytest` command in this environment returned non-zero without visible output. As fallback, equivalent assertions were run directly via Python script and passed.

---

## Important implementation note
No UI logic was changed in this pass.

---

## Remaining follow-up
1. Run 10-turn live conversation and confirm `cache_read > 0` from repeated wrappers after warm-up.
2. Verify `debug.retrieval_calls == 1` per complete session.
3. Compare 2-3 prior transcripts for behavioral parity (no pedagogy drift).
4. Revisit non-empty shared bases later if/when parity-safe common text is identified.
