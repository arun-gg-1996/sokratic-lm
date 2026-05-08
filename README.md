# Sokratic

A Socratic tutor for human anatomy, built around a retrieval-augmented LangGraph
backend (FastAPI + Anthropic + OpenAI), a Qdrant vector store, and a Vite/React
frontend.

The backend pre-loads the OpenStax Anatomy & Physiology corpus (chunks JSONL +
BM25 index + Qdrant snapshot of dense embeddings), wraps an LLM-driven Dean +
Teacher + verifier pipeline behind a token-streaming WebSocket, and persists
per-student session/mastery state via mem0 and SQLite.

---

## Repository layout

```
backend/                 FastAPI app, auth, REST routes, WebSocket handler
  api/                     /api/* routers (session, sessions, users, vlm, mastery)
  models/                  pydantic schemas
  auth.py                  HMAC-token bearer auth + middleware
  main.py                  uvicorn entry point + lifespan warmup
config/
  base.yaml                prompts, retrieval thresholds, model names
  domains/                 per-domain overrides (ot.yaml, physics.yaml)
config.py                  config loader (env-driven domain selection)
conversation/            tutoring graph (Dean + Teacher + verifier)
  graph.py                 LangGraph wiring
  state.py                 TutorState schema
  dean.py / dean_v2.py     planner agent
  teacher.py / teacher_v2.py
                           response generator (streams via Anthropic)
  preflight.py             cheap classifiers run before Dean (off-topic,
                           help-abuse, deflection, low-effort)
  retry_orchestrator.py    Dean→Teacher loop with quartet QC
  verifier_quartet.py      4 Haiku checks against the locked answer
  topic_lock_v2.py         topic resolution + anchor-question lock
  assessment_v2.py         post-tutoring clinical phase
  lifecycle_v2.py          rapport + memory_update phases
  snapshots.py             per-turn observability writer
ingestion/               PDF→chunks→BM25/embeddings pipeline
memory/                  mem0 client + SQLite store
retrieval/               BM25 + Qdrant retriever (ChunkRetriever)
frontend/                Vite + React + Zustand UI
scripts/
  bootstrap_corpus.py      pull artifacts from HuggingFace (sha256-aware)
  publish_corpus.py        publish artifacts to HuggingFace
  qdrant_up.sh             start qdrant via docker
  reindex_chunks.py        re-embed chunks into Qdrant
  preflight_check.py       runtime prerequisites (env, disk, network)
  postflight_check.py      data integrity + retrieval probe + LLM probe
  deploy_vm_bootstrap.sh   one-shot VM provisioner (Ubuntu 22.04/24.04)
tests/                   pytest suite
data/MANIFEST.json       sha256 manifest for the HuggingFace corpus snapshot
```

---

## Quick start (local)

### 1. Prerequisites

- Python 3.11 (3.10 also works)
- Node 20+
- Docker (for Qdrant)
- An OpenAI API key, an Anthropic API key
- A HuggingFace token (read access to the corpus repo)

### 2. Clone + create virtualenv

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r backend/requirements.txt
pip install https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz
```

The scispaCy model is ~1 GB on first install (UMLS knowledge base for
biomedical entity linking).

### 3. Build the frontend

```bash
cd frontend
npm ci
npm run build
cd ..
```

### 4. Configure secrets

```bash
cp .env.example .env
# Open .env in your editor and fill in:
#   ANTHROPIC_API_KEY      — Claude / Haiku access
#   OPENAI_API_KEY         — embeddings + classifier fallback
#   HF_TOKEN               — HuggingFace read access (only for private repos)
#   HF_USERNAME            — HuggingFace account that owns the corpus repo
#   SOKRATIC_AUTH_USERS    — username:password,username:password,…
#   SOKRATIC_AUTH_SECRET   — random 32-byte hex (openssl rand -hex 32)
#   SOKRATIC_CORS_ORIGINS  — comma-separated allowlist for the frontend
#   SOKRATIC_DOMAIN        — "ot" (anatomy) or "physics"
```

### 5. Pull the corpus from HuggingFace

```bash
python scripts/bootstrap_corpus.py
```

Idempotent and sha256-aware — safe to re-run. Pulls:

- `data/processed/chunks_openstax_anatomy.jsonl` (~11 MB)
- `data/indexes/bm25_chunks_openstax_anatomy.pkl` (~17 MB)
- `data/indexes/qdrant_sokratic_kb_chunks.snapshot` (~360 MB)
- `data/topic_index.json`
- `data/textbook_structure.json`

### 6. Start Qdrant + restore the snapshot

```bash
bash scripts/qdrant_up.sh

