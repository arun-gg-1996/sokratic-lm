# P1 + P4: UMLS Ontology + Observability Hardening

Date: 2026-04-22 (continues from `2026-04-21_23-00-00_p0_complete_strict_groundedness.md`)

## What shipped this session

### P1.1 — scispacy UMLS spike (UMLSAdapter)
- `.venv/bin/pip install scispacy==0.5.5` + `spacy==3.7.5` (scispacy ships as an extras-layer over spaCy; numpy auto-downgraded from 2.2.6 → 1.26.4, verified no Qdrant/sentence-transformers breakage).
- `pip install https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz` (~35 MB).
- First `EntityLinker` construction auto-downloaded the UMLS 2022 KB (~1 GB — `nmslib` ANN index + `concept_aliases.json` + `umls_2022_ab_cat0129.jsonl`) to `~/.scispacy/datasets/`. First-run cost ~210 s (wall). Subsequent pipeline loads ~60 s (disk-cached KB, still slow but one-time per process).
- [retrieval/ontology.py](retrieval/ontology.py) rewritten from P0.8 stub to the real adapter:
  - Module-level cached pipeline (`_UMLS_PIPELINE` / `_UMLS_LINKER` / `_UMLS_LOAD_FAILED` sticky-fail flag) so the 60 s warmup hits exactly once per process even if several components build adapters.
  - `link_entities(text)` runs NER → links each `ent._.kb_ents` to UMLS via `linker.kb.cui_to_entity`. Falls back to `span-as-canonical` when NER fires but the linker returns no candidates (preserves the surface-form signal for downstream fuzzy matching).
  - `get_synonyms(canonical)` links the canonical back to its own CUI then returns all UMLS aliases.
  - Any ImportError / OSError / linker-load failure sticks the adapter in noop mode for the process lifetime — the rest of the tutor works without scispacy.

Verified on-corpus:
- `"deltoid"` → `"Structure of deltoid muscle"` (CUI C0224234, 0.974).
- `"cranial nerve VII palsy"` → `"Facial paralysis"` (C0015469, 0.964).
- `"axillary nerve"` → `"Structure of axillary nerve"` (C0228885, 0.984).

### P1.2 — Ontology-aware TopicMatcher
- [retrieval/topic_matcher.py](retrieval/topic_matcher.py) takes an `ontology: DomainOntologyAdapter` param; `get_topic_matcher()` plugs in the domain-appropriate adapter via `get_ontology_adapter(cfg.domain.retrieval_domain)`.
- Before scoring, `match()` calls `_expand_query()` which appends UMLS canonicals to the raw query so RapidFuzz token_set_ratio sees both the student phrasing and the canonical form.
- **Canonical-cleanup pass** (`_clean_canonical`): UMLS wraps terms in generic scaffolding ("Structure of …", "Left …", "Entire …", ", NOS", "(body structure)") that inflates token-set overlap against any TOC section sharing those words. We strip those wrappers before joining the canonical into the fuzzy query. Without this, "deltoid" matched "Structure of Cardiac Muscle" because UMLS returned "Structure of deltoid muscle" and "Structure of …" matched every "Structure of …" section title.

Match results after cleanup (was `none` on all fine-grained terms before P1):

| Query | Before P1 | After P1 (UMLS) |
|---|---|---|
| `liver` | strong "The Liver" (101) | unchanged |
| `heart` | borderline "Conduction System of the Heart" (101) | unchanged |
| `deltoid` | none (top 50.0) | **borderline "Muscle Tone" (73)** |
| `cranial nerve VII` | none | **borderline "Blood and Nerve Supply" (78)** |
| `brachial plexus` | none | **borderline "Blood and Nerve Supply" (78)** |
| `cn vii` (abbrev) | none | none — scispacy's AbbreviationDetector needs long-form context |
| `wrist drop` | none | none — not in UMLS standard concepts |
| `quantum mechanics` | none | unchanged |

