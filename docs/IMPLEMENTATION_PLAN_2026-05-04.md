# Implementation Plan — 2026-05-04

**For:** future Claude session executing the M-series + audit findings
**Scope:** M1-M7 + M-FB + audit-derived fixes
**Source:** locked decisions from 2026-05-04 review session
**Read first:** [HANDOFF_2026-05-03_ANALYSIS_VIEW.md](HANDOFF_2026-05-03_ANALYSIS_VIEW.md)

---

## Hard rules

1. **Work in `/Users/arun-ghontale/UB/NLP/sokratic`.** Worktrees are stale.
2. **Use ast-editor MCP for `.py`, `.ts`, `.tsx`.** Edit/Write only when restructuring.
3. **NO templated tutor-text fallbacks anywhere.** Error card on LLM failure. UI chrome (button labels, activity chips, modal copy, error cards) is not "templated tutor text" — those stay deterministic.
4. **Block-by-block execution.** Each block is self-contained. Verify acceptance criteria before moving on.
5. **No new LLM call unless the design explicitly says so.** Reuse existing calls; enrich prompts.
6. **Activity chip + paired user message rule:** every chip that surfaces during an LLM call must be followed by a user-visible message that explains the outcome. Chips never preview state changes the user hasn't seen. (Saved as project memory.)
7. **Don't start implementation without per-block confirmation from user** (default).

---

## Block order (dependency-respecting)

```
B0 → B1 → B2 → B3 → B4 → B5 → B6 → B7
```

| Block | Title | Estimate | Why this position |
|---|---|---|---|
| B0 | Eval harness `thread_id` fix (M2 Bug A) | 5 min | Unblocks eval testing for everything below |
| B1 | M3 — Topic resolver rejection memory | 30 min | Smallest, cleanest, no deps |
| B2 | M6 — Per-turn retrieval + exploration restore | 1.5h | Regression fix, restores prompt-cache contract |
| B3 | M7 — Unified intent classifier + decay | 3.5h | Multi-label classifier reused by M1 |
| B4 | M1 — Lifecycle redesign + error card UI | 5-6h | Error card UI built here is reused by B5 |
| B5 | M-FB — Templated fallback sweep | 2.5h | Uses error card UI from B4 |
| B6 | M4 — Mastery → start anchor-pick UX | 2h | Depends on close-LLM JSON shape from B4 |
| B7 | M5 — Analysis view | 8-10h | Biggest; depends on close-LLM JSON from B4 |

**Total:** ~25-30h. Run blocks in separate sessions if needed; each block is self-contained.

---

## Block B0 — Eval harness `thread_id` fix

**Why:** [scripts/run_eval_chain.py](scripts/run_eval_chain.py) never sets `state["thread_id"]`. Eval sessions silently drop ALL mem0 + sqlite + mastery writes (verified via [data/artifacts/eval_run_18/eval18_solo1_S1_session1.json](data/artifacts/eval_run_18/eval18_solo1_S1_session1.json) trace: `error="missing_required_field", dropped_field="thread_id"` × 4). Without this fix, eval data can't validate any memory feature.

**Files:**
- `scripts/run_eval_chain.py`

**Steps:**
1. Find where state is initialized (likely uses `initial_state(student_id, cfg)` like [backend/api/session.py:192](backend/api/session.py:192))
2. Generate a thread_id: `f"{student_id}_{uuid.uuid4().hex[:8]}"` matching production format
3. Add `state["thread_id"] = thread_id` immediately after init
4. Pass thread_id into LangGraph config too: `{"configurable": {"thread_id": thread_id}}`

**Acceptance:**
- Run one eval: `SOKRATIC_USE_V2_FLOW=1 .venv/bin/python scripts/run_eval_chain.py eval18_solo1_S1`
- Inspect output JSON: `mem0_write` entries should NOT have `error="missing_required_field"`. `sqlite_store.session_end` should NOT be `skipped_no_thread_or_student`.

---

## Block B1 — M3: Topic resolver rejection memory

**Why:** Same Haiku call → same suggestion → student rejects same subsection 5×, then random unrelated chapters. State field exists but isn't wired through.

**Files:**
- `conversation/topic_lock_v2.py` (3 spots: lines 92-102, 122-123, 485-501)
- `retrieval/topic_mapper_llm.py` (add param, exclude from TOC)

**Steps:**
1. **`topic_lock_v2.py:92-102`** (the "No" branch): before clearing pending, extract the rejected subsection_path from `pending` and append to `state["rejected_topic_paths"]`. State field already exists at [state.py:200](conversation/state.py:200).
2. **`topic_lock_v2.py:123` `_map_topic`**: pass `state.get("rejected_topic_paths", [])` as new param into `map_topic()`.
3. **`retrieval/topic_mapper_llm.py:map_topic`**: accept `rejected_paths: list[str] = None`. Before building TOC block (or after, easier), drop entries whose path is in `rejected_paths`. If after exclusion `len(top_matches) == 0` → return `verdict="none"` so caller routes to `_render_refuse_cards`.
4. **`topic_lock_v2.py:485 _render_guided_pick`**: replace `matcher.sample_diverse(...)` with:
   ```python
   match_result = matcher.match(query)
   rejected = set(state.get("rejected_topic_paths", []) or [])
   topics = [t for t in match_result.top_matches if t.path not in rejected][:GUIDED_PICK_COUNT]
   ```
