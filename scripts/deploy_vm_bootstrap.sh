#!/usr/bin/env bash
# scripts/deploy_vm_bootstrap.sh
# ------------------------------
# One-shot VM provisioner for Sokratic on Ubuntu 22.04 / 24.04.
# Targets the deploy shape documented in docs/DEPLOY_GCP.md.
#
# Idempotent — safe to re-run after partial failures. Each step
# checks for the artifact it produces and skips if already present.
#
# What this DOES (gated pipeline — every gate is a hard fail):
#   1. Installs system packages (python3.11, node, docker, nginx, git)
#   2. Clones / fast-forwards the repo into /opt/sokratic
#   3. Creates Python venv + installs requirements.txt + scispacy model
#   4. Builds the frontend (npm ci + npm run build)
#   5. .env presence check (warns if missing)
#  ─5b. PREFLIGHT GATE  → scripts/preflight_check.py
#       Verifies python/node/docker/disk/network/.env-keys/MANIFEST.
#       Hard-fail before any byte pulled from HuggingFace.
#   6. Pulls expensive artifacts from HuggingFace via bootstrap_corpus.py
#      (idempotent, sha256-aware — only fetches missing or mismatched)
#   7. Starts Qdrant (docker) and restores the kb_chunks snapshot if
#      the collection is empty
#  ─7b. POSTFLIGHT GATE → scripts/postflight_check.py
#       Verifies manifest sha + topic_index + bm25 + qdrant points +
#       LIVE retrieval probe + LLM round-trip + smoke harness.
#       Hard-fail before systemd starts uvicorn.
#   8. Installs the sokratic-backend systemd unit + nginx site, starts
#      services, waits for /health = 200.
#
# What this does NOT do (left to the operator):
#   - Write /opt/sokratic/.env (place secrets manually before step 6/7;
#     the script will refuse to start the backend without it)
#   - Issue a TLS cert (use certbot once DNS is in place)
#   - Run scripts/publish_corpus.py (HF push lives off the dev box)
#
# Usage (on the VM, as the deploy user):
#   sudo bash scripts/deploy_vm_bootstrap.sh
#
# Or interactively (re-run after fixing .env):
#   sudo bash scripts/deploy_vm_bootstrap.sh --skip-system   # skip apt
#   sudo bash scripts/deploy_vm_bootstrap.sh --skip-frontend # skip npm
#   sudo bash scripts/deploy_vm_bootstrap.sh --restart-only  # only systemctl
#
# Prerequisites on the VM (fresh Ubuntu install):
#   - sudo access for the running user
#   - SSH-key-based github access for the repo (or use HTTPS clone with PAT)
#   - The .env file ready to drop in (will prompt if missing)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config knobs (env-overridable)
# ---------------------------------------------------------------------------

REPO_URL="${SOKRATIC_REPO_URL:-https://github.com/arun-gg-1996/sokratic-lm.git}"
BRANCH="${SOKRATIC_BRANCH:-nidhi/reach-gate-and-override-analysis}"
INSTALL_DIR="${SOKRATIC_INSTALL_DIR:-/opt/sokratic}"
DEPLOY_USER="${SOKRATIC_DEPLOY_USER:-${SUDO_USER:-${USER:-root}}}"
PY_BIN="${SOKRATIC_PY_BIN:-python3.11}"
QDRANT_HTTP_PORT="${QDRANT_HTTP_PORT:-6333}"
QDRANT_SNAPSHOT="${QDRANT_SNAPSHOT:-data/indexes/qdrant_sokratic_kb_chunks.snapshot}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-sokratic_kb_chunks}"

# Step toggles
SKIP_SYSTEM=0
SKIP_FRONTEND=0
SKIP_BOOTSTRAP=0
SKIP_QDRANT=0
SKIP_SMOKE=0
RESTART_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-system)     SKIP_SYSTEM=1 ;;
    --skip-frontend)   SKIP_FRONTEND=1 ;;
    --skip-bootstrap)  SKIP_BOOTSTRAP=1 ;;
    --skip-qdrant)     SKIP_QDRANT=1 ;;
    --skip-smoke)      SKIP_SMOKE=1 ;;
    --restart-only)    RESTART_ONLY=1 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf "\033[1;36m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[bootstrap]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[bootstrap]\033[0m %s\n" "$*" >&2; exit 1; }
need_root() { [[ $(id -u) -eq 0 ]] || fail "must be run with sudo"; }
have()   { command -v "$1" >/dev/null 2>&1; }
as_user() { sudo -u "$DEPLOY_USER" -E "$@"; }

