# Session Journal — 2026-04-29 (chunks rebuild + revised forward plan)

Continuation of the Phase B work; this session reframed the retrieval
architecture and re-prioritized the remaining roadmap. Capturing both
what shipped and the revised plan against the existing forward plan
(`progress_journal/2026-04-22_23-00-00_forward_plan_through_thesis_submission.md`).

---

## What shipped this session (5 commits on main)

| Commit  | Scope |
|---|---|
| `9f0dc0c` | CE truncation fix + realistic eval runner (112-row test set) |
| `23b061b` | HyDE 0.65→0.50 + OOD 0.30→0.45 thresholds, MPS cross-encoder, eager UMLS warmup, per-stage timing instrumentation, 3 eval scripts |
| `72d0308` | Chunks-mode reindex (`scripts/reindex_chunks.py`) + ChunkRetriever subclass + RAPTOR builder (`scripts/build_raptor_summaries.py`) + e2e bug fix |
| `da4d50c` | Locked-answer grounding check (`_sanitize_locked_answer` in dean.py) + dean topic-gate threshold (separate `dean_topic_gate_ce_threshold`) + chunks-friendly anchor prompts |
| `be0c7be` | **Semantic topic resolution** in dean.py replacing brittle rapidfuzz TopicMatcher + ChunkRetriever BM25-path fix (was silently falling back to MockRetriever) + textbook-answerable e2e topics |

## Architectural shift

