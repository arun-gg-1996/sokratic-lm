# Domain switcher — VM deploy steps

The frontend now reads a `sokratic_domain` choice from `localStorage` and
routes API + WS calls through `/physics/...` for physics or `/...` for
anatomy (the default). To make those routes resolve to the right backend,
the VM needs:

1. A second `sokratic-backend-physics` systemd unit on port 8001 with
   `SOKRATIC_DOMAIN=physics`.
2. An nginx upstream block that strips the `/physics` prefix and proxies
   to `127.0.0.1:8001`.

The existing anatomy backend on port 8000 stays untouched. JWTs are
shared because both backends read the same `SOKRATIC_AUTH_USERS` and
`SOKRATIC_AUTH_SECRET` from `/opt/sokratic/.env`.

## Prerequisites on the VM

```bash
# Confirm physics artifacts are on the VM (pulled via bootstrap_corpus.py)
ls -la /opt/sokratic/data/processed/chunks_openstax_physics.jsonl \
       /opt/sokratic/data/indexes/bm25_chunks_openstax_physics.pkl \
       /opt/sokratic/data/topic_index_openstax_physics.json \
       /opt/sokratic/data/textbook_structure_openstax_physics.json \
       /opt/sokratic/data/curated_abbrevs_openstax_physics.json \
       /opt/sokratic/data/artifacts/raptor_subsection_summaries_openstax_physics.jsonl

# Confirm physics qdrant collection exists
curl -s http://127.0.0.1:6333/collections/sokratic_physics_kb | python3 -m json.tool | head -8

# Confirm physics SQL is seeded (chapters/sections/subsections/abbrevs)
sudo -u $USER /opt/sokratic/.venv/bin/python -c "
from memory.sqlite_store import SQLiteStore
import os
os.environ['SOKRATIC_DOMAIN'] = 'physics'
s = SQLiteStore()
c = s._conn()
def n(q): return c.execute(q).fetchone()[0]
print('physics chapters:    ', n('SELECT COUNT(*) FROM chapters'))
print('physics sections:    ', n('SELECT COUNT(*) FROM sections'))
print('physics subsections: ', n('SELECT COUNT(*) FROM subsections'))
print('physics abbreviations:', n('SELECT COUNT(*) FROM topic_abbreviations'))
"
# Expect: 17 chapters / 113 sections / 387 subsections / 60 abbreviations
```

If any of those are missing, run:

```bash
cd /opt/sokratic
.venv/bin/python scripts/bootstrap_corpus.py            # pulls all HF artifacts
SOKRATIC_DOMAIN=physics .venv/bin/python scripts/seed_curriculum.py
SOKRATIC_DOMAIN=physics .venv/bin/python scripts/seed_summaries_and_abbreviations.py

# Restore physics qdrant snapshot if missing
curl -X POST "http://127.0.0.1:6333/collections/sokratic_physics_kb/snapshots/upload?priority=snapshot" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@data/indexes/qdrant_sokratic_physics_kb.snapshot"
```

## Step 1 — second systemd unit

```bash
sudo tee /etc/systemd/system/sokratic-backend-physics.service > /dev/null <<'EOF'
[Unit]
Description=Sokratic FastAPI backend (physics domain)
After=network-online.target docker.service sokratic-backend.service
Wants=network-online.target

[Service]
User=arun-ghontale
WorkingDirectory=/opt/sokratic
EnvironmentFile=/opt/sokratic/.env
Environment=HF_HOME=/opt/sokratic/.cache/huggingface
Environment=SOKRATIC_DOMAIN=physics
ExecStart=/opt/sokratic/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sokratic-backend-physics
# wait ~60s for startup (model loading)
until curl -fsS http://127.0.0.1:8001/health 2>/dev/null; do sleep 5; done && echo "physics backend up"
```

The `Environment=SOKRATIC_DOMAIN=physics` line in the unit file is what
binds this process to physics — no app code change needed.

## Step 2 — nginx routing

Edit the existing nginx site (`/etc/nginx/sites-available/sokratic`) and add
two location blocks BEFORE the existing `/api/` and `/ws/` blocks (nginx
matches the longest prefix first, so order doesn't strictly matter, but
this keeps it readable):

```nginx
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 16M;

    root /opt/sokratic/frontend/dist;
    index index.html;

    # NEW: physics backend — strip /physics prefix, proxy to :8001
    location /physics/api/ {
        proxy_pass http://127.0.0.1:8001/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
    }

    location /physics/ws/ {
        proxy_pass http://127.0.0.1:8001/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    # EXISTING: anatomy backend (default, no prefix)
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
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

Then:

```bash
sudo nginx -t   # syntax check
sudo systemctl reload nginx
```

## Step 3 — verify routing

```bash
# Anatomy backend reachable through default path
curl -s http://127.0.0.1/api/users | python3 -m json.tool | head -5

# Physics backend reachable through /physics prefix
curl -s http://127.0.0.1/physics/api/users | python3 -m json.tool | head -5
# Should return the SAME 3 users (arun, nidhi, grader) — auth is shared.

# Health on each
curl -fsS http://127.0.0.1:8000/health    # anatomy
curl -fsS http://127.0.0.1:8001/health    # physics
```

Both should return `{"status": "ok"}`.

## Step 4 — frontend build + deploy

```bash
cd /opt/sokratic/frontend
sudo -u $USER npm run build
# nginx serves from /opt/sokratic/frontend/dist — no nginx reload needed,
# just a hard browser refresh (Cmd+Shift+R / Ctrl+F5).
```

## Memory footprint

Each backend warms up retriever + BM25 + scispacy + qdrant client. On the
GCP VM (e2-standard-4, 16 GB RAM):

- anatomy backend (already running): ~2 GB resident
- physics backend (new): ~2 GB resident
- qdrant: ~1.5 GB
- nginx + system: ~500 MB

Total: ~6 GB of 16 GB. Comfortable headroom.

## Rollback

If something breaks, the cleanest rollback is just stopping the physics
backend — the frontend's domain pill defaults to anatomy, and `/physics/*`
URLs return 502 (which the frontend can handle as "physics offline").

```bash
sudo systemctl stop sokratic-backend-physics
sudo systemctl disable sokratic-backend-physics
# Optionally remove the /physics nginx blocks and reload nginx.
```

The anatomy backend on :8000 is untouched throughout, so this never puts
the existing demo at risk.