### P1.3 — Ontology expansion in Retriever.retrieve
- `Retriever.__init__` now builds `self._ontology = get_ontology_adapter(self.default_domain)` (lazy — no scispacy load at construction).
- New `_apply_ontology_expansion(query)` method mirrors the topic-matcher expansion: runs the adapter, appends canonicals + spans in parentheses (preserves original phrasing for BM25/dense exact matches), capped at `cfg.retrieval.ontology_max_extras` (default 6).
- `retrieve()` now pipes: `query → _apply_ontology_expansion → _apply_query_aliases → expanded_query`. Both stages are purely additive.
- The alias dict is **not retired** — it's downgraded to a high-precision fallback alongside the ontology. Retiring it outright is deferred until UMLS coverage is re-validated on the full eval set (guardrail against regression on terms the linker misses). Kill-switches: `cfg.retrieval.ontology_expansion_enabled` and `cfg.retrieval.aliases_enabled` both default `true`.

### P1.4 — Distributional HyDE pregate
- [retrieval/retriever.py::_is_weak_retrieval](retrieval/retriever.py) now fires HyDE rescue on **either** weak-max OR weak-top-K-mean cosine:
  - `max_cosine < hyde_weak_cosine_threshold` (unchanged primary signal).
  - `topk_mean < hyde_weak_topk_mean_threshold` (new secondary signal, default 0.45 over top-5).
