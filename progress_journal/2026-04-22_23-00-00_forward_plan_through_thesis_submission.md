# Forward Plan — HF Corpus Plumbing → Re-ingest → Adaptive + KT → Sim → Thesis

Date: 2026-04-22 (continues from `2026-04-22_01-30-00_p1_p4_umls_and_observability.md`)

This is a forward-looking plan. Every item below is a discrete deliverable. Order matters: some unblock others.

---

## Phase A — HuggingFace corpus plumbing (now, ~1 session)

**Goal:** teammate can clone the repo, run one command, and get a working environment with all expensive-to-regenerate artifacts (chunks, propositions, BM25 index, topic index, Qdrant snapshot once available).

**A.1 Decide on HF repo**
- Account: `arun-ghontale`
- Repo: `arun-ghontale/sokratic-anatomy-corpus` (HF dataset, public)
- Token: `HF_TOKEN` read from `.env`

**A.2 Write `scripts/publish_corpus.py`**
- Creates the HF dataset repo if missing (idempotent).
- Skips copyrighted source (`data/raw/*.pdf`), secrets, Claude Code state, ephemeral artifacts.
- Attaches a manifest: file list, sizes, sha256, `prompt_version`, `ingested_at`, git commit.
- Supports `--manifest-only` (plumbing test) and `--dry-run` (preview only).
- Default: skip any file already present at the same sha256 on the remote (cheap re-runs).

**A.3 Write `scripts/bootstrap_corpus.py`**
- Teammate-facing. Reads manifest from HF, downloads missing/mismatched files into their correct local paths.
- Supports `--target-dir` (safe test into /tmp), `--force` (always re-download), `--version <tag>` (pin to a revision).
- Handles the two tracks separately: plain files (JSONL, pickle, JSON) and Qdrant snapshot restore (when present).

**A.4 Plumbing test (safe, no real data touched)**
1. Publish: `python scripts/publish_corpus.py --manifest-only` — pushes just a MANIFEST to prove auth works.
2. Bootstrap into scratch: `python scripts/bootstrap_corpus.py --target-dir /tmp/plumb_test --manifest-only` — downloads MANIFEST only.
3. Verify round-trip, clean up `/tmp/plumb_test`.

