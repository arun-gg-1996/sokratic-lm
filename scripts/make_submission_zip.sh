#!/usr/bin/env bash
# scripts/make_submission_zip.sh
# -------------------------------
# Build a clean submission zip with code only — no .env, no data, no
# build outputs, no journal artifacts, no extra markdown.
#
# Run from the repo root:
#   bash scripts/make_submission_zip.sh
#
# Output:
#   sokratic_submission.zip   (in the project root)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT="${SOKRATIC_ZIP_OUT:-$ROOT/sokratic_submission.zip}"
STAGE="$(mktemp -d)"
PKG="$STAGE/sokratic"
mkdir -p "$PKG"

echo "[zip] staging at $STAGE"

# ---------------------------------------------------------------------------
# What goes in the zip — explicit allowlist of dirs / files.
# ---------------------------------------------------------------------------

# Source dirs copied wholesale (filter step below removes junk).
SOURCE_DIRS=(
  backend
  conversation
  memory
  retrieval
  ingestion
  evaluation
  tools
  config
  tests
)

# Frontend: only src + config files (no node_modules, no dist).
FRONTEND_KEEP=(
  frontend/src
  frontend/index.html
  frontend/package.json
  frontend/package-lock.json
  frontend/vite.config.ts
  frontend/tailwind.config.js
  frontend/postcss.config.js
  frontend/tsconfig.json
  frontend/tsconfig.app.json
  frontend/tsconfig.node.json
)

# Scripts: explicit allowlist (skip experimental / sweep helpers).
SCRIPTS_KEEP=(
  scripts/bootstrap_corpus.py
  scripts/publish_corpus.py
  scripts/qdrant_up.sh
  scripts/qdrant_down.sh
  scripts/reindex_chunks.py
  scripts/preflight_check.py
  scripts/postflight_check.py
  scripts/deploy_vm_bootstrap.sh
)

# Top-level files.
TOP_FILES=(
  README.md
  requirements.txt
  pytest.ini
  config.py
  .env.example
  .gitignore
  data/MANIFEST.json
)

# ---------------------------------------------------------------------------
# Copy source directories with rsync, filtering known junk.
# ---------------------------------------------------------------------------

RSYNC_FILTER=(
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude='*.pyo'
  --exclude='.pytest_cache'
  --exclude='.mypy_cache'
  --exclude='.ruff_cache'
  --exclude='node_modules'
  --exclude='dist'
  --exclude='.DS_Store'
  --exclude='*.bak'
  --exclude='*.pre_*.bak'
  --exclude='*.swp'
  --exclude='.cache'
)

for d in "${SOURCE_DIRS[@]}"; do
  if [[ -d "$d" ]]; then
    mkdir -p "$PKG/$d"
    rsync -a "${RSYNC_FILTER[@]}" "$d/" "$PKG/$d/"
    echo "[zip]  + $d/"
  fi
done

# Frontend: copy only the listed files / dirs.
mkdir -p "$PKG/frontend"
for p in "${FRONTEND_KEEP[@]}"; do
  if [[ -e "$p" ]]; then
    if [[ -d "$p" ]]; then
      mkdir -p "$PKG/$p"
      rsync -a "${RSYNC_FILTER[@]}" "$p/" "$PKG/$p/"
    else
      mkdir -p "$PKG/$(dirname "$p")"
      cp -p "$p" "$PKG/$p"
    fi
    echo "[zip]  + $p"
  fi
done

# Scripts: copy each named script.
mkdir -p "$PKG/scripts"
for f in "${SCRIPTS_KEEP[@]}"; do
  if [[ -f "$f" ]]; then
    cp -p "$f" "$PKG/$f"
    echo "[zip]  + $f"
  fi
done

# Top-level files.
for f in "${TOP_FILES[@]}"; do
  if [[ -e "$f" ]]; then
    mkdir -p "$PKG/$(dirname "$f")"
    cp -p "$f" "$PKG/$f"
    echo "[zip]  + $f"
  fi
done

# Backend has its own requirements.txt that the deploy script reads.
if [[ -f backend/requirements.txt ]]; then
  cp -p backend/requirements.txt "$PKG/backend/requirements.txt"
fi

# ---------------------------------------------------------------------------
# Defensive removals — if anything sneaked in via rsync, strip it now.
# ---------------------------------------------------------------------------

find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$PKG" -name '*.pyc' -delete
find "$PKG" -name '.DS_Store' -delete
find "$PKG" -name '*.bak' -delete
find "$PKG" -name '.env' -not -name '.env.example' -delete

# Belt-and-braces: only the top-level README.md survives. Any other
# markdown (backend/README.md, sub-package docs, etc.) is dropped to
# match the "single README" submission spec.
find "$PKG" -mindepth 2 -type f -name '*.md' -delete
find "$PKG" -maxdepth 1 -type f -name '*.md' ! -name 'README.md' -delete

# Drop the data/ tree except MANIFEST.json (already explicitly added).
find "$PKG/data" -type f ! -name 'MANIFEST.json' -delete 2>/dev/null || true
find "$PKG/data" -type d -empty -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# Sanity report — list what's in the package.
# ---------------------------------------------------------------------------

echo
echo "[zip] package contents (top level):"
ls -la "$PKG" | head -30
echo
echo "[zip] file count by top-level dir:"
for d in "$PKG"/*/; do
  n=$(find "$d" -type f | wc -l)
  printf "        %5s files  %s\n" "$n" "$(basename "$d")"
done
echo
echo "[zip] sensitive-file scan (should be empty):"
find "$PKG" \( -name '.env' -o -name '*credentials*' -o -name '*service-account*.json' \
              -o -name '*.csv' -o -name '*.snapshot' -o -name '*.db' \) -print
echo

# ---------------------------------------------------------------------------
# Build the zip.
# ---------------------------------------------------------------------------

rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" sokratic )

SIZE=$(du -h "$OUT" | cut -f1)
NFILES=$(unzip -l "$OUT" | tail -1 | awk '{print $2}')
echo "[zip] DONE: $OUT  ($SIZE, $NFILES files)"

# Cleanup stage dir.
rm -rf "$STAGE"
