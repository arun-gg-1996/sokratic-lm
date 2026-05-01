# Setup notes — clean-clone walkthrough (Nidhi, 2026-04-30)

Captures every deviation from `README.md §First-time setup` that was needed to
get a fresh clone running on macOS (Darwin 24.6, Python 3.11.9, Docker 28.5,
Node 25.8). Companion to `docs/HANDOFF_NIDHI.md` — that handoff doc still
listed "verify the clean-clone walkthrough" as an outstanding owner-Arun
punch-list item; this document is the result of that verification.

All five issues below are real bugs in the published repo as of commit
3e0d73da on `main`. Each one was patched locally to bring the stack up;
they need to be upstreamed before the next handoff.

---

## 1. `requirements.txt` — internal version conflict on `llama-index`

**Symptom.** `pip install -r requirements.txt` fails with `ResolutionImpossible`:

```
llama-index 0.12.* depends on llama-index-core<0.13 and >=0.12.*
…but requirements.txt also pins llama-index-core>=0.14.21
```

The two lines as published cannot coexist:

```
llama-index>=0.12.34
llama-index-core>=0.14.21
```

**Why.** llama-index reorganised its umbrella package between 0.12 and 0.14;
the `llama-index>=0.12` floor pulls a 0.12.x version which transitively
requires `llama-index-core<0.13`, contradicting the explicit `core>=0.14.21`
pin.

**Fix applied.** Bump the umbrella floor to match core:

```diff
-llama-index>=0.12.34
+llama-index>=0.14.21
```

**Cascading second conflict.** Bumping llama-index pulled in
`llama-index-llms-openai 0.7.x`, which requires `openai>=1.108.1`. But
`requirements.txt` pinned `openai==1.76.0`. Loosened that too:

```diff
-openai==1.76.0
+openai>=1.108.1
```

llama-index is only used in `ingestion/core/chunker.py` (rebuild path) — it
doesn't run at runtime. So the version bump is safe for serving but should
be regression-tested if anyone re-runs the chunker.

**Bonus pip note.** Latest pip (26.1) hit `resolution-too-deep` on this
graph even after the version fix; downgrading to `pip<25` (24.3.1) resolved
it. Worth either pinning pip in the venv bootstrap or adding tighter lower
bounds throughout `requirements.txt`.

---

## 2. `config/base.yaml` — stale BM25 filename after chunks-only flip

**Symptom.** Smoke test fails immediately on `Retriever()` construction:

```
FileNotFoundError: 'data/indexes/bm25_openstax_anatomy.pkl'
```

**Why.** The HuggingFace dataset (tag `v1-chunks-only`) publishes the
chunks-era BM25 index as `bm25_chunks_openstax_anatomy.pkl`. `config/base.yaml`
still points to the pre-chunks name:

```yaml
paths:
  bm25_openstax_anatomy: "data/indexes/bm25_openstax_anatomy.pkl"   # stale
```

**Fix applied.**

```diff
-  bm25_openstax_anatomy: "data/indexes/bm25_openstax_anatomy.pkl"
+  bm25_openstax_anatomy: "data/indexes/bm25_chunks_openstax_anatomy.pkl"
```

This is the same chunks-only flip Arun's punch-list noted he had done for
`kb_collection`; the BM25 path was missed.

---

## 3. **(critical, silent)** `backend/dependencies.py` instantiates the legacy `Retriever`, not `ChunkRetriever`

**Symptom.** Backend boots cleanly, frontend connects, but every retrieval
call returns 0 chunks. No error in the logs — the retriever's diagnostics
just show:

```
n_qdrant_orig: 20    # raw hits found
n_bm25: 20           # raw hits found
n_parent_chunks: 0   # ← all dropped here
n_returned: 0
```

This means the system would accept conversations and silently produce
ungrounded responses on every turn. The eval framework would log every
session as broken. None of this surfaces as an error.

**Why.** `retrieval/retriever.py` defines two classes:

