# Future Work

## Turn / Operation envelope with per-turn persistence

Current `state["debug"]["turn_trace"]` stores summaries per API call. Planned
upgrade:

- Add `Operation` TypedDict: wrapper_id, input_hash, rendered_prompt, output,
  tokens (input/output/cache_read/cache_write), latency_ms, decision_effect
- Add `Turn` TypedDict: turn_id, phase, student_input, operations, tutor_output,
  state_before, state_after, transition
- Add `turns: list[Turn]` to TutorState
- Persist each Turn to disk at end of dean_node and assessment_node (not only
  at session end via _log_conversation)

Enables: deterministic replay by input_hash, per-operation eval scoring, full
cache-hit diagnostics, owner attribution for regressions.

Deferred because the current milestone is focused on cache plumbing and we don't
want to churn TutorState schema simultaneously.
