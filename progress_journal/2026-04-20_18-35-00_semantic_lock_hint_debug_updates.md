# Execution Log - 2026-04-20 (Semantic Lock + Hint/Debug Flow)

## User-Requested Changes
1. Use LLM/API decisioning for answer reach checks (avoid brittle exact-string rule gates).
2. Keep `hint_level=0` until topic/answer are locked.
3. Show locked topic in sidebar once locked.
4. Show locked answer state in debug (not left panel).
5. Fix repeated "I need a narrower focus..." loop.
6. Expose progressive hints in debug.
7. Journal all changes.

## Implemented

### `conversation/dean.py`
- Removed strict lexical gate dependency in setup fallback (`_student_named_locked_answer` reference removed).
- `student_reached_answer` now uses Dean model signal + confidence threshold + locked answer presence:
  - semantic classification from Dean remains primary (no exact substring requirement).
- `_sanitize_locked_answer`:
  - removed brittle `cand in proposition_text` exact-string wipe.
  - retains concise cap (`max 15 words`) and normalization.
- Topic pre-lock and reprompt returns continue forcing `hint_level: 0`.
- Hint defaults standardized to `0` in setup parse paths.
- Added/kept progressive hint visibility:
  - `debug.turn_trace` entry `dean.hint_progress`
  - cumulative `debug.hint_progress` list.
- Added lock-repair retry:
  - if first anchor lock yields empty/invalid `locked_answer`, performs one additional LLM repair call (`dean._lock_anchors_repair_call`) before failing lock.
- `dean._teacher_preflight_brief` now includes active hint text from `debug.hint_plan`.

### `config.yaml`
- Dean setup prompts updated to explicitly require **semantic concept match** (not exact string overlap) when `locked_answer` exists.
- Lock-anchors prompt changed:
  - from strict `<=6 words` to concise target (`typically 2-10`, hard max 15).
- Added hint planning prompts:
  - `dean_hint_plan_static`
  - `dean_hint_plan_delta`
  - `dean_hint_plan_dynamic`

### Backend Debug Payload
- `backend/api/session.py` and `backend/api/chat.py` now include in debug payload:
  - `topic_confirmed`
  - `topic_selection`
  - `locked_question`
  - `locked_answer`
  - `answer_locked` (boolean)
- Hint debug default standardized to `0` pre-lock in payload.

### Frontend Visibility
- `frontend/src/components/layout/Sidebar.tsx`
  - hint fallback default now `0`
  - shows `Topic: <topic_selection>` when `topic_confirmed=true`.
- `frontend/src/components/debug/DebugPanel.tsx`
  - added summary fields: `topic_confirmed`, `topic_selection`, `locked_question`, `answer_locked`
  - added collapsible sections for:
    - `hint_plan`
    - `hint_progress`

## Validation Run
- Python compile:
  - `.venv/bin/python -m py_compile conversation/dean.py backend/api/chat.py backend/api/session.py conversation/state.py`
  - Result: PASS
- Frontend build:
  - `cd frontend && npm run -s build`
  - Result: PASS

## Notes
- The repeated narrow-focus loop was primarily driven by anchor lock failure churn; this pass reduces that by:
  1) removing brittle exact proposition-string sanitizer wipes,
  2) adding one LLM repair attempt for lock answer before failing the lock.
- If loop persists in specific sessions, inspect `dean.anchors_locked`, `dean.anchor_extraction_failed`, and `dean.sanitize_locked_answer` entries in exported `all_turn_traces`.