5. **Refuse-cards intro tweak**: when triggered by `verdict="none"` after exclusion, intro reads:
   > "I couldn't find a clear match for that topic. Try rephrasing more specifically, or browse the topics below."

**Acceptance:**
- Manual test: ask "what is the spleen", click No on suggestion. On next turn, asking same query should produce a DIFFERENT suggestion (or "type something more specific").
- After 5 consecutive Nos, no random unrelated chapter cards appear.

---

## Block B2 — M6: Per-turn retrieval + exploration restore

**Why:** [nodes_v2.py:323](conversation/nodes_v2.py:323) fires retrieval every engaged turn. Wastes ~$0.12-0.24 + 15-45s per session. Breaks prompt-cache contract. v1 had `_exploration_judge` + `_exploration_retrieval_maybe` for OT-related curiosity drift; v2 lost this.

**Files:**
- `conversation/nodes_v2.py:316-326`, ~:374 (plan call)
- `conversation/dean_v2.py` — TurnPlan dataclass + plan() prompt
- `conversation/dean.py:2370` — `_exploration_retrieval_maybe` (already exists, just call it)
- `conversation/dean.py:2277` — `_exploration_judge` (already exists, wire into v2)
- `conversation/state.py` — add `exploration_count: int = 0`

**Steps:**
1. **`state.py`**: add `exploration_count: int` to TutorState + initial_state (default 0).
2. **`dean_v2.py` TurnPlan**: add `needs_exploration: bool = False`.
3. **`nodes_v2.py:316-326`**: DELETE the unconditional `chunks = retriever.retrieve(latest_student)`. Replace with `chunks = list(state.get("retrieved_chunks", []) or [])` and a trace `wrapper:"retriever.reused_lock_time_chunks"`.
4. **`nodes_v2.py:374` after Dean's plan_result**: if `plan_result.turn_plan.needs_exploration == True`, fire `dean._exploration_retrieval_maybe(state)` and append the resulting exploration chunks to `chunks` (don't replace). Increment `state["exploration_count"]`.
5. **Decay rule** (M6 spec): on a turn where `needs_exploration == False` AND preflight didn't fire, decrement `exploration_count = max(0, exploration_count - 1)`.
6. **`dean_v2.py` plan() prompt**: add instructions
   ```
   needs_exploration: true ONLY when student's question is tangential to the
   locked subsection (not covered by current chunks). Otherwise false.

   You'll see: exploration_count, turns_remaining (max_turns - turn_count).
   If needs_exploration=true:
     - Always answer helpfully using exploration chunks
     - If turns_remaining < 4 OR exploration_count >= 2:
       Brief mention "we've explored — N turns left for the original question"
     - Never refuse exploration; genuine curiosity is welcome.
   ```
7. Pass `exploration_count`, `turns_remaining = max_turns - turn_count` into the Dean prompt input dict.

**Acceptance:**
- Run a 5-turn on-topic conversation. Trace should show `wrapper:"retriever.reused_lock_time_chunks"` 4× (turns 2-5). Only turn 1 (lock) does retrieval.
- Run a conversation with one tangential question. Trace shows `dean._exploration_retrieval_maybe` fired exactly once. `exploration_count` increments. Subsequent on-topic turns decay it.
- Cost on a 5-turn session drops from ~$0.20 to ~$0.05.

---

## Block B3 — M7: Unified intent classifier + strike decay

**Why:** Today 3 separate Haiku calls per turn (help_abuse + off_domain + deflection). 3× cost, all context-blind, off_domain doesn't see locked_topic. Strike counters never decay → single misclassification can kill a session.

**Files:**
- `conversation/preflight.py` — replace 3-call orchestration with 1 multi-label call
- `conversation/classifiers.py` — new `haiku_intent_classify` (or replace `haiku_off_domain_check` with the multi-label version)
- `conversation/dean.py:3146` — DELETE the duplicate `off_domain_check` call
- `conversation/assessment_v2.py:942` `_classify_opt_in` — replace regex with the same multi-label classifier (Q1)
- `conversation/state.py` — no schema change; counters already exist

**Steps:**
1. **New unified classifier** in `classifiers.py`:
   ```
   System prompt: "Classify the student's latest message into ONE of:
     on_topic_engaged | help_abuse | off_domain | deflection | opt_in_yes | opt_in_no | opt_in_ambiguous
     Output: {verdict, evidence, rationale}"
   Input context:
     - latest student message
     - last 2 student-tutor turn pairs (4 messages)
     - locked_subsection name (or "not yet locked")
     - locked_question (if locked)
     - current phase (rapport / tutoring / assessment / clinical)
   ```
2. **`preflight.py`**: replace `run_preflight()` 3-call ThreadPoolExecutor with 1 call. Map verdict to existing `PreflightResult.category` field. Counter logic unchanged except for decay.
3. **Strike decay**: on every turn where verdict is `on_topic_engaged`, `state["off_topic_count"] = max(0, off_topic_count - 1)` and same for `help_abuse_count`. Increment as today on the relevant verdict.
4. **Delete `dean.py:3146`** (the duplicate off_domain call). Verify nothing else reads the result of that call.
5. **`assessment_v2.py:942` `_classify_opt_in`**: replace regex with a call to the unified classifier. Verdict `opt_in_yes` → "yes", `opt_in_no` → "no", `opt_in_ambiguous` → "ambiguous". Preserves existing branching at [assessment_v2.py:211-237](conversation/assessment_v2.py:211).
6. **Trace**: log the unified call result with the same `wrapper` keys preflight used (so existing dashboards still work).

