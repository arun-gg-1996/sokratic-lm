# Demo Flow Simulation Log — 2026-05-06

Use this file while running `docs/DEMO_FLOW_SIMULATION_PLAN_2026-05-06.md`.

Summary:

| # | Simulation | Status | Export JSON | Main Finding |
|---|---|---|---|---|
| 1 | Happy path to clinical completion | PASS with issues | [arun_f78d4cde_turn_6.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_f78d4cde_turn_6.json) | Flow reached clinical and closed correctly; follow-ups are composer/send automation fragility and missing clinical mastery persistence. |
| 2 | Opt-in No path | PASS with issues | [arun_6e6b8906_turn_4.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_6e6b8906_turn_4.json) | Declining clinical worked, but the app emitted duplicate opt-in tutor messages before closing. |
| 3 | Ambiguous clinical opt-in | PASS with issues | [arun_83ad8d80_turn_5.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_83ad8d80_turn_5.json) | Ambiguous opt-in did not break flow, but the “clarification” mostly just re-asked the opt-in instead of explaining it. |
| 4 | Hints exhausted / struggling student | FAIL | [state snapshot](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/simulated_convos/2026-05-05_2244_gpt-5.5_sim4-hints-exhausted/arun_c517772d_state.json) | Did not terminate as `hints_exhausted`; answer leaked and tutoring continued. |
| 5 | Help-abuse counter | FAIL | [arun_2d00565d_turn_8.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_2d00565d_turn_8.json) | Help-abuse was detected in trace but visible counter never incremented; exit-intent no-save semantics also look broken. |
| 6 | Off-topic counter | PASS with issues | [arun_78c605a5_turn_6.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_78c605a5_turn_6.json) | Fourth off-topic turn ended the session, but it saved the wrong `close_reason` (`tutoring_cap`). |
| 7 | Low-effort counter | TODO |  |  |
| 8 | My Mastery revisit flow | TODO |  |  |
| 9 | Analysis page / session review | TODO |  |  |
| 10 | Multimodal upload | TODO |  |  |
| 11 | Explicit End Session and Cancel | TODO |  |  |
| 12 | Backend-triggered exit intent mid-plan | TODO |  |  |
| 13 | Repeat same topic after memory | TODO |  |  |
| 14 | Ambiguous topic mapping | TODO |  |  |
| 15 | Clinical cap | TODO |  |  |
| 16 | New chat / stale websocket | TODO |  |  |
| 17 | Confirm-topic No path | TODO |  |  |
| 18 | Topic card Something Else/custom | TODO |  |  |
| 19 | Topic card Give Up/End | TODO |  |  |
| 20 | Anchor pick pivot/custom | TODO |  |  |
| 21 | Upload failure/unsupported image | TODO |  |  |
| 22 | WebSocket reconnect/lost banner | TODO |  |  |
| 23 | Backend error card rendering | TODO |  |  |
| 24 | Debug trace click UI | TODO |  |  |
| 25 | Mobile / narrow viewport | TODO |  |  |
| 26 | Rapid click / double submit | TODO |  |  |
| 27 | No-memory mode toggle | TODO |  |  |
| 28 | Analysis page missing/partial data | TODO |  |  |
| 29 | Off-domain strike to no-save close | TODO |  |  |

---

Per-run required metadata:
- Run by:
- Model:
- Date/time start:
- Date/time end:
- Duration:
- Slowest step observed:
- Browser URL:
- Export JSON:
- Artifact folder under `data/artifacts/simulated_convos/`:
- Memory writes after memory_update:
- SQLite/session row after memory_update:
- Mastery score/tier/confidence after refresh:
- My Mastery visible row after refresh:
- Analysis page values:
- UI/UX notes:

---

## Simulation 1 — Happy Path To Clinical Completion

Status: PASS with issues
Date/time: 2026-05-05 22:24:34 EDT to 2026-05-05 22:32:03 EDT
Browser URL: `http://localhost:5173/chat`
Export JSON: [arun_f78d4cde_turn_6.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_f78d4cde_turn_6.json)

