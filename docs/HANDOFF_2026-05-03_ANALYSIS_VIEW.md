# Handoff — Analysis View Build + Pre-Demo Punch List

**Date:** 2026-05-03
**For:** fresh Claude session with zero prior context
**Author:** prior Claude session (`claude-opus-4-7[1m]`) working with user
**Read this first.** Then read [PRE_DEMO_ISSUES.md](PRE_DEMO_ISSUES.md). Then read [AUDIT_2026-05-02.md L29-L34, L37](AUDIT_2026-05-02.md). Then ask the user where to start.

---

## 0. Hard rules — do not violate

1. **Do NOT implement code without explicit per-item go-ahead from the user.** They've said "do not start implementation" multiple times. Even when in auto mode, no code changes happen on these issues until they say "go" on a specific item.
2. **NO templated reply fallbacks.** When LLM calls fail, render an explicit error card in the UI (see M-FB). Templated fallback strings mask failures and are explicitly forbidden.
3. **Work in the parent repo path** at `/Users/arun-ghontale/UB/NLP/sokratic`. There IS a worktree at `.claude/worktrees/serene-proskuriakova-b246c8`, but it is STALE (HEAD `4577ef4`, predates recent UX/B2/retriever fixes). All editable docs (PRE_DEMO_ISSUES.md, etc.) live in the parent.
4. **Use ast-editor MCP for `.py`, `.ts`, `.tsx` edits.** Per global CLAUDE.md instructions. Use rmd-editor for `.rmd`. Bash `rtk proxy <cmd>` for low-token shell ops.
5. **User wants verbose docs but concise replies.** Don't write walls of text in chat. Save the long form for files.
6. **No emoji unless user asks for them.** No comments in code unless they explain a non-obvious WHY.
7. **⚠ Pattern to watch: Claude can introduce regressions during multi-track work.** M6 (per-turn retrieval bug) is a concrete example — a prior Claude session built a new code path (per-turn retrieval) that mirrored an existing one (lock-time retrieval) but forgot to port the guard. When you ship code, especially in `conversation/nodes_v2.py`, `dean_v2.py`, `assessment_v2.py`, ASK: "is there a guard on a similar path elsewhere that I need to mirror here?" Don't trust that prior tracks set up everything correctly.

---

## 1. The project in 60 seconds

- **What:** A Socratic tutoring system over an Anatomy & Physiology textbook (Open Stax). Single domain.
- **Architecture (v2 stack, currently shipped):** Dean (planner LLM) + Teacher (writer LLM) split. Dean produces a `TurnPlan` with mode/tone/scaffolding hint; Teacher writes the message. Phase machine: rapport → tutoring → assessment (incl. clinical bonus) → memory_update → exit.
- **Stack:** Python backend (FastAPI + LangGraph). React frontend (Vite + TypeScript). Qdrant for retrieval. SQLite for sessions/mastery. mem0 for student observations.
- **Eval:** Programmatic conversations via `scripts/run_eval_chain.py` per student profile (S1 strong, S2 moderate, S3 weak, S4 overconfident, S5 disengaged, S6 anxious).
- **Demo:** single-user; concurrency issues are out of scope for demo.

---

## 2. Where things live

### Top-level dirs

| Dir | Purpose |
|---|---|
| `conversation/` | v2 conversation engine — Dean, Teacher, phase nodes, turn planning |
| `memory/` | `memory_manager.py`, `mem0_safe.py`, `mastery_store.py`, `sqlite_store.py` |
| `retrieval/` | ChunkRetriever, topic_mapper, topic_matcher |
| `backend/` | FastAPI app — `backend/api/` has chat, memory, mastery, session, vlm, users |
| `frontend/src/` | React app — `routes/`, `components/`, `stores/`, `hooks/`, `api/`, `types/` |
| `scripts/` | Eval harness, ingestion, scoring scripts |
| `data/artifacts/eval_run_18/` | Latest eval output JSONs (per-session) |
| `docs/` | All design docs and handoffs |

### Key files for each issue

