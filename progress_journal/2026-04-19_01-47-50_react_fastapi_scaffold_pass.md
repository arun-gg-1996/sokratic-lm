# React + FastAPI Scaffold Pass

Date: 2026-04-19 01:47:50

## Objective
Scaffold migration path from Streamlit UI to React frontend + FastAPI backend wrapper while keeping tutor core logic untouched.

## Implemented in this pass

### Backend scaffold (`backend/`)
- Added FastAPI app with CORS and health endpoint.
- Added APIs:
  - `GET /api/users`
  - `POST /api/session/start`
  - `GET /api/session/{thread_id}/state`
  - `GET /api/session/{thread_id}/export`
  - `GET /api/students/{student_id}/overview`
  - `WS /ws/chat/{thread_id}`
- Added schema layer (`backend/models/schemas.py`) for requests/responses.
- Added dependency layer (`backend/dependencies.py`) that mirrors Streamlit init pattern:
  - `Retriever()` with `MockRetriever()` fallback
  - `MemoryManager()`
  - `build_graph(retriever, memory_manager)`
- Added in-memory runtime store for per-thread state handling in scaffold phase.

### Frontend scaffold (`frontend/`)
- Created React + Vite + Tailwind structure and route shell:
  - `/` user picker
  - `/chat` chat view
  - `/overview` session overview
- Implemented app shell + sidebar (independent lane structure).
- Implemented chat surface with:
  - message list
  - composer
  - pending-choice cards (`opt_in`, `topic`)
  - thinking indicator dots
  - UI text streaming (`StreamingText`)
- Implemented account popover with:
  - switch user
  - theme toggle (light/dark persisted)
  - debug toggle (persisted)
  - export current session
- Implemented debug panel that reads `debug.turn_trace`.
- Added API/WebSocket client hooks and Zustand stores.

### Docs/config hygiene
- Updated frontend README run commands to npm.
- Updated root README frontend run commands to npm.
- Updated root `.gitignore` with frontend/backend local artifact ignores.

## Validation results
- Backend Python syntax compile: PASS.
- Backend import check (`from backend.main import app`): PASS.
- FastAPI TestClient smoke:
  - `/health` -> 200
  - `/api/users` -> 200
- Frontend build:
  - initially failed (`import.meta.env` typing)
  - fixed by adding `frontend/src/vite-env.d.ts`
  - final `npm run build`: PASS.

## Notes
- `pnpm` is not installed on this machine; npm used successfully.
- Backend requirements installed in `.venv`.

## Remaining work (next pass)
1. Run live end-to-end smoke (`uvicorn` + `npm run dev`) and verify full turn flow over WebSocket.
2. Validate `pending_choice` UX behavior against real tutor states (topic lock + clinical opt-in moments).
3. Tighten visual polish to Claude-like structure with Sokratic identity (spacing, composer behavior, dark/light parity).
4. Decide if runtime should remain in-memory store or switch to graph checkpointer-backed session management.
5. Add optional endpoint/contract tests for session start and chat loops.