### Script Actually Used

- Start from fresh chat as `arun` (already active user).
- Topic: `B cell differentiation and activation`
- Partial tutoring answer: `It becomes an antibody-producing cell, but I don't remember the exact name.`
- Reach answer: `Plasma cell.`
- Opt-in: `Yes`
- Clinical answer: `The breakdown is at the step where activated B cells differentiate into plasma cells...`
- Recovery note: Chrome/computer-use got stuck on the composer send action after turn 1, so I recovered by sending the exact same messages over the live websocket thread `arun_f78d4cde`.

### Expected Flow

- Topic locks to `B Cell Differentiation and Activation`.
- Tutoring reaches the locked answer.
- Clinical opt-in Yes enters clinical.
- Correct first clinical answer closes with `reach_full`.
- Memory update writes transcript + memory + mastery/session state.

### Actual Flow

- Topic lock succeeded immediately.
- Partial answer was accepted as engaged and led to a Socratic follow-up.
- Tutor draft retried twice because `haiku_leak_check` rejected earlier drafts.
- `Plasma cell.` was accepted as the reached answer.
- Opt-in Yes entered clinical and produced a valid scenario.
- First clinical answer fired `assessment_v2.clinical_target_reached` and closed to `memory_update`.
- Session row completed successfully with `core_score=0.5`, `mastery_tier=developing`, `close_reason=reach_full` embedded in `key_takeaways`.

### Trace Evidence

- `assessment_v2.opt_in_response intent=yes`
- `assessment_v2.clinical_scenario_gen_start`
- `assessment_v2.clinical_phase_entered elapsed_ms=10044`
- `assessment_v2.clinical_target_reached reason=student_response_contains_locked_answer_or_alias`
- `memory_update_node.transcript_snapshot result=wrote arun_f78d4cde_turn_6.json (14 msgs)`
- `memory_manager.flush result=wrote_3_failed_0_dropped_0_of_3`
- `sqlite_store.session_end result=sqlite_session_end ok status=completed score=0.5 path_set=True`

### Conversation Quality

- Good: no leaked answer reached the user; the verifier caught two leaky drafts before one passed.
- Good: clinical scenario was directly grounded in the locked concept.
- Mild concern: the final close is a little over-warm (`That's a clean session — you got there on your own`) given the student needed a contextual scaffold first, though it is not a hard sycophancy failure.
- Latency concern: verifier retries noticeably slowed the tutoring turn.

### Verdict

Pass for the main presentation flow. The core end-to-end path now works: topic lock -> tutoring -> clinical opt-in -> clinical completion -> memory update.

### Follow-Up Fixes

- Investigate why successful clinical completion still leaves `clinical_mastery_tier=not_assessed` and `clinical_score=null` in the saved session row.
- Investigate `mastery_store.update result=updated mastery=None confidence=None sessions=None` despite the subsection mastery row updating to `ewma_score=0.5`.
- Re-test My Mastery and Analysis page as explicit follow-up UI checks for this same saved session; I did not trust Chrome enough in this run to treat the page navigation as signal.
- Revisit composer/send behavior under automation. It may be a computer-use limitation rather than a product bug, but it is noisy enough to keep an eye on.

---

## Simulation 2 — Opt-In No Path

Status: PASS with issues
Date/time: 2026-05-05 22:37:29 EDT to 2026-05-05 22:39:02 EDT
Browser URL: `http://localhost:5173/chat`
Export JSON: [arun_6e6b8906_turn_4.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_6e6b8906_turn_4.json)

### Script Actually Used

- Fresh session as `arun`
- Topic: `compact and spongy bone`
- First answer: `Compact bone is dense and solid, while spongy bone has trabeculae with open spaces.`
- Follow-up answer: `The key idea is osteon.`
- Clinical choice: `No`
- Run method: real `/api/session/start` + websocket session, no UI bypassing of backend logic

### Expected Flow