**A.5 Real publish (current messy-metadata v0)**
- Pushes `data/processed/*.jsonl`, `data/indexes/bm25_ot.pkl`, `data/textbook_structure.json`, `data/topic_index.json`.
- **Skips** Qdrant snapshot for now (the Qdrant payload is the mashed-metadata one we're about to throw away; not worth snapshotting).
- Tags as `v0-messy-metadata` for historical reference.

**A.6 `SETUP.md`**
- One page. Teammate steps: `pip install -r requirements.txt` → start local Qdrant via Docker → `python scripts/bootstrap_corpus.py` → run tutor.

**Exit criteria:** teammate runs bootstrap on a fresh checkout and the tutor starts (even if topic-lock fails due to the known metadata bug — that's expected for v0).

---

## Phase B — Full pipeline rebuild from PDF (revised 2026-04-28)

**Decision reversed (2026-04-28): WE ARE re-chunking from the PDF.**
Multiple deeper audits surfaced structural chunk problems that scripts on top of existing data can't cleanly fix:
- 9.4% of unique chunks end mid-sentence; 64% of overlap chunks end mid-sentence (token-cap not sentence-cap).
- ~50-100 back-matter index/glossary chunks polluting Ch 28.
- 64 Claude-converted tables and 685 figure_captions sitting orphaned in `raw_elements_ot.jsonl` because chunker reads `raw_sections_ot.jsonl` instead.
- 82% of TOC subsections (~600) never assigned as `subsection_title`.
- `section_title` mashed (regex-fixable but easier to re-emit cleanly).

User decision: don't patch existing artifacts; re-run the whole pipeline cleanly from PDF. Single coherent provenance for thesis. Once-and-done architecture refactor that supports book 2 in Phase C without re-deriving design choices.

**Architectural commitment**: split ingestion into `core/` (reusable across textbooks) and `sources/<book>/` (textbook-specific). Adding a new book in Phase C means writing only `sources/<book>/{parse.py, filters.py, prompt_overrides.py, config.yaml}`.

**Model decisions (2026-04-28)**:
- Propositions: **Sonnet 4.5 (latest)**, dual-task (clean text + extract propositions in one call), async parallel + cached system prompt. ~$22-28 for full corpus.
- Subsection summaries: **DEFERRED** to Phase D pending eval evidence. Generate only if Phase E shows context-augmentation actually helps. v1 ships with structural metadata that supports them as a forward hook.

**Window-navigation metadata in v1**: at index time we populate `prev_chunk_id`, `next_chunk_id`, `sequence_index`, `subsection_id`, `subsection_chunk_count` so the retriever can do W=1 or W=2 window expansion. Default `window=1` (1 before + 1 after = 3 chunks per retrieved chunk). Phase E A/B-tests the window size.

**Running cost meter**: paid steps emit per-call billing readouts (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`) and an accumulated total so we can abort if cost trends high.

### Target architecture

```
ingestion/
├── core/                              # Reusable across textbooks
│   ├── chunker.py                     # Sentence-aware splitting; sentence-aware overlap
│   ├── propositions.py                # Dual-task LLM call (generic prompt body)
│   ├── embed.py                       # OpenAI embedding wrapper
│   ├── bm25.py                        # BM25 index build
│   ├── qdrant.py                      # Schema + upsert; window-nav metadata
│   ├── pipeline.py                    # Orchestrator: parses CLI, loads source, runs stages
│   └── cost_tracker.py                # Per-call billing accumulator
│
├── sources/                           # One module per textbook
│   ├── openstax_anatomy/
│   │   ├── parse.py                   # PDF parsing for OpenStax layout (PyMuPDF)
│   │   ├── filters.py                 # OpenStax-specific structural filters
│   │   │                              #   (back-matter index/glossary detection,
│   │   │                              #    sidebar headers, page-header echoes)
│   │   ├── config.yaml                # textbook_id, font sizes, chapter format
│   │   └── prompt_overrides.py        # Anatomy-specific prompt additions if any
│   │
│   └── (future: second_book/, etc.)
│
└── run.py                             # CLI entry: python -m ingestion.run --source openstax_anatomy
```

**Reusable vs source-specific concerns**:
| Concern | Layer |
|---|---|
| PDF-to-elements extraction | sources/X/parse.py (font sizes & layout differ per book) |
| Back-matter / sidebar filtering | sources/X/filters.py (each book labels these differently) |
| Sentence-boundary chunking | core/chunker.py |
| Sentence-aware overlap generation | core/chunker.py |
| Dual-task LLM proposition prompt body | core/propositions.py |
| Domain-specific prompt suffix (e.g., "preserve medical terminology") | sources/X/prompt_overrides.py |
| Embedding, BM25, Qdrant indexing | core/* |
| Window-navigation metadata (prev/next chunk IDs, sequence_index, subsection_id) | core/qdrant.py |
| Retrieval, conversation, evaluation | unchanged from current code |

### Step-by-step (revised, 2026-04-28)

| # | What | Cost | Time |
|---|---|---|---|
| **B.1** | **Scaffold core/sources directory** — move existing `ingestion/parse_pdf.py`, `ingestion/chunk.py`, `ingestion/extract.py` into `sources/openstax_anatomy/`; move `ingestion/propositions.py`, `ingestion/index.py` (decomposed) into `core/`; create empty stubs for new modules. **No behavior change yet** — just reshuffle and ensure tests still run. | $0 | 1-1.5 hr |
| **B.2** | **Fix `sources/openstax_anatomy/parse.py`** — detect and exclude back-matter (index page numeric-density >0.4, glossary `term: def` patterns, references). Also detect and tag sidebars (`Career Connection`, `Everyday Connection`, `Aging and the X System`, `Disorders of...`) as `element_type="sidebar"` instead of leaking them into paragraphs. | $0 | 1.5-2 hr |
| **B.3** | **Fix `core/chunker.py`** — sentence-boundary aware token cap (back off to last `. `/`! `/`? ` boundary if hitting 440-tok limit); sentence-aware overlap (last 2 *complete* sentences of chunk N + first content of chunk N+1, capped at 440 tok at sentence boundary). Properly include `element_type="table"` and `element_type="figure_caption"` chunks in output. | $0 | 3-4 hr |
| **B.4** | **Build `core/qdrant.py` payload schema** — fields: `chunk_id`, `textbook_id`, `chunk_type`, `chapter_num`, `chapter_title`, `section_num`, `section_title`, `subsection_title`, `subsection_id` (composite: `textbook_id:section_num:subsection_normalized`), `sequence_index` (within subsection, 0-indexed), `prev_chunk_id`, `next_chunk_id`, `subsection_chunk_count`, `page`, `prompt_version`, `ingested_at`, `subsection_summary_id` (null in v1, forward hook). `--fresh` flag for full wipe. | $0 | 1 hr |
| **B.5** | **Build `core/propositions.py`** — Sonnet 4.5 (latest), dual-task prompt (clean text + extract propositions, returns JSON `{cleaned_text, propositions}`), `temperature=0`, async parallel via `AsyncAnthropic` + `asyncio.gather` (semaphore=20), system prompt cached via `cache_control: ephemeral`, per-call cost tracker. One few-shot example in cached prompt. Source-agnostic. | $0 | 1.5 hr |
| **B.6** | **Build `core/cost_tracker.py`** — accumulate `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens` per call. Print running total after every batch of N. Compute Sonnet pricing (in/out/cache rates). | $0 | 30 min |
| **B.7** | **Build `core/pipeline.py`** — CLI orchestrator: `python -m ingestion.run --source openstax_anatomy [--fresh] [--limit N]`. Stages: parse → filter → chunk → propositions → embed → BM25 → upsert. Each stage writes intermediate artifact; can resume from any stage. | $0 | 1.5 hr |
| **B'.1** | **Wire `hint_plan` into teacher's socratic draft** (conversation quality fix from 2026-04-28 audit). | $0 | 30 min |
| **B'.2** | **Wire `dean_critique` into `_quality_check_call`** (conversation quality fix). | $0 | 20 min |
| --- | --- | --- | --- |
| **B.8** | **Pilot: dual-task on 50 stratified chunks**. Validate: JSON parse rate ≥98%, cleaned_text 60-95% of original, no medical content lost (manual spot-check 10), propositions atomicity reasonable, cache hits visible. Running cost meter on. | <$1 | 5-10 min |
| **B.9** | **Full pipeline run** on PDF: parse → filter → chunk → propositions → embed → BM25 → Qdrant `--fresh` upsert. Running cost meter visible throughout. Abort if cost trends >$35. | ~$25-30 | 1-2 hr |
| **B.10** | **Validate** — `scripts/validate_topic_index.py` (≥90% teachable), spot-check 5 previously-broken queries (Cranial Fossae, deltoid innervation, Rh blood typing, parathyroid function, rugae). Verify window-nav metadata populated correctly (every chunk has prev/next ids except boundaries). | $0 | 30 min |
| **B.11** | **End-to-end sim** — `sim_repl.py` 6-profile run, verify Cranial Fossae topic-lock succeeds, no quality regression vs current sim baseline. | ~$3-5 | 30 min |
| **B.12** | **Publish v1 to HF** — `python scripts/publish_corpus.py --tag v1-clean --force`. | $0 | 10 min |

**Total Anthropic ~$28-35. OpenAI ~$3-5. Wall ~15-20 hr (most $0 code).**

### Explicitly NOT doing in v1

- ~~Patch existing chunks in place~~ — replaced by full pipeline rebuild.
- ~~Subsection summary generation~~ — deferred to Phase D pending eval evidence. Forward-hook field reserved in payload (`subsection_summary_id`).
- ~~pdfplumber separate table pipeline~~ — superseded by including raw-element tables in chunker output.
- ~~Vision pass over figures~~ — deferred to multimodal phase (later).

### Exit criteria
- `v1-clean` tag on HF.
- Cranial Fossae topic-lock succeeds end-to-end in sim.
- 5 spot-check queries each retrieve ≥3 on-topic chunks after hard section filter.
- Window expansion (W=1) works: retrieving any chunk returns its 2 neighbors via `prev_chunk_id`/`next_chunk_id`.
- 0 chunks with mid-sentence boundaries in v1 corpus (audit re-run at exit).
- 0 back-matter pollution (no chunks tagged Ch 28 that are actually index entries).

---

## Phase B' — Conversation quality fixes (pre-latency, ~1 hr, $0)

Found via content-completeness audit 2026-04-28. Two real bugs where TutorState fields exist but never reach the LLM that needs them. Must land **before** D.6b (cache restructuring) — optimizing latency on a degraded reply is wrong order.

**B'.1 Wire `hint_plan` into teacher's socratic draft**
- Bug: `_hint_plan_call` runs once at topic-lock and stores 3-step hint progression in `state["debug"]["hint_plan"]` ([dean.py:1022](conversation/dean.py:1022)). Teacher reads `hint_level` (1/2/3) but never reads the actual planned hint for that level. So the teacher re-derives hints from scratch every turn, ignoring Dean's pre-planned progression.
- Effect: inconsistent hint pacing, possible repetition across turns, no global hint strategy.
- Fix: add `{hint_plan_active}` placeholder to teacher_socratic_dynamic at [config/base.yaml:348](config/base.yaml:348); inject `state["debug"]["hint_plan"][hint_level-1]` at [teacher.py:~199](conversation/teacher.py:199).
- Effort: ~30 min. Validate: at hint_level=2 the draft should reflect the pre-planned hint #2.

**B'.2 Wire `dean_critique` into quality-check**
- Bug: when teacher's draft fails the deterministic check, `dean_critique` is set with the failure reason ([dean.py:1903](conversation/dean.py:1903)) and used for the teacher's retry. But the quality-check call ([dean.py:2014](conversation/dean.py:2014)) doesn't see it — evaluates the revised draft as if it's the first attempt.
- Effect: revision instructions are generic ("rewrite to be more focused") instead of targeted ("the prior draft revealed the answer; revise without revealing X"). Same error can repeat across retries.
- Fix: add `prior_preflight_critique` placeholder to dean_quality_check_dynamic at [config/base.yaml:806](config/base.yaml:806); inject existing `dean_critique` at [dean.py:2051](conversation/dean.py:2051).
- Effort: ~20 min. Validate: trigger a known-fail draft, verify revision instruction references the original failure mode.

**B'.3 (optional, low-priority)**: enhance `_exploration_judge` with `locked_topic.path` for marginally better tangentiality precision. ~10 min if done, skip if pressed for time.

**Verified-correct architectural omissions (do NOT change):**
- `draft_socratic` does NOT receive `locked_answer` — pedagogical integrity (teacher must derive from chunks, not memorize the target).
- `maybe_summarize` and `_hyde_rewrite` receive only what they need.
- Confidence/mastery fields are populated for analytics (mem0 / KT D.3), not for runtime tutoring LLM calls — correct.

**Deferred (not dead code):**
- `is_multimodal` and `image_structures` in TutorState — populated but unused by any LLM call. **Retained for the multimodal path being revisited later** (vision-grounded clinical questions, image-prompted tutoring). Do not retire.

**Exit criteria:** B'.1 and B'.2 verified in a sim turn; conversation quality unchanged or improved; no regression on existing eval scripts.

---

## Phase C — Second textbook (deferred additive, post-v1)

Do this *after* Phase B's `v1-clean` is published and validated. It's a pure additive step — new `textbook_id`, append-only to Qdrant, no impact on existing data.

**C.1 Pick the second source**
- Candidate confirmed; stage at `data/raw/<book2>.pdf`.
- Update `config/base.yaml` with `textbooks: [{id: "ot_main", path: ...}, {id: "ot_suppl", path: ...}]`.

**C.2 Run ingest on book 2 only**
- `python -m ingestion.chunk --textbook ot_suppl` → `python -m ingestion.propositions --textbook ot_suppl` → `python -m ingestion.index --textbook ot_suppl` (no `--fresh`, so book 1 stays).
- Cost: LLM ~$3-8, embedding ~$2, wall ~1-2 hr (depends on book size).

**C.3 Validate + publish v2**
- `scripts/validate_topic_index.py` covers both textbooks.
- `python scripts/publish_corpus.py --tag v2-dual-source`.

**Exit criteria:** retrieval over combined corpus returns on-topic chunks from both books for at least 3 OT-specific queries.

---

## Phase D — Architectural additions

All are drop-in on top of the re-ingested corpus. No further ingestion work.

**D.1 Soft metadata fallback (post-lock tutoring only)**
- [retriever.py::retrieve](retrieval/retriever.py) — after lock, if strict section-filtered retrieval returns <N chunks, widen to chapter (should-filter + cosine penalty), then global with larger penalty.
- **Not** applied at topic-lock time (that's still HARD — relaxing there re-opens the card-loop bug).
- Config knob: `cfg.retrieval.softfallback_min_chunks` (default 3).

**D.2 Adaptive-RAG query routing (Option A, prompt-based)**
- New `DeanAgent._classify_complexity` method. LLM classifies each student message: `simple | tangential | complex`, returns JSON `{tier, rationale}`.
- Wired at top of tutoring turn in [dean.py::run_turn](conversation/dean.py).
- `simple` → current path. `tangential` → trigger existing `_exploration_retrieval_maybe` explicitly (instead of its own internal judge). `complex` → two-hop retrieval stub (defer actual two-hop impl to D.4 if time permits).
- Consolidates what's currently ad-hoc routing into one citable architectural component (Adaptive-RAG, Jeong 2024).

**D.3 Per-concept Knowledge Tracing (LLMKT-style)**
- After each student turn, Dean emits a `{"<concept>": mastery_0_to_1}` dict via a small LLM call.
- Persisted in `mem0` under `per_concept_mastery`.
- Rapport uses this to surface specific weak concepts, not generic weak_topics.
- Assessment summary references per-concept trajectory if multi-session data exists.
- Paper: Scarlatos et al. 2024 (arXiv:2409.16490).

**D.4 (Optional) Two-hop retrieval for complex queries**
- Only if D.2's `complex` tier sees real use.
- Simple pattern: extract 2 concept mentions from query → retrieve each → concatenate top-k from each.
- Defer decision until we see real complex-tier traffic.

**D.5 (Optional) CRAG 3-state coverage gate**
- Upgrade [dean.py::_coverage_gate](conversation/dean.py) from binary to 3-state: `pass | rescue-via-HyDE | reject`.
- HyDE rescue is already implemented; just wire it as the middle branch.

**D.6 Latency reduction — streaming + caching + parallelization** (added 2026-04-27)

Audit (this session) mapped per-turn LLM calls. Today a normal tutoring turn = 7-12s wall, ~17-25% recoverable. Implement in priority order; each is independent.

**D.6a Streaming the teacher draft** (highest perceived-latency win)
- Switch [teacher.py::draft_socratic](conversation/teacher.py:297) from `client.messages.create(...)` to `client.messages.stream(...)` and surface tokens to UI as they arrive.
- First-token-time drops to ~500ms regardless of full-response time.
- Effort: ~30 min if UI already supports streaming; otherwise needs streaming hookup in `backend/` API + `frontend/`.
- Win: ~5s perceived → ~0.5s perceived (10× user-visible improvement).

**D.6b Prompt caching: fix existing breakage + fill gaps** (audit 2026-04-27)

**Critical finding**: caching infrastructure exists (`_cached_system` in [dean.py:89](conversation/dean.py:89), [teacher.py:~95](conversation/teacher.py:95)) and telemetry is wired (`cache_read`/`cache_write` per call). **But cache hit rate is 0% in production** across multi-turn sessions. Evidence: [data/artifacts/conversations/arun_turn_8.json](data/artifacts/conversations/arun_turn_8.json) — turns 2-7 all show `cache_read=0` despite ~3200 cached_est_tokens. Currently we pay the 1.25× cache-write premium every turn and never get a 0.1× cache-read.

**Root cause**: `conversation_history` is appended into the cached block ([dean.py:111](conversation/dean.py:111)). History grows each turn → cache prefix bytes change → cache MISS every turn → cache rewritten every turn.

**Fix B-1 (critical, ~1 hr): split history out of cached prefix.**
Restructure `_cached_system` to use multi-block caching (Anthropic supports 4 breakpoints):
- Block 1 [CACHED]: `role_base + wrapper_delta + chunks` (stable across whole session, ~2KB)
- Block 2 [CACHED]: `history_prefix` for turns `0..N-1` (append-only, grows by appending)
- Block 3 UNCACHED: current turn message + `turn_deltas`

Change scope: [dean.py:89-124](conversation/dean.py:89), [teacher.py:~95](conversation/teacher.py:95), and the call sites that pass `history` into `_cached_system`. Validate post-fix: `cache_read` should be >0 starting turn 2, ratio ~50-70% on long sessions.

**Fix B-2 (medium, ~30 min): cache summarizer.**
[conversation/summarizer.py:68](conversation/summarizer.py:68) makes a Sonnet call with no cache_control. System prompt (cfg.prompts.summarizer_system) is static. Wrap with `_cached_system(...)` pattern.

**Fix B-3 (medium, ~30 min): cache HyDE.**
[retrieval/retriever.py:368](retrieval/retriever.py:368) makes a Haiku call with no cache_control. The HyDE prompt template (cfg.prompts.hyde_reformulate) is stable. Add cache_control to the system block. Note: client-side `_HYDE_CACHE` dict at [retriever.py:32](retrieval/retriever.py:32) already dedupes on identical query strings; API-level caching helps when query strings differ but prompt prefix matches.

**Fix B-4 (verify): wrapper_delta stability.**
Audit whether `wrapper_delta` actually stays stable across turns or rotates with `phase`/`student_state`/`hint_level`. If it rotates, move the rotating parts out of the cached block.

**Fix B-5 (~30 min): client-time greeting + cache hygiene.**
[teacher.py:141](conversation/teacher.py:141) computes `datetime.now().hour` server-side for the rapport greeting (`morning`/`afternoon`/`evening`). Two problems: (a) server runs in UTC on cloud → wrong greeting for 70% of users; (b) the time string sits inside the cached prompt → invalidates cache on every hour rollover.

Fix:
- Frontend (React): on session start send `client_hour = new Date().getHours()` or IANA tz `Intl.DateTimeFormat().resolvedOptions().timeZone`.
- Backend (FastAPI): accept `client_hour` (or `client_tz`) on `/session/start`, pass to `teacher.draft_rapport`.
- Teacher: drop `datetime.now()`, use parameter.
- Place `time_of_day` in the uncached suffix block so the cached prefix stays stable across hour rollovers.

**Decision (after careful audit): NOT trimming history in classifier calls.**
Earlier audit flagged `_prelock_intent_call`, `_prelock_refuse_call`, `_lock_anchors_call`, `_exploration_judge` as over-sending history. Re-examination found that trimming would break reference resolution ("yeah, the first one"), tone matching on repeated refuses, and tangentiality judgment. Token savings (~400-1500/call) is small after caching is fixed; behavioral risk is real. **Keep full history; rely on the cache structure fix for the win.**

**Win after fixes**: 30-50% latency reduction on every Anthropic call after turn 1; ~80% input-token cost reduction on cached content; cache hit rate goes 0% → 50-70% on long sessions; correct greeting for any timezone.

**Effort total**: ~3.5 hr including verification. Validate via `data/artifacts/conversations/*.json` — `cache_read` per turn should be non-zero starting turn 2.

**D.6c Parallelize independent LLM calls** (per-turn flow win)
Convert call sites to `anthropic.AsyncAnthropic` + `asyncio.gather`. Three opportunities ranked:

1. **Exploration judge ∥ teacher draft** (every turn) — [dean.py:1205-1211](conversation/dean.py:1205), [dean.py:1383](conversation/dean.py:1383), [teacher.py:297](conversation/teacher.py:297). Judge result only gates whether to *append* exploration chunks; draft uses chunks already in state. **Saves ~2-4s/turn** depending on model. Easy win, $0.
2. **HyDE rewrite ∥ original retrieval** — [retriever.py:368, 597-614](retrieval/retriever.py:368). Fire HyDE concurrently with original qdrant search; discard if original is strong. **Saves ~2s on weak-retrieval paths**, ~$0.0001/turn waste on strong-retrieval paths. Speculative.
3. **Pre-lock intent ∥ refuse draft** — [dean.py:1464, 1528](conversation/dean.py:1464). Fire refuse speculatively in parallel with intent. **Saves ~2-3s on no-match paths**, ~$0.0005/turn waste otherwise. Speculative, only fires during topic-lock phase.

Effort: ~3 hr total (one async-wrapper change unlocks all three).
Win: 17-25% per-turn latency reduction (more after Sonnet 4.5 swap, since wins scale with longest concurrent call).

**D.6 Exit criteria:** median tutoring-turn first-token-time <1s; full response wall <8s on Sonnet 4.5.

---

## Phase E — Evaluation

**E.1 Re-run 6-profile sim**
- Driver: `scripts/sim_repl.py` (already built in worktree).
- Cover S1 (Strong), S2 (Moderate), S3 (Weak), S4 (Overconfident), S5 (Disengaged), S6 (Anxious).
- Save outputs to `data/artifacts/final_convo/`.

**E.2 EULER scoring run**
- `python -m scripts.score_euler` on the 6 profiles.
- Produces per-turn pedagogical rubric scores.

**E.3 Ablation table** (thesis-worthy)
- Baseline (strict hard filter only, no adaptive, no KT).
- + Soft fallback.
- + Adaptive routing.
- + KT.
- Full system.
- Report retrieval hit@k, coverage-gate pass rate, EULER scores, student-reached-answer rate.

**Exit criteria:** ablation table in the thesis draft, numbers filled in.

---

## Phase F — Write-up (final)

**F.1 Thesis lit review section**
- AutoTutor (Graesser) — classical ITS baseline.
- Self-RAG (Asai 2023).
- Corrective-RAG (Yan 2024).
- Adaptive-RAG (Jeong 2024).
- LLMKT (Scarlatos 2024).
- NotebookLM + SocraticAI + LPITutor practitioner comparisons.

**F.2 Architecture section**
- TOC-grounding-by-construction as the distinctive claim.
- Map each component to its paper lineage.
- Include the ablation table from E.3.

**F.3 Figures**
- LangGraph flow diagram.
- Retrieval stack diagram (dense + BM25 + RRF + CE + section filter).
- Per-concept KT mastery trajectory example.

---

## Explicit non-goals

- **Fine-tuning any model.** Pure prompting throughout.
- **Building GraphRAG / multi-hop reasoning graph.** Adaptive routing's `complex` tier is the ceiling.
- **Longitudinal real-user study.** Thesis scope is simulation + rubric evaluation.
- **Voice / multimodal beyond the existing image-structure path.** Not in thesis scope.

---

## Status tracker

- [ ] A.1 repo decision
- [ ] A.2 publish script
- [ ] A.3 bootstrap script
- [ ] A.4 plumbing test
- [ ] A.5 real v0 publish
- [ ] A.6 SETUP.md
- [x] A.1-A.6 HF plumbing complete (v0-messy-metadata published 2026-04-22)
- [ ] B.1 scaffold ingestion/core + ingestion/sources/openstax_anatomy (move existing code, no behavior change) (~1-1.5 hr, $0)
- [ ] B.2 fix sources/openstax_anatomy/parse.py — exclude back-matter, tag sidebars (~1.5-2 hr, $0)
- [ ] B.3 fix core/chunker.py — sentence-aware splitting + sentence-aware overlap; include tables + figure_captions (~3-4 hr, $0)
- [ ] B.4 build core/qdrant.py payload schema with window-nav metadata (prev/next chunk_id, sequence_index, subsection_id, etc.) (~1 hr, $0)
- [ ] B.5 build core/propositions.py — Sonnet 4.5 dual-task, async, cached, generic prompt (~1.5 hr, $0)
- [ ] B.6 build core/cost_tracker.py — per-call billing accumulator (~30 min, $0)
- [ ] B.7 build core/pipeline.py — CLI orchestrator with resumable stages (~1.5 hr, $0)
- [ ] B'.1 wire hint_plan into teacher.draft_socratic (~30 min, $0)
- [ ] B'.2 wire dean_critique into _quality_check_call (~20 min, $0)
- [ ] B.8 pilot dual-task on 50 stratified chunks (<$1, ~10 min)
- [ ] B.9 full pipeline run on PDF (~$25-30, ~1-2 hr)
- [ ] B.10 validate (topic index + 5 spot queries + window-nav check) (~30 min, $0)
- [ ] B.11 end-to-end sim 6-profile (~$3-5, ~30 min)
- [ ] B.12 publish v1-clean to HF (~10 min, $0)
- [ ] C.1 second source staged (deferred additive post-v1)
- [ ] C.2 run ingest on book 2 only
- [ ] C.3 validate + publish v2-dual-source
- [ ] D.1 soft fallback
- [ ] D.2 adaptive routing
- [ ] D.3 KT
- [ ] D.4 two-hop (maybe)
- [ ] D.5 CRAG 3-state (maybe)
- [ ] D.6a streaming teacher draft
- [ ] D.6b-1 fix history-in-cached-block bug (cache hit rate 0% → 50-70%)
- [ ] D.6b-2 cache summarizer
- [ ] D.6b-3 cache HyDE
- [ ] D.6b-4 verify wrapper_delta stability across turns
- [ ] D.6b-5 client-time greeting (frontend → backend pass-through, kills cache-buster too)
- [ ] D.6c parallelize independent LLM calls (3 opportunities)
- [ ] E.1 sim run
- [ ] E.2 EULER
- [ ] E.3 ablation table
- [ ] F.1-F.3 write-up
