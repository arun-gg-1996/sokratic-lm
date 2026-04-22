# Strict-Groundedness Architecture + Consolidated Plan

Date: 2026-04-21 (late-session)

## Why this entry

A live user session (liver topic, non-OT background) exposed that the system:
1. Shows LLM-brainstormed menu options (not corpus-grounded)
2. Accepts any free-text topic, even ones absent from the textbook
3. Teaches from parametric knowledge when retrieval drifts
4. Has no guard that the tutor's teaching subject == the locked subject

The project's core requirement is **faithful teaching from source material**. The above behaviors violate that. This entry captures the revised architecture and the full pending backlog.

## Core principle (architectural)

> Every accepted topic must be a node in the textbook's table-of-contents (or the corresponding taxonomy of any added source). If a student's request cannot be mapped to a TOC node with sufficient confidence, the system refuses with alternatives. The tutor never teaches from parametric knowledge.

## Consolidated plan

### P0 — Strict-groundedness + blockers (no re-ingestion)

| # | Task | Scope |
|---|---|---|
| P0.1 | `scripts/build_topic_index.py` — parse `data/textbook_structure.json`, drop junk (INTERACTIVE LINK, CAREER CONNECTION, Everyday Connection, `AGING AND THE`, `DISORDERS OF THE`, `...`-truncated), enrich with chunk_count via Qdrant payload filter on `chapter_title + section_title`. Output `data/topic_index.json` with `{chapter, section, subsection, difficulty, chunk_count}`. | ~45 min |
| P0.2 | Replace `teacher.draft_topic_engagement` with TOC-grounded matcher: student free-text → fuzzy (RapidFuzz) + semantic (dense embed over subsection names) → top-K TOC candidates. Lock on single high-confidence match; show top-3 as cards on borderline; refuse with closest alternatives if none match. | ~1.5 hr |
| P0.3 | Remove teacher parametric fallback. If retrieval returns insufficient content for a locked TOC node, turn emits "I don't have strong coverage here — try [X, Y, Z]" instead of LLM-drafted teaching. | ~30 min |
| P0.4 | Retrieval: add **soft section-affinity boost** in CE rerank when a topic is locked (keeps cross-chapter breadth but prefers in-section chunks). No hard payload filter. | ~30 min |
| P0.5 | Fix `_session_topic_name` (nodes.py:440) — return the *locked TOC node name*, not raw `topic_selection`. Also write `weak_topics` using TOC node names only. | ~20 min |
| P0.6 | Grading guard — if no TOC lock was ever established OR session had zero grounded retrieval, emit `ungraded` tier, not `developing`. | ~20 min |
| P0.7 | Observability — session-level flags in export: `topic_locked_to_toc`, `retrieval_calls`, `coverage_gap_events`, `ungrounded_turns`. Log every "no TOC match" event to a structured file for Phase 3 curation. | ~30 min |

**P0 total: ~4.5 hr, zero re-ingestion, zero API spend.**

### P1 — De-rule-ification (replace rules/dicts with principled components)

| # | Task | Scope |
|---|---|---|
| P1.1 | **UMLS spike via scispacy** — `scispacy.linking.EntityLinker` bundles a UMLS subset; no UTS account needed. Verify coverage on 12 alias-dependent queries: "wrist drop → radial", "empty can test → supraspinatus", "CN VII → facial", "jobe test → supraspinatus", etc. | ~1 hr |
| P1.2 | If spike passes: integrate scispacy EntityLinker into (a) student-text → TOC matching step and (b) retrieval BM25 expansion. Delete `query_aliases` dict. | ~1.5 hr |
| P1.3 | Replace `_classify_opt_in` prefix rules with embedding-similarity or tiny Haiku classifier. | ~45 min |
| P1.4 | Replace `_is_weak_retrieval` cosine threshold with CE score-distribution check (gap between top-1 and top-5). | ~30 min |

### P4 — Observability (elevated above P2/P3 because ingestion is expensive/irreversible)