- Reach the locked answer quickly
- Show one clinical opt-in prompt
- On `No`, route directly to memory update with `reach_skipped`
- Do not enter clinical or show clinical counters

### Actual Flow

- Topic lock succeeded immediately
- First answer was on-topic but did not hit the locked answer, so tutor asked one follow-up
- Second answer hit `osteon`
- Student declined clinical with `No`
- Session closed correctly to `memory_update` with `reach_skipped`
- No clinical scenario was entered
- Unexpected: there were two consecutive opt-in tutor messages, one tagged `phase=tutoring` and then another tagged `phase=assessment`

### Trace Evidence

- `dean.reached_answer_gate reached=true evidence=osteon`
- `assessment_v2.opt_in_response intent=no`
- `assessment_v2.reach_close_routed close_reason=reach_skipped`
- `memory_update_node.transcript_snapshot result=wrote arun_6e6b8906_turn_4.json (10 msgs)`
- `memory_manager.flush result=wrote_2_failed_0_dropped_0_of_2`
- `sqlite_store.session_end result=sqlite_session_end ok status=completed score=0.4 path_set=True`

### Conversation Quality

- Good: opting out of clinical truly skipped the clinical branch
- Good: no clinical question leaked through after `No`
- Issue: duplicate opt-in wording creates a clunky user experience and could look buggy in a demo
- Mild mismatch: final close says the student got there on their own without hints, but the saved mastery is still `needs_review` at `0.4`

### Verdict

Pass for branch correctness. The `No` path does the right thing, but the duplicated opt-in prompt is a genuine flow issue.

### Follow-Up Fixes

- Remove the duplicate opt-in tutor message so only the assessment-owned prompt is visible
- Review scoring/messaging alignment for sessions that reach the answer after one redirected attempt
- Re-test this branch in the browser later to confirm whether the duplicate opt-in appears exactly as two stacked cards/bubbles in the UI

---

## Simulation 3 — Ambiguous Clinical Opt-In

Status: PASS with issues
Date/time: 2026-05-05 22:41:22 EDT to 2026-05-05 22:42:39 EDT
Browser URL: `http://localhost:5173/chat`
Export JSON: [arun_83ad8d80_turn_5.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_83ad8d80_turn_5.json)

### Script Actually Used

- Fresh session as `arun`
- Topic: `B cell differentiation and activation`
- Core answer: `Plasma cells.`
- Ambiguous opt-in reply: `what do you mean?`
- Clarified opt-in reply: `Yes`
- Clinical answer: `The breakdown is at the step where activated B cells differentiate into plasma cells...`
- Run method: real `/api/session/start` + websocket session

### Expected Flow

- Reach core answer
- Ambiguous opt-in should not close or enter clinical
- Tutor should clarify/re-ask opt-in
- `Yes` should then enter clinical
- Correct first clinical answer should close cleanly

### Actual Flow

- Topic lock and answer reach both worked immediately
- Ambiguous `what do you mean?` stayed in assessment and did not branch incorrectly
- Tutor re-asked the opt-in, though in a pretty thin way
- `Yes` entered clinical and generated a valid scenario
- First clinical answer closed to `memory_update` with `reach_full`

### Trace Evidence

- `assessment_v2.opt_in_response intent=ambiguous`
- `assessment_v2.opt_in_response intent=yes`
- `assessment_v2.clinical_scenario_gen_start`
- `assessment_v2.clinical_phase_entered elapsed_ms=10283`
- `assessment_v2.clinical_target_reached reason=student_response_contains_locked_answer_or_alias`
- `memory_update_node.transcript_snapshot result=wrote arun_83ad8d80_turn_5.json (12 msgs)`
- `memory_manager.flush result=wrote_3_failed_0_dropped_0_of_3`
- `sqlite_store.session_end result=sqlite_session_end ok status=completed score=0.5 path_set=True`

### Conversation Quality

