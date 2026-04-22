# Execution Log - 2026-04-20

## Scope
Started execution of the approved 4-phase plan (cache, anchors, de-hardcode, UX/debug/export).

## Phase Progress

### Phase 1 (Cache wiring + observability)
- `conversation/teacher.py`
  - Added `input_hash` per API call trace entry.
  - Added `decision_effect` field (default `None`) to call traces.
- `conversation/dean.py`
  - Added `input_hash` per API call trace entry.
  - Added `decision_effect` field (default `None`) to call traces.
  - Added explicit `decision_effect` updates:
    - `classification_only` on setup classification,
    - `qc_pass` / `qc_fail` on quality check,
    - `session_close_evaluated` on successful closeout JSON,
    - `classification_fallback` / `fallback_used` on fallback paths.

### Phase 2 (Anchors + proposition schema discipline)
- Retrieval diagnostic executed (live):
  - `sample_count = 5`
  - `sample_keys = ['chapter_num', 'chapter_title', 'chunk_id', 'element_type', 'image_filename', 'page', 'score', 'section_title', 'subsection_title', 'text']`
- Result: schema matches expected chunk/proposition mapping used by sanitizer.
- No sanitizer field remap needed.

### Phase 3 (strict runtime path cleanup)
- `backend/dependencies.py`
  - retriever path remains strict production path (no runtime mock fallback).
- `retrieval/retriever.py`
  - strict Qdrant query path retained.

### Phase 4 (UX/debug/export parity)
- `backend/api/session.py`
  - export now strips underscore-prefixed internal runtime/debug fields recursively.
  - export now includes `locked_question` and `locked_answer`.
- `backend/api/chat.py`
  - websocket payload now includes `phase`.
- `backend/models/schemas.py`
  - added `phase` field to `ServerMessage`.
- `frontend/src/stores/sessionStore.ts`
  - added `sessionPhase` state.
  - duplicate tutor-message dedupe no longer mutates `pendingChoice` (prevents early card flashes).
- `frontend/src/hooks/useWebSocket.ts`
  - consumes websocket `phase` and sets store `sessionPhase`.
  - tutor message now receives phase for message metadata.
- `frontend/src/hooks/useSession.ts`
  - sets initial phase from initial debug payload.
  - added `restartSession()` helper.
- `frontend/src/components/chat/ChatView.tsx`
  - disables cards/composer in terminal phase (`memory_update`).
  - adds terminal footer: "Session complete" + "Start a new chat".
  - topic "Something else" now dismisses cards and returns to normal composer.
- `frontend/src/components/cards/TopicCard.tsx`
  - replaced inline free-text box with explicit escape-hatch button:
    - "Something else (type your own topic)".
- `frontend/src/components/layout/Sidebar.tsx`
  - hint display clamped to max; shows "Hints exhausted" when over cap.
- `frontend/src/components/debug/DebugPanel.tsx`
  - added visible `input_hash` and `decision_effect` per API call entry.
- `frontend/src/types/index.ts`
  - added websocket `phase?: string`.
- `config.yaml`
  - replaced multiple prompt-text mentions of "Retrieved passages" with "Retrieved propositions".
  - replaced several hardcoded anatomy-specific prompt literals with domain-parameterized placeholders.
- `conversation/teacher.py`
  - replaced one hardcoded anatomy fallback user message with domain-parameterized text.

## Validation Run Notes
- Python compile checks passed:
  - `conversation/teacher.py`
  - `conversation/dean.py`
  - `conversation/nodes.py`
  - `conversation/state.py`
  - `backend/api/session.py`
  - `backend/api/chat.py`
  - `backend/models/schemas.py`
  - `backend/dependencies.py`
  - `retrieval/retriever.py`
- Frontend production build passed:
  - `cd frontend && npm run -s build`
- `pytest` in this environment returned code `-1` with no stdout; requires separate investigation.

## Reviewer Reminders (kept explicit)
1. Phase 1 must be accepted only from a **real exported trace**:
   - repeated wrapper call has `cache_write > 0`,
   - second invocation has non-trivial `cache_read > 0` (hundreds/thousands, not tiny),
   - compare against Apr 20 baseline (~$0.21 / ~49k input tokens / ~9 turns).
2. Phase 2 pre-step is mandatory:
   - live retrieval schema diagnostic must be run/logged before sanitizer assumptions.
   - if keys differ, stop and remap field access before proceeding.