- `Retriever` — propositions-era code path. Reads `parent_chunk_id` from
  Qdrant payloads (which doesn't exist on chunks-only payloads).
- `ChunkRetriever(Retriever)` — chunks-only override. Synthesises
  `parent_chunk_id = chunk_id` (chunks ARE the parents).

Most of the codebase still imports `Retriever` directly:

```
backend/dependencies.py:11    from retrieval.retriever import Retriever
backend/dependencies.py:41    return Retriever()
ui/app.py:23, simulation/runner.py, scripts/sim_repl.py, …
```

Only `scripts/run_multi_session_test.py` uses `ChunkRetriever()`.

**Fix applied (minimum viable — backend only).**

```diff
-from retrieval.retriever import Retriever
+from retrieval.retriever import ChunkRetriever
…
-    return Retriever()
+    return ChunkRetriever()
```

**Recommended cleanup.** Either:
1. Have `Retriever()` dispatch to `ChunkRetriever` when
   `cfg.memory.kb_collection` ends in `_chunks`. Single source of truth.
2. Delete the legacy `Retriever` propositions code path entirely (the
   handoff doc records that propositions are deprecated and not coming back).

Option 2 is the cleaner story for the thesis — the propositions code is
dead weight that survived the chunks-only migration. Worth ~half a day to
strip out. Note that several scripts (`scripts/eval_rag_v2.py`,
`scripts/diagnose_misses.py`, `scripts/validate_topic_index.py`,
`run_tests_2026_04_17.py`) all instantiate `Retriever()` directly — they'd
break under option 2 and need the same swap.

---

## 4. Qdrant snapshot restore — RocksDB version mismatch

**Symptom.** README §4 Path A (snapshot restore) fails:

```
$ curl -X POST http://localhost:6333/collections/sokratic_kb_chunks/snapshots/upload?priority=snapshot \
    -F snapshot=@data/indexes/qdrant_sokratic_kb_chunks.snapshot
{"status":{"error":"Service internal error: Failed to restore snapshot…
  Service runtime error: failed to restore RocksDB backup: NotFound: Backup not found"}}
```

The snapshot file is a valid POSIX tar (378 MB) and Qdrant 1.13.4 boots
fine — the failure is on the snapshot's internal RocksDB format, almost
certainly because the snapshot was generated with a Qdrant version newer
than the pinned `qdrant/qdrant:v1.13.4` image.

**Workaround applied.** Used Path B (rebuild from chunks):

```bash
.venv/bin/python scripts/reindex_chunks.py --collection sokratic_kb_chunks --fresh
```

~10 min, ~$0.10 OpenAI cost (text-embedding-3-large × 7,574 chunks). Result:
collection green with 7,574 points, vector size 3072. Smoke test passes.

**To fix upstream (one of):**
1. Re-snapshot from the same Qdrant version pinned in `scripts/qdrant_up.sh`
   (currently `v1.13.4`) and re-publish to HF.
2. Bump the pinned image to whatever version generated the current snapshot.
3. Drop Path A from the README and document Path B as the only path
   (~$0.10 / 10 min is a small price for a reproducible rebuild).

---

## 5. **(critical, silent at boot)** `backend/main.py` never loads `.env`

**Symptom.** Backend boots, `/health` returns `{"status":"ok"}`, frontend
connects, user clicks "New chat" — backend throws on the first
`graph.invoke(state)` inside `start_session`:

```
TypeError: Could not resolve authentication method. Expected either
api_key or auth_token to be set. Or for one of the `X-Api-Key` or
`Authorization` headers to be explicitly omitted
```

The Anthropic SDK can't find `ANTHROPIC_API_KEY` in the environment, so
the very first LLM call (rapport_node → teacher.draft_rapport) fails. The
frontend surfaces this as *"Unable to start session. Please retry."*

**Why.** `ui/app.py` (Streamlit), `conftest.py`, and several ingestion
scripts all call `load_dotenv(...)` explicitly. The FastAPI entrypoint at
`backend/main.py` does not. uvicorn does not auto-load `.env` either.
Result: env vars set in `.env` reach the test harness and the Streamlit UI
but never the production backend.

**Fix applied.** Add `load_dotenv` at the top of `backend/main.py`, before
any module imports that construct LLM clients:

```python
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from backend.api import chat, mastery, memory, session, users
from backend.dependencies import get_graph, get_memory_manager, get_retriever
```

**`override=True` is required**, not optional. On macOS dev machines it's
common to have `ANTHROPIC_API_KEY=""` (empty string) exported from a shell
profile (`~/.zshrc`, `~/.bash_profile`, or a tool like `direnv` /
`mise` / `asdf` shimming the var with no value). `load_dotenv` defaults to
not overwriting existing env vars, so the empty shell value wins and the
key in `.env` is silently ignored. With `override=True` the `.env` value
takes precedence — same behavior `ingestion/run.py:51` uses.

The `Path(__file__).parent.parent / ".env"` pattern matches what
`ui/app.py` already does — anchors to repo root regardless of where
uvicorn is invoked from.

**Why this is a worse class of bug than #3.** Like the legacy `Retriever`
issue, this fails *after* boot succeeds, so logs/health checks are clean
and the failure only shows in the user-facing UI. Unlike #3, the error
message ("Could not resolve authentication method") is at least
diagnosable. Both should land in the same upstream PR — they're
sibling silent-failure modes that any clean-clone walkthrough would hit.

---

## 6. README — wrong health endpoint

**Symptom.** README §Smoke test:

```bash
curl http://localhost:8000/api/health
# expect: {"status":"ok"}
```

returns 404. Actual route is `/health` (no `/api` prefix):

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

**Fix.** Trivial doc update.

---

## Optional / non-blocking warnings observed

- **scispacy `en_core_sci_sm` model not installed.** UMLSAdapter degrades
  to noop and logs a warning. The README and `requirements.txt` mention
  installing it from the scispacy S3 wheel separately, but it's not in the
  default install. Without it, biomedical NER doesn't fire — degraded
  retrieval expansion but still functional. Install if doing thesis-quality
  evals:
  ```
  pip install https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz
  ```
- **qdrant-client 1.17.1 vs server 1.13.4 minor-version mismatch warning.**
  Cosmetic; everything works. Either pin the client to 1.13.x or bump the
  server image.

---

## Verification command (post-fix)

After the five fixes above, this end-to-end smoke test passes:

```bash
.venv/bin/python -c "
from retrieval.retriever import ChunkRetriever
r = ChunkRetriever()
hits = r.retrieve('What is the rotator cuff?', domain='openstax_anatomy')
print(f'retrieved {len(hits)} chunks, top: {hits[0][\"chapter_title\"]} / {hits[0][\"section_title\"]}')
"
# retrieved 11 chunks, top: Joints / Anatomy of Selected Synovial Joints
```

Backend at http://localhost:8000/health → `{"status":"ok"}`.
Frontend at http://localhost:5173 → tutor UI loads.