- Good: ambiguous opt-in did not get misread as Yes or No
- Good: clinical question was grounded and the completion gate fired on the first clinical answer
- Issue: the “clarification” after `what do you mean?` is weak; it just rephrases the offer instead of explaining what the student is opting into
- Persistent issue: session save still marks `clinical_mastery_tier=not_assessed` even after successful clinical completion

### Verdict

Pass for flow safety. The ambiguous opt-in branch is stable enough, but the wording quality can be better.

### Follow-Up Fixes

- Improve ambiguous opt-in clarification so it explicitly says what the clinical bonus is
- Investigate why clinical completion still does not populate clinical mastery fields
- Investigate `mastery_store.update` trace mismatch versus actual subsection row updates

---

## Simulation 4 — Hints Exhausted / Struggling Student

Status: FAIL
Date/time: 2026-05-05 22:44 EDT onward (paused after failure signal)
Browser URL: `http://localhost:5173/chat`
Export JSON: [export snapshot](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/simulated_convos/2026-05-05_2244_gpt-5.5_sim4-hints-exhausted/arun_c517772d_export.json)

### Script Actually Used

- Fresh session as `arun`
- Topic: `classification of connective tissues`
- Student struggle sequence:
  - `i don't know`
  - `maybe epithelial tissue`
  - `not sure`
  - `can you tell me?`
  - repeated `i do not know`
- Run method: real `/api/session/start` + websocket session
- Run intentionally paused after capturing the failure state, per user request

### Expected Flow

- Progressive hints
- No premature answer reveal
- Eventual close as `hints_exhausted` if the student still cannot answer

### Actual Flow

- Topic lock worked
- Hinting escalated and low-effort/help-abuse counters moved as expected
- Hint level force-advanced from 1 to 2
- The tutor eventually revealed the answer categories directly
- After revealing them, the tutor still kept the session open and asked another question
- The run never reached `memory_update` during the observed session

### Trace Evidence

- `dean_node_v2.low_effort_pre_plan_advance from_hint=1 to_hint=2`
- Repeated `preflight category=low_effort` and `preflight category=help_abuse`
- Late turn trace showed `dean_v2.plan used_fallback=true error=RateLimitError 429`
- State snapshot at failure:
  - `phase=tutoring`
  - `close_reason=""`
  - tutor message explicitly names `proper`, `supportive`, and `fluid`

### Conversation Quality

- Good: tutor stayed calm and non-scolding
- Bad: the struggling branch became increasingly revealing and finally gave away the answer
- Bad: even after answer reveal, it failed to terminate
- This is a real demo-risk branch if a student stonewalls for long enough

### Verdict

Fail. This path did not honor the intended `hints_exhausted` behavior.

### Follow-Up Fixes

- Add a hard termination condition for repeated low-effort/help-abuse once hint escalation is exhausted
- Prevent full answer reveal before the close fires
- Investigate whether rate limiting is contributing to fallback behavior that weakens the close logic

---

## Simulation 5 — Help-Abuse Counter

Status: FAIL
Date/time: 2026-05-05 23:19:34 EDT to 2026-05-05 23:22:20 EDT
Browser URL: `http://localhost:5173/chat`
Export JSON: [arun_2d00565d_turn_8.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_2d00565d_turn_8.json)

### Script Actually Used

- Fresh session as `arun`
- Topic: `B cell differentiation and activation`
- Help-abuse sequence:
  - `give me the answer`
  - `tell me the answer`
  - `answer please`
  - `just tell me`
- Then one low-effort turn: `i do not know`
- Then answered correctly: `plasma cell`
- Then explicit exit sentinel `__exit_session__` to inspect no-save behavior

### Expected Flow

- Help-abuse counter increments visibly and in export
- Tutor resists direct answer leakage
- Counter threshold should force hint advancement
- Exit-intent path should skip persistence per design

### Actual Flow

