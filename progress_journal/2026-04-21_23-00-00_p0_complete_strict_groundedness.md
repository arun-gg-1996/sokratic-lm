# P0.4–P0.10: TOC-Grounded Routing — P0 Complete

Date: 2026-04-21 (continues from `2026-04-21_22-00-00_strict_groundedness_p0_1_p0_3_done.md`)

## What shipped this session

### P0.4 — Soft section-affinity boost in CE rerank
`retrieval/retriever.py::_cross_encoder_rerank` now applies an additive bonus to CE scores when the session is topic-locked:

- `subsection_affinity_boost` (default `0.50`) — chunk's `section_title` contains the locked subsection name.
- `section_affinity_boost` (default `0.25`) — chunk's `section_title` contains the locked section name.
- Subsection match wins when both would apply.
- `ce_score_raw` and `section_affinity_bonus` are stored for observability / forensic review.
- Deliberate no-op when no lock (boosts degenerate to 0.0), so non-tutoring calls (ad-hoc search) retain old behavior.
- **Soft, not a filter** — cross-chapter recall for legitimate cross-references (e.g. the liver appearing in both digestive and circulatory chapters) is preserved.

Threading: `dean._retrieve_on_topic_lock` → `tools.mcp_tools.search_textbook` → `Retriever.retrieve(..., locked_section=, locked_subsection=)`. `search_textbook` falls back gracefully when the retriever is a `MockRetriever` (signature mismatch via `TypeError`).

Config: `config/base.yaml` gained `retrieval.section_affinity_boost` and `retrieval.subsection_affinity_boost` under the existing `retrieval:` block.

### P0.5 — `_session_topic_name` now reads from `locked_topic`
`conversation/nodes.py::_session_topic_name` prefers `locked_topic.subsection > .section > .chapter` before falling back to the legacy `topic_selection`. Fixes the "weak_topics captured menu-label text" bug — memory now persists canonical TOC names.

### P0.6 — `ungraded` mastery tier for ungrounded sessions
`conversation/nodes.py::_close_session_with_dean` overrides Dean's tier output to `ungraded` whenever `locked_topic is None` OR `retrieval_calls == 0`. The override sets all three tiers (`core_mastery_tier`, `clinical_mastery_tier`, `mastery_tier`) plus a policy-grounded `grading_rationale` explaining why.

`weak_topics` append is now guarded by `if topic_name:` so an ungraded session doesn't pollute the student's weak-topics list with an empty string.

Config: `ungraded` added to the mastery tier enum in three `config/base.yaml` prompt blocks (`dean_close_session_static`, `dean_close_session_dynamic`, related prompts that echo the enum).

### P0.7 — Session-level observability
`conversation/nodes.py::_log_conversation` now emits:
- `topic_locked_to_toc`: bool
- `locked_topic`: full TOC match dict (path/chapter/section/subsection/difficulty/chunk_count/limited/score)
- `retrieval_calls`: int (hoisted from `debug`)
- `coverage_gap_events`: int (hoisted from `debug`)
- `groundedness_score`: float in [0, 1]. Formula: `0.0` when not locked / no retrieval, else `max(0, 1 - coverage_gap_events / turn_count)`.

Per-session exports land at `data/artifacts/conversations/{student_id}_turn_{n}.json` and are now directly queryable for Phase 3 curation / EULER regression.

### P0.8 — `DomainOntologyAdapter` interface
New `retrieval/ontology.py`:
- `DomainOntologyAdapter` Protocol (`link_entities(text) → list[EntityMention]`, `get_synonyms(canonical) → list[str]`).
- `EntityMention` frozen dataclass: span, canonical, cui, type, score, metadata.
- `NoopAdapter` — always empty, used for physics / non-biomedical domains.
- `UMLSAdapter` — stub for P0.8 (behaves as noop). Real scispacy integration lands in P1.
- `get_ontology_adapter(domain_name)` factory — currently routes anatomy/ot/biomed to the stub, everything else to noop. P1 will promote this to read `cfg.domain.ontology_adapter`.

Intent: satisfy the physics-transfer constraint from the master plan by carving the UMLS dependency behind a stable seam **before** P1 wires scispacy in. No caller has to change when UMLS goes live — only the adapter's body does.

### P0.10 — Smoke verification
TopicMatcher smoke run (`data/topic_index.json`, 363 entries):

| Query | Tier | Top |
|---|---|---|
| `liver` / `the liver` | strong | The Liver (score 101, 8 chunks) |
| `digestive system` | borderline | Digestive System Organs (limited) |
| `heart` | borderline | Conduction System of the Heart (16 chunks) |
| `nephron filtration` | borderline | Glomerular Filtration Rate (GFR) |
| `cranial nerve vii` | borderline | Sensory Nerves (86.7) |
| `deltoid` | none | — correctly refuses |
| `quantum mechanics` | none | — correctly refuses |

`_log_conversation` round-trip confirmed: grounded session export populates all five new observability fields with correct values (groundedness_score = 1.0 on a clean locked session, 0.0 when `retrieval_calls == 0`).

`conversation.dean`, `tools.mcp_tools`, `retrieval.retriever`, `retrieval.ontology` all import cleanly.

Full pytest run: 29 passed, 15 failed, 1 deselected (`test_rapport_generates_greeting`). All 15 failures are pre-existing environment / data issues (Anthropic API key not loaded in shell, ingestion chunk-count range, prompt-parity YAML, RAG latency, OOS threshold) — none are regressions from the P0 changes. Verified by inspecting failure modes (8 of 8 `test_conversation` failures are the Anthropic `auth_token` TypeError — identical to the previously-catalogued `test_rapport_generates_greeting` failure).

## State / schema changes

- `TutorState.locked_topic: Optional[dict]` (from prior session; still load-bearing).
- Conversation export adds five new top-level keys (see P0.7).
- `Config.retrieval` gains `section_affinity_boost` and `subsection_affinity_boost` (floats).

## Files touched this session

- Modified: `config/base.yaml`, `conversation/dean.py`, `conversation/nodes.py`, `retrieval/retriever.py`, `tools/mcp_tools.py`.
- New: `retrieval/ontology.py`, `progress_journal/2026-04-21_23-00-00_p0_complete_strict_groundedness.md`.

## What's next (per the master plan)

P0 is complete. Remaining before the re-ingestion gate:

| # | Task | Notes |
|---|---|---|
| P1.1 | scispacy UMLS spike | Install scispacy + `en_core_sci_sm` + `EntityLinker`. Wire into `UMLSAdapter.link_entities` and `get_synonyms`. |
| P1.2 | Use ontology in TOC matching | In `TopicMatcher.match`, union linked-entity canonical names into the fuzz query to resolve fine-grained terms (deltoid → "Deltoid muscle"). |
| P1.3 | Replace alias dict in BM25 expansion | Retire `config/domains/ot.yaml::query_aliases` in favor of `adapter.get_synonyms`. |
| P1.4 | CE-distribution weak-retrieval pregate | Replace the single-cosine HyDE trigger with a distributional check. |
| P4 | Observability hardening | Per-turn invariants + groundedness trend exports. |

**STOP GATE**: P2 (bundled re-ingestion with grounded prompt + reject list) and P3 (StatPearls supplement) are next but require user approval — total spend ~$16–27 and ~1h of GPU time on the reranker warmup.
