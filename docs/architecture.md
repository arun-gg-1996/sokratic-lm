# Sokratic Architecture (post 2026-04-30 fixes)

End-to-end architecture of the Socratic tutoring system.
Companion to [`README.md`](../README.md) and [`EVALUATION_FRAMEWORK.md`](EVALUATION_FRAMEWORK.md).

---

## 1. High-level flow

```
                    ┌──────────────────┐
                    │   Browser (UI)   │
                    │  React + Vite    │
                    └────────┬─────────┘
                             │ HTTP (REST) / WS
                    ┌────────▼─────────┐
                    │   FastAPI app    │
                    │  backend/api/*   │
                    │  - /api/session  │
                    │  - /api/chat WS  │
                    │  - /api/mastery  │
                    │  - /api/memory   │
                    └────────┬─────────┘
                             │ invokes
                    ┌────────▼─────────┐
                    │   LangGraph      │
                    │  (TutorState)    │
                    └────────┬─────────┘
                             │
   ┌──────────┬──────────────┼──────────────┬──────────┐
   │          │              │              │          │
┌──▼──┐  ┌────▼────┐    ┌────▼────┐    ┌────▼────┐  ┌──▼─────┐
│ Rap │→ │  Dean   │ ←→ │ Teacher │ →  │ Assess  │→ │ Memory │
│port │  │  node   │    │ (LLM)   │    │  node   │  │ Update │
└─────┘  └────┬────┘    └─────────┘    └─────────┘  └────────┘
              │ uses
   ┌──────────┼──────────┐
   │          │          │
┌──▼──┐  ┌────▼────┐ ┌───▼────┐
│Topic│  │Reached  │ │Counter │
│Match│  │  Gate   │ │ system │
└──┬──┘  └────┬────┘ └────────┘
   │          │
┌──▼──────────▼──┐
│  Retriever     │  ← Qdrant + BM25 + RRF + Cross-encoder rerank
└────────────────┘
```

LangGraph is the state machine. Every node returns a partial `TutorState`
update; LangGraph merges. Phases are:

```
rapport → tutoring → assessment → memory_update
```

---

## 2. State (`conversation/state.py`)

Single shared `TutorState` TypedDict flows through every node. Key fields:

| Field | Purpose |
|---|---|
| `student_id` | identity (also keys mem0 + mastery_store) |
| `phase` | one of `rapport / tutoring / assessment / memory_update` |
| `messages` | full message list (`role`, `content`, `phase`) |
| `locked_topic` | `{path, chapter, section, subsection, ...}` once topic locks |
| `locked_question` | the anchor question Dean asks |
| `locked_answer` | **1–5 word concept anchor** (Round 4 two-tier change) — used by reached gate |
| `locked_answer_aliases` | 4–5 alternate phrasings — used by gate Step A overlap |
| `full_answer` | **rich/multi-component textbook answer** — used by mastery scorer + assessment |
| `topic_just_locked` | True for one turn after lock — triggers ack message (Change 2) |
| `hint_level` | 0 = ack just emitted; 1–3 = active hint; max+1 = exhausted → assessment |
| `student_state` | `correct / partial_correct / incorrect / question / irrelevant / low_effort` |
| `student_reached_answer` | gate output — only flips True when gate fires |
| `student_answer_confidence` / `student_mastery_confidence` | telemetry; not used to gate |
| `help_abuse_count` | consecutive `low_effort` turns; resets on engagement |
| `off_topic_count` | consecutive off-DOMAIN turns (irrelevant + non-tangent); resets on engagement |
| `total_low_effort_turns` / `total_off_topic_turns` | non-resetting telemetry; mastery scorer reads |
| `clinical_low_effort_count` / `clinical_off_topic_count` | clinical-phase counters (Change 5.1) |
| `core_mastery_tier` / `clinical_mastery_tier` / `mastery_tier` | tier strings: `proficient / developing / needs_review / not_assessed` |
| `debug.*` | per-turn trace, all_turn_traces, hint_plan, cost/token counters |

---

## 3. Nodes

### 3.1 `rapport_node` (`conversation/nodes.py`)
- Fires once at session start.
- Reads mem0 (filtered by `student_id` + `category=session_summary`) and produces a personalised greeting.
- For pre-locked sessions (revisit flow), the dean's `_apply_prelock` runs BEFORE the graph and sets `topic_just_locked=True`; rapport then renders the deterministic ack message inline as a second tutor turn (so the user lands directly on the question).