| # | Task |
|---|---|
| P4.1 | Hard assertion — tutoring turn MUST have non-empty `retrieval_query` OR explicit `skip_reason`. |
| P4.2 | Session-export `groundedness_score` — fraction of tutoring turns that ran on retrieved content. |
| P4.3 | Per-turn invariant checks logged to `turn_trace`. |
| P4.4 | "0 retrieval calls across N tutoring turns" → loud warning in export and UI. |

### P2 + P3 — Re-ingestion + content supplement (bundled, requires user go-ahead)

Single ingestion run combining:

**Prerequisites (code-only, applied before the run):**
- 3.1 Grounded proposition prompt ([config/base.yaml:73-79](config/base.yaml#L73))
- 3.2 Reject-list parser
- 3.3 Table → multi-proposition decomposition
- Topic-index pipeline integrated into ingestion (rebuild `topic_index.json` as part of ingest)
- StatPearls source added: ~200 curated articles covering upper-limb neuroanatomy, shoulder/elbow/wrist/hand, cranial nerves, OT-relevant pathologies
- `source_id` payload field on chunks (`openstax` / `statpearls`)

**Run:**
- `.venv/bin/python -m ingestion.propositions` (~2–3 hrs, ~$15–25 Claude)
- `.venv/bin/python -m ingestion.index` (~15 min, ~$1–2 OpenAI)

**Validation:**
- `scripts/eval_rag_expanded.py` — Hit@K, MRR, OOD
- Live conversation test on both a covered and an uncovered topic (coverage-gate must fire on the latter)

## Sequencing

1. P0 (4.5 hr) — ship strict-groundedness end-to-end on current index
2. P1.1 spike (1 hr) — decide UMLS fate
3. P1.2–P1.4 (2.5 hr) — principled replacements
4. P4 (~2 hr) — observability hardening
5. **STOP — user approval required** for P2/P3 (cost + time)
6. P2+P3 bundled ingestion
7. Post-ingest eval + journal entry

## Budget snapshot

- Cumulative spent: ~$1.85
- Phase 1–4 estimated: ~$0.30 (mostly UMLS spike API calls, observability tests)
- P2+P3 bundled ingestion: ~$16–27

## User-confirmed decisions

1. **Borderline match** — show top-3 "Did you mean…?" cards; refuse-with-alternatives on no-match.
2. **Chunk-count threshold `>= 1`** — every TOC node with at least one chunk appears in the menu. Nodes with chunk_count 1–2 get a "limited coverage" tag in UI. Nothing silently hidden.
3. **Parametric fallback removed** — tutor refuses when retrieval thin.
4. **Alias dict retained during transition** — delete only after UMLS spike confirms equivalent coverage.
5. **Difficulty tag shown on cards** (`easy` / `moderate` / `hard`).
6. **Ungraded tier** — new value in grading enum for sessions that never grounded.

## Domain-transferability constraint

System must transfer from anatomy (OpenStax A&P + StatPearls) to physics (future) without re-architecting. That means:

**Transfers as-is:**
- TOC parser, topic index, matcher, soft section-affinity boost
- Coverage gate + refusal-with-alternatives
- Grading-guard logic, observability hooks

**Pluggable (domain-specific):**
- **Junk-filter patterns** — moved to `config/domains/{domain}.yaml` under `topic_index.junk_patterns`. Anatomy: `INTERACTIVE LINK`, `CAREER CONNECTION`, `Everyday Connection`, `HOMEOSTATIC IMBALANCES`, etc. Physics (future): `WORKED EXAMPLE`, `TRY IT YOURSELF`, etc.
- **Entity linker** — abstracted behind `DomainOntologyAdapter` interface (`link_entities(text)`, `get_synonyms(concept)`). Implementations: `UMLSAdapter` (scispacy, for anatomy/medical), `NoopAdapter` (physics initially; can upgrade to curated dict later).
- **Difficulty rater prompt** — already domain-configured.
- **Query aliases** — already domain-scoped.

## Out of scope for this plan (explicitly deferred)

- Building our own topic taxonomy from chunk clustering (TOC is already good enough)
- Gray's Anatomy or Wikipedia as additional sources (StatPearls is the priority)
- UMLS Metathesaurus full install (scispacy bundle is sufficient for spike)