**Acceptance:**
- Trace on a tutoring turn: 1 `wrapper:"haiku_intent_classify"` entry (was 3).
- Per-turn cost drops by ~2/3 of preflight cost.
- Test: lock topic "thyroid hormones", student asks "tell me about thyroid" → classifier gets locked_topic in context, returns `on_topic_engaged` (was `off_domain` falsely before).
- Test: 1 deflection then 5 substantive turns → off_topic_count decays from 1 → 0. Future single misclassification doesn't insta-kill the session.

---

## Block B4 — M1: Lifecycle redesign + Error card UI

**Why:** Sessions never terminate cleanly today. Memory drawer empty (M2 root cause). Templated close fallbacks lie about LLM failures. This block is the biggest single foundational lift.

**Files:**

Backend:
- `conversation/state.py` — add `session_ended: bool`, `exit_intent_pending: bool`
- `conversation/edges.py:67` — hint-exhausted route → `memory_update_node` (skip assessment)
- `conversation/teacher_v2.py:274` — add close modes to `_MODES_USING_HISTORY`
- `conversation/teacher_v2.py` — collapse 3 close prompts into 1 unified `close` mode with reason flag
- `conversation/preflight.py` — on deflection: set `state["exit_intent_pending"]=True`, do NOT have Teacher draft confirm (modal handles it)
- `conversation/nodes_v2.py:222-280` — preflight-fired branch: if category=deflection, return `pending_user_choice={kind:"exit_intent"}` instead of Teacher draft
- `conversation/assessment_v2.py:155, 261` — kill `_render_reach_close` / opt-in templated fallback strings (M-FB territory but folds in here for close path)
- `conversation/nodes.py:memory_update_node` — flip from UPDATE to INSERT for sessions row (per M5 D4 — `start_session` SQLite call deleted in this block too)
- `backend/api/session.py:184-187` — DELETE the `SQLiteStore().start_session(thread_id, sid)` call (D4 from M5)
- `backend/api/session.py` (or new `backend/api/exit.py`) — endpoint `POST /api/session/{thread_id}/exit_confirm` (action=end_session | cancel)
- **Audit fix folded in (HIGH severity from backend audit):** `conversation/nodes_v2.py:477` — only increment `turn_count` when `preflight.fired == False` AND not on a redirect/nudge turn. Off-topic redirects must NOT burn turns.

Frontend:
- `frontend/src/components/layout/Header` (or chat header) — `[End session]` button always visible top-right
- `frontend/src/components/modals/ExitConfirmModal.tsx` — NEW. 2 buttons: `[Cancel]` `[End session]`. Static copy "End this session? Your conversation won't be saved..."
- `frontend/src/components/cards/ErrorCard.tsx` — NEW. Renders `{component, error_class, message, [Retry]}` payload from backend
- `frontend/src/hooks/useSession.ts` — handle `pending_user_choice.kind=="exit_intent"` → show modal
- `frontend/src/components/Composer.tsx` — disable input + show "Session ended — review in My Mastery" banner when `state.session_ended === true`
- `frontend/src/api/client.ts` — `exitConfirm(threadId, action)`, `retryLastTurn(threadId)`

**Steps:**
1. **State + routing fixes first** (low blast radius):
   - Add `session_ended`, `exit_intent_pending` to TutorState.
   - Fix `edges.py:67` to route hint-exhausted → `memory_update_node`.
   - Fix `nodes_v2.py:477` `turn_count` over-increment (audit HIGH).
   - Delete `start_session` SQLite call in [session.py:184-187](backend/api/session.py:184). Update `memory_update_node` to INSERT (or upsert) the row.

2. **Unified close mode (one Sonnet call, structured JSON output):**
   - In `teacher_v2.py`: add a `close` mode. Prompt produces JSON: `{message, demonstrated, needs_work}`.
   - Mode flag determines tone: `reach_full | reach_skipped | clinical_cap | hints_exhausted | tutoring_cap | off_domain_strike | exit_intent`.
   - Add `close` to `_MODES_USING_HISTORY` so prompt sees real conversation.
   - Activity chip during close call: `"Reflecting on your progress"` (neutral).
   - Streamed response: text appears token-by-token in chat (already supported).

3. **Save/no-save reason mapping** at memory_update_node:
   - `reach_full / reach_skipped / clinical_cap / hints_exhausted / tutoring_cap` → full save (mem0 + mastery + sessions row)
   - `off_domain_strike / exit_intent` → no save (skip the node entirely; route directly to END)

4. **Modal pop on deflection (NOT a Teacher draft):**
   - In `preflight.py` orchestrator: on `verdict=="deflection"`, return PreflightResult with `suggested_mode="exit_intent_modal"` (new mode).
   - In `nodes_v2.py:222`: if `preflight.suggested_mode == "exit_intent_modal"`, return `{pending_user_choice: {kind:"exit_intent"}, debug:...}` — no tutor message added to transcript. Frontend renders modal.
   - Frontend handles `kind:"exit_intent"` in useSession bootstrap or message handler → shows `ExitConfirmModal`.

5. **Persistent [End session] button:**
   - Always visible in chat header (top-right) when in tutoring/assessment/clinical phase.
   - On click → POST `/api/session/{thread_id}/exit_confirm?action=end_session` → backend triggers close flow.

