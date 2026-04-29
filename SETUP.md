# Sokratic — Teammate Setup

One-command bootstrap for getting a working dev environment with all processed
corpus artifacts (chunks, propositions, BM25 index, topic index). The source
textbook PDF is not shipped; grab it separately if you need to re-ingest.

## 1. Prerequisites

- Python 3.10+
- Docker (for local Qdrant)
- A checkout of this repo

## 2. Install Python deps

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Start local Qdrant

```bash
docker run -d -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant
```

Verify: `curl http://localhost:6333/healthz`.

## 4. Download processed corpus from HuggingFace

```bash
python scripts/bootstrap_corpus.py
```

This pulls the current dataset revision from
[`arun-ghontale/sokratic-anatomy-corpus`](https://huggingface.co/datasets/arun-ghontale/sokratic-anatomy-corpus)
and places files in the exact local paths the code expects. Files already
present with the correct sha256 are skipped — safe to re-run.

To pin a specific version:

```bash
python scripts/bootstrap_corpus.py --version v0-messy-metadata
```

**Current tag**: `v0-messy-metadata` — OpenStax A&P ingestion with the known
metadata-mashing bug (`section_title = "7.2 The Skull — Cranial Fossae"`).
Sections are mashed with numbers and em-dashes, so the section-filtered
retrieval currently returns zero chunks. This is documented; re-ingestion is
planned.

## 5. Source textbook (optional, only if re-ingesting)

```bash
mkdir -p data/raw
# Download OpenStax A&P 2e PDF manually from https://openstax.org
# and place it at: data/raw/Anatomy_and_Physiology_2e_-_WEB_c9nD9QL.pdf
```

## 6. Qdrant population

Until we publish the Qdrant snapshot (planned after re-ingestion), you need to
re-index into your local Qdrant. If the BM25 pickle is present and the
propositions JSONL is present:

```bash
python -m ingestion.index
```

This reads `data/processed/propositions_ot.jsonl`, embeds with OpenAI
`text-embedding-3-large`, and upserts into local Qdrant. Requires
`OPENAI_API_KEY` in `.env`.

Estimated cost: ~$5 embedding + a few minutes wall.

(After re-ingestion, a Qdrant snapshot will be published with the dataset and
this step will be replaced by a `scripts/bootstrap_corpus.py --restore-qdrant`
flag.)

## 7. Env vars

Create `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
HF_USERNAME=arun-ghontale
HF_TOKEN=hf_...           # optional; only needed for private repos or publish
```

## 8. Run the tutor

```bash
.venv/bin/python -m streamlit run ui/app.py
```

Or for a programmatic run:

```bash
python scripts/run_final_convos.py
```

## Troubleshooting

- **Bootstrap says `failed`**: check internet connection and HF token (public repo works without a token).
- **Qdrant returns 0 chunks for a known topic**: this is the v0 metadata-mashing bug. Re-ingest locally with `python -m ingestion.index` after fixes land.
- **`OPENAI_API_KEY` missing**: embeddings need it; set in `.env`.

## Publishing (maintainers only)

```bash
python scripts/publish_corpus.py --tag vX-description
```

Pushes changed files to HF and creates a revision tag. Uses `HF_TOKEN` from `.env`.