# ---------------------------------------------------------------------------
# Step 0 — sanity
# ---------------------------------------------------------------------------

need_root
log "deploy_user=$DEPLOY_USER  install_dir=$INSTALL_DIR  branch=$BRANCH"

if [[ "$RESTART_ONLY" == "1" ]]; then
  log "restart-only: skipping setup, just bouncing services"
  systemctl restart sokratic-backend || warn "sokratic-backend not yet installed"
  systemctl reload nginx || warn "nginx not yet configured"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1 — system packages
# ---------------------------------------------------------------------------

if [[ "$SKIP_SYSTEM" == "0" ]]; then
  log "step 1/8: system packages"
  apt-get update -y
  apt-get install -y \
    git curl ca-certificates gnupg \
    "$PY_BIN" "${PY_BIN}-venv" "${PY_BIN}-dev" python3-pip \
    nodejs npm \
    nginx docker.io \
    build-essential
  # docker group for the deploy user (so they can run docker compose
  # without sudo). Effective on next login; the script uses `docker`
  # via root anyway during this run.
  usermod -aG docker "$DEPLOY_USER" || true
else
  log "step 1/8: system packages — SKIPPED"
fi

# ---------------------------------------------------------------------------
# Step 2 — clone / update repo
# ---------------------------------------------------------------------------

log "step 2/8: clone or update $INSTALL_DIR"
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  mkdir -p "$INSTALL_DIR"
  chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$INSTALL_DIR"
  as_user git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
else
  cd "$INSTALL_DIR"
  as_user git fetch --all
  as_user git checkout "$BRANCH"
  as_user git pull --ff-only origin "$BRANCH"
fi
cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Step 3 — python env + deps
# ---------------------------------------------------------------------------

log "step 3/8: python venv + requirements"
if [[ ! -x .venv/bin/python ]]; then
  as_user "$PY_BIN" -m venv .venv
fi
as_user .venv/bin/pip install --upgrade pip wheel
as_user .venv/bin/pip install -r requirements.txt
as_user .venv/bin/pip install -r backend/requirements.txt

# scispaCy biomedical model — required for the OT/anatomy domain.
# First install pulls ~1GB UMLS KB to ~/.scispacy on first .nlp() call;
# pre-warming here avoids a slow first request.
SCISPACY_WHEEL="https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz"
if ! as_user .venv/bin/python -c "import en_core_sci_sm" 2>/dev/null; then
  log "  installing en_core_sci_sm (one-time, ~1GB)"
  as_user .venv/bin/pip install "$SCISPACY_WHEEL"
fi

# ---------------------------------------------------------------------------
# Step 4 — frontend build
# ---------------------------------------------------------------------------

if [[ "$SKIP_FRONTEND" == "0" ]]; then
  log "step 4/8: frontend build (npm ci + npm run build)"
  if [[ ! -d frontend/node_modules ]]; then
    cd "$INSTALL_DIR/frontend"
    as_user npm ci
    cd "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR/frontend"
  as_user npm run build
  cd "$INSTALL_DIR"
  if [[ ! -d frontend/dist ]]; then
    fail "frontend build did not produce frontend/dist"
  fi
else
  log "step 4/8: frontend build — SKIPPED"
fi

# ---------------------------------------------------------------------------
# Step 5 — .env presence check (we DO NOT write secrets)
# ---------------------------------------------------------------------------

log "step 5/8: .env presence check"
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  warn ".env missing at $INSTALL_DIR/.env"
  warn "Place .env (with ANTHROPIC_API_KEY, OPENAI_API_KEY, HF_TOKEN,"
  warn "SOKRATIC_AUTH_USERS, SOKRATIC_AUTH_SECRET) and re-run with"
  warn "  sudo bash scripts/deploy_vm_bootstrap.sh --skip-system --skip-frontend"
  warn "Continuing without .env will fail bootstrap_corpus + smoke."
fi

# ---------------------------------------------------------------------------
# Step 5b — PREFLIGHT GATE
# ---------------------------------------------------------------------------
# Verify prerequisites BEFORE we pull a single byte from HuggingFace
# or restore a qdrant snapshot. Cheap, deterministic, all-or-nothing.

log "step 5b/8: preflight gate"
if ! as_user .venv/bin/python scripts/preflight_check.py; then
  fail "preflight check failed — fix the ✗ items above and re-run. "\
"Common fixes:  (a) populate /opt/sokratic/.env  "\
"(b) install missing system binary  (c) start docker daemon  "\
"(d) free up disk space."
fi

# ---------------------------------------------------------------------------
# Step 6 — pull expensive artifacts from HuggingFace
# ---------------------------------------------------------------------------