### 3.2 `dean_node` (`conversation/dean.py`)
The largest module. Runs every tutoring turn. Workflow per turn:

1. **Topic resolution** (free-text path only):
   - `_setup_call` LLM classifies student message intent
   - Topic matcher (`retrieval/topic_matcher`) returns top-K matches with `tier` (strong / borderline / none)
   - **Strong tier (score ≥ 78, gap ≥ 10)** → auto-lock
   - **Borderline tier** → present cards UI (3 alternatives)
   - **Coverage gate** rejects topics with insufficient in-section chunks
2. **Lock anchors** (once after topic_confirmed): `_lock_anchors_call` produces `{locked_question, locked_answer, locked_answer_aliases, full_answer, rationale}` from chunks (filtered to subsection per Round 4 fix)
3. **Hint plan** (once): `_hint_plan_call` produces 3-step progression (currently progressive directness; future Change 6 = dimensional)
4. **Counter logic**:
   - help_abuse / off_topic / total_* updated based on student_state + off-domain check
   - Strikes 1–3 → warning brief injected into `dean_critique`
   - Strike 4 (help_abuse) → hint_level advances + LLM-narrated transition
   - Strike 4 (off_topic) → session terminates with farewell + mastery=not_assessed
5. **Reached gate** (`reached_answer_gate`):
   - Step A: token-overlap of student message vs `locked_answer + aliases` (with hedge filter)
   - Step B (LLM): paraphrase check requiring verbatim quote from student message
   - Returns `{reached, evidence, path}`
6. **Teacher draft**:
   - For lock-just-happened turn → deterministic ack message (Change 2)
   - Otherwise → `teacher.draft_socratic` LLM call with `dean_critique`, `hint_plan_active`, `hint_level`
7. **Quality check**:
   - `_deterministic_tutoring_check`: question count, sentence count, reveal regex, repetition similarity
   - `_quality_check_call` LLM (EULER 4): question_present, relevance, helpful, no_reveal
   - On fail: revised_teacher_draft applied OR `_dean_fallback`
8. **Hint indicator** post-process: appends `— Hint X of Y —` (or "Last hint" at max) when applicable

### 3.3 `assessment_node` (`conversation/nodes.py`)
- Triggered when `student_reached_answer=True` OR hints exhausted.
- `assessment_turn` state machine: 0=ask opt-in, 1=parse yes/no, 2=clinical loop (max 3 turns), 3=done
- **Clinical counters (Change 5.1)**: `clinical_low_effort_count` + `clinical_off_topic_count` cap at 2 → end clinical phase only (preserves tutoring credit)
- `clinical_mastery_tier=not_assessed` set in 3 branches: declined opt-in, didn't reach, clinical cap

### 3.4 `memory_update_node` (`conversation/nodes.py`)
- Calls `memory_manager.flush()` writing 5 mem0 categories (session_summary, misconceptions, open_thread, topics_covered, learning_style_cues) — filtered by `student_id`
- Calls `mastery_store.score_session_llm` with full TutorState — produces `{mastery, confidence, rationale}` blended via EWMA into `data/student_state/{student_id}.json`
- The mastery scorer reads `total_low_effort_turns` + `total_off_topic_turns` (Change 4.7) so session-wide patterns drive the score

---

## 4. Reached gate — the critical path (Change 1)

```
reached_answer_gate(state, student_msg)
  │
  ├── Step A: token overlap (deterministic, fast)
  │     ├── Hedge filter ("idk", "not sure", etc.) → fail-fast → Step B
  │     ├── For each in [locked_answer] + aliases:
  │     │     ├── Tokenize msg + candidate (drop stopwords)
  │     │     └── If all candidate tokens ⊆ msg tokens → reached=True, path="overlap"
  │     └── No match → Step B
  │
  └── Step B: LLM paraphrase check (Haiku call)
        ├── Prompt requires: "quote the student verbatim"
        ├── Parse JSON: {reached, evidence, rationale}
        └── Post-validate: evidence MUST be substring of student message
              ├── Pass → reached=True, path="paraphrase"
              └── No quote → reached=False, path="llm_no_quote"
```

Gate path is logged per-turn in `state.debug.turn_trace` with the
`dean.reached_answer_gate` wrapper for downstream eval analysis.

---

## 5. Counter system — gamified gating (Change 4)

Two resetting counters + two non-resetting telemetry counters, with a third pair for clinical phase.