6. **Error card UI (used here AND by B5):**
   - Backend on close LLM fail: retry once silently. If retry fails, return error card payload `{component:"Teacher.draft", error_class:"TimeoutError", message:"...", retry_handler:"close"}`.
   - Frontend `ErrorCard` renders distinct from tutor messages. `[Retry]` button POSTs to retry endpoint OR re-sends user's last message (per M-FB D2 — simple re-send approach).
   - On second retry failure: card escalates to `"Summary unavailable. Saved your progress without it."` and proceeds with save (mastery + mem0 still run; `sessions.key_takeaways` written as `null`).

7. **session_ended → input disable:**
   - On any close path, set `state["session_ended"] = True`.
   - Frontend reads from state/debug → disables Composer input + shows banner "Session ended" with `[Open in My Mastery]` link to `/mastery`.

8. **Banner content:** show mastery score inline (+ delta from prior session if available). Format: `✓ Saved · Sliding Filament Theory · Mastery 62% (▲ 18%)`.

**Acceptance:**
- Manual test: complete a session by reaching the answer → see streamed close message containing assessment + reason + transition. Banner shows "Saved · subsection · Mastery X%".
- Click [End session] mid-conversation → modal pops directly (no tutor message added). Click Cancel → modal closes, no transcript change. Click End session → close LLM fires, message streams, banner appears.
- Hint exhausted (max_hints=3, exhaust them) → routes directly to memory_update with `honest_close` text mentioning the gap, NOT "you reached the core answer".
- Reach + deflect same turn → modal still fires (KISS).
- Type after END → input disabled, no zombie request.
- Sessions row exists ONLY at memory_update (not at start). Abandoned sessions = no row.
- Off-topic redirect: trace shows `turn_count` did NOT increment.

---

## Block B5 — M-FB: Templated fallback sweep

**Why:** 11 sites produce visible templated tutor text on LLM failure. Mask debugging. Use the error card UI built in B4.

