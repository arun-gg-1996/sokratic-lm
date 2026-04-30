# Sokratic-OT

Sokratic-OT is a Socratic anatomy tutoring system with a Dean–Teacher
architecture, cross-session memory, dimensional quality evaluation, and
gamified gating around student engagement. Built as a CSE 635 NLP thesis
at UB.

---

## Current state (2026-04-30)

System has shipped through five major change rounds. All validated end-to-end
on the 4 gate tests (T1–T4) plus an 18-conversation eval batch (11 students,
6 single-session + 6 paired + 6 triple) covering S1–S6 student profiles.

### Changes shipped

| Round | What | Result |
|---|---|---|
| **Change 1** | `reached_answer_gate` (token-overlap Step A + LLM Step B with verbatim-quote enforcement) — replaces the old `confidence ≥ 0.72` heuristic that produced false positives like "gravity" → "skeletal muscle pump" | T1+T4 regression eliminated; gate path tracked per turn (overlap / paraphrase / hedge_block / no_overlap) |
| **Change 2** | Topic-lock acknowledgement turn — when topic locks, dean emits a deterministic message (chapter → section → subsection + verbatim locked_question) before any hints fire | Student now sees the exact question before scaffolds; hint counter doesn't burn on the ack turn |
| **Change 3** | Tightened `dean_setup_classify` (low-effort detection) + mastery-scorer attribution rules (rationale must quote student utterances, not tutor utterances) | Reduces credit-inflation pattern (the "you correctly identified X" when only the tutor said X) |
| **Change 4** | Counter system: `help_abuse_count` (low-effort) + `off_topic_count` (off-domain) with strike warnings 1–3 + LLM-narrated hint advance at strike 4. Off-topic terminate at strike 4 (sets mastery tiers to `not_assessed`). Plus telemetry counters `total_low_effort_turns` / `total_off_topic_turns` read by mastery scorer. | Gamification: student knows when they're approaching a fail state; no more rewarded stonewalling |
| **Change 5.1** | Clinical-phase counters (`clinical_low_effort_count` / `clinical_off_topic_count`) — cap at strike 2, ends clinical phase only (preserves tutoring credit) | Symmetrical engagement check during clinical assessment |
| **Round 4 fixes** | Eval framework `INVARIANT_VIOLATION` dedup (one per session, not per turn); strict teacher leak prohibition (no letter hints, no mid-component reveals); two-tier locked answer (`locked_answer` 1–5 words for gate + `full_answer` for grading); lock-time section filter | Penalty count dropped 110 → 17; first session ever passing the eval framework's penalty channel |
| **Round 5 fixes** | Teacher prompt: explicit forbidden patterns (anagram leaks, mid-component reveals, "just tell me" caves). Lock prompt: 1–5 word hard cap on `locked_answer` with concrete multi-component examples. Matcher threshold: `STRONG_MIN` 65 → 78 + `STRONG_GAP` 5 → 10 (low-confidence matches now show cards instead of auto-locking). | Targets the qualitative-review issues found in the 18-convo batch |

### Eval framework

Two-tier evaluation system documented in [`docs/EVALUATION_FRAMEWORK.md`](docs/EVALUATION_FRAMEWORK.md):

- **Primary (headline / thesis-reported):** EULER (4 criteria) + RAGAS (context_precision, context_recall, faithfulness, answer_relevancy, answer_correctness)
- **Secondary (diagnostic):** 10 internal dimensions — TLQ, RRQ, AQ, TRQ, RGC, PP, ARC, CC, CE, MSC
- **Penalties (separate channel):** Critical / Major flags

CLI entry point: `scripts/score_conversation_quality.py path/to/session.json`

---

## Architecture