if [[ "$SKIP_BOOTSTRAP" == "0" ]]; then
  log "step 6/8: pull corpus artifacts from HuggingFace (idempotent)"
  # bootstrap_corpus.py is sha256-aware and skips files that already match,
  # so re-runs cost nothing on the network. Only pulls files missing or
  # mis-hashed against data/MANIFEST.json.
  as_user .venv/bin/python scripts/bootstrap_corpus.py
else
  log "step 6/8: HF bootstrap — SKIPPED"
fi

# ---------------------------------------------------------------------------
# Step 7 — start qdrant + restore snapshot
# ---------------------------------------------------------------------------

if [[ "$SKIP_QDRANT" == "0" ]]; then
  log "step 7/8: start qdrant + restore snapshot"
  bash scripts/qdrant_up.sh

  # Wait for qdrant readiness
  for i in {1..30}; do
    if curl -fsS "http://localhost:${QDRANT_HTTP_PORT}/collections" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  # Skip restore if collection already populated (idempotency)
  POINTS_COUNT=$(curl -fsS "http://localhost:${QDRANT_HTTP_PORT}/collections/${QDRANT_COLLECTION}" 2>/dev/null \
    | grep -oE '"points_count":[0-9]+' | head -1 | cut -d: -f2 || echo "0")
  POINTS_COUNT="${POINTS_COUNT:-0}"
  if [[ "$POINTS_COUNT" -gt 0 ]]; then
    log "  qdrant collection $QDRANT_COLLECTION already has $POINTS_COUNT points — skip restore"
  else
    if [[ ! -f "$QDRANT_SNAPSHOT" ]]; then
      fail "qdrant snapshot not found at $QDRANT_SNAPSHOT — bootstrap_corpus.py did not pull it"
    fi
    log "  restoring snapshot from $QDRANT_SNAPSHOT"
    curl -fsS -X POST \
      "http://localhost:${QDRANT_HTTP_PORT}/collections/${QDRANT_COLLECTION}/snapshots/upload?priority=snapshot" \
      -H "Content-Type: multipart/form-data" \
      -F "snapshot=@${QDRANT_SNAPSHOT}" >/dev/null
    log "  snapshot restore complete"
  fi
else
  log "step 7/8: qdrant — SKIPPED"
fi

# ---------------------------------------------------------------------------
# Step 7b — POSTFLIGHT GATE
# ---------------------------------------------------------------------------
# Verify the *running* environment (data files sha-correct, qdrant
# populated, retriever can actually retrieve, LLMs reachable, smoke
# harness green). Hard fail before we start uvicorn.

if [[ "$SKIP_SMOKE" == "0" ]]; then
  log "step 7b/8: postflight gate"
  if ! as_user .venv/bin/python scripts/postflight_check.py; then
    fail "postflight check failed — do NOT start the backend until "\
"the ✗ items above are resolved.  Common fixes:  (a) re-run bootstrap "\
"(missing/mismatched files),  (b) restore the qdrant snapshot manually "\
"from data/indexes/qdrant_*.snapshot,  (c) verify .env keys for the "\
"failing LLM provider."
  fi
else
  log "step 7b/8: postflight — SKIPPED"
fi

# ---------------------------------------------------------------------------
# Step 8b — systemd unit + nginx site
# ---------------------------------------------------------------------------

log "step 8/8b: systemd unit + nginx site"

# Backend systemd unit (idempotent)
SYSTEMD_UNIT=/etc/systemd/system/sokratic-backend.service
if [[ ! -f "$SYSTEMD_UNIT" ]]; then
  cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=Sokratic FastAPI backend
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$DEPLOY_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment=HF_HOME=$INSTALL_DIR/.cache/huggingface
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi

# Nginx site (idempotent — only writes if missing)
NGINX_SITE=/etc/nginx/sites-available/sokratic
if [[ ! -f "$NGINX_SITE" ]]; then
  cat > "$NGINX_SITE" <<EOF
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 16M;

    root $INSTALL_DIR/frontend/dist;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 3600s;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF
  ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/sokratic
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
fi

if [[ -f "$INSTALL_DIR/.env" ]]; then
  systemctl enable --now sokratic-backend
  systemctl restart sokratic-backend
  systemctl reload nginx || systemctl restart nginx
  log "backend + nginx are up. health:"
  for i in {1..30}; do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
      log "  ✓ backend healthy"
      break
    fi
    sleep 2
  done
else
  warn "backend NOT started — drop .env and re-run with --restart-only"
fi

log "DONE. Public IP serves the app on :80."
log "Tail backend logs: journalctl -u sokratic-backend -f"