- Tutor resisted all direct answer requests and did not leak the answer
- `preflight` detected help-abuse on each demand
- But the surfaced `help_abuse_count` remained `0`
- Fourth abuse turn marked `should_force_hint_advance=true`, but visible hint state did not clearly advance
- After the student finally answered, the app again emitted duplicate opt-in tutor messages
- Exit-intent close fired, but mem0 writes still occurred and the SQLite session row still completed

### Trace Evidence

- Repeated `preflight category=help_abuse`
- Fourth abuse turn: `should_force_hint_advance=true`
- Debug payload: `help_abuse_count=0`
- Exit trace:
  - `mem0_write` x2
  - `memory_update_node.no_save result=skipped_save_per_M1_design`
- SQLite row still ended `status=completed`

### Conversation Quality

- Good: no direct answer leak before the student reached `plasma cell`
- Good: tone stayed firm without becoming hostile
- Issue: counter feedback is not trustworthy from the surfaced state
- Issue: duplicated opt-in prompt returns here too
- Issue: no-save branch appears internally inconsistent

### Verdict

Fail. The pedagogy mostly held, but the product behavior around counters and no-save semantics did not.

### Follow-Up Fixes

- Fix help-abuse counter propagation to frontend/debug payload
- Verify forced hint advancement actually mutates exposed state after abuse threshold
- Remove duplicate opt-in prompt
- Fix exit-intent no-save path so mem0/mastery/SQLite writes are all skipped consistently

---

## Simulation 6 — Off-Topic Counter

Status: PASS with issues
Date/time: 2026-05-05 23:23:41 EDT to 2026-05-05 23:24:45 EDT
Browser URL: `http://localhost:5173/chat`
Export JSON: [arun_78c605a5_turn_6.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/conversations/arun_78c605a5_turn_6.json)

### Script Actually Used

- Fresh session as `arun`
- Topic: `respiratory zone`
- Off-topic sequence:
  - `what is the weather today?`
  - `who won the game last night?`
  - `recommend me a movie`
  - `what is bitcoin price now?`

### Expected Flow

- Off-topic counter increments
- Tutor redirects while keeping topic locked
- Fourth off-topic strike ends the session gracefully with the matching close reason

### Actual Flow

- Topic lock worked
- Visible off-topic count incremented to 4
- Tutor progressively firmed up redirection language
- Session ended on the fourth strike
- Saved close reason was wrong: `tutoring_cap` instead of an off-topic strike reason

### Trace Evidence

- Visible `off_topic_count=4`
- Final close text explicitly referenced `Four off-topic detours`
- `teacher_v2.close_draft close_reason=tutoring_cap`
- SQLite row `key_takeaways.close_reason=tutoring_cap`
- `sqlite_store.session_end ok status=completed score=0.05 path_set=True`

### Conversation Quality

- Good: the tutor stayed on-topic and redirected cleanly
- Good: the branch terminated promptly on the fourth strike
- Issue: close reason classification is inconsistent with the user-facing explanation

### Verdict

Pass for behavioral flow, fail for close-reason labeling accuracy.

### Follow-Up Fixes

- Map off-topic strike closure to the correct `close_reason`
- Verify downstream consumers (My Mastery, analysis summaries) don’t mistake this for a tutoring-cap session

---

## Simulation 7 — Low-Effort Counter

Status: PASS with finalization issues
Date/time: 2026-05-05 23:26:22 EDT to 2026-05-05 23:28:28 EDT
Browser URL: `http://localhost:5173/chat`
Export JSON: [arun_38c966e7_export.json](/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/simulated_convos/2026-05-05_2331_gpt-5.5_sim7-low-effort/arun_38c966e7_export.json)

### Script Actually Used

- Fresh session as `arun`
- Topic: `directional terms`
- Low-effort sequence:
  - `idk`
  - `not sure`
  - `not sure`
  - `hmm`
  - `i don't know`
- Then explicit exit sentinel: `__exit_session__`

### Expected Flow

- Low-effort count should increment across weak replies
- Hinting should escalate after repeated low-effort
- Tutor should stay non-leaky and supportive
- Exit-intent no-save should close cleanly without memory/mastery/session persistence