**Dropped:** Dense-X-Retrieval / propositions-as-retrieval-units (Chen et al. EMNLP 2024).
End-to-end test on canonical anatomy questions ("which nerve innervates the
deltoid?") showed atomic propositions destroy the relational verb that
should be the discriminative retrieval signal. Tracing showed the right
propositions don't reach top-50 of either dense Qdrant or BM25.

**Adopted:** Chunks-mode indexing (~7,574 chunks) + RAPTOR summary tree
(549 subsection + 170 section summaries; 8,293 total points in
`sokratic_kb_chunks`). Literature-aligned with the Oct 2025 SOTA pattern
for biomedical RAG (`arXiv:2510.04757`).

## End-to-end measured improvement

Same 6-profile harness (`scripts/run_final_convos.py`):

| Run | Topics | Real retrieval? | Correct lock |
|---|---|---|---|
| v1 baseline (propositions, off-corpus topics) | wrist drop, deltoid innervation | ✅ | 0/6 (1 hallucinated) |
| v4 (chunks + RAPTOR + semantic topic + grounding, in-corpus topics) | T cells, blood pressure, nephron, reflexes, elbow, digestion | ✅ | **4/6 correctly grounded**, 1 reached |

Two remaining e2e failures (S4 overconfident "Reflexes are basically just
instant nerve reactions, right?" and S6 anxious "I'm not sure I really
understand chemical digestion — could we go through it?") are isolated
to the LLM intent classifier in `_prelock_intent_call`.

## Newly discovered issues (not in original plan)

1. **Intent classifier prompt brittleness on non-question student turns** (S4 leading assertions, S6 hesitancy markers) — `_prelock_intent_call` fails to extract a topic
2. **`_sanitize_locked_answer` formatting too aggressive** — produces "tc th1 th2 treg cells" instead of "Tc, Th1, Th2, Treg cells"
3. **TopicMatcher fuzzy fallback now redundant** since semantic resolution lands in dean.py
4. **No published baseline for Dense-X-Retrieval on biomedical / textbook corpora** — tonight's empirical failure result is novel territory; potential thesis contribution
5. **Source coverage gap** — OpenStax A&P 2e doesn't cover OT-clinical content (wrist drop, claw hand, tinel sign, etc.); 0 chunks contain "wrist drop"; only 2 chunks mention "axillary nerve" at all. The OLD pipeline's "successes" on these were LLM parametric-knowledge hallucinations.

## Cache-bug verification status

Plan flagged "cache hit rate is 0% in production due to history-in-cached-block
bug" (`progress_journal/2026-04-22_..._forward_plan...md` D.6b-1). Tonight's
verification:

- **Code (dean.py:111) confirmed**: `cached_primary` includes `history`,
  which grows every turn. Structurally guaranteed to invalidate the cache
  prefix every turn.
- **Telemetry (data/artifacts/conversations/arun_turn_5.json, Apr 20)**:
  `cache_read=0`, `cache_write=0`, `cached_est_tokens=185–789` (under the
  4096 Haiku 4.5 minimum) across all turns.
- **Telemetry on tonight's conversations**: NOT directly observed —
  the per-call cache fields were dropped from the saved JSON schema at
  some point (now just `api_calls: 31`, no per-call detail). Need to run
  a 2-turn smoke test that explicitly logs `cache_read_input_tokens` /
  `cache_creation_input_tokens` from the Anthropic response object before
  claiming the fix is observably needed on the current pipeline.
- **Net**: code confirms bug exists; old telemetry confirms it manifested;
  fresh telemetry on the current pipeline still TODO before fix-and-verify.

---

# Revised forward plan (priority order)

User direction (this session):
- Move **Phase C (2nd textbook) to last**.
- Move **cosmetic changes (formatting, write-ups, hygiene cleanup, propositions removal) to last**.
- **Phase F (write-up)** deferred to thesis submission time.
- **Phase E (eval)** done in between when major changes land and architecture is stable.
- **ColBERT + iterative retrieval** — only after baseline eval shows retrieval is the bottleneck.

## Tier 1 — Bug fixes + correctness + perf (immediate)

| #  | Item | Win | Effort |
|----|---|---|---|
| 1  | **B'.1** Wire `hint_plan` into teacher's socratic draft | Hint pacing consistency | 30 min |
| 2  | **B'.2** Wire `dean_critique` into `_quality_check_call` retry | Targeted revision instructions | 20 min |
| 3  | **Intent classifier prompt** (S4 leading-assertion, S6 hesitancy) — pure prompt work, no heuristics | Conversation success rate | 30 min |
| 4  | **Cache verification smoke test** (2-turn convo logging cache_read directly) | Confirms #5 is real on current pipeline | 15 min |
| 5  | **D.6b-1** Fix history-in-cached-block bug — split into 4 cache breakpoints | **30–50% latency, ~80% input-token cost reduction** | 1 hr |
| 6  | D.6b-2 Cache summarizer | Latency on summary turns | 30 min |
| 7  | D.6b-3 Cache HyDE | Latency on HyDE-fired turns | 30 min |
| 8  | D.6b-4 Verify `wrapper_delta` stability across turns | Confirms #5 holds | 10 min |
| 9  | D.6b-5 Client-time greeting + cache hygiene (frontend → backend tz pass-through) | Correct greeting + cache stability across hour rollovers | 30 min |
| 10 | D.6c Parallelize independent LLM calls (3 opportunities) | 17–25% per-turn latency | 3 hr |
| 11 | D.6a Streaming teacher draft (depends on UI) | Perceived latency 5s → 0.5s | 30 min if UI ready |
| 12 | **D.1** Soft metadata fallback (post-lock tutoring only) | Recall when strict section filter empty | 1 hr |

## Tier 2 — Architecture additions (after Tier 1 stable)

| #  | Item | Notes |
|----|---|---|
| 13 | **Topic bank + scaled harness** (3 per profile × 6 = 18 convos) | Already started: 30 in-corpus topics + Haiku-generated profile-specific phrasings in `data/eval/topic_bank_v1.jsonl` |
| 14 | **D.2** Adaptive-RAG query routing — simple/tangential/complex (Jeong 2024) | Thesis-bearing |
| 15 | **D.3** Per-concept Knowledge Tracing (LLMKT, Scarlatos 2024) | Thesis-bearing |
| 16 | D.4 Two-hop retrieval (optional) | Only if D.2 shows real complex-tier traffic |
| 17 | D.5 CRAG 3-state coverage gate (optional) | Marginal |

## Tier 3 — Eval pass with stable architecture

| #  | Item |
|----|---|
| 18 | E.1 6-profile sim on the full system (~$3-5, ~30 min) |
| 19 | E.2 EULER scoring (`scripts/score_euler.py`, already built) |
| 20 | **E.3 Ablation table v1** — baseline / +soft fallback / +adaptive / +KT / full |

## Tier 4 — ColBERT + iterative retrieval (only if Tier 3 shows retrieval bottleneck)

| #  | Item |
|----|---|
| 21 | ColBERT v2 reranker bake-off (vs MedCPT-CE) |
| 22 | Iterative retrieval (i-MedRAG style) |
| 23 | E.3 ablation table v2 (re-run with these added if used) |

## Tier 5 — Cosmetic / cleanup / 2nd textbook / write-up (last)

| #  | Item |
|----|---|
| 24 | `_sanitize_locked_answer` formatting (preserve case/punctuation) |
| 25 | Remove proposition pipeline (alias `Retriever` → `ChunkRetriever`, deprecate `propositions_dual.py`) |
| 26 | Commit pre-existing uncommitted hygiene (`config.py`, `conversation/nodes.py`, `conversation/state.py`, `tools/mcp_tools.py`, untracked utility scripts, progress journals, SETUP.md, MANIFEST.json) |
| 27 | Write-up: "atomic propositions don't help on biomedical relational RAG" — empirical novel result |
| 28 | **Phase C** — 2nd textbook (clinical OT reference) — addresses the source coverage gap discovered tonight |
| 29 | B.12 Publish v1-clean to HuggingFace |
| 30 | **Phase F** — Thesis lit review + architecture section + figures |

---

## Topic bank (already produced this session, ready for the harness)

`data/eval/topic_bank_v1.jsonl` — 30 topics across 28 chapters of the OpenStax
A&P 2e corpus, all with chunk_count ≥ 6 and non-clinical-callout subsection
titles. Each row has 6 profile-specific student-style phrasings generated by
Haiku 4.5. Examples:

- Ch11 Lever Systems > Exercise and Stretching
- Ch12 Communication Between Neurons > Graded Potentials
- Ch21 Anatomy of Lymphatic & Immune Systems > Immune System
- Ch25 Microscopic Anatomy of the Kidney > Nephrons: The Functional Unit

## Honest non-goals for the next sprint

- No fine-tuning (per existing forward plan)
- No real-user study (per existing forward plan)
- ColBERT NOT to be integrated until baseline eval shows retrieval is the
  weakest link
- Iterative retrieval NOT to be integrated until D.2 adaptive routing
  shows real complex-tier traffic
- 2nd textbook NOT to be added until Tiers 1-3 stabilize on the OpenStax
  corpus alone

## Immediate next action

Tier 1 #1–#3 (B'.1 + B'.2 + intent classifier prompt) in one focused pass,
then #4 (cache smoke test) to verify on current pipeline before committing
to #5 (cache fix).
