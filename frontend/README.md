# Sokratic Frontend (React + Vite)

## Prerequisites

- Node.js 20+
- pnpm 10+ (or npm/yarn)

## Setup

```bash
cd frontend
npm install
cp .env.example .env.local
```

## Run locally

```bash
# terminal 1 (repo root)
uvicorn backend.main:app --reload --port 8000

# terminal 2
cd frontend
npm run dev
```

Open <http://localhost:5173>.

## Notes

- User picker route: `/`
- Chat route: `/chat`
- Session overview route: `/overview`
- Debug panel visibility is toggled via account popover.