| Issue | Files to read |
|---|---|
| **M1 lifecycle** | `conversation/nodes_v2.py:77-484`, `conversation/preflight.py:156-405`, `conversation/teacher_v2.py:120-220`, `conversation/assessment_v2.py:128-394`, `conversation/state.py:19-89`, `conversation/edges.py:44-54` |
| **M2 memory drawer** | `memory/memory_manager.py:132-229`, `memory/mem0_safe.py:136-189`, `memory/persistent_memory.py:70-154`, `backend/api/memory.py:87-129`, `frontend/src/components/account/MemoryDrawer.tsx` |
| **M3 topic resolver** | `retrieval/topic_mapper_llm.py:139-182`, `conversation/topic_lock_v2.py:39-159`, `conversation/state.py:200`, `frontend/src/components/cards/TopicCard.tsx` |
| **M4 mastery → start** | `frontend/src/routes/MasteryView.tsx:364-388`, `frontend/src/hooks/useSession.ts:110-124`, `backend/api/session.py` |
| **M5 analysis view** | `frontend/src/routes/MasteryView.tsx`, `frontend/src/routes/SessionOverview.tsx` (currently UNRELATED), `backend/api/mastery.py:297-463`, `memory/sqlite_store.py:398-441` |
| **M-FB fallback removal** | See full audit list in PRE_DEMO_ISSUES.md M-FB section |

### Specs that already exist (read these BEFORE designing anything new)

- **[docs/AUDIT_2026-05-02.md](AUDIT_2026-05-02.md)** — the master design doc. Especially:
  - **L29** Mastery page layout
  - **L30** Subsection row content
  - **L31** Revisit Chat / Analysis Mode (the analysis view spec — see M5 below)
  - **L32** Analysis Mode Summary Card
  - **L33** New Chat From My Mastery Click (relates to M4)
  - **L34** UI Design Pass
  - **L37** Analysis Mode Across Multiple Prior Sessions
- **[docs/PRE_DEMO_ISSUES.md](PRE_DEMO_ISSUES.md)** — the running punch list with M-series entries
- **[docs/EVAL_RESUME_JOURNAL.md](EVAL_RESUME_JOURNAL.md)** — eval state and runbook
- **[docs/DEMO_SCRIPT_VERIFIED.md](DEMO_SCRIPT_VERIFIED.md)** — the demo flow we're protecting

---

## 3. The full punch list (what needs to happen)

### Q-series (older, from prior sessions)

| ID | Status | Summary |
|---|---|---|
| Q1 | confirmed real | `_classify_opt_in` is regex — replace with Haiku call. ~30 min. |
| Q2 | confirmed real | Dean's hint_text leaks into Teacher's reply. Designed verify-loop unshipped. ~1-2 hrs. |
| Q3 | **SUPERSEDED by M1** | 3-mode close prompt → 1 lifecycle-aware close in M1 |
| Q4 | **DEFERRED** | v2 scoring run — wait until UI + flow are done + re-simulated |

### M-series (decided 2026-05-03)

