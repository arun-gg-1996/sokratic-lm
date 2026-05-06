# Demo Flow Simulation Plan — 2026-05-06

Purpose: run a small number of high-value browser simulations that exercise the whole Sokratic system before the presentation. The agent should use Chrome/local UI, adapt naturally as the tutor responds, export/inspect JSON after each run, and keep a running log of both flow correctness and conversation quality.

Recommended model: GPT-5.4 is sufficient. Use careful trace inspection after each run.

## Ground Rules

- Start from `http://localhost:5173` unless a scenario says otherwise.
- Keep the student id as `nidhi` unless the app already has a different active user.
- Do not blindly follow a fixed script. Use the target behavior, then adapt to whatever question the tutor actually asks.
- After each run, export or locate the saved conversation JSON and inspect it.
- Record all findings in `docs/DEMO_FLOW_SIMULATION_LOG_2026-05-06.md`.
- A run is not “pass” just because it reaches a terminal state. Judge the quality of the tutoring too.
- For every completed run, copy the exported JSON and any useful screenshots into `data/artifacts/simulated_convos/YYYY-MM-DD_HHMM_<agent>_<scenario-slug>/`.
- In the run folder, include a `notes.md` with: who ran it, model used, start/end time, browser URL, final close reason, and main verdict.
- After every memory update, inspect what was written to memory and mastery: mem0 summary writes, SQLite/session row, mastery score/tier/confidence, My Mastery visible state, and analysis page values.
- Record wall-clock duration for every run: start time, end time, and total minutes.

## Speed Rules For GPT-5.4

The goal is faster testing, not cheating. Do not bypass the UI, inject backend state, or pre-write answers into storage. You may speed up honestly by:

- Use direct, concise student messages.
- Use reliable topic phrases already known to lock well: `B cell differentiation and activation`, `compact and spongy bone`, `directional terms`, `respiratory zone`, `tissue membranes`.
- When testing a branch, drive straight to that branch. Example: for opt-in No, answer the tutoring question correctly as soon as possible, then click No.
- Use one partial answer only when the scenario needs to test sycophancy/hinting.
- Do not test audio/TTS or Listen buttons; they are intentionally out of scope for this pass.
- Do not open analysis/My Mastery after no-save runs unless the scenario requires proving no-save behavior.
- Export/inspect JSON only after terminal states or when a stop condition appears.
- If a tutor asks an unexpected but valid question, answer it naturally rather than restarting.
- If the app is clearly stuck for more than 90 seconds with no activity, record as UI/backend stall and move to trace inspection.
- Prefer shorter full-conversation runs and separate 1-2 minute edge probes for UI-only branches.

Timing fields required in the log:
- `Started at:`
- `Ended at:`
- `Duration:`
- `Slowest step observed:`

## Full Scenario Surface

The app has more than one “happy path.” The current practical surface is about 18 scenario families:

| # | Scenario Family | Main Branches |
|---|---|---|
| 1 | Fresh free-topic session | rapport → topic lock → tutoring |
| 2 | Strong topic lock | direct lock with `topic_ack` |
| 3 | Confirm-topic card | ambiguous but likely topic → Yes/No confirm |
| 4 | Topic-card/guided-pick | weak/vague topic → cards/custom/end option |
| 5 | My Mastery prelock | `/mastery` revisit → anchor-pick/prelocked topic |
| 6 | Anchor-pick resolution | choose starter question, pivot, or custom |
| 7 | Normal tutoring reach | student reaches locked answer |
| 8 | Clinical opt-in Yes | assessment opt-in → clinical scenario |
| 9 | Clinical opt-in No | assessment opt-in → `reach_skipped` |
| 10 | Clinical opt-in ambiguous | re-ask opt-in |
| 11 | Clinical completion | correct clinical answer → `reach_full` |
| 12 | Clinical cap | weak clinical answers → `clinical_cap` |
| 13 | Hints exhausted | hint level exceeds max → `hints_exhausted` |
| 14 | Low-effort counter | `idk` / no attempts → counter and angle shifts |
| 15 | Help-abuse counter | “tell me answer” demands → counter/redirect |
| 16 | Off-topic counter | unrelated content → redirect/strike behavior |
| 17 | End-session modal | user clicks End session → confirm/cancel |
| 18 | Backend-triggered exit intent | user asks to end mid-plan → modal, cancel soft reset, or confirm no-save |
| 19 | Multimodal image session | upload image → VLM context → topic lock |
| 20 | Analysis/history review | saved session review, trace, score, transcript |
| 21 | Repeat topic after memory | later same topic should use mem0/SQLite carryover |
| 22 | New chat/stale websocket | completed session → new clean thread |
| 23 | Error/fallback UI | backend/VLM/teacher error card rendering |
| 24 | Debug controls | Trace-inspection affordances |
| 25 | Responsive layout | narrow viewport/card/composer/sidebar behavior |
| 26 | Reconnect/lost websocket | connection banner and disabled input behavior |
| 27 | Rapid interaction races | double-clicks, repeated submit, stale pending choice |

