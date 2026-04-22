# Sokratic Backend (FastAPI)

Thin API layer around the existing `conversation/graph.py` tutor graph.

## Run locally

```bash
# from repo root, with your Python venv active
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --port 8000
```

## Endpoints

- `GET /health`
- `GET /api/users`
- `POST /api/session/start`
- `GET /api/session/{thread_id}/state`
- `GET /api/session/{thread_id}/export`
- `GET /api/students/{student_id}/overview`
- `WS /ws/chat/{thread_id}`

CORS is enabled for `http://localhost:5173` (Vite frontend dev server).