```
conversation/
  ├── state.py        — TutorState TypedDict + initial_state()
  ├── nodes.py        — LangGraph nodes (rapport, dean, assessment, memory_update)
  ├── edges.py        — graph routing logic
  ├── dean.py         — Dean's logic: topic resolution, anchor lock,
  │                     reached_answer_gate, QC, counter system, clinical phase
  ├── teacher.py      — Teacher's draft_socratic / draft_clinical
  └── graph.py        — LangGraph builder
retrieval/
  ├── topic_matcher.py — fuzzy + semantic match against topic_index
  ├── retriever.py     — RRF + cross-encoder
  └── ...
backend/
  ├── api/            — FastAPI routes (session, chat WS, mastery, memory)
  └── dependencies.py
frontend/             — React + Vite UI
evaluation/
  └── quality/        — quality scorer (deterministic + LLM judges + dimensions + penalties + runner)
memory/
  └── mastery_store.py — per-concept BKT-extension knowledge tracking
config/
  ├── base.yaml        — main config + prompts
  └── eval_prompts.yaml — evaluator prompts (4 batched LLM calls)
docs/
  ├── EVALUATION_FRAMEWORK.md — full eval framework spec
  ├── HANDOFF_NIDHI.md        — Phase 3 task plan for Nidhi
  ├── SESSION_JOURNAL_2026-04-30.md — decision log
  └── architecture.md         — full pipeline diagram (this round)
scripts/
  ├── test_reached_gate_e2e.py   — gate tests (T1-T13)
  ├── run_eval_18_convos.py      — 18-conversation eval batch
  ├── score_conversation_quality.py — eval scorer CLI
  └── ...
```

---

## Conversation flow

```
rapport (greeting + mem0 read)
  ↓
topic resolution (free-text or pre-locked) [Change 2: ack turn here]
  ↓
tutoring loop:
   ├── dean classify (student_state)
   ├── reached_answer_gate (Step A overlap + Step B LLM)  [Change 1]
   ├── counter updates (help_abuse / off_topic / telemetry)  [Change 4]
   ├── strike warnings 1-3 / hint advance at 4 / terminate at off_topic=4
   ├── teacher draft (Socratic, leak-prohibited)
   └── dean QC + retry
  ↓
assessment (clinical opt-in + multi-turn clinical scenario)  [Change 5.1: clinical counters]
  ↓
memory_update (mem0 write + per-concept mastery store)
```

---

## First-time setup (clean clone)

End-to-end setup is six steps. Total time ~15 min, ~$0.10 in OpenAI
embeddings if you choose to rebuild the Qdrant index instead of pulling a
pre-built snapshot.

### 1. Clone + Python environment

```bash
git clone https://github.com/arun-gg-1996/sokratic-lm.git sokratic
cd sokratic

python3.11 -m venv .venv          # any 3.10+ works; 3.11 is the lockfile target
source .venv/bin/activate
pip install -r requirements.txt   # core (LLM, retrieval, ingestion, eval)
pip install -r backend/requirements.txt   # FastAPI/WebSocket layer
```

### 2. `.env` — secrets and domain

```bash
cp .env.example .env
# then edit .env and fill in:
#   ANTHROPIC_API_KEY  (required, runtime)
#   OPENAI_API_KEY     (required only for index rebuild)
#   SOKRATIC_DOMAIN    (default "ot" is fine)
#   HF_USERNAME        (default "arun-ghontale" for the public corpus repo)
#   HF_TOKEN           (leave blank — corpus is public)
```

Full env-var reference and optional flags (LangSmith, Qdrant overrides, Vite
API base) are in `.env.example`.

### 3. Pull the processed corpus from HuggingFace