The 10 required simulations below collapse this surface into a manageable test set. Optional probes cover the remaining branches.

## What To Check Every Run

Flow:
- Sidebar phase matches actual conversation: `RAPPORT`, `TUTORING`, `CLINICAL`, `WRAPPING UP`/memory update.
- Topic lock is correct and stable.
- Pending cards appear and disappear correctly.
- Counters increment when expected: low-effort, help-abuse, off-topic, clinical turn.
- Close reason is correct in export: `reach_full`, `reach_skipped`, `hints_exhausted`, `clinical_cap`, `tutoring_cap`, etc.
- Memory save completes and the session appears in history/mastery where expected.

Conversation quality:
- No answer leak before the student reaches the answer.
- No sycophancy: no “Exactly / nailed it / right track” on wrong or weak answers.
- Hints are progressive, not repetitive.
- Tutor asks exactly one clear question on tutoring turns.
- Tutor remains on the locked topic after off-topic or help-abuse turns.
- Clinical scenario is grounded in the locked concept.
- Clinical stops when the student correctly applies the locked concept.
- Final close accurately describes what happened.

Export trace checks:
- `debug.turn_trace` and `debug.all_turn_traces` contain expected wrappers.
- For clinical opt-in: expect `assessment_v2.opt_in_response` with `intent: yes`, then `clinical_scenario_gen_start`, then `clinical_phase_entered`.
- For clinical completion: expect `assessment_v2.clinical_target_reached` or an equivalent completion marker.
- No `NoneType` errors, empty tutor messages, or fallback close after Yes.

Memory and persistence checks after each saved conversation:
- Export JSON path and copied artifact folder path.
- `memory_update_node.transcript_snapshot` result.
- `memory_manager.flush` result and mem0 write count/content categories.
- `mastery_store.update` result: mastery, confidence, sessions.
- `sqlite_store.session_end` result: status, score, path set.
- My Mastery row after refresh: chapter, section, subsection, tier, score.
- Analysis page: transcript length, close reason, locked topic, score/tier, debug trace visible.
- Confirm no-save behavior for `exit_intent` and `off_domain_strike`: transcript may exist, but mem0/mastery/sqlite save should be skipped per design.

UI/UX checks every run:
- No overlapping text, hidden buttons, clipped cards, or stuck spinners.
- Composer disabled only when it should be disabled.
- Pending cards do not remain after a choice or after memory update.
- Activity log labels match backend phase and do not contradict final outcome.
- Sidebar phase, counters, and topic metadata update in sync with the chat.
- End-session modal can be canceled by button and backdrop without leaving stale state.
- `+ New chat` clears old messages, pending choices, counters, websocket state, and activity log.

Cross-cut checkboxes (added 2026-05-06 — POST_DEMO_FIXES.md S3/S4):
- **Exploration phase visible?** When the student wanders off the locked
  topic (in-domain tangent), the sidebar should show the `EXPLORING`
  sub-badge with `query` + `goal` metadata (A1). Mark N/A on sims
  without a tangent.