| ID | Summary | Estimate |
|---|---|---|
| **M1** | Lifecycle redesign — auto-end vs explicit-exit. Option B exit modal (3 buttons: End & save / End without saving / Cancel). LLM-written goodbye that uses conversation history (close modes added to `_MODES_USING_HISTORY`). | 3-5 hrs |
| **M2** | Memory drawer empty after a complete session. Diagnostic-first: read `state.debug.turn_trace` for `wrapper:"mem0_write"` in latest session JSON before fixing anything. May be downstream symptom of M1. | unknown |
| **M3** | Topic resolver re-suggests rejected subsections. Pass `rejected_topic_paths` (state field already exists) into `_map_topic`. Cap-fallback should rerank for original query, not random chapters. | ~30 min |
| **M4** | Mastery → start session has buffered topic-add. Backend skips pre-lock when `prelocked_topic` provided + writes contextual intro + shows anchor-question cards (does NOT auto-lock). TopicCard gets visual hierarchy (chapter / topic / question). Subsection-row buttons rename: Start (untouched) / + New session (touched). | 1-2 hrs |
| **M-FB** | Remove all visible templated tutor-text fallbacks. On LLM failure render an error CARD with component, error class, message, retry button — NOT a fake tutor reply. 11 sites to remove, 7 to keep, 2 already-correct. | 2-3 hrs |
| **M5** | **Build the analysis view** per AUDIT L31/L32/L37. NEW — full spec in section 4 below. | unknown — biggest item |
| **M6 ⚠** | **REGRESSION introduced by prior Claude session.** Per-turn retrieval at [conversation/nodes_v2.py:323](../conversation/nodes_v2.py) fires Qdrant + BM25 + cross-encoder rerank every engaged on-topic turn. Wastes ~$0.12-0.24 and 15-45s per session. The lock-time guard at dean.py:2233-2239 protects only the lock path; this per-turn call has no guard. Fix: add `needs_fresh_chunks` flag to TurnPlan, default reuse `state["retrieved_chunks"]` from lock time. See PRE_DEMO_ISSUES.md M6 for full details. | ~1 hr |
| **M7 🗣️** | **AUDIT ONLY — user wants to DISCUSS with you before deciding.** Intent classification has 3 separate Haiku calls per tutoring turn (`haiku_help_abuse_check`, `haiku_off_domain_check`, `haiku_deflection_check` at [preflight.py:351-378](../conversation/preflight.py)). 7 risks identified: off_domain check ignores locked_topic, no conversation context, hardcoded priority, no strike decay, no confidence threshold, double-fire risk, etc. Recommended fix priority table in PRE_DEMO_ISSUES.md M7 section. **DO NOT implement until user explicitly says go.** When user brings this up, walk them through the audit table and answer their open questions before proposing approach. | discuss first |

### Build order (when user gives go-ahead)

1. **M3** (cheapest, cleanest, ~30 min)
2. **M2** diagnosis (read trace; may auto-resolve once M1 lands)
3. **M1** + **M-FB** together (close-mode fallbacks die with M1 anyway)
4. **M6** (~1 hr, kills the visible "Searching textbook" lag every turn — big perceived-quality win for low effort)
5. **M4** (depends on M1 lifecycle being clean)
6. **M5** analysis view (biggest, builds on stable session-end state from M1)
7. **M7** — discuss with user FIRST, then decide whether to bundle some/all of #1-3 from the audit table into M1+M-FB work (since M7 #2 helps M1)
8. **Q1**, **Q2** if time
9. Re-simulate conversations
10. **Q4** scoring → produce v1-vs-v2 comparison

---

## 4. M5 — Analysis view (the big new build)

### Background

The audit doc [docs/AUDIT_2026-05-02.md](AUDIT_2026-05-02.md) at L31, L32, L37 fully specifies an "analysis mode" for past sessions. **It was specified but never built.** Today:
- `SubsectionRow` ([frontend/src/routes/MasteryView.tsx:138-176](frontend/src/routes/MasteryView.tsx)) is a flat row with a `[Revisit]` button that just opens `/chat` with the subsection name in localStorage. There is no per-session UI, no expand, no analysis chat.
- `/overview` route exists ([frontend/src/routes/SessionOverview.tsx](frontend/src/routes/SessionOverview.tsx)) but is unrelated — a per-student weak/strong topic aggregate, not a transcript reader.
- Backend endpoints for sessions exist (`GET /api/mastery/v2/{student_id}/sessions`, `GET /api/mastery/v2/session/{thread_id}` at [backend/api/mastery.py:428-463](backend/api/mastery.py)) but no frontend consumer.
- `key_takeaways` field exists in `MasterySessionRow` ([backend/api/mastery.py:351](backend/api/mastery.py)) and the SQL schema, but is never populated and never rendered.

### Design — single entry point, three layers

User confirmed:
- Single entry point only (no multi-entry complexity)
- Layout reordered: TRANSCRIPT on top, SUMMARY in middle, ANALYSIS CHAT at bottom
- Naming: keep `[Start]` for untouched, change `[Revisit]` to `[+ New session]` for touched, add `[Open]` per session row in the inline expand