The chunks, BM25 index, topic_index, textbook_structure, and Qdrant snapshot
are hosted at [`arun-ghontale/sokratic-anatomy-corpus`](https://huggingface.co/datasets/arun-ghontale/sokratic-anatomy-corpus).
A single command pulls everything into `data/processed/`, `data/indexes/`,
and `data/`:

```bash
.venv/bin/python scripts/bootstrap_corpus.py
```

This is sha256-verified and idempotent (re-run is a no-op when files match).
Pin a release tag with `--version v1-chunks-only` if you want a frozen state.
The script bootstraps:

| Path | Source | Why |
|---|---|---|
| `data/processed/chunks_openstax_anatomy.jsonl` (~11 MB) | HF | Body of the corpus, chunked at the section level |
| `data/indexes/bm25_chunks_openstax_anatomy.pkl` (~17 MB) | HF | Sparse retrieval index over chunks |
| `data/indexes/qdrant_sokratic_kb_chunks.snapshot` (~600 MB) | HF | Pre-built dense index — restore in step 4 to skip the rebuild |
| `data/textbook_structure.json` | HF | TOC tree (chapter → section → subsection) |
| `data/topic_index.json` | HF | Lock-time TOC matcher input (~360 entries) |
| `data/eval/rag_qa.jsonl`, `rag_qa_edge_cases.jsonl` | git | RAG eval set (already in repo) |

**Note.** The previous proposition-based artifacts (`chunks_ot.jsonl`,
`propositions_ot.jsonl`, `bm25_ot.pkl`, etc.) are deprecated and not
republished. End-to-end testing showed propositions atomize the relational
verbs that should be the discriminative retrieval signal on biomedical
corpora — see `scripts/reindex_chunks.py` docstring for the full rationale.

**Source PDF.** Not redistributed — grab OpenStax *Anatomy & Physiology 2e*
[from openstax.org](https://openstax.org/details/books/anatomy-and-physiology-2e)
into `data/raw/` only if you need to re-run extraction. For everything else
the processed chunks JSONL is sufficient.

### 4. Start Qdrant + populate the dense index

Qdrant runs locally via Docker (port 6333). The repo ships a helper:

```bash
scripts/qdrant_up.sh           # starts qdrant/qdrant:v1.13.4 in Docker
curl http://localhost:6333/healthz   # sanity check
```

The runtime retriever queries the `sokratic_kb_chunks` collection (built
from `chunks_openstax_anatomy.jsonl`). Two paths to populate it:

**Path A — restore the pre-built snapshot (preferred, zero LLM cost).**
`bootstrap_corpus.py` already downloaded the snapshot to
`data/indexes/qdrant_sokratic_kb_chunks.snapshot`. Restore via the Qdrant
HTTP API:

```bash
SNAP=data/indexes/qdrant_sokratic_kb_chunks.snapshot
curl -X POST "http://localhost:6333/collections/sokratic_kb_chunks/snapshots/upload?priority=snapshot" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@${SNAP}"
# verify (~7,500 points expected)
curl -s http://localhost:6333/collections/sokratic_kb_chunks | python3 -m json.tool | grep points_count
```

**Path B — rebuild from chunks (~$0.10 OpenAI cost, ~10 min).** Use this if
the snapshot isn't on HF yet or you've regenerated the chunks:

```bash
.venv/bin/python scripts/reindex_chunks.py --collection sokratic_kb_chunks --fresh
```

> **Heads-up:** `config/domains/ot.yaml` currently sets
> `kb_collection: "sokratic_kb"` (the deprecated propositions collection).
> If your runtime retrieval returns nothing after restore, change that line
> to `kb_collection: "sokratic_kb_chunks"`. (Tracked as a punch-list item in
> `docs/HANDOFF_NIDHI.md` — should be flipped before this handoff goes out.)

### 5. Run the backend (FastAPI + WebSocket)

```bash
uvicorn backend.main:app --reload --port 8000
# WS endpoint: ws://localhost:8000/api/chat/ws
# health:      curl http://localhost:8000/api/health
```

### 6. Run the frontend (React + Vite)

```bash
cd frontend
npm install            # or pnpm install
npm run dev            # serves on http://localhost:5173
# open http://localhost:5173
```

The Vite dev server proxies API calls to `http://localhost:8000` by default.
Override via `VITE_API_BASE` in `.env` if the backend is elsewhere.

---

## Running tests + eval

```bash
# Smoke test the gate logic (T1–T12 scenarios)
.venv/bin/python scripts/test_reached_gate_e2e.py T1_wrong_answer
.venv/bin/python scripts/test_reached_gate_e2e.py        # run all

# Run the 18-conversation eval batch (~13 min, ~$8-9 LLM cost)
.venv/bin/python scripts/run_eval_18_convos.py

# Score a single saved session (deterministic + LLM judges)
.venv/bin/python scripts/score_conversation_quality.py path/to/session.json
.venv/bin/python scripts/score_conversation_quality.py path/to/session.json --no-llm     # free, deterministic only

# Score every JSON in a directory (used by the v3 batch loop)
for f in data/artifacts/eval_run_18/eval18_*.json; do
  .venv/bin/python scripts/score_conversation_quality.py "$f" \
    -o "data/artifacts/eval_run_18/scored/$(basename "$f" .json)_eval.json" --quiet
done
```

Eval scorer output schema is in `docs/EVALUATION_FRAMEWORK.md` §6.

---

## Updating the published corpus (HuggingFace)

If you regenerate chunks / propositions / BM25 / topic_index, re-publish so
teammates can pull the new state with one command:

```bash
.venv/bin/python scripts/publish_corpus.py --dry-run                   # preview
.venv/bin/python scripts/publish_corpus.py --tag vN-<descriptor>       # publish + tag
```

Requires `HF_TOKEN` with write access to the dataset repo. The script is
sha256-idempotent — only files whose content changed are re-uploaded.

---

## Retrieval stack

1. Qdrant dense search (top-K = 20)
2. BM25 sparse search (top-K = 20)
3. RRF merge
4. Parent chunk expansion (window = surrounding sentences)
5. Cross-encoder rerank → top-7 chunks
6. Lock-time section filtering (Round 4 fix): chunks filtered to `locked_topic.subsection` before passing to lock LLM

### Retrieval baseline (`data/eval/eval_results_2026_04_17.json`)
- Hit@1: 0.57 / Hit@3: 0.64 / Hit@7: 0.71 / MRR: 0.616
- vs baseline: Hit@5 0.52 / MRR 0.493

---

## Recent eval batch results (2026-04-30, 18 conversations)

| Metric | v1 (pre 4-fix round) | v2 (post 4-fix round) | Δ |
|---|---|---|---|
| INVARIANT_VIOLATION events | 110 | **17** | **-93** |
| Sessions with `failed_critical_penalty` | 18/18 | 17/18 | -1 |
| RGC dim mean | 0.759 | **0.852** | **+0.093** |
| ARC dim mean | 0.914 | **0.953** | **+0.039** |
| Reach rate | 13/18 | 9/18 | -4 (good regression: leak-then-parrot pattern eliminated) |
| LEAK_DETECTED | 10 | 15 | +5 (new patterns surfaced; Round 5 prompt addresses them) |
| Total cost | $9.24 | ~$8.50 | similar |

Full reports:
- [`data/artifacts/eval_run_18/REPORT_v1_vs_v2.md`](data/artifacts/eval_run_18/REPORT_v1_vs_v2.md) — quantitative comparison
- [`data/artifacts/eval_run_18/REPORT_qualitative_v2.md`](data/artifacts/eval_run_18/REPORT_qualitative_v2.md) — read-each-dialog qualitative review

---

## Open issues / known limitations

1. **Topic-resolution drift** (~5/18 in v2): student asks about long bone structure, system locks on muscle types. Round 5 matcher threshold bump targets this — pending validation in Nidhi's batch.
2. **Two-tier locked answer not always activating**: lock LLM sometimes silently uses `full_answer == locked_answer`. Round 5 prompt strengthening may help; alternative is splitting into a separate LLM call.
3. **Coverage gate over-strictness on broad topics**: "walk me through chemical digestion of carbohydrates" got rejected with random alternatives. Threshold needs tuning.
4. **EULER `helpful` 0.61** — Socratic drafts not always advancing reasoning. The deferred dimensional-hints work (Change 6) targets this.
5. **CE cost target $0.20/turn** — too tight; should be $0.50/turn with note about long-session amortization.

---

## Handoff plan

- **Phase 3 (Nidhi, Sat–Sun)**: 10 manual conversations + 50–60 simulation conversations + Sonnet/Haiku A/B + secondary textbook integration + vision flow. See [`docs/HANDOFF_NIDHI.md`](docs/HANDOFF_NIDHI.md).

---

## Documentation

| Doc | Purpose |
|---|---|
| [`README.md`](README.md) | This file (system overview) |
| [`docs/EVALUATION_FRAMEWORK.md`](docs/EVALUATION_FRAMEWORK.md) | Eval framework spec (primary + secondary metrics) |
| [`docs/HANDOFF_NIDHI.md`](docs/HANDOFF_NIDHI.md) | Phase 3 handoff to Nidhi |
| [`docs/SESSION_JOURNAL_2026-04-30.md`](docs/SESSION_JOURNAL_2026-04-30.md) | Decision log for the 2026-04-30 work session |
| [`docs/architecture.md`](docs/architecture.md) | Full pipeline diagram + per-component spec |
| [`docs/FINAL_EXECUTION_PLAN_2026-04-19.md`](docs/FINAL_EXECUTION_PLAN_2026-04-19.md) | Original execution plan |
| [`docs/hardcoded_audit.md`](docs/hardcoded_audit.md) | Audit of deterministic logic in the flow |

---

## Important notes

- Persistent memory requires Qdrant + mem0 (auto-degraded if unavailable; sessions still complete but no cross-session carryover)
- The mastery store is a per-student JSON at `data/student_state/{student_id}.json` — survives restart
- Session exports via `/api/session/{thread_id}/export` produce a full TutorState dump compatible with the eval scorer