- Rationale: a single lucky chunk + noise around it is not the same as broad coverage of a topic. Under the old single-max gate, that case passed and HyDE was skipped — but it's exactly where HyDE's hypothetical passage helps. Config threshold 0.0 disables the new signal (restores old behavior for A/B).
- BM25 and CE are still intentionally excluded from the pregate (the comment inside `_is_weak_retrieval` explains why — BM25 inflates on function-word overlap, CE requires the full candidate expansion we're trying to avoid).

### P4 — Observability hardening
Per-turn invariant check + explicit grounded/ungrounded counters — replaces the previous "derive groundedness from coverage_gap_events" heuristic.

[conversation/nodes.py::dean_node](conversation/nodes.py) now, on every tutoring turn:
1. Inspects `partial_update.locked_topic` and `retrieved_chunks`.
2. Increments `debug.grounded_turns` when both are present, `debug.ungrounded_turns` otherwise.
3. Appends an `invariant_violations` entry when an ungrounded tutoring turn occurs — captures `{turn, kind, has_locked_topic, chunk_count}` for post-hoc investigation.

`_log_conversation` exports three new top-level fields:
- `grounded_turns` (int)
- `ungrounded_turns` (int)
- `invariant_violations` (list of dicts)

`groundedness_score` formula now prefers `grounded_turns / (grounded_turns + ungrounded_turns)` when those counters have been populated, with the prior `1 - coverage_gap_events / turn_count` fallback for backward compat with older session exports.

`initial_state()` now initializes `debug.grounded_turns`, `debug.ungrounded_turns`, `debug.coverage_gap_events`, and `debug.invariant_violations` so callers never have to guard against KeyErrors.

## Verification
- All modified modules import cleanly (`conversation.dean`, `conversation.nodes`, `conversation.state`, `retrieval.retriever`, `retrieval.topic_matcher`, `retrieval.ontology`, `tools.mcp_tools`).
- Topic matcher smoke pass across both adapter types — matches P0 regressions and adds four fine-grained terms to the `borderline` tier.
- `_log_conversation` round-trip confirmed the new P4 fields are populated and `groundedness_score` switches from the old formula to the new counter-based formula when counters are non-zero.
- UMLS adapter end-to-end: `link_entities("deltoid")` returns the expected canonical after the cold-cache 60 s warmup. Once warmed, per-query link cost is sub-second.

## Known caveats / follow-ups
- **60 s cold-start** on first UMLS use per process. Acceptable for batch eval but visible in interactive tutor startup. Mitigation options (none yet shipped): (a) pre-warm on app boot in `backend/main.py`; (b) run the pipeline in a background process with IPC.
- **Abbreviation handling** — "cn vii" alone doesn't resolve. scispacy's `AbbreviationDetector` needs the long form in-context. We'd need a domain-specific abbreviation expander or to fold the existing alias dict entries for abbreviations into preprocessing.
- **UMLS canonical cleanup is heuristic** — the `_CANONICAL_STRIP_PREFIXES`/`_SUFFIXES` list is a small curated set and will miss other generic wrappers UMLS uses. Grep the linker output on a broader eval set to expand it.
- **Numpy downgrade** from 2.2.6 → 1.26.4 (forced by nmslib). Verified Qdrant/sentence-transformers still work, but anything new that wants numpy ≥ 2 (e.g. future jax/torch bumps) will need nmslib to publish a wheel compatible with numpy 2.

## Files touched this session
- Modified: [config/base.yaml](config/base.yaml), [conversation/nodes.py](conversation/nodes.py), [conversation/state.py](conversation/state.py), [requirements.txt](requirements.txt), [retrieval/ontology.py](retrieval/ontology.py) (replaced stub), [retrieval/retriever.py](retrieval/retriever.py), [retrieval/topic_matcher.py](retrieval/topic_matcher.py).
- New: [progress_journal/2026-04-22_01-30-00_p1_p4_umls_and_observability.md](progress_journal/2026-04-22_01-30-00_p1_p4_umls_and_observability.md).

## Stop gate
Next up in the master plan is **P2 + P3**: bundled re-ingestion with grounded extraction prompt + reject list + StatPearls supplement. Cost estimate ~$16-27 (Anthropic + OpenAI embeddings) + ~1 h GPU time on the reranker warmup. This is the user-review checkpoint we agreed on — pausing here.

## Paused for conversation-quality testing (2026-04-22)

User is taking the tutor for a hands-on conversation quality pass before we authorize the expensive re-ingestion run. All P0/P1/P4 changes are landed on the working branch; the tutor is in the fully hardened state (strict groundedness + UMLS ontology expansion + per-turn grounded/ungrounded accounting).

### Next-phase queue (deferred until after quality testing)

1. **P2 — Grounded proposition extraction pass** (bundled with P3 ingestion)
   - Revised extraction prompt that rejects generic/boilerplate propositions
   - Reject-list pass over the existing proposition store before re-embed
   - Cost shared with P3 re-ingestion run

2. **P3 — StatPearls supplement** (see review above in this session)
   - Source: NCBI Bookshelf (NBK430685), ~500-800 relevant articles via E-utilities filter
   - Coverage target: closes axillary/musculocutaneous/CN VII gaps measured in Phase 1.5
   - Effort: ~2-3 days (filter script + NXML parser + dedup + ingestion integration)
   - Blocker to clear first: CC BY-NC-ND 4.0 research-use clause verification with NCBI
   - Fallback sources if StatPearls licensing blocks: Wikipedia anatomy (CC BY-SA) as second-stage fill; LibreTexts/OpenStax 2e as tertiary
   - Open question: need a dedicated OT-intervention textbook to cover the ~10-15% NBCOT slice StatPearls doesn't (ADL retraining, splinting, assessment protocols)

3. **Project finish + submission items** (to be scoped after conversation testing surfaces any remaining gaps)
   - Re-run full eval suite (Hit@K, RAG latency, groundedness_score distribution) post re-ingestion
   - Address the 15 pre-existing pytest failures (Anthropic auth, ingestion chunk-count drift, OOS threshold, prompt parity YAML)
   - UI polish + final demo prep
   - Final report / submission writeup for CSE 635

### What the user will be looking for during testing
- Groundedness: no hallucinated anatomy facts, tutor stays within locked TOC section
- UMLS linker behavior on fine-grained terms (deltoid, CN VII, brachial plexus, axillary nerve)
- Topic-lock correctness + borderline "did you mean" flow
- HyDE rescue firing on genuinely weak retrievals (new top-K-mean signal)
- Per-turn invariant violations showing up in debug panel when they should

Resume point after testing: either (a) proceed with P2+P3 bundled run if tutor quality is acceptable, or (b) address quality-testing findings first and re-queue P2+P3.
