# Deploying Sokratic on GCP for a Short Test Window

This app is easiest to deploy like an AWS EC2 app: one Compute Engine VM, one
Python backend process, one local Qdrant container, and the built Vite frontend
served by Nginx on the same public origin.

## Why Compute Engine

The backend is stateful and memory-heavy:

- FastAPI pre-warms the retriever, BM25 index, spaCy/scispaCy, MedCPT
  cross-encoder, mem0, and LangGraph on startup.
- Qdrant stores both the knowledge-base collection and cross-session memory.
- Session state is currently in-process.
- Mastery/student data is persisted under `data/`.

Cloud Run is possible only after splitting Qdrant and state into managed
services. For a 10-day tester demo, that adds more operational risk than it
removes.

## Recommended VM

Use `us-central1` unless you have a reason to choose another region.

Start with:

- Machine: `e2-standard-4` (4 vCPU, 16 GiB RAM)
- Disk: 80-100 GB `pd-balanced`
- OS: Ubuntu 22.04 or 24.04 LTS
- Firewall: allow `tcp:80`, and `tcp:443` if you add TLS

Scale up to `e2-standard-8` (8 vCPU, 32 GiB RAM) if several testers use it at
the same time or the cross-encoder/torch stack pushes memory too close to the
limit. Avoid the free-tier/small instances; this repo needs real RAM.

## Cost Shape

For 10 days, approximate on-demand compute before taxes/egress:

- `e2-standard-4`: about `0.134 * 24 * 10 = $32.17`
- `e2-standard-8`: about `0.268 * 24 * 10 = $64.33`
- 100 GB `pd-balanced`: about `$10/month`, so roughly `$3.33` for 10 days

LLM provider costs are separate. Stop or delete the VM after the test window.
Deleting the VM plus disk stops both compute and disk charges.

## Deployment Shape

```text
Browser
  -> https://YOUR_DOMAIN/
      Nginx
        /       -> frontend/dist static files
        /api/*  -> http://127.0.0.1:8000/api/*
        /ws/*   -> http://127.0.0.1:8000/ws/* with websocket upgrade

VM
  - uvicorn backend.main:app --host 127.0.0.1 --port 8000
  - qdrant/qdrant:v1.13.4 bound to localhost:6333
  - data/ and .qdrant_data/ on persistent disk
```

## Setup Outline

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3-pip nodejs npm nginx docker.io git
sudo usermod -aG docker "$USER"
```

Clone the repo on the Nidhi branch, then from the repo root:

```bash
git checkout nidhi/reach-gate-and-override-analysis
```

Install dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r backend/requirements.txt
pip install https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz

cp .env.example .env
```

Put all credentials in `/opt/sokratic/.env` and nowhere else:

- `SOKRATIC_AUTH_USERS` for `arun`, `nidhi`, and `grader`
- `SOKRATIC_AUTH_SECRET`
- `ANTHROPIC_API_KEY` or AWS Bedrock credentials
- `OPENAI_API_KEY`
- `HF_USERNAME` / `HF_TOKEN` if needed
- GCP deployment coordinates such as `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_ZONE`

The `grader` profile is separate from `arun` and `nidhi`, so grader sessions
write to their own student id and memory namespace.

Bootstrap and restore data from HuggingFace:

```bash
.venv/bin/python scripts/bootstrap_corpus.py
scripts/qdrant_up.sh

SNAP=data/indexes/qdrant_sokratic_kb_chunks.snapshot
curl -X POST "http://localhost:6333/collections/sokratic_kb_chunks/snapshots/upload?priority=snapshot" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@${SNAP}"
```

Do not rebuild or manually move data on the VM for the demo. HuggingFace is the
source of truth for every deployable artifact listed in `data/MANIFEST.json`.
At the time of writing that includes:

- `data/processed/chunks_openstax_anatomy.jsonl`
- `data/indexes/bm25_chunks_openstax_anatomy.pkl`
- `data/textbook_structure.json`
- `data/topic_index.json`
- `data/indexes/qdrant_sokratic_kb_chunks.snapshot`

The Qdrant snapshot restore populates the `sokratic_kb_chunks` collection
directly, so no OpenAI embedding rebuild is needed during deployment.

Build the frontend:

```bash
cd frontend
npm ci
npm run build
```

For same-origin Nginx deployment, `VITE_API_BASE` can be omitted. In local dev,
the frontend still defaults to `http://localhost:8000`.

## Backend systemd Unit

Create `/etc/systemd/system/sokratic-backend.service`:

```ini
[Unit]
Description=Sokratic FastAPI backend
After=network-online.target docker.service
Wants=network-online.target

[Service]
WorkingDirectory=/opt/sokratic
EnvironmentFile=/opt/sokratic/.env
Environment=HF_HOME=/opt/sokratic/.cache/huggingface
ExecStart=/opt/sokratic/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sokratic-backend
```

## Nginx Site

```nginx
server {
    listen 80;
    server_name YOUR_DOMAIN_OR_IP;

    root /opt/sokratic/frontend/dist;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

## Smoke Checks

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:6333/healthz
.venv/bin/python -c "from retrieval.retriever import ChunkRetriever; r=ChunkRetriever(); print(r.collection, len(r.retrieve('What is the rotator cuff?')))"
```

Open the public URL and start one chat session. The first backend start can be
slow while torch/model caches initialize; subsequent restarts should be faster
once Hugging Face/scispaCy assets are cached on disk.

## Cleanup After 10 Days

If the test is over and you do not need the data, delete the VM and disk. If you
want to preserve tester data, snapshot or download:

- `data/student_state/`
- `data/*.db`
- `.qdrant_data/`