```
Per turn, after _setup_call returns student_state:

  if student_state == "low_effort":
    help_abuse_count += 1
    total_low_effort_turns += 1
  elif student_state == "irrelevant" AND off_domain_judgment(msg):
    off_topic_count += 1
    total_off_topic_turns += 1
  else:
    help_abuse_count = 0  (engaged → reset)
    off_topic_count = 0

Then:
  Strikes 1: no warning
  Strikes 2-3: warning brief injected into dean_critique (LLM phrases the warning)
  Strike 4 (help_abuse): hint_level += 1 + LLM-narrated transition
  Strike 4 (off_topic): session terminates with farewell narration
                       core/clinical/mastery_tier all set to "not_assessed"
                       hint_level → max_hints+1 (forces route to memory_update)
```

Off-domain detection is keyword-based (regex against off-domain markers
like "vape", "weed", "sex", profanity). Domain-tangential queries (e.g.
"actually, what about veins?" when locked on SA node) are routed via
`_exploration_judge` → exploration retrieval, NOT counted as off-topic.

---

## 6. Eval framework (`evaluation/quality/`)

Two-tier scoring system:

```
Primary (headline / thesis):
  EULER       — question_present, relevance, helpful, no_reveal (per turn)
  RAGAS       — context_precision, context_recall, faithfulness,
                answer_relevancy, answer_correctness (session-level)

Secondary (diagnostic, 10 dimensions):
  TLQ  topic-lock quality
  RRQ  RAG retrieval quality
  AQ   anchor quality
  TRQ  tutor response quality (incorporates EULER)
  RGC  reached-gate correctness
  PP   pedagogical progression
  ARC  answer-reach vs step-correctness (Change 3 focus)
  CC   conversation continuity (invariants)
  CE   cost & efficiency
  MSC  mastery scoring calibration

Penalties (separate channel; any Critical = failed_critical_penalty):
  LEAK_DETECTED                  — locked_answer revealed in tutor msg
  INVARIANT_VIOLATION            — ungrounded tutoring turn (one per session)
  FABRICATION_AT_REACHED_FALSE   — tutor falsely confirmed reach
  OFF_TOPIC_DRIFT_NOT_REDIRECTED — off-topic engaged with
  MASTERY_OVERCLAIM              — mastery > 0.7 but reached=False
  HELP_ABUSE_RESPONDED_WITH_ANSWER
```

Pipeline:
1. `evaluation.quality.schema.load_session()` reads JSON → `SessionView`
2. `deterministic.compute_all()` runs all non-LLM signals
3. `llm_judges.evaluate_*` runs 3–4 batched LLM calls (Haiku, ~$0.005/session)
4. `primary.assemble_*` builds EULER + RAGAS dicts
5. `dimensions.assemble_dimensions` builds the 10 dim dicts
6. `penalties.compute_penalties` runs all penalty checks (Round 4: INVARIANT dedup)
7. `penalties.compute_verdict` → `passed | warning | failed_threshold | failed_critical_penalty`

CLI: `scripts/score_conversation_quality.py SESSION.json [-o OUT.json] [--no-llm] [--skip-anchor]`

---

## 7. Memory + mastery

### mem0 (Qdrant-backed)
- Per-student namespace via metadata filter (`student_id`)
- 5 categories per session (`memory/memory_manager.py:flush`):
  - `session_summary` — tutor's wrap-up
  - `misconceptions` — student's wrong attempts
  - `open_thread` — what they didn't reach
  - `topics_covered` — locked_topic.path strings
  - `learning_style_cues` — observed engagement patterns
- Read at rapport_node (filtered by category) — produces personalised greeting

### mastery_store (file-backed)
- Path: `data/student_state/{student_id}.json`
- Per-concept records: `{path: {mastery, confidence, sessions, last_seen, last_outcome, last_rationale}}`
- EWMA blend: `blended = 0.6 * new_score + 0.4 * prior_mastery`
- Badge threshold: mastered iff `mastery ≥ 0.80 AND confidence ≥ 0.60`
- Scored by `score_session_llm` (Round 4 fix: reads `full_answer`; Change 4.7: reads telemetry counters; Change 3C: strict student-utterance attribution rules)

---

## 8. Retrieval stack (`retrieval/`)

```
query
  ├── _RETRIEVAL_NOISE_PATTERNS strip (deterministic preprocessing)
  ├── Qdrant dense search (top-K = 20)
  ├── BM25 sparse search (top-K = 20)
  ├── RRF merge (k = 60)
  ├── Parent chunk window expansion (surrounding sentences from chunks_openstax_anatomy.jsonl)
  ├── Cross-encoder rerank (MedCPT) → top-7
  └── return chunks: [{text, score, chapter, section, subsection_title, page}]
```

