# P0.1–P0.3: TOC-Grounded Topic Routing — Done

Date: 2026-04-21 (continued from 2026-04-21_20-00-00 plan)

## What shipped this session

### P0.1 — Topic index builder (`scripts/build_topic_index.py`)
- Walks `data/textbook_structure.json` → emits `data/topic_index.json`.
- Enriches every TOC leaf with `chunk_count` from `data/processed/chunks_ot.jsonl` by normalizing the chunk schema (chunks encode `section + " — " + subsection` in `section_title` with a section-number prefix; structure keys are clean names).
- Junk filter + limited-coverage tag are domain-configurable in `config/domains/ot.yaml` under `topic_index.{junk_patterns, junk_suffixes, min_chunk_count, limited_chunk_threshold}`. This completes the "Move junk_patterns to domain config" task from the plan.
- Registered new `Config.topic_index` section in `config.py`.
- Output: 363 chunk-backed topics retained, 202 junk nodes dropped, 220 empty nodes dropped, 93 flagged `limited` (≤ 2 chunks).

### P0.2 — TOC-grounded topic matcher (`retrieval/topic_matcher.py`)
- `TopicMatcher.match(query)` returns `MatchResult{tier, matches}` with three tiers:
  - `strong` — single unambiguous hit (top score ≥ 90 AND gap to second ≥ 10)
  - `borderline` — 2–5 plausible candidates (60 ≤ top < 90)
  - `none` — nothing above 60
- Scoring via `rapidfuzz.fuzz.token_set_ratio` across subsection/section/chapter; small bias to subsection-level hits (most specific).
- `sample_diverse(n)` samples across distinct chapters for the refusal-with-alternatives path.
- Added `rapidfuzz==3.10.1` to `requirements.txt`.

### Dean run_turn rewired (`conversation/dean.py`)
Replaced the LLM-brainstormed options flow with a strict-groundedness gate:
1. Student free-text → `TopicMatcher.match`.
2. `strong` → auto-lock to the TOC node; rewrite latest student message to canonical label; proceed to retrieval + anchor lock.
3. `borderline` → present top-3 TOC matches as `"Did you mean …?"` cards.
4. `none` → refuse with diverse starter topics (`"I don't see 'X' covered in the textbook yet"`).
- Card-to-TOC mapping persisted in `pending_user_choice.topic_meta` so a numeric pick resolves back to the full TOC entry (path/difficulty/chunk_count/limited).
- New `locked_topic` field on `TutorState` stores the grounded match (for P0.5 `_session_topic_name`, P0.6 grading guard, P0.7 observability).

### P0.3 — Teacher `draft_topic_engagement` deleted
- The LLM-brainstormed options path is gone from `conversation/teacher.py`; option generation is now a pure TOC lookup.
- The "refuse when retrieval thin" portion of P0.3 (teacher parametric fallback on coverage gap) is NOT yet shipped — see Pending below.

## Verification

- `.venv/bin/python -m scripts.build_topic_index` → 363 topics, "The Liver" = 8 chunks, 93 limited.
- Matcher smoke test: `liver`/`the liver` → strong→`The Liver`; `heart` → borderline (3 heart-related subsections tied); `brachial plexus`/`deltoid`/`cn vii` → none (see known gap below); `quantum mechanics` → none.
- Pre-existing test failures (`test_prompt_parity`, `test_ingestion.*`, `test_conversation::test_rapport_generates_greeting` due to missing API key) confirmed present on `main` before my changes via `git stash` — no regressions introduced.
- `conversation.dean` imports cleanly.

## Known gap (expected, covered by P1)

Fine-grained anatomy terms that live *inside* chunks but aren't TOC node names ("deltoid", "brachial plexus", "cn vii", "wrist drop") currently route to `none` and get the starter-alternatives path. This is the principled-replacement target for P1 (UMLS via scispacy). The current alias dict (`config/domains/ot.yaml` `query_aliases`) still works for retrieval-time expansion but isn't consulted at TOC-match time yet — deliberate, keeps the matcher pure so P1 can replace the lookup cleanly.

## Pending P0 work

| # | Task | Notes |
|---|---|---|
| P0.3 (remainder) | Coverage gate after `_retrieve_on_topic_lock`: if `retrieved_chunks` empty OR top cosine < `ood_cosine_threshold`, refuse and unlock. | Architecturally critical — this is what stops the tutor from teaching off-topic when retrieval drifts (the liver→spinal cord bug). |
| P0.4 | Soft section-affinity boost in CE rerank using `locked_topic.section`/`subsection`. | No hard filter — preserves cross-chapter recall for legitimate cross-references. |
| P0.5 | `_session_topic_name` + `weak_topics` must read from `state["locked_topic"]` not `topic_selection`. | Fixes the "weak_topics captured menu-label text" bug. |
| P0.6 | New `ungraded` mastery tier when `locked_topic is None` OR `retrieval_calls == 0`. | Add to `dean_close_session_static` enum + memory plumbing. |
| P0.7 | Export `topic_locked_to_toc`, `groundedness_score`, `coverage_gap_events`, `ungrounded_turns` in session export. | Observability for Phase 3 curation. |
| P0.8 | `DomainOntologyAdapter` interface (`link_entities`, `get_synonyms`) with `UMLSAdapter` (scispacy) and `NoopAdapter` stubs. | Physics-transfer constraint. |
| P0.10 | Live test: liver (covered) and a deliberately uncovered topic. | End-to-end validation of the coverage gate. |

P1 (UMLS spike + alias-dict retirement), P4 (observability hardening), and P2/P3 (bundled re-ingestion) are unchanged from the master plan.

## State/schema changes

- `TutorState.locked_topic: Optional[dict]` — new field, None when no TOC lock.
- `pending_user_choice["topic_meta"]: dict[str, dict]` — card-label → TOC entry map, enables deterministic card-pick resolution.

## Files touched

- New: `scripts/build_topic_index.py`, `retrieval/topic_matcher.py`, `data/topic_index.json`, `progress_journal/2026-04-21_22-00-00_strict_groundedness_p0_1_p0_3_done.md`.
- Modified: `config.py`, `config/domains/ot.yaml`, `conversation/dean.py`, `conversation/state.py`, `conversation/teacher.py`, `requirements.txt`.