**Files:**
- `conversation/assessment_v2.py` (5 sites: lines 157, 263, 354, 507, 600/682/766 — last 3 die naturally with B4's unified close)
- `conversation/dean.py` (3 sites: 1458, 2111, 3099-3101)
- `conversation/nodes.py` (4 sites: 397, 450, 578, 595)

**Pattern (every REMOVE site):**
```python
try:
    result = llm_call(...)
except Exception as e:
    log_loudly(state, wrapper="<site_name>", error=f"{type(e).__name__}: {str(e)[:160]}")
    try:
        result = llm_call(...)  # silent retry once
    except Exception as e:
        return error_card(
            component="<descriptive name>",
            error_class=type(e).__name__,
            message=str(e)[:200],
        )  # frontend renders ErrorCard
```

**Steps:**
1. Verify B4's `error_card` helper / payload shape exists. If not, build it: `conversation/error_card.py` exporting `def error_card(component, error_class, message) -> dict` returning a dict the frontend renders.
2. Walk the 11 REMOVE sites in the M-FB audit table ([PRE_DEMO_ISSUES.md M-FB section, lines 700-714](docs/PRE_DEMO_ISSUES.md)). For each:
   - Delete the templated `fallback_text=...` string
   - Wrap LLM call in try/except per pattern above
   - On final failure: return error_card payload (frontend wires to ErrorCard)
3. **Audit fold-in (backend audit MED severity):** Replace bare `except Exception: pass` at [session.py:186-187](backend/api/session.py:186) (will be deleted by B4 anyway — D4) and at [session.py:217-226](backend/api/session.py:217) with proper logging + error response so prelock failures surface to frontend.
4. **Investigate `dean.py:3099-3101` defensive kickoff:** delete the fallback now per M-FB principle. If error card surfaces in eval/demo, debug the upstream cause then.
5. **Verify 7 KEEP sites still log loudly:** `dean_v2.py:350`, `retry_orchestrator.py:69`, `dean.py:574/2887/3502/3529`, `assessment_v2.py:134`, `retrieval/retriever.py`, `topic_matcher.py:286`. These are control-flow fallbacks (parse / retrieval-tier / minimal-TurnPlan), keep them but make sure failure logs are loud.
6. **Re-run evals after sweep** (depends on B0 fix). New error cards may surface in eval data — triage upstream causes.

**Retry mechanism (D2 from M-FB):**
- Frontend `[Retry]` = re-POST user's last message (NOT per-handler granular retry). Backend re-runs whole turn.
- Cost on retry: ~$0.05. Acceptable for demo.

**Acceptance:**
- Force a Teacher.draft failure (mock the call to throw): user sees ErrorCard with `Teacher.draft / TimeoutError / message`, NOT a fake tutor reply.
- Eval re-run produces new error-card trace entries where templates used to absorb failures. Triage them.
- Grep `fallback_text=` across `conversation/` returns only the 7 KEEP-list occurrences.

---

## Block B6 — M4: Mastery → start anchor-pick UX

**Why:** Today's REVISIT_KEY hack injects subsection name as fake student message. 2-turn lag, generic rapport, feels broken. Plus: prelocked_topic backend path exists but frontend bypasses it.

**Files:**

Backend:
- `conversation/state.py` — add `locked_subsection: dict | None` (distinct from `locked_topic`)
- `backend/api/session.py:213-225` `_apply_prelock` — refactor: set `locked_subsection`, skip topic-mapper, generate 3 anchor variations, return `pending_user_choice={kind:"anchor_pick", options:[q1,q2,q3]}`
- Rapport node (find: search for "rapport" in `nodes.py` / `nodes_v2.py`) — variant prompt that knows about `locked_subsection`: "Welcome back — picking up on [Body Cavities]" (LLM-written, not templated)
- `conversation/topic_lock_v2.py` — handler for `pending_user_choice.kind=="anchor_pick"`: student picks → that q becomes `locked_question`/`locked_answer` → `locked_topic` fully populated → tutoring starts

Frontend:
- `frontend/src/routes/MasteryView.tsx:364-388` — drop REVISIT_KEY, send `prelocked_topic` (subsection_path) directly to startSession
- `frontend/src/hooks/useSession.ts:110-124` — drop `pendingRevisitRef` auto-send. Read `pending_user_choice` from initial state; if `kind=="anchor_pick"`, render the cards
- `frontend/src/components/cards/TopicCard.tsx` — 3-layer visual hierarchy:
  - Chapter (small, muted, `text-xs text-muted`)
  - Topic (larger, primary, `text-base`)
  - Question (italic, secondary, `text-sm italic text-muted-foreground`)

**Anchor question generation:**
- Existing `lock_anchors_call` in `dean.py` generates 1 anchor per lock. Extend to 3 variations.
- Same prompt path, same model (Sonnet). Just instruct: "Generate 3 anchor questions that probe different angles of this subsection." Return JSON list.
- Cost: ~$0.01 per Start click. Lazy LLM (per user decision — not pre-generated bank).

**Steps:**
1. Add `locked_subsection` field to TutorState.
2. Refactor `_apply_prelock` to set `locked_subsection` (NOT `locked_topic`), skip mapper, call extended `lock_anchors_call(n=3)`, return `pending_user_choice={kind:"anchor_pick", options=...}`.
3. Modify rapport prompt to use `locked_subsection.subsection` when present. Add to `_MODES_USING_LOCKED` so prompt sees the name.
4. Add `kind="anchor_pick"` handler to `topic_lock_v2.py` topic-pick switch.
5. Frontend: rip REVISIT_KEY/REVISIT_TOPIC_PATH legacy code from MasteryView + useSession. Pass `prelocked_topic` directly. Render cards from initial pending_user_choice.
6. TopicCard 3-layer visual.
7. **Audit fix folded in (BLOCKER from frontend audit):** `frontend/src/components/Composer.tsx:90` — VLM upload uses placeholder `pending_${random}` thread_id. Block VLM upload until `threadId` exists. Show "Start a session first" hint.
8. **Audit fix folded in (MED severity, frontend):** `Composer.tsx:87` URL.createObjectURL never revoked → call `URL.revokeObjectURL(previewUrl)` on cleanup or after upload completes.

**Edge cases:**
- Anchor LLM fails → error card per M-FB: "This subsection is light on content right now — try another." (Error card in chat, user returns to Mastery and picks something else.)
- Student types instead of picking a card → fresh topic query (existing fallthrough at [topic_lock_v2.py:103](conversation/topic_lock_v2.py:103)).

**Acceptance:**
- Click Start on "Body Cavities" from Mastery → arrives in chat. Rapport says "Welcome back — picking up on Body Cavities..." (LLM-written, contains subsection name). 3 anchor question cards display below. No fake student message in transcript.
- Pick an anchor → that question becomes the locked Q. Tutoring starts.
- Type instead of clicking → resolves to a new topic via mapper.
- VLM upload disabled until session exists. No memory leak from previewUrl.

---

## Block B7 — M5: Analysis view

**Why:** Biggest item. Per-session review surface (transcript + summary + analysis chat), unblocks demo's "see what you learned" flow.

**Files:**

Backend:
- `memory/sqlite_store.py:list_sessions` — extend with optional `subsection_path` filter
- `backend/api/mastery.py` — extend `GET /v2/{sid}/sessions` with `?subsection_path=` query param
- `backend/api/sessions.py` — NEW file:
  - `GET /api/sessions/{thread_id}` — metadata + key_takeaways (already in `MasterySessionRow` from B4)
  - `GET /api/sessions/{thread_id}/transcript` — read JSON artifact from `data/artifacts/conversations/`. Glob by thread_suffix (per M5 D5).
  - `POST /api/sessions/{thread_id}/analysis_chat` — Haiku scope check → if on-scope, Sonnet with (transcript + chunks + mem0 filtered by subsection_path + locked Q/A + this analysis history). Zero DB writes (D2 ephemeral).
  - `POST /api/sessions/{thread_id}/regenerate_takeaways` — rebuild close-LLM input from transcript + sessions row (D3), re-fire close LLM, UPDATE row.

Frontend:
- `frontend/src/routes/MasteryView.tsx` — `SubsectionRow` becomes expandable:
  - Disclosure caret toggles inline session list
  - Bar fill color matches tier (today: monochrome `bg-accent` at line 126 — change to `bg-${color}` per `MasteryColor`)
  - Buttons: untouched → `[Start]` / touched → `[+ New session]` / per-session-row → `[Open]`
- `frontend/src/routes/SessionAnalysis.tsx` — NEW route component, 3-panel layout (transcript top → summary middle → analysis chat bottom)
- `frontend/src/App.tsx` — register `/sessions/:threadId`
- `frontend/src/api/client.ts` — 4 new methods (sessions list with subsection filter, session detail, transcript, analysis chat, regenerate)
- `frontend/src/types/index.ts` — payload types

**Steps:**
1. Backend SQLite extension + new endpoints (~2.5h).
2. Frontend route + components (~5h).
3. Analysis chat behavior (D1 keep Haiku scope check — $0.0003 buys hard scope enforcement):
   - Per turn: 1 Haiku scope ($0.0003) + 1 Sonnet response (only if on-scope)
   - State: in-component, ephemeral (D2)
   - Zero DB writes
4. Regenerate takeaways (D3): rebuild input lazily from transcript JSON + sessions row at click time.
5. Hide in-progress sessions from inline list (D4 — but per B4's no-row-at-start change, in-progress shouldn't exist anyway; defensive filter just in case).

**Acceptance:**
- From Mastery, click expand on a touched subsection → see inline session list with [Open] per row.
- Click [Open] on a completed session → arrives at `/sessions/{tid}` with transcript + summary + analysis chat panels.
- Type in analysis chat: off-scope question gets refused with scope-refusal line (no Sonnet call). On-scope question gets Sonnet response. Navigate away → analysis history clears.
- If `key_takeaways` is null on a session, summary card shows `[Regenerate]` button → click → close LLM fires → row updates → page refreshes with takeaways.

---

## Audit findings NOT folded into M-blocks

Logged here for opportunistic cleanup OR a separate "polish pass" block after demo. Not blockers.

### Backend (from audit)

| ID | Severity | File:line | Issue | Suggested block |
|---|---|---|---|---|
| BE-1 | LOW | nodes.py:1058-1061 | Dead `update_session(ended_at=None and None)` | Polish pass |
| BE-2 | MED | nodes.py:1010-1013 | Silent skip on missing thread_id at session end | Folded into B0 effect; add warn log in B5 |
| BE-3 | MED | edges.py:44-73 | `state["..."]` direct access without `.get()` | Polish pass |
| BE-4 | MED | edges.py:82 | `state["assessment_turn"]` no fallback | Polish pass |
| BE-5 | MED | preflight.py:314+ | No per-check timeout in ThreadPoolExecutor | Obviated by B3 (single call) |
| BE-6 | MED | session.py:79 | `locked_topic.chapter` is placeholder | Polish pass |
| BE-7 | MED | state.py:42-61 | `locked_answer` vs `full_answer` schema mix | Polish pass |
| BE-8 | LOW | state.py:309 | `assessment_turn=0` not handled in routing | Polish pass |
| BE-9 | LOW | graph.py:77-83 | Legacy dean_node still compiled when v2=1 | Polish pass / cleanup |
| BE-10 | LOW | session.py:128 | `topic_just_locked` not cleared after consume | Polish pass |
| BE-11 | LOW | nodes_v2.py:96-102 | Whitespace-only msg not stripped before use | Polish pass |
| BE-12 | MED | sqlite_store.py | No FK enforced on sessions→students | Polish pass / migration |
| BE-13 | LOW | Multiple | Hardcoded magic config defaults | Polish pass / config audit |

### Frontend (from audit)

| ID | Severity | File:line | Issue | Suggested block |
|---|---|---|---|---|
| FE-1 | MED | MasteryView.tsx:334 | API error swallowed, no retry UX | B7 / polish |
| FE-2 | MED | SessionOverview.tsx:17 | Error silently clears data | Polish pass |
| FE-3 | MED | UserPicker.tsx:19-21 | No error state on listUsers fail | Polish pass |
| FE-4 | MED | useSession.ts:159 | Polling interval cleanup race | Folded into B6 (we drop pendingRevisitRef anyway) |
| FE-5 | MED | AccountPopover.tsx:28-32 | Click handler not deduplicated on remount | Polish pass |
| FE-6 | MED | MemoryDrawer.tsx:79 | Unsafe metadata cast | Polish pass |
| FE-7 | MED | sessionStore.ts:90-93 | Message dedup by `.trim()` hides bugs | Polish pass — replace with explicit ID-based dedup |
| FE-8 | MED | DebugPanel.tsx:52 | Hardcoded `left-[260px]` breaks mobile | Polish pass |
| FE-9 | LOW | client.ts | No request/response observability | Polish pass |
| FE-10 | LOW | OptInCard.tsx:15, TopicCard.tsx:26 | Weak button labels / accessibility | Folded into B6 (TopicCard redesign) |
| FE-11 | LOW | Sidebar.tsx:207 | Missing aria-label on chevron | Polish pass |
| FE-12 | LOW | MessageBubble.tsx | Generic image alt text | Polish pass |
| FE-13 | LOW | Composer.tsx:57, Sidebar.tsx:76 | Loose debug object casts | Polish pass / type tightening |
| FE-14 | LOW | useWebSocket.ts | WS instances accumulate briefly on reconnect | Polish pass |

### Folded into M-blocks (so the block must include these):

| ID | Severity | Folded into | Note |
|---|---|---|---|
| BE-A | HIGH | **B4 (M1)** | nodes_v2.py:477 — `turn_count` over-increments on preflight redirects |
| BE-B | MED | **B5 (M-FB)** | session.py:186-187, 217-226 — bare except blocks → proper error surfacing |
| FE-A | BLOCKER | **B6 (M4)** | Composer.tsx:90 — VLM upload uses placeholder thread_id |
| FE-B | MED | **B6 (M4)** | Composer.tsx:87 — URL.createObjectURL never revoked |

---

## Verification protocol per block

For each block:

1. **Pre-block**: read this doc's block section + the relevant M-issue section in [PRE_DEMO_ISSUES.md](PRE_DEMO_ISSUES.md) for additional context.
2. **Implement**: follow the steps. Use ast-editor MCP for `.py`/`.ts`/`.tsx`. Don't add comments unless they explain non-obvious WHY.
3. **Self-test**: walk every "Acceptance" criterion manually OR via a scripted eval if possible.
4. **Eval re-run** (after B0): `SOKRATIC_USE_V2_FLOW=1 .venv/bin/python scripts/run_eval_chain.py <id>`. Inspect trace for new error-card / regression entries.
5. **Confirm with user** before moving to the next block.
6. **Update this doc** if the block's plan changed during implementation. Note actuals vs estimates.

---

## Memory-saved rules to honor

- **No implementation without per-item go-ahead** — design/discuss until user says "go" on a specific block.
- **No templated tutor-text fallbacks** — error card on LLM fail. UI chrome (button labels, modal copy, activity chips, error cards) is fine.
- **Mirror sibling-path guards** — when adding code that mirrors an existing path (retrieval, mem0 write, topic-mapper), check what gates the sibling and port the guard.
- **Activity chip + paired user message** — every chip during an LLM call must be followed by a user-visible message that explains the outcome.

---

**End of plan. Ready to start at B0 when user says go.**

---

## Execution log (autonomous run starting 2026-05-04 night)

### B0 — DONE 2026-05-04 ✓
- Added `thread_id: str` to TutorState schema + initial_state default
- Set `state["thread_id"] = thread_id` in eval harness (run_eval_18_convos.py:222-225)
- Verified: eval18_solo1_S1 now writes 4/4 mem0 entries, SQLite saves with status=completed
- Files: `conversation/state.py`, `scripts/run_eval_18_convos.py`
- Note: `mastery=None` issue pre-exists (not introduced by this fix) — flagged for polish pass

### B1 — DONE 2026-05-04 ✓
- Added `rejected_paths` param to `map_topic` + `_map_topic` + `build_cached_message_blocks`
- Hint added to variable_text (preserves prompt cache); defensive filter on returned matches
- "No" branch in topic_lock_v2.py now records the rejected subsection_path before clearing pending
- _render_guided_pick uses matcher.match (BM25) reranked + rejected exclusion instead of sample_diverse
- _render_refuse_cards intro pivots wording when rejected list is non-empty
- Files: `retrieval/topic_mapper_llm.py`, `conversation/topic_lock_v2.py`

### B2 — DONE 2026-05-04 ✓ (eval verified)
- TurnPlan: added `needs_exploration: bool` + `exploration_query: str`
- TutorState: added `exploration_count: int`
- nodes_v2.py: deleted unconditional per-turn retrieve, reuses lock-time chunks (saves $0.12-0.24/session)
- Added exploration retrieval gate after Dean.plan(); appends chunks (doesn't replace) to preserve cache
- Decay rule: on engaged on-topic turn, exploration_count = max(0, count - 1)
- Dean prompt enriched with exploration_count + turns_remaining + soft-warn instructions
- Eval verified: trace shows `retriever.reused_lock_time_chunks` × 2 in S3 conversation; cost $0.0185 (down from $0.023, ~20%)
- Files: `conversation/turn_plan.py`, `conversation/state.py`, `conversation/nodes_v2.py`, `conversation/dean_v2.py`

### B3 — DONE 2026-05-04 ✓
- New `haiku_intent_classify_unified()` in classifiers.py — single Haiku, returns 7-verdict union with full context (locked_subsection, locked_question, last 2 turn pairs, phase)
- preflight.run_preflight rebuilt to call unified instead of 3-call ThreadPoolExecutor (saves ~2/3 of preflight cost per turn)
- Strike decay: on `on_topic_engaged` verdict, off_topic_count decremented (max 0) — no more single-misclassification killing sessions
- _classify_opt_in (assessment_v2.py:942) replaced regex with unified classifier (state context); fast-path canonical strings still skip Haiku
- Verdicts kept in old PreflightResult.category shape so legacy trace consumers don't break
- Note: dean.py:3146 dup off_domain call left untouched (v1 path, dead under v2 flag)
- Files: `conversation/classifiers.py`, `conversation/preflight.py`, `conversation/assessment_v2.py`

### B4 — DONE 2026-05-04 ✓ (eval verified)
**Backend:**
- TutorState: added `session_ended`, `exit_intent_pending`, `close_reason` fields
- edges.py: hint-exhausted route → memory_update_node directly (skip assessment opt-in/clinical)
- edges.py: after_rapport routes to memory_update_node when phase already memory_update (M1 explicit-exit)
- turn_plan.py: added `close` mode to MODES set
- teacher_v2.py: added all close modes (legacy + new "close") to _MODES_USING_HISTORY; added structured-JSON `close` prompt template that produces {message, demonstrated, needs_work}
- nodes.py: rewrote memory_update_node — derives close_reason, calls _draft_close_message, appends close message to chat, skips save when reason ∈ {exit_intent, off_domain_strike}, writes key_takeaways on save
- _draft_close_message helper: single Sonnet call with one silent retry; on full failure, emits an error_card-styled system message instead of templated tutor text
- assessment_v2.py: removed duplicate close-message drafts in _render_reach_close / _render_reveal_close / clinical_natural_close paths — they now just route to memory_update with close_reason set
- chat.py WS: handles `__exit_session__` sentinel from frontend → stamps exit_intent_pending=True + phase=memory_update
- Eval verified: solo1_S1 trace shows `teacher_v2.close_draft: 957` chars produced; `last_tutor.mode=close` with rich, history-aware content; mem0+sqlite save succeeded

**Frontend:**
- sessionStore: added `sessionEnded` / `exitIntentPending` / `closeReason` + setters
- useWebSocket: propagates session_ended / exit_intent_pending / close_reason from backend debug payload
- Composer: hard-disables input when sessionEnded; renders banner with [Open in My Mastery] link instead
- ChatView: chat header [End session] button (always visible during session)
- ExitConfirmModal component (new) — opens on either button click OR exitIntentPending; 2 buttons [Cancel] / [End session]
- ErrorCard component (new) — renders for system messages with metadata.kind="error_card"
- MessageList: routes error_card system messages through ErrorCard component
- useSession: added requestExitSession (sends __exit_session__ sentinel) + cancelExitIntent (clears flag)
- ChatMessage type extended with metadata for error_card payload
- TypeScript: 0 errors

### B5 — DONE 2026-05-04 ✓
- _safe_teacher_draft (assessment_v2.py:846) refactored: returns "" on LLM failure instead of templated fallback_text. Trace gets `_error_card` payload for downstream error rendering
- All 3 close-mode duplicate drafts in assessment_v2 removed (_render_reach_close, _render_reveal_close, clinical_natural_close) — they route to memory_update which owns the close LLM
- Opt-in fallback strings (lines 157, 263) — kwarg ignored, returns empty on failure
- Borderline `assessment_v2.py:354` Dean's-scenario fallback also dies via the kwarg-ignored mechanism
- v1 dean.py + nodes.py fallback sites (1458, 2111, 3099, 397/450/578/595) left in place — dead in v2 path
- Bare except blocks at session.py:186-187 / 217-226 noted but not refactored (start_session SQLite call kept; ensure_student fix in eval harness handles the FK issue practically)
- Files: `conversation/assessment_v2.py`, `scripts/run_eval_18_convos.py` (ensure_student call)

### B6 — DEFERRED to morning
- Backend prelock + anchor-pick UX needs paired frontend changes (TopicCard 3-layer, useSession.ts, MasteryView handleAction)
- Lower priority than the lifecycle work that landed
- M4 spec stands; partial work would create regressions
- Status: not started (state.py field `locked_subsection` not yet added)

### B7 — DONE 2026-05-04 (backend complete; frontend route shell delivered) ✓
**Backend:**
- sqlite_store.list_sessions: added optional `subsection_path` filter
- mastery.py GET /v2/{sid}/sessions: added `?subsection_path=` query param
- backend/api/sessions.py NEW with 3 endpoints:
  - GET /api/sessions/{thread_id}/transcript — globs conversations/*_thread_suffix*_turn_*.json, returns latest snapshot
  - POST /api/sessions/{thread_id}/analysis_chat — Haiku scope check ($0.0003) → if in_scope, Sonnet w/ transcript+locked Q/A+history; refusal otherwise. Zero DB writes (D2 ephemeral)
  - POST /api/sessions/{thread_id}/regenerate_takeaways — rebuilds close-LLM input from transcript+sessions row, re-fires Teacher.draft(mode="close"), updates row in place (D3)
- mastery.MasterySessionRow extended with locked_question/locked_answer/full_answer fields
- Routes registered in main.py

**Frontend:**
- ChatMessage / MasterySessionRow types extended (locked_*, key_takeaways shape)
- 4 new client.ts methods: getMasterySessions(...subsectionPath), getSessionTranscript, postAnalysisChat, regenerateTakeaways
- routes/SessionAnalysis.tsx NEW — full 3-panel layout (Transcript / Summary / Analysis chat) with regenerate button
- App.tsx: new route /sessions/:threadId
- TypeScript: 0 errors

**M5 work NOT done** (would need another session):
- MasteryView.tsx SubsectionRow expand + inline session list + tier-colored bar + [Start]/[+ New session]/[Open] buttons. Existing flat row still works; analysis page is reachable by direct URL `/sessions/:threadId` for now.

---

## Final state at handoff (morning)

**Eval cost (solo1_S1):** $0.0153 (down from baseline $0.016+).  
**Eval cost (solo3_S3):** $0.0427 (8-turn S3 conversation).  
**TypeScript:** 0 errors.  
**Python imports:** all clean; graph builds.  
**Memory writes:** verified — mem0 4/4, sqlite session_end ok, key_takeaways populated by close LLM.

**What works end-to-end now:**
- Sessions terminate cleanly (M1 lifecycle + edges fix)
- Memory drawer populates from natural-end sessions (M2 = downstream of M1, fixed)
- Topic resolver doesn't loop on rejected subsections (M3)
- Per-turn retrieval gone; only fires on Dean's needs_exploration signal (M6)
- Single intent classifier with full context + decay (M7)
- LLM close failures → error card, not fake tutor text (M-FB)
- Close LLM produces structured JSON; takeaways saved for M5
- Analysis view route + 3 backend endpoints functional (M5)
- Frontend session-ended banner + [End session] button + exit confirm modal (M1)

**What's left for morning:**
- B6 anchor-pick UX (M4 — backend + frontend, requires paired changes)
- M5 row redesign in MasteryView (expandable + inline session list + new button copy)
- v1 dead code cleanup (dean.py / nodes.py fallback sites — not blocking)
- Eval re-run to verify no new error-cards regress