# One-time snapshot restore:
curl -X POST \
  "http://localhost:6333/collections/sokratic_kb_chunks/snapshots/upload?priority=snapshot" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@data/indexes/qdrant_sokratic_kb_chunks.snapshot"
```

If the snapshot fails to restore (Qdrant version mismatch), you can re-embed
chunks instead:

```bash
python scripts/reindex_chunks.py
```

### 7. Verify the environment

```bash
python scripts/preflight_check.py     # before starting the backend
python scripts/postflight_check.py    # after the corpus is in place
```

Both should report `0 fail`. The postflight check includes a live retrieval
probe and a 1-token round-trip to each LLM provider.

### 8. Run

```bash
# Terminal 1 — backend
uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — frontend (dev mode)
cd frontend
npm run dev
```

The frontend defaults to `http://localhost:5173` and proxies API calls to
`http://localhost:8000` when run in dev. Login with one of the credentials
defined in `SOKRATIC_AUTH_USERS`.

For a production-style local run, serve `frontend/dist/` through nginx and
proxy `/api/*` and `/ws/*` to `127.0.0.1:8000`.

---

## Deployment

For Ubuntu 22.04 / 24.04 servers (e.g. a GCP Compute Engine VM):

```bash
sudo bash scripts/deploy_vm_bootstrap.sh
```

The script is idempotent and runs eight stages with two hard gates:

1. apt packages + Node 20 from NodeSource
2. clone / fast-forward the repo into `/opt/sokratic`
3. Python venv + `requirements.txt`
4. `npm ci && npm run build`
5. `.env` presence check
   - **Preflight gate** — refuses to proceed if any required check fails.
6. `bootstrap_corpus.py` (HuggingFace pull, sha256-aware)
7. Qdrant up + snapshot restore
   - **Postflight gate** — refuses to start the backend if data integrity,
     retrieval, or LLM connectivity is broken.
8. systemd unit + nginx site, start services, wait for `/health = 200`.

Place `/opt/sokratic/.env` before running. Pass `--restart-only` to bounce the
backend after editing config.

---

## Tests

```bash
pytest -q
```

The suite covers state schema, the conversation graph, classifiers, retriever,
SQLite store, and prompt-parity checks between v1 and v2 paths.

---

## Architecture overview

A single tutoring turn flows through:

```
student message
   ↓
preflight (3 Haiku classifiers in parallel — ~0.3¢ each)
   ↓ if any fires → Teacher renders a redirect, Dean is skipped
   ↓
Dean (Sonnet plans the turn — TurnPlan)
   ↓
Teacher (Sonnet drafts the response — streams via WebSocket)
   ↓
verifier quartet (4 Haiku QC checks against the locked answer)
   ↓ if QC fails → Dean replans → Teacher redrafts (one retry)
   ↓
state update + per-turn snapshot + transmission to client
```

The Dean and Teacher share the same Sonnet model, gated by different prompts
and tool-call shapes. Retrieval is BM25 + dense (Qdrant) + cross-encoder
re-rank on the OpenStax Anatomy chunks; the locked anchor question is generated
once at topic-lock time and cached on session state.

Per-student memory lives in mem0 (Qdrant collection `sokratic_memory`) plus a
SQLite store for session metadata, mastery breakdowns, and weak-topic
tracking. Memory is read in `rapport_node` and written in `memory_update_node`
at the end of each session.

---

## License

This project bundles content from the OpenStax Anatomy & Physiology textbook
(CC-BY 4.0). All other source code is original.