- **Memory injection visible?** On rapport with prior sessions on the
  same subsection, the activity log should emit "Recalling what worked
  last time on {subsection}". On a hint-advance turn after prior
  observations exist, expect "Loading your learning style from past
  sessions" (A3). Mark N/A on cold-start sessions.
- **Markdown rendering correct?** Bold/italic/lists should render in
  message bubbles + analysis-page transcript (N5). No flashing
  `**half-typed` asterisks during streaming.
- **Diagnostic counters update?** On help_abuse / off_topic / low_effort
  turns, the sidebar telemetry pill ("Total: N low / N off / N demand")
  should tick up (F6). Total counters should NEVER drop to 0 mid-session
  even after engagement resets the consecutive counters.

## Required Simulations

Run all 10 if time allows. These are designed to hit most important conditionals with minimal redundancy.

### 1. Happy Path To Clinical Completion

Start: new chat.

Topic prompt:
`B cell differentiation and activation`

Behavior:
- Answer the first tutoring question correctly, but only after one partial attempt if useful.
- Click/answer Yes to clinical opt-in.
- Answer the first clinical case by applying the locked concept.

Expected:
- Topic locks to `B Cell Differentiation and Activation`.
- Tutoring reaches answer.
- Clinical scenario appears after Yes.
- Correct clinical answer closes the clinical phase, not another chain of unrelated clinical questions.
- Final close reason should be `reach_full`.

Quality risks to watch:
- Sycophancy after partial answer.
- Clinical scenario appears but then keeps asking after correct application.
- Empty clinical message.

### 2. Opt-In No Path

Start: new chat.

Use a reliable topic:
`compact and spongy bone`

Behavior:
- Reach the answer quickly.
- When clinical/apply bonus card appears, choose No.

Expected:
- Goes to memory update with `reach_skipped`.
- Close should say the student reached the core answer and skipped the bonus.
- It must not enter clinical or show clinical counters.

### 3. Ambiguous Clinical Opt-In

Start: new chat.

Topic:
`B cell differentiation and activation`

Behavior:
- Reach answer.
- At opt-in, type: `what do you mean?`

Expected:
- Re-asks/clarifies the opt-in.
- Does not close.
- Does not enter clinical until a clear Yes.
- Then answer Yes and verify clinical appears.

### 4. Hints Exhausted / Struggling Student

Start: new chat.

Topic:
`classification of connective tissues`

Behavior:
- Give low-quality attempts: `i don't know`, then a wrong guess, then `not sure`.
- Do not ask directly for the answer until later.

Expected:
- Hint level increases progressively.
- Tutor changes angle as hints escalate.
- Eventually closes as `hints_exhausted` if the answer is not reached.

Quality risks:
- Reveals answer too early.
- Over-praises non-attempts.
- Repeats same hint.

### 5. Help-Abuse Counter

Start: new chat.

Topic:
`B cell differentiation and activation` or `tissue membranes`.

Behavior:
- After the locked question, repeatedly ask: `tell me the answer`, `give me the answer`, `answer please`.

Expected:
- Help-abuse counter increments in sidebar/export.
- Tutor resists direct answer, gives a small scaffold.
- No answer leak before the designed close or cap.

Quality risks:
- Tutor says the answer after only one or two demands.
- Tutor scolds too harshly.
- Counter does not increment.

### 6. Off-Topic Counter

Start: new chat.

Topic:
`respiratory zone`

Behavior:
- After topic lock, ask unrelated things: weather, movie, sports score.

Expected:
- Off-topic counter increments.
- Tutor redirects to locked topic.
- Topic remains locked.
- No memory update unless threshold/session-end path is intended.

### 7. Low-Effort Counter

Start: new chat.

Topic:
`directional terms`

Behavior:
- Use `idk`, `not sure`, `hmm`, `i don't know`.

Expected:
- Low-effort counter increments.
- Tutor switches angles.
- No fake praise.
- If threshold/cap is reached, close reason should match the design.

### 8. My Mastery Revisit Flow