### Topic matcher (`retrieval/topic_matcher.py`)
- Loads `data/processed/topic_index.json` (chapter → section → subsection)
- Each TOC node has metadata: difficulty, chunk_count, limited (whether retrieval returns enough chunks)
- `match(query, k)` returns `MatchResult(query, tier, matches)`:
  - `tier="strong"`: top.score ≥ STRONG_MIN (78) AND (top.score - second.score) ≥ STRONG_GAP (10)
  - `tier="borderline"`: matches present but no clear winner
  - `tier="none"`: no in-corpus topics matched
- `sample_diverse(k, min_chunk_count, exclude_paths)` returns alternatives for the cards UI

---

## 9. Frontend (`frontend/`)

React + Vite + TailwindCSS. Key components:

```
src/
  ├── routes/          — pages (chat, mastery, etc.)
  ├── stores/          — Zustand stores (sessionStore, userStore)
  ├── api/client.ts    — REST client (start session, mastery, memory)
  ├── hooks/
  │   ├── useSession.ts    — bootstrap + WS connection + auto-send for revisit
  │   └── useWebSocket.ts  — WS message handling (stream, activity, complete)
  └── components/
      ├── layout/Sidebar.tsx        — counters, locked-topic collapsible (Round 4)
      ├── chat/Chat.tsx
      ├── chat/ActivityFeed.tsx     — live activity log (per-turn step indicators)
      └── account/MemoryDrawer.tsx  — per-session memory entries
```

WS messages from backend:
- `stream_chunk` — incremental tutor response text
- `stream_reset` — tells frontend to clear streaming buffer (when QC rewrites)
- `activity` — `fire_activity()` events (status feed during turn)
- `message_complete` — final tutor message + `pending_choice` + `debug` payload

---

## 10. Configuration (`config/base.yaml`)

Big YAML containing:
- `models`: per-role model selection (haiku for teacher/dean/evaluator; sonnet for propositions/vision)
- `retrieval`: top-K values, RRF k, cross-encoder model
- `session`: max_turns, max_hints, clinical_max_turns
- `dean`: thresholds (`help_abuse_threshold=4`, `off_topic_threshold=4`, `clinical_strike_threshold=2`, `max_teacher_retries=2`, `adaptive_rag_enabled=true`)
- `thresholds`: `reached_answer_confidence=0.72` (legacy, kept for telemetry only), `repetition_similarity=0.9`
- `paths`: artifacts/data/processed locations
- `prompts`: ALL system prompts (teacher, dean, mastery_scorer, etc.)

Plus `config/eval_prompts.yaml` for the 4 batched evaluator prompts.

Config loader (`config.py`):
- Deep-merges `base.yaml` with `domains/{SOKRATIC_DOMAIN}.yaml` (default `ot`)
- Optionally loads `eval_prompts.yaml` into `cfg.eval_prompts.*`

---

## 11. Key code paths to know

| What you want to change | Where |
|---|---|
| Reached gate logic | `conversation/dean.py:reached_answer_gate` |
| Topic acknowledgement message | `conversation/dean.py:_build_topic_ack_message` |
| Counter thresholds | `config/base.yaml:dean.help_abuse_threshold/off_topic_threshold/clinical_strike_threshold` |
| Teacher prompt | `config/base.yaml:teacher_socratic_static + _delta` |
| Lock prompt | `config/base.yaml:dean_lock_anchors_static` |
| Mastery scorer prompt | `config/base.yaml:mastery_scorer_static + _dynamic` |
| Eval framework dimensions | `evaluation/quality/dimensions.py` |
| Eval framework penalties | `evaluation/quality/penalties.py` |
| Frontend sidebar counters | `frontend/src/components/layout/Sidebar.tsx` |
| Topic matcher thresholds | `retrieval/topic_matcher.py:STRONG_MIN/STRONG_GAP` |

---

## 12. Recent decision history (compressed)

See [`docs/SESSION_JOURNAL_2026-04-30.md`](SESSION_JOURNAL_2026-04-30.md) for full decision log of the 2026-04-30 work session.

Key decisions captured there:
- Hint design: hybrid dimensional (deferred — Change 6)
- Chunk window: don't increase (precision is the bottleneck, not recall)
- CRAG: deferred (HyDE rescue is current proxy)
- Sonnet vs Haiku A/B: Nidhi runs (recommend dean → Sonnet)
- Vision flow: Sonnet 4.6 (Haiku doesn't support vision)
