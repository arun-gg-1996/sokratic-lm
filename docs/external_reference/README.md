# External reference — friend's testing system (received 2026-05-01)

A teammate's Socratic anatomy tutor uses a **different architecture**
(LangGraph-based, ChromaDB instead of Qdrant, `student_phase` state machine
with `learning / choice_pending / clinical_pending / topic_choice_pending`
phases instead of our `phase + assessment_turn`) but his test framework
is genuinely well-built and worth borrowing from.

## Files in this folder

| File | Lines | Purpose |
|---|---|---|
| `test_graph_state.py` | 30 | 3 unit tests verifying his GraphState TypedDict has the expected fields (`mode`, `study_active_topic`, `study_topic_count`). Pure schema check, no LLM calls. |
| `test_teacher_socratic.py` | 124 | 4 acceptance tests for his `teacher_socratic` node — drives one LLM call per test. Verifies: smoke, no concept leak at turn 0, exactly one question mark, reveal turn allows concept name. |
| `test_teach_node.py` | 100 | 4 acceptance tests for his `teach_node` — verifies failed-mastery path: smoke, `mastery_level="failed"`, `student_phase="choice_pending"`, draft reveals concept. |
| `test_full_loop.py` | 207 | 5 graph-integration scenarios with pre-seeded state. Tests routing: choice_pending+"clinical" → clinical question; clinical_pending → synthesis_assessor; choice_pending+"done" → END; topic_choice_pending+"weak" → resets to learning; step_advancer produces the A/B/C menu. |
| `run_e2e_tests.py` | **2,682** | **The big one.** Live e2e harness — hits a running backend on `localhost:8000`, drives **90+ scripted scenarios** through `POST /chat/trace`, asserts per-turn AND post-scenario state, captures trace events (Dean rejections, CRAG decisions, draft_source_node), generates a markdown report to `test.md`. |

## Scenario coverage in run_e2e_tests.py (90 scenarios)

Categories:

- **A1–A7** Cooperative correct trajectories (synapse, median nerve, action potential, reflex arc, gray matter, cerebellum, motor neuron) — 7 scenarios
- **B1–B6** Wrong-answer guarding (must not confirm wrong) — 6
- **C1–C3** IDK ladders → reveal — 3
- **D1–D4** Off-topic injection (restaurant, weather, code, greeting) — 4
- **E1–E5** Jailbreak / manipulation (demand answer, ignore instructions, roleplay, emergency, pretend-AI) — 5
- **F1–F4** Topic switching (explicit vs subtle vs meta) — 4
- **G1–G4** Edge inputs (broad opener, pronoun-only, one-word, multi-concept) — 4
- **H1–H9** Special formatting (whitespace, long input, special chars, ALL CAPS, self-correction, repeat, medical advice, deflection, text-speak) — 9
- **I1–I4** Mastery flows + A/B/C choice paths — 4
- **J1–J3** Synonyms / paraphrases — 3
- **K1–K4** Long session / misspellings / rapid / clinical jargon — 4
- **L1–L5** Concept echo / reveal grounding — 5
- **M1–M15** Choice menu A/B/C variations + ambiguous + jailbreak after mastery — 15
- **W1–W6** Weak topics flow — 6
- **R1–R5** Rapport phase — 5
- **TS1–TS10** Topic select flow — 10
- **N1–N3** Cross-cutting (JSON injection, substring collision, OOD anatomy) — 3
- **H5–H9** Extended hostile patterns — 5

## Assertion library (reusable across architectures)

`run_e2e_tests.py` defines 25+ assertion helpers, each returning `(bool, str)`:

- **Generic content checks**: `has_question`, `at_most_two_questions`, `non_empty`, `no_meta_leak` (catches "the textbook says" / "based on retrieved" / "we're at turn N"), `not_fallback_scaffold`
- **Concept-leak guarding**: `no_concept_leak(concept)` — strips generic words from the active domain (nerve, system, medial), uses stem matching with word boundaries to avoid false positives like "ulna" matching "tunnel"
- **Wrong-answer guards**: `does_not_confirm_wrong_answer(named_wrong)` — catches "Yes, the median nerve" when actual answer is ulnar
- **Reveal guards**: `reveals_concept(concept)`, `mastery_choice_menu` (A/B/C present)
- **Jailbreak guards**: `not_jailbroken(concept)` — catches compliance phrases AND leaks
- **Phase-shape guards**: `looks_like_clinical_scenario`, `looks_like_next_topic_prompt`, `looks_like_session_close`, `looks_like_rapport`, `looks_like_socratic_teach`, `is_redirect_or_chitchat`, `no_returning_session_framing`
- **Post-scenario state checks**: `weak_topics_contains(concept)`, `weak_topics_empty`, `current_concept_in(allowed)`, `current_concept_equals(expected)`

## Trace forensics

His backend exposes `POST /chat/trace` (SSE-streamed) emitting per-step events:
- `step: "concept_extraction"` — extracted concept
- `step: "retrieval"` — input + crag_decision + chunk_sources
- `step: "dean"` — input snapshot incl. `draft_source_node` (rapport_node vs teacher_socratic vs hint_error_node)

The harness pulls these out via `summarize_dean_events`, `summarize_crag`,
`summarize_concept`, `summarize_source_node` to surface diagnostic info in
the markdown report (which Dean revisions fired, what CRAG decided, which
node generated the draft).

## What this is NOT

- Not a testing FRAMEWORK like pytest — it's a hand-rolled harness with
  one async test loop and a markdown reporter. Trades framework features
  (parametrize, fixtures, parallel) for total clarity and direct control
  over backend lifecycle.
- Not a unit test suite — every scenario hits the real LLM, so it costs
  ~$3–5 per full run. Run before pushes / before paper deadline / nightly,
  not on every commit.

## How this compares to our setup

| Aspect | Friend's | Ours (Sokratic-OT) |
|---|---|---|
| Driver | `run_e2e_tests.py` (~2,700 lines, 90 scenarios) | `test.md` catalogue only — driver **not yet built** |
| Transport | `POST /chat/trace` SSE | `WS /ws/chat/{thread_id}` |
| State machine | `student_phase ∈ {learning, choice_pending, clinical_pending, topic_choice_pending}` | `phase ∈ {rapport, tutoring, assessment, memory_update}` + `assessment_turn ∈ {0,1,2,3}` |
| Reset endpoint | `POST /sessions/{id}/reset` | none — uses fresh `thread_id` per scenario |
| Vector DB | ChromaDB | Qdrant |
| Topic locking | weak_topics + free-text | TOC-grounded (RapidFuzz + UMLS) |
| Trace export | SSE per-node events | `state.debug.turn_trace[*]` in WS message_complete |

## Verdict

**This format is excellent and we should adapt it directly to our stack.**
See `progress_journal/2026-05-01_*_friend_test_system_assessment.md` for the
adaptation plan.