### Visual flow — start to finish

#### Step 1: My Mastery index — subsection row in each state

```
┌─ Untouched (attempt_count == 0) ───────────────────────────────────┐
│ ●  Body Cavities and Serous Membranes                              │
│    untouched                          [░░░░░░░░░]  —    [Start]    │
└────────────────────────────────────────────────────────────────────┘

┌─ Red (< 50%) ──────────────────────────────────────────────────────┐
│ ▾ ●  Sliding Filament Theory                                       │
│       1 session · 4 days ago         [██░░░░░░░] 18%  [+ New]      │
│       ────────────────────────────────────────                     │
│       ● 2026-04-25  not reached  .18  [Open]                       │
└────────────────────────────────────────────────────────────────────┘

┌─ Yellow (50–75%) ──────────────────────────────────────────────────┐
│ ▾ ●  Anatomical Position                                           │
│       2 sessions · yesterday         [██████░░░] 62%  [+ New]      │
│       ────────────────────────────────────────                     │
│       ● 2026-05-02  partial   .62  [Open]                          │
│       ● 2026-04-29  partial   .42  [Open]                          │
└────────────────────────────────────────────────────────────────────┘

┌─ Green (≥ 75%) ────────────────────────────────────────────────────┐
│ ▾ ●  Sliding Filament Theory                                       │
│       3 sessions · 2 days ago        [████████░] 85%  [+ New]      │
│       ────────────────────────────────────────                     │
│       ● 2026-05-01  reached    .85  [Open]                         │
│       ● 2026-04-29  partial    .42  [Open]                         │
│       ● 2026-04-25  not reached .18  [Open]                        │
└────────────────────────────────────────────────────────────────────┘
```

**Color rules** (already in code via `MasteryColor` type):
- 🟢 green — score ≥ 75%
- 🟡 yellow — 50–75%
- 🔴 red — < 50%
- ⚪ grey — untouched

The colored ● dot already does this. Today the **bar itself** is monochrome `bg-accent` ([MasteryView.tsx:126](frontend/src/routes/MasteryView.tsx)). Change: bar fill matches the tier color.

#### Step 2: Click [Open] → analysis page

```
┌─ /sessions/{thread_id} ───────────────────────────────────┐
│  ← My Mastery / Anatomical Terminology / Sliding Filament │
│                                                           │
│  ╔════ TRANSCRIPT ═══════════════════════════════════╗    │
│  ║  Tutor:   Welcome — what do you know about…       ║    │
│  ║  You:     I think it's the cross-bridge cycle…    ║    │
│  ║  Tutor:   Good. Now what about ATP's role?        ║    │
│  ║  You:     ATP binds to myosin and…                ║    │
│  ║  Tutor:   Exactly. You've got it.                 ║    │
│  ║                                  (read-only)      ║    │
│  ╚═══════════════════════════════════════════════════╝    │
│                                                           │
│  ╭──── SUMMARY ────────────────────────────────────╮     │
│  │  Locked Q: How does ATP drive cross-bridge…     │     │
│  │  Answer:   ATP binding releases myosin from…    │     │
│  │  ✓ reached  ·  ●  85%  ·  GREEN                 │     │
│  │  ✓ Demonstrated: cross-bridge mechanics         │     │
│  │  ⚠ Needs work:  calcium release timing          │     │
│  ╰──────────────────────────────────────────────────╯     │
│                                                           │
│  ╭──── ANALYSIS CHAT ──────────────────────────────╮     │
│  │  📖 Read-only · scoped to this session          │     │
│  │                                                  │     │
│  │  You:    Why did I get stuck on calcium?        │     │
│  │  Tutor:  Looking at turn 4, you described it…   │     │
│  │                                                  │     │
│  │  ┌──────────────────────────────────────────┐   │     │
│  │  │ Ask about this session…          [Send]  │   │     │
│  │  └──────────────────────────────────────────┘   │     │
│  ╰──────────────────────────────────────────────────╯     │
└───────────────────────────────────────────────────────────┘
```

