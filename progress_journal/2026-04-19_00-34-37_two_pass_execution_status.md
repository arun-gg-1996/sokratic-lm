# Two-Pass Execution Status (Pass 1 + Pass 2)

Date: 2026-04-19 00:34:37

## Scope executed

### Pass 1 (backend / orchestration)
- Added domain-aware config plumbing (`domain.*`) and threshold config (`thresholds.repetition_similarity`).
- Wired config loader to include `domain` block.
- Wired retriever to prefer `domain.kb_collection` and domain default for retrieval.
- Wired persistent memory namespace and domain collection (`mem0_namespace`, `memory_collection`).
- Removed confidence heuristics (`_heuristic_confidence` + uncertainty marker lists + blend/cap); now uses model confidence with deterministic state fallback only.
- Removed hardcoded domain guess list (`_COMMON_NERVE_GUESSES`); replaced with generic nerve-span extraction.
- Added one-call session closeout (`dean._close_session_call`) returning:
  - `core_mastery_tier`
  - `clinical_mastery_tier`
  - `mastery_tier`
  - `grading_rationale`
  - `student_facing_message`
  - `memory_summary`
- Added deterministic closeout fallback payload for malformed/unavailable JSON.
- Rewired assessment closeout through `_close_session_with_dean(...)`.
- Rewired memory update to flush `session_memory_summary` from closeout payload.
- Extended TutorState with:
  - `grading_rationale`
  - `session_memory_summary`
  - `pending_user_choice`

### Pass 2 (UI deterministic choices)
- Topic lock choice now reads deterministic pending-choice state and renders card buttons.
- Clinical opt-in choice now uses deterministic button flow with literal payloads (`"yes"`/`"no"`).
- During deterministic choice moments (topic selection, opt-in), free-text chat input is suppressed.
- Added lane alignment improvements for option card blocks.

## Validation performed
- Compile check passed:
  - `conversation/dean.py`
  - `conversation/nodes.py`
  - `conversation/state.py`
  - `conversation/teacher.py`
  - `conversation/rendering.py`
  - `conversation/summarizer.py`
  - `config.py`
  - `retrieval/retriever.py`
  - `memory/persistent_memory.py`
  - `ui/app.py`
- Manual execution of new tests via Python imports passed:
  - `tests/test_rendering.py`
  - `tests/test_prompt_parity.py`

## Notes / caveats
- Direct `pytest` invocation in this environment exits with code `-1` and no stdout; manual in-process test execution succeeded.
- Prompt factoring is currently structurally valid with parity tests passing; additional base extraction depth can be expanded later if desired.
- Retrieval freeze is enforced once topic locks (`debug.retrieval_calls` guard).

## Suggested immediate smoke checks in app
1. New session -> broad topic -> topic cards appear -> select card -> tutoring starts.
2. Reach core answer -> clinical opt-in buttons appear -> select yes/no and verify deterministic branch.
3. End session -> ensure single closeout message and memory flush path uses `session_memory_summary`.