### Actual Flow

- Topic lock worked and the question targeted `superficial`
- `preflight` recognized every weak response as `category=low_effort`
- On the fourth weak turn, Dean force-advanced hinting from `0` to `1`
- Tutor stayed patient and did not reveal the answer
- Exit-intent no-save avoided mem0 writes, mastery updates, and SQLite completion
- But the session row was left hanging as `in_progress` with `ended_at=null` even though the thread had already moved to `memory_update`

### Trace Evidence

- `preflight category=low_effort` fired on `idk`, `not sure`, `not sure`, and `i don't know`
- Fourth weak turn:
  - `dean_node_v2.low_effort_pre_plan_advance consecutive_low_effort_at_fire=4 from_hint=0 to_hint=1`
  - `dean_node_v2.low_effort_force_socratic result=overrode_close_mode_to_socratic_after_pre_advance`
- Exit trace:
  - `teacher_v2.close_draft close_reason=exit_intent`
  - `memory_update_node.transcript_snapshot wrote arun_38c966e7_turn_6.json`
  - `memory_update_node.no_save close_reason=exit_intent result=skipped_save_per_M1_design`
- Export contained zero:
  - `mem0_write`
  - `mastery_store.update`
  - `sqlite_store.session_end`
- SQLite row remained:
  - `status=in_progress`
  - `ended_at=null`
  - `key_takeaways=null`

### Conversation Quality

- Good: the tutor stayed gentle and specific instead of getting snappy with repeated low-effort
- Good: there was no answer leak for `superficial`
- Good: the low-effort escalation logic is real, not imaginary
- Issue: the no-save branch does not finalize the session record cleanly

### Verdict

Pass for low-effort detection and no-save scoring behavior, fail for session finalization.

### Follow-Up Fixes

- Mark no-save exit-intent sessions as terminal in SQLite, even if they intentionally skip mastery/memory writes
- Make sure `ended_at` is set on no-save exits so analysis/session history do not accumulate dangling rows
- Keep transcript snapshots if desired, but align them with a clear terminal session status
---

## Simulation 8 — My Mastery Revisit Flow

Status: TODO
Date/time:
Browser URL:
Export JSON:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 9 — Analysis Page / Session Review

Status: TODO
Date/time:
Browser URL:
Export JSON:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 10 — Multimodal Upload

Status: TODO
Date/time:
Browser URL:
Export JSON:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 11 — Explicit End Session and Cancel

Status: TODO
Date/time:
Browser URL:
Export JSON:
Artifact folder:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Memory And Scores

### UI/UX

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 12 — Backend-Triggered Exit Intent Mid-Plan

Status: TODO
Date/time:
Browser URL:
Export JSON:
Artifact folder:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Memory And Scores

### UI/UX

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 13 — Repeat Same Topic After Memory

Status: TODO
Date/time:
Browser URL:
Export JSON:
Artifact folder:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Memory And Scores

### UI/UX

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 14 — Ambiguous Topic Mapping

Status: TODO
Date/time:
Browser URL:
Export JSON:
Artifact folder:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Memory And Scores

### UI/UX

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 15 — Clinical Cap

Status: TODO
Date/time:
Browser URL:
Export JSON:
Artifact folder:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Memory And Scores

### UI/UX

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Simulation 16 — New Chat / Stale WebSocket

Status: TODO
Date/time:
Browser URL:
Export JSON:
Artifact folder:

### Script Actually Used

### Expected Flow

### Actual Flow

### Trace Evidence

### Memory And Scores

### UI/UX

### Conversation Quality

### Verdict

### Follow-Up Fixes

---

## Edge Probe Results — Simulations 17-29

Use one compact block per probe.

```md
### Simulation N — Name

Status:
Run by:
Model:
Started at:
Ended at:
Duration:
Slowest step observed:
Browser URL:
Export JSON:
Artifact folder:

Expected:
Actual:
UI/UX:
Memory/Scores:
Trace:
Verdict:
Follow-up:
```