### Analysis chat behavior (per AUDIT L31)

When the student types and sends a message:
1. **Scope check** (Haiku, ~$0.0003): is this question about this subsection? If NO → render scope-refusal line ("This is your {subsection} review session — to learn other topics, start a new session from My Mastery."), no further LLM cost.
2. **If YES** → call Sonnet with: prior session transcript, subsection chunks (re-retrieved fresh), mem0 observations filtered by `subsection_path`, locked Q/A pair, this analysis chat's history.
3. Reply renders in the analysis chat.
4. **No state writes**: no mastery update, no new mem0 entry, no `sessions` row, no SQLite mutation. Pure read-only meta-discussion. Per L31.

### Session-end state needed (so analysis works at all)

When a tutoring session ends (any path: auto-end, explicit-exit-and-save, explicit-exit-no-save), one extra Haiku call:
- Input: full transcript + locked Q/A pair
- Output: `{demonstrated: "...", needs_work: "..."}` (one line each)
- Stored: `sessions.key_takeaways` JSON column (column already exists; just unused today)
- Cached forever; never regenerated
- Cost: ~$0.001 per session
- On Haiku failure: per M-FB, NO fallback. Store `null`. Analysis page shows error card on the summary panel (component: `session_takeaways_haiku`, with message + [Regenerate] button).

### Files that need to change for M5

**Frontend:**
- `frontend/src/routes/MasteryView.tsx` — make `SubsectionRow` expandable; add disclosure caret; render inline session list with `[Open]` links; rename Revisit → "+ New session"; bar fill color-coded by tier
- `frontend/src/routes/SessionAnalysis.tsx` — NEW route component, layout per the diagram above
- `frontend/src/App.tsx` — register new route `/sessions/:threadId`
- `frontend/src/api/client.ts` — new client methods: `getSessionTranscript(threadId)`, `getSessionTakeaways(threadId)`, `postAnalysisChat(threadId, message)`, `getSessionsForSubsection(studentId, subsectionPath)`
- `frontend/src/types/index.ts` — types for the new endpoint payloads

**Backend:**
- `backend/api/mastery.py` — extend `list_sessions()` consumer to filter by `subsection_path`; add `GET /api/mastery/v2/{student_id}/subsections/{path:path}/sessions`
- `backend/api/sessions.py` — NEW file (or extend existing) for session-detail + transcript + analysis-chat endpoints:
  - `GET /api/sessions/{thread_id}` — metadata + key_takeaways
  - `GET /api/sessions/{thread_id}/transcript` — full message log from the session JSON
  - `POST /api/sessions/{thread_id}/analysis_chat` — accepts user message, returns reply (no state writes)
  - `POST /api/sessions/{thread_id}/regenerate_takeaways` — retry the Haiku takeaways call