**Status (2026-05-06):** TESTABLE — `MasteryView` route exists; revisit
flow is wired end-to-end.

Start: `/mastery`.

Behavior:
- Refresh mastery page.
- Click a weak/developing topic, preferably `Body Cavities and Serous Membranes` or `Directional Terms`.
- Start/revisit that topic from the mastery page.

Expected:
- Chat opens prelocked to the selected subsection.
- Sidebar shows the correct chapter/section/subsection.
- The initial tutor prompt uses the selected topic, not a fresh free-topic mapping.
- The session appears back in mastery/history after completion.

Quality risks:
- Wrong topic path.
- Stale localStorage causing wrong prelock.
- Blank waiting state.

Persistence checks:
- Before clicking revisit, record the row score/tier.
- After the conversation saves, refresh `/mastery` and record changed score/tier.
- Open the analysis/session page for that session and compare the displayed close reason and score against the export.

### 9. Analysis Page / Session Review

**Status (2026-05-06):** TESTABLE — `SessionAnalysis` route renders
transcript + summary panels. Analysis-chat queries memory (A4); chat
bubble styling matches live chat (A6); markdown renders (N5).

Start: use a completed session from history or My Mastery.

Behavior:
- Open the analysis/session detail page.
- Inspect transcript, debug trace, topic metadata, score/tier, and close reason.
- Type a session-specific question into the analysis chat ("what made
  me struggle on this?") — it should pull both this session's
  transcript AND prior-session mem0 observations on the same
  `subsection_path` (A4).

Expected:
- Transcript matches the real session.
- Debug trace is readable.
- Mastery score/tier and close reason match export JSON.
- No missing/undefined UI fields.
- Tutor bubble has icon + panel-card styling (matches live chat).
- Student turns render right-aligned with `bg-accent-soft` pill.
- Markdown formatting (bold/italic/lists) renders inside transcript
  bubbles.

### 10. Multimodal Upload

Start: new chat.

Behavior:
- Upload one of the curated VLM hero images from `vlm/images`.
- Ask the system to help identify/explain the relevant structure/topic.
- Continue through topic lock and at least one tutoring turn.

Expected:
- Image preview appears in student bubble.
- Backend receives image context.
- Topic mapping uses image context.
- Normal tutoring flow proceeds after lock.

Recommended image choices:
- Happy-path strong lock: `vlm/images/strong/01_HERO_strong_inhalation_breathing.jpg`
  - Shows inhalation mechanics.
  - Expected lock: **Process of Breathing**.
- Multilingual strong lock: `vlm/images/strong/02_HERO_strong_heart_atrial_septal_defects_DUTCH.jpg`
  - Dutch-labeled atrial septal defect diagram.
  - Expected lock: **Heart: Heart Defects**.
- Graceful fallback/out-of-scope: `vlm/images/weak/06_HERO_weak_auscultation_fallback_demo.png`
  - Auscultation points, more clinical exam than textbook anatomy.
  - Expected behavior: refuse/starter cards or fallback path, not a confident wrong lock.

Quality risks:
- Image upload card/button hidden unexpectedly.
- Image context not used.
- Topic maps to unrelated subsection.
- VLM failure not surfaced clearly.

Persistence checks:
- Export should include image context or image-related system/debug evidence.
- Student bubble should show image preview.
- Saved transcript should preserve image reference if supported.

### 11. Explicit End Session And Cancel

Start: new chat, after at least one meaningful student turn.

Behavior:
- Click the visible `End session` button.
- First choose `Cancel`.
- Continue the conversation with one normal answer.
- Then click `End session` again and choose `End session`.

Expected:
- Cancel closes modal and sends `__cancel_exit__`.
- Backend clears `exit_intent_pending`.
- Tutor gives a soft reset / continuation, not memory update.
- Confirming end routes to memory update with `close_reason=exit_intent`.
- `exit_intent` is no-save: mem0/mastery/sqlite should be skipped, but transcript snapshot may still be written.

UI/UX risks:
- Modal reappears after cancel.
- Composer stuck disabled after cancel.
- Confirmed end leaves cards/composer visible.

### 12. Backend-Triggered Exit Intent Mid-Plan

Start: new chat, after topic lock.

Behavior:
- Type natural exit intent: `I want to stop this session` or `can we end here?`
- When modal appears, cancel once.
- Then type a normal content answer.

Expected:
- Backend sets `exit_intent_pending=true` and frontend opens modal.
- Cancel triggers `__cancel_exit__`, clears backend flag, logs system event, and allows normal continuation.
- No memory save on cancel.

### 13. Repeat Same Topic After Memory

Start: after completing Simulation 1 or 2 and refreshing app.

Behavior:
- Start a new chat later with the same topic phrase: `B cell differentiation and activation`.

Expected:
- Rapport references the prior work naturally if mem0 retrieval finds it.
- Topic locks to same subsection.
- Carryover should help but not leak the answer before the new question is answered.
- My Mastery score/session count should reflect prior attempt.

Quality risks:
- Memory over-personalizes or reveals answer.
- Same-topic stale state reuses old thread instead of new session.

## Optional Stress Probes

Run if the core simulations finish with time left. If there is time for a full sweep, run Simulations 11-13 above as required too, then run probes 14-16 below.

### 14. Ambiguous Topic Mapping

Use vague prompts:
- `blood stuff`
- `immune thing`
- `bones`

Expected:
- Either locks confidently when obvious, or shows cards/clarification.
- Should not hard-lock to a random wrong subsection.

### 15. Clinical Cap

Enter clinical, then give weak or off-target clinical answers.

Expected:
- Clinical turn counter increments.
- Eventually closes with `clinical_cap`.
- No endless loop.

### 16. New Chat / Stale WebSocket

Complete a session, click `+ New chat`, start another.

Expected:
- New thread starts cleanly.
- No immediate “session complete” from old thread.
- No duplicate websocket messages.

## Edge UI/Flow Probes

These are shorter probes. They do not all need full memory-update conversations. Still record duration and a short verdict. Use them to catch UI/UX and conditional gaps not covered by the 16 core simulations.

### 17. Confirm-Topic No Path

Goal: trigger a confirm-topic card, click No, then verify recovery.

How:
- Start with a phrase likely to be close but not exact, such as `immune cell activation` or `bone tissue`.
- If a confirm card appears, click No.

Expected:
- Confirm card disappears.
- App offers topic cards or lets the user rephrase.
- No stale topic lock in sidebar.

### 18. Topic Card Something Else / Custom Entry

Goal: verify topic-card custom flow.

How:
- Use a vague topic like `blood stuff`.
- If cards appear, click `Something else` if available, then type a clearer custom topic.

Expected:
- Cards disappear.
- Composer returns.
- Custom topic maps cleanly.

### 19. Topic Card Give Up / End Session Option

Goal: verify card-level end option.

How:
- Trigger topic cards.
- Click the `Give up / End session` option if shown.

Expected:
- Routes to memory update/no-save or the designed graceful close.
- Pending cards clear.
- No transcript confusion with fake student content.

### 20. Anchor Pick Pivot / Custom Behavior

Goal: verify My Mastery anchor picker beyond the default first option.

How:
- Start from `/mastery`, click a revisit topic.
- If anchor questions appear, pick a non-first card.
- If a pivot/custom affordance appears, try it.

Expected:
- Sidebar question matches selected anchor.
- No mismatch between chosen question and locked answer.

### 21. Upload Failure / Unsupported Image

Goal: verify graceful multimodal failure.

How:
- Try an unsupported or intentionally poor image if available, or cancel upload midway.

Expected:
- Clear error message/card.
- Composer recovers.
- No stuck image upload state.

### 22. WebSocket Reconnect / Lost Banner

Goal: inspect connection UI.

How:
- If safe, briefly stop/restart backend or disconnect network only if the user approves in the current working context. If not safe, skip destructive action and inspect code/available UI behavior.

Expected:
- Connection banner shows reconnect/lost state.
- Composer disables appropriately.
- After reconnect, no duplicate messages or old-thread routing.

### 23. Backend Error Card Rendering

Goal: verify error-card UI and retry affordance if a backend component fails.

How:
- Prefer non-destructive observation: inspect existing exports or code path for `metadata.kind="error_card"`.
- Only induce a failure if safe and reversible.

Expected:
- ErrorCard renders cleanly.
- It names component/error.
- Retry handler, if present, does not break layout.

### 24. Debug Trace Click UI

Goal: verify debug-mode trace inspection if available.

How:
- Enable debug mode if there is a visible toggle.
- Click a tutor bubble with activity/debug trace.

Expected:
- Trace panel/selection opens.
- Correct turn trace shown.
- Click again closes or changes selection cleanly.

### 25. Mobile / Narrow Viewport Smoke Test

Goal: catch responsive layout bugs.

How:
- Resize browser narrow or use device toolbar if available.
- Open chat, My Mastery, and one card/opt-in state.

Expected:
- Text does not overlap.
- Composer remains reachable.
- Sidebar/nav does not cover chat.
- Buttons fit without truncating critical labels.

### 26. Rapid Click / Double Submit Race

Goal: catch duplicate sends and stale pending-choice bugs.

How:
- On an opt-in or topic card, double-click once deliberately.
- In composer, press submit twice quickly once.

Expected:
- Only one student message is sent.
- Pending card clears once.
- No duplicate backend turns.

### 27. No-Memory Mode Toggle

Goal: verify memory-off behavior if the UI exposes the toggle.

How:
- Turn memory off before a short session.
- Complete or exit a session.

Expected:
- Rapport should not use prior memory.
- Save behavior should match product design for memory-off mode.
- My Mastery/SQLite behavior should be checked against intended design; record if unclear.

### 28. Analysis Page Missing/Partial Data

Goal: verify analysis page resilience.

How:
- Open analysis for no-save exit session, failed session, or old seeded transcript.

Expected:
- No undefined/null UI.
- Missing score/trace is explained or absent cleanly.

### 29. Off-Domain Strike To No-Save Close

Goal: verify full off-domain termination, not just counter increment.

How:
- Stay off-topic repeatedly until the designed threshold.

Expected:
- Closes with `off_domain_strike`.
- No-save bucket: mem0/mastery/sqlite skipped.
- Transcript snapshot may still exist.

## Running Log Template

Append one section per run to `docs/DEMO_FLOW_SIMULATION_LOG_2026-05-06.md`:

```md
## Simulation N — Name

Status: PASS / FLAKY / FAIL
Date/time:
Browser URL:
Export JSON:

### Script Actually Used
- Student:
- Tutor:
- Student:

### Expected Flow
- ...

### Actual Flow
- ...

### Trace Evidence
- `phase`:
- `close_reason`:
- Important wrappers:
- Counters:

### Conversation Quality
- Sycophancy:
- Hint leak:
- Repetition:
- Pedagogy:
- Clinical quality:

### Verdict
One or two sentences.

### Follow-Up Fixes
- ...
```

## Stop Conditions

Pause and report immediately if any of these appear:

- Clinical Yes routes to memory update.
- `NoneType` / backend exception appears in export.
- Empty tutor message is sent.
- Wrong topic lock after a clear topic phrase.
- My Mastery revisit starts the wrong subsection.
- New chat reuses a completed thread.
- Tutor leaks answer before reach in a demo-critical flow.

## Suggested Order

Run in this order:

1. Happy path to clinical completion
2. Opt-in No
3. My Mastery revisit
4. Analysis page
5. Multimodal upload
6. Help-abuse counter
7. Low-effort counter
8. Off-topic counter
9. Hints exhausted
10. Ambiguous opt-in
11. Explicit End Session and Cancel
12. Backend-triggered exit intent mid-plan
13. Repeat same topic after memory
14. Ambiguous topic mapping
15. Clinical cap
16. New chat / stale websocket
17-29. Edge UI/Flow probes as time permits; record duration and verdict for each

This front-loads demo-critical flows, then moves into stress behavior.