- `memory/sqlite_store.py:398-441` — extend `list_sessions()` to accept optional `subsection_path` filter
- `conversation/` — wire up the session-end Haiku takeaways call (lands in M1's lifecycle work, since M1 is what fires session-end cleanly)

**No code yet — wait for explicit user go-ahead.**

---

## 5. Detailed M-issue notes (the rest)

### M1 — Lifecycle redesign

**Root cause confirmed via investigation:**
- `dean_node_v2` ([conversation/nodes_v2.py:77-484](conversation/nodes_v2.py)) calls `dean.reached_answer_gate()` at line 169, then preflight at 206, then plan at 374, then Teacher drafts at 424. Returns without phase transition.
- Phase transition to `assessment` / `memory_update` only happens inside `assessment_node_v2` on the NEXT turn ([conversation/assessment_v2.py:180](conversation/assessment_v2.py), [:627](conversation/assessment_v2.py)).
- Preflight ([conversation/preflight.py:394-409](conversation/preflight.py)) suggests `mode="confirm_end"` on deflection without checking `state.student_reached_answer`.

**The decided lifecycle:**

**A. Auto-end** — terminal condition fires, no question asked. Triggered by:
- clinical answered correctly
- clinical turn-cap hit
- tutoring done + clinical opt-in = "no"
- tutoring turn-cap hit

Path: terminal condition → memory_update → exit. LLM goodbye uses conversation history.

**B. Explicit-exit** — student types "I have to go" mid-phase:
1. Intent classifier detects `exit_intent`
2. Frontend renders Option B confirm modal — 3 buttons: `[End & save progress]` `[End without saving]` `[Cancel]`
3. End-with-save → memory_update → goodbye → exit
4. End-without-save → goodbye → exit (skip memory_update)
5. Cancel → resume current phase

**Goodbye message rule:**
- Add the close modes to `_MODES_USING_HISTORY` ([conversation/teacher_v2.py:273-274](conversation/teacher_v2.py)) so Teacher sees the last N turns
- Pass targeted signals: strongest student turn, misconceptions corrected, clinical state
- Eliminate templated close fallbacks per M-FB

### M2 — Memory drawer empty after completed session

**Wiring is correct** (confirmed via investigation):
- Flush path ([memory/memory_manager.py:132-229](memory/memory_manager.py)) and read path ([backend/api/memory.py:87-129](backend/api/memory.py)) use the same namespaced student_id ([memory/persistent_memory.py:70](memory/persistent_memory.py)). They cannot diverge on user_id.
- Every write attempt logged to `state.debug.turn_trace` with `wrapper:"mem0_write"` ([memory/mem0_safe.py:159-188](memory/mem0_safe.py)).

**Likely causes (ranked):**
1. Metadata validation drops the write (`subsection_path`, `section_path`, `session_at`, `thread_id` required)
2. Qdrant down at flush time
3. Haiku extractor returned empty observations
4. mem0.add() exception

**Action:** read `data/artifacts/eval_run_18/eval18_solo1_S1_session1.json` (newest, fresh), grep `state.debug.turn_trace` for `wrapper:"mem0_write"` and `wrapper:"memory.session_summary"` entries. Trace will say which cause fired. Then fix that one.

**May be downstream of M1**: if phase never transitions to `memory_update`, flush never fires.

### M3 — Topic resolver rejection memory

**Confirmed deterministic loop:**
- `_map_topic` ([retrieval/topic_mapper_llm.py:139-182](retrieval/topic_mapper_llm.py)) is single Haiku call with cached TOC + abbrevs. Same query → same suggestion.
- Pre-lock loop ([conversation/topic_lock_v2.py:92-102](conversation/topic_lock_v2.py)) clears state on "No" but re-runs `_map_topic` with same query.
- State has `rejected_topic_paths` ([conversation/state.py:200](conversation/state.py)) but ONLY used for cap-7 fallback `sample_diverse(exclude_paths=...)`, NOT passed back to resolver.
- Cap-7 fallback `_render_guided_pick` ([conversation/topic_lock_v2.py:485](conversation/topic_lock_v2.py)) calls `matcher.sample_diverse()` ([retrieval/topic_matcher.py:230](retrieval/topic_matcher.py)) — pure random sampling.

**Fix shape:**
1. On every "No" rejection, append the proposed `subsection_path` to `state.rejected_topic_paths`
2. Pass `rejected_topic_paths` into `_map_topic`. Resolver excludes them from `top_matches`. If after exclusion no `top_matches` ≥ borderline confidence, return `verdict="none"` and let the loop show the "type a topic" UX instead of bleeding into random fallback
3. Cap-7 fallback should rerank the resolver's full candidate set against the ORIGINAL query, not switch to `sample_diverse()`. Random unrelated cards are never the right UX.

**Scope:** in-session only. Rejection memory does NOT persist across sessions.

### M4 — Mastery → start UX

**Current behavior (verified in code):**
- [MasteryView.tsx:364-388](frontend/src/routes/MasteryView.tsx) writes the subsection NAME to `REVISIT_KEY` localStorage and navigates to `/chat`.
- Comment in code (line 372-376): *"We previously also wrote REVISIT_TOPIC_PATH to drive the backend prelocked_topic shortcut, but that produced a stacked 'Got it - let's work on X' tutor message immediately after the rapport, which read like the LLM was talking to itself. The subsection-name injection is the natural-feeling alternative."*

**Fix shape:**
1. When `prelocked_topic` is in StartSessionRequest, backend populates `state.locked_subsection` (NOT `locked_topic`) and skips topic-mapper. Sends tailored welcome: *"Welcome back — picking up on [Body Cavities]."* Single LLM-written contextual message, NOT templated.
2. **DECISION:** show 2-3 anchor-question cards from that subsection. Student picks one → that question gets locked → tutoring starts. Do NOT auto-lock.
3. Drop localStorage `REVISIT_KEY` legacy fallback once backend handles cleanly.
4. **TopicCard visual hierarchy** ([frontend/src/components/cards/TopicCard.tsx](frontend/src/components/cards/TopicCard.tsx)): each option renders three layers — Chapter (small, muted) / Topic (larger, primary) / Question (italicized, secondary). Today it's a single-line button.
5. **Subsection row buttons rename** (overlaps with M5):
   - `attempt_count == 0` → `[Start]`
   - `attempt_count > 0` → `[+ New session]`

### M-FB — Templated reply fallbacks

**Principle:**
> Templated fallbacks mask LLM failures. Remove them everywhere they produce visible tutor text. On LLM failure: log loudly, retry once, then render an **error card** in the chat UI showing the cause. Keep fallbacks ONLY where the alternative is graph crash.

**Error card content:**
- Component that failed (e.g. `Teacher.draft`, `Dean.replan`, `MemoryManager.flush`, `topic_mapper_llm`)
- Error class (`RateLimitError`, `JSONDecodeError`, `TimeoutError`)
- Short error message (truncated to ~200 chars)
- `[Retry]` button

**Visual:** distinct chat element, visually separate from tutor messages so it's unmistakable.

**Policy during demo-prep / iteration:** errors surfaced verbatim. Production-soft version (hide internals, generic "something went wrong") comes later.

**Audit results — full REMOVE / KEEP / no-fallback-by-design tables in [PRE_DEMO_ISSUES.md](PRE_DEMO_ISSUES.md) M-FB section.** 11 sites to remove, 7 to keep, 2 already-correct.

---

## 6. Eval state and runbook

### Latest eval state (2026-05-03)

8 single-session evals were run earlier today (varying freshness). Outcomes:

| # | id | profile | reach | turns | locked topic | cost |
|---|---|---|---|---|---|---|
| 1 | solo1_S1 | S1 Strong | ✓ | 1 | B Cell Differentiation | $0.016 |
| 2 | solo3_S3 | S3 Weak | ✓ | 3 | Gross Anatomy of Bone | $0.023 |
| 3 | solo4_S4 | S4 Overconfident | ✓ | 13 | Elbow Joint | $0.097 |
| 4 | solo5_S5 | S5 Disengaged | ✗ | 5 | **Food and Metabolism** (asked spleen — wrong topic locked) | $0.088 |
| 5 | solo2_S2 | S2 Moderate | ✓ | 1 | Respiratory Rate | $0.034 |
| 6 | solo6_S6 | S6 Anxious | ✓ | 4 | Thyroid Hormones | $0.029 |
| 7 | triple1_progressing | S3 Weak | ✗ | 14 | **Overview of Urine Formation** (asked nephron — wrong topic locked) | $0.114 |
| 8 | pair3_disengaged | S5 Disengaged | ✗ | 3 | Phases of Cardiac Cycle | $0.092 |

5/8 reached. Total cost: ~$0.49.

**Note for new Claude:** sessions 3,4,5,6,7,8 timestamps are 09:52:16 (single-batch), so they predate the 8-sequential run plan in [docs/EVAL_RESUME_JOURNAL.md](EVAL_RESUME_JOURNAL.md). Only solo1_S1 (10:08) and solo2_S2 (09:59) are demonstrably fresh from the most recent run. The data is real either way.

### Pre-flight before running anything

```bash
# Qdrant up?
nc -z localhost 6333 && echo OK || docker start qdrant-sokratic

# No leftover workers?
ps aux | grep run_eval_chain | grep -v grep

# Memory ok? (>1GB free recommended; today was tight at ~52MB)
vm_stat | grep "Pages free"
```

### Run a single session eval

```bash
cd /Users/arun-ghontale/UB/NLP/sokratic
SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks \
  .venv/bin/python scripts/run_eval_chain.py <student_id>
```

Output: `data/artifacts/eval_run_18/<student_id>_session1.json`

### Inspect a session's outcome

```bash
.venv/bin/python3 -c "
import json
d = json.loads(open('data/artifacts/eval_run_18/eval18_solo1_S1_session1.json').read())
o = d['outcome']; fs = d['final_state']
locked = (fs.get('locked_topic') or {}).get('subsection') or '(none)'
print(f'reached={o[\"reached_answer\"]} turns={o[\"turn_count\"]} asmt={o[\"assessment_turn\"]} locked={locked} cost=\${d[\"debug_summary\"][\"cost_usd\"]:.4f}')
"
```

### Inspect M2 trace (memory write failures)

```bash
.venv/bin/python3 -c "
import json
d = json.loads(open('data/artifacts/eval_run_18/eval18_solo1_S1_session1.json').read())
trace = d.get('debug_summary', {}).get('turn_trace', [])
mem_entries = [t for t in trace if t.get('wrapper', '').startswith('mem0_') or 'memory' in t.get('wrapper', '')]
for t in mem_entries:
    print(t)
"
```

---

## 7. Conversation summary — what got decided this session

The user and prior Claude went through every issue in detail. Decisions in chronological order:

1. **Q1, Q2 confirmed real**, will fix when convenient
2. **Q4 deferred** until UI + flow are done + re-simulated. Don't run scoring against half-fixed system.
3. **M1 lifecycle confirmed**:
   - Auto-end (no question to student) when terminal condition fires
   - Explicit-exit (student types "I want to stop") gets Option B modal: `[End & save]` `[End without saving]` `[Cancel]`
   - Goodbye message MUST be LLM-written using conversation history (close modes need `_MODES_USING_HISTORY`)
4. **M2 confirmed** — debug-first, read trace before fixing
5. **M3 confirmed** — in-session `rejected_topic_paths` plumbed into resolver
6. **M4 confirmed** with addition: contextual welcome THEN cards (do NOT auto-lock); TopicCard visual hierarchy; subsection-row buttons rename (Start / + New session)
7. **M-FB confirmed** — error CARD in chat (component + error class + message + [Retry]). Demo-prep policy: surface errors verbatim.
8. **M5 designed** — analysis view per AUDIT L31/L32/L37:
   - Single entry point: My Mastery → expand subsection → click [Open] on a session row
   - Page layout (top→bottom): TRANSCRIPT, SUMMARY (clean card), ANALYSIS CHAT
   - Read-only mutation policy
   - Bar fill color-coded by tier (green/yellow/red/grey) on subsection row
   - Session-end Haiku generates `key_takeaways` once, cached forever

User finally said: "now log everything ... give a handoff document that loads in context for the new claude session." That's this doc.

---

## 8. What NOT to do

- Don't add fallbacks back. If you see an LLM call without a fallback, that's deliberate.
- Don't make decisions for the user on M-issues — they want to confirm each one before code lands.
- Don't run the eval harness without checking memory (it crashed the system 3× today on parallel runs).
- Don't assume the worktree is current. Always verify with `git log --oneline -5` against the parent repo.
- Don't write code in any of these areas without being asked. The plan is in the doc; the implementation waits.
- Don't write multi-paragraph docstrings or chatty code comments.

---

## 9. First message to user (suggested)

When the new Claude session starts and reads this doc, suggested opener:

> Read the handoff. I have full context on the M-series punch list and the M5 analysis-view design. Where do you want to start?
>
> Suggested order is M3 (~30 min, cleanest) → M2 diagnosis (read trace) → M1 + M-FB together → M4 → M5. But you call it.

That's the handoff. Welcome to the project, future Claude.
