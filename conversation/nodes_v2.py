"""
LangGraph node implementations that compose the tutoring stack.

`dean_node_v2` runs one tutoring turn end-to-end:

  1. If the topic isn't locked yet, hand off to topic_lock_v2.
  2. Run the preflight Haiku checks. If any fires, the Teacher writes
     a redirect / nudge / confirm-end message and the Dean is skipped.
  3. Otherwise fetch chunks, ask the Dean for a TurnPlan, and let
     `retry_orchestrator.run_turn` produce a Teacher draft that
     passes the four Haiku safety checks.
  4. Update state with the final text plus hint, strike, and counter
     updates.

Rapport and assessment phases run through their own nodes
(`rapport_node` in lifecycle_v2, `assessment_node_v2` in
assessment_v2). This file holds the per-turn tutoring node and the
helpers it relies on.
"""
from __future__ import annotations

import time
from typing import Any, Optional

# Module-level imports of the v2 stack so tests can monkeypatch them
# via N.<name> rather than digging into nested imports.
from conversation.preflight import run_preflight
from conversation.dean_v2 import DeanV2
from conversation.teacher_v2 import TeacherV2, TeacherPromptInputs
from conversation.retry_orchestrator import run_turn
from conversation.turn_plan import TurnPlan
from conversation.topic_lock_v2 import run_topic_lock_v2
from conversation.assessment_v2 import assessment_node_v2 as _assessment_node_v2_impl

# ─────────────────────────────────────────────────────────────────────────────
# dean_node_v2 — per-turn tutoring loop using the new stack
# ─────────────────────────────────────────────────────────────────────────────

def dean_node_v2(state: dict, dean, teacher, retriever) -> dict:
    """V2 per-turn tutoring node — wires preflight + dean_v2 + teacher_v2 +
 retry_orchestrator.

 `dean` and `teacher` are the LEGACY agent instances (kept for the
 unlocked-topic path which we delegate to). The v2 modules are
 instantiated lazily inside this function so the graph builder
 doesn't need to thread them through.

 Args:
 state: TutorState dict
 dean: legacy DeanAgent — used for topic-locking + retrieval
 teacher: legacy TeacherAgent — kept for legacy fallback paths
 retriever: ChunkRetriever — for fetching chunks at lock time

 Returns:
 Partial state dict for LangGraph reducer (messages, phase, hint_level
 help_abuse_count, off_topic_count, debug.turn_trace, etc.)
"""
    # ── Latest student message ───────────────────────────────────────────
    latest_student = ""
    has_student_msg = False
    for m in reversed(state.get("messages", []) or []):
        if (m or {}).get("role") == "student":
            has_student_msg = True
            latest_student = str(m.get("content", "") or "")
            break

    # — cancel-modal early bypass. When cancel_modal_pending
    # is True, the most recent student message is the one that triggered
    # the deflection (now canceled). If we ran preflight on it again, we'd
    # re-trigger the modal. Skip preflight + topic_lock; force soft_reset
    # mode directly. The latest_student stays as-is so Teacher has context.
    if state.get("cancel_modal_pending"):
        latest_student = "(student canceled exit modal — wants to keep going)"
        # Synthesize a placeholder so downstream code that requires
        # has_student_msg works. Don't append to actual messages —
        # transcript stays clean.
        has_student_msg = True

    if not has_student_msg:
        # Rapport just fired on the same graph invoke; wait for real input.
        return {}
    if not latest_student or not latest_student.strip():
        # Whitespace guard — never route empty messages through LLMs
        msgs = list(state.get("messages", []))
        msgs.append({
            "role": "tutor",
            "content": (
                "Looks like your last message came through empty — "
                "could you type your question or response again?"
            ),
        })
        return {"messages": msgs}

    # Archive the previous turn's trace before resetting
    prior_trace = list(state.get("debug", {}).get("turn_trace", []) or [])
    if prior_trace:
        att = list(state.get("debug", {}).get("all_turn_traces", []) or [])
        att.append({
            "turn": int(state.get("turn_count", 0) or 0),
            "phase": state.get("phase", "tutoring"),
            "trace": prior_trace,
        })
        state.setdefault("debug", {})["all_turn_traces"] = att[-50:]
    state.setdefault("debug", {})["turn_trace"] = []

    state["debug"]["current_node"] = "dean_node_v2"
    debug_trace = state["debug"]["turn_trace"]
    t0 = time.time()

    # Live activity feed — first signal to the UI that we received the
    # message and are starting work. Mirrors v1 dean.py:fire_activity.
    from conversation.streaming import fire_activity
    fire_activity("Reading your message")

    # If topic isn't locked yet, owns the v2 pre-lock path:
    # topic_mapper_llm, confirm-and-lock, prelock counter
    # guided-pick at cap 7, and anchor_pick.
    # : even when locked_topic.path is set (from _apply_prelock), we
    # MUST route to topic_lock_v2 if there's a pending anchor_pick — that
    # handler resolves the student's anchor selection into the actual
    # would bypass the handler and Teacher would draft against empty Q/A
    # (then retry-fail 3× and ship SAFE_GENERIC_PROBE).
    locked = state.get("locked_topic") or {}
    pending_for_lock = state.get("pending_user_choice") or {}
    pending_is_anchor_pick = (
        isinstance(pending_for_lock, dict)
        and pending_for_lock.get("kind") == "anchor_pick"
    )
    # Tracks anchor_pick resolution so we can (a) fall through to tutoring
    # on the same invocation when the pick is successfully resolved, and
    # (b) merge the handler's state updates into the final tutoring return.
    anchor_pick_overrides: dict = {}
    # Bug #1 fix — deflection before topic_lock_v2: fire preflight FIRST when
    # (a) topic isn't locked (post-greeting), or (b) anchor_pick is pending.
    # ambiguous-intent → topic cards path. Mirrors the post-lock deflection
    # short-circuit at line ~273 so unlocked / anchor-pick state gets the
    # same UX.
    # — skip preflight when cancel_modal_pending. The previous
    # student message ("i want to stop") would re-trigger deflection.
    if state.get("cancel_modal_pending"):
        debug_trace.append({"wrapper": "dean_node_v2.cancel_modal_skip_preflight"})
    elif (not locked or not locked.get("path")) or pending_is_anchor_pick:
        try:
            preflight_pre = run_preflight(state, latest_student, locked_topic=locked)
            debug_trace.append({
                "wrapper": "preflight.pre_topic_lock",
                "fired": preflight_pre.fired,
                "category": preflight_pre.category,
                "evidence": preflight_pre.evidence[:120],
                "in_anchor_pick": pending_is_anchor_pick,
            })
            if preflight_pre.fired and preflight_pre.category == "deflection":
                elapsed_ms_d = int((time.time() - t0) * 1000)
                # — rapport-stage decline → direct close.
                # No locked topic + deflection at greeting = student
                # doesn't want to engage. Skip modal.
                is_rapport_stage_pre = (
                    not state.get("topic_confirmed")
                    and not (state.get("locked_topic") or {}).get("path")
                )
                if is_rapport_stage_pre:
                    debug_trace.append({
                        "wrapper": "dean_node_v2.rapport_decline_direct_close_pre_lock",
                        "category": preflight_pre.category,
                        "evidence": preflight_pre.evidence[:120],
                    })
                    debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms_d})
                    return {
                        "phase": "memory_update",
                        "close_reason": "exit_intent",
                        "exit_intent_pending": True,
                        "help_abuse_count": preflight_pre.new_help_abuse_count,
                        "off_topic_count": preflight_pre.new_off_topic_count,
                        "debug": state["debug"],
                    }
                debug_trace.append({
                    "wrapper": "dean_node_v2.exit_intent_modal_triggered_pre_lock",
                    "category": preflight_pre.category,
                    "evidence": preflight_pre.evidence[:120],
                })
                debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms_d})
                return {
                    "exit_intent_pending": True,
                    "help_abuse_count": preflight_pre.new_help_abuse_count,
                    "off_topic_count": preflight_pre.new_off_topic_count,
                    "debug": state["debug"],
                }
        except Exception as e:
            debug_trace.append({
                "wrapper": "preflight.pre_topic_lock.error",
                "error": f"{type(e).__name__}: {str(e)[:160]}",
            })

    if (not locked or not locked.get("path")) or pending_is_anchor_pick:
        handler_result = run_topic_lock_v2(
            state,
            dean=dean,
            retriever=retriever,
            latest_student=latest_student,
        )
        just_picked = (
            pending_is_anchor_pick
            and bool(handler_result.get("locked_question"))
            and not (handler_result.get("pending_user_choice") or {}).get("kind")
        )
        if not just_picked:
            return handler_result
        # Anchor was resolved — apply the handler's state updates to the
        # live state so the tutoring code below sees the locked Q/A, then
        # fall through. Also capture the keys we'll merge into the final
        # tutoring return (the engaged-tutoring return doesn't normally
        for k, v in handler_result.items():
            if k == "debug":
                continue
            state[k] = v
        for k in (
            "locked_topic", "locked_question", "locked_answer", "full_answer",
            "locked_answer_aliases", "topic_confirmed", "topic_selection",
            "topic_options", "topic_question", "pending_user_choice",
            "topic_just_locked", "student_state", "prelock_loop_count",
            "phase",
        ):
            if k in handler_result:
                anchor_pick_overrides[k] = handler_result[k]
        # Refresh the locked snapshot since we just mutated it.
        locked = state.get("locked_topic") or {}

    # ── 0. reach-answer gate ─────────────────────────────
    # Mirrors legacy dean.run_turn lines 1741-1783 — fires the SAME
    # gate (Step A.1 token-overlap → Step A.2 K-of-N partial reach →
    # Step B LLM paraphrase) on the student's latest message before any
    # planning. Stamps state so:
    # * after_dean can route to assessment_node when reached=True
    # * Dean.plan and Teacher inputs can see student_reached_answer
    # Skip on the lock-time ack turn (topic_just_locked=True) per legacy:
    # the lock-acknowledgment turn isn't a real attempt and would produce
    # spurious reach=True via topic-keyword overlap with the locked answer.
    skip_gate_for_ack = bool(state.get("topic_just_locked", False))
    if (
        latest_student
        and state.get("locked_answer")
        and not skip_gate_for_ack
    ):
        try:
            # Reach-gate ported from V1 dean.reached_answer_gate during D1.
            # Lives in conversation/reach_gate.py — self-contained module.
            from conversation.reach_gate import reached_answer_gate
            from conversation.llm_client import make_anthropic_client, resolve_model
            from config import cfg as _cfg
            _gate_client = make_anthropic_client()
            _gate_model = resolve_model(_cfg.models.dean)
            gate_result = reached_answer_gate(
                state, latest_student, _gate_client, _gate_model,
            )
        except Exception as e:
            debug_trace.append({
                "wrapper": "dean.reached_answer_gate.error",
                "error": f"{type(e).__name__}: {str(e)[:160]}",
            })
            gate_result = {"reached": False, "evidence": "", "path": "error", "coverage": 0.0}
        state["student_reached_answer"] = bool(gate_result.get("reached", False))
        state["student_reach_coverage"] = round(
            float(gate_result.get(
                "coverage", 1.0 if state["student_reached_answer"] else 0.0,
            )),
            3,
        )
        state["student_reach_path"] = str(gate_result.get("path", "unknown"))
        debug_trace.append({
            "wrapper": "dean.reached_answer_gate",
            "reached": state["student_reached_answer"],
            "path": state["student_reach_path"],
            "coverage": state["student_reach_coverage"],
            "evidence": str(gate_result.get("evidence", ""))[:160],
            "n_matched": gate_result.get("n_matched"),
            "n_total": gate_result.get("n_total"),
        })
    else:
        # No student msg yet, no locked answer, or lock-ack turn — gate
        # cannot fire. Preserve any prior value (don't downgrade to False).
        debug_trace.append({
            "wrapper": "dean.reached_answer_gate.skipped",
            "reason": (
                "topic_just_locked" if skip_gate_for_ack
                else ("no_locked_answer" if not state.get("locked_answer") else "no_msg")
            ),
        })

    # ── 0b. Reach short-circuit — Sim 2/5 fix ──────────────────────
    # If the reach gate just fired with reached=True, skip Dean planning +
    # Teacher draft entirely. The graph routes to assessment_node next
    # an acknowledgment + assessment_node also drafts opt-in → duplicate
    # tutor messages (one tagged phase=tutoring, one tagged phase=assessment).
    # Counters preserved; turn_count NOT incremented (assessment_node owns
    # the turn count for its own state machine).
    if state.get("student_reached_answer") and not skip_gate_for_ack:
        debug_trace.append({
            "wrapper": "dean_node_v2.reach_short_circuit",
            "result": "suppressing_teacher_draft_assessment_node_will_render_opt_in",
        })
        # Match the existing engaged-tutoring return shape so the merge is
        # consistent (LangGraph reducer expects fields in the return dict).
        return {
            "messages": list(state.get("messages", []) or []),  # no new tutor msg
            "help_abuse_count": int(state.get("help_abuse_count", 0) or 0),
            "off_topic_count": int(state.get("off_topic_count", 0) or 0),
            "hint_level": int(state.get("hint_level", 0) or 0),
            "last_hint_advance_at_turn": int(state.get("last_hint_advance_at_turn", -1) or -1),
            "phase": state.get("phase", "tutoring"),
            "student_reached_answer": True,
            "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
            "student_reach_path": str(state.get("student_reach_path", "") or ""),
            "session_ended_off_domain": bool(state.get("session_ended_off_domain", False)),
            "debug": state["debug"],
        }

    # ── 1. Pre-flight Haiku layer ────────────────────────────────────────
    fire_activity("Checking message intent")
    # — skip preflight on cancel-modal turn. The previous
    # student message would re-classify as deflection and ping-pong the
    # modal. Synthesize a non-firing preflight result so downstream
    # branches behave normally.
    if state.get("cancel_modal_pending"):
        from conversation.preflight import PreflightResult
        preflight = PreflightResult(
            fired=False, category="none",
            new_help_abuse_count=int(state.get("help_abuse_count", 0) or 0),
            new_off_topic_count=int(state.get("off_topic_count", 0) or 0),
        )
        debug_trace.append({
            "wrapper": "preflight.skipped_for_cancel_modal",
            "result": "synthetic_non_fire",
        })
    else:
        preflight = run_preflight(state, latest_student, locked_topic=locked)
        debug_trace.append({
            "wrapper": "preflight",
            "fired": preflight.fired,
            "category": preflight.category,
            "evidence": preflight.evidence[:120],
            "rationale": preflight.rationale[:120],
            "elapsed_s": preflight.elapsed_s,
            "should_force_hint_advance": preflight.should_force_hint_advance,
            "should_end_session": preflight.should_end_session,
        })

    # Update strike counters in state regardless of branch
    new_help_count = preflight.new_help_abuse_count
    new_off_count = preflight.new_off_topic_count
    # Apply counter updates to state BEFORE snapshot so the snapshot
    # reflects the post-preflight counter values
    state["help_abuse_count"] = new_help_count
    state["off_topic_count"] = new_off_count

    # snapshot the student turn with classifier verdict.
    # Counters are now current; the snapshot will reflect intent + counters
    # the LLM should see when reading history on subsequent turns.
    from conversation.snapshots import snapshot_student_turn, log_system_event
    snapshot_student_turn(
        state,
        intent=preflight.category if preflight.category else "on_topic_engaged",
        intent_evidence=preflight.evidence[:120],
    )
    if preflight.fired:
        log_system_event(
            state,
            "preflight_intervened",
            category=preflight.category,
        )

    # ── 2a. Pre-flight fired → Dean SKIPPED, Teacher renders redirect ───
    if preflight.fired:
        # DEFLECTION SHORT-CIRCUIT: when preflight detects the student
        # wants to end, do NOT have Teacher draft a confirm_end message
        # (that's templated tutor text per ). Instead, stamp
        # exit_intent_pending=True; the frontend (useWebSocket → store →
        # ChatView) renders ExitConfirmModal directly. Modal pops, no
        # tutor bubble added to transcript.
        if preflight.category == "deflection":
            elapsed_ms_e = int((time.time() - t0) * 1000)
            # — rapport-stage decline: route DIRECTLY to
            # memory_update (skip modal). Student hasn't invested any
            # progress to confirm-exit over.
            is_rapport_stage = (
                not state.get("topic_confirmed")
                and not (state.get("locked_topic") or {}).get("path")
            )
            if is_rapport_stage:
                debug_trace.append({
                    "wrapper": "dean_node_v2.rapport_decline_direct_close",
                    "category": preflight.category,
                    "evidence": preflight.evidence[:120],
                })
                debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms_e})
                # : preserve anchor_pick_overrides through early returns —
                # a deflection would lose locked_question/locked_topic.
                _ret = {
                    "phase": "memory_update",
                    "close_reason": "exit_intent",
                    "exit_intent_pending": True,
                    "help_abuse_count": new_help_count,
                    "off_topic_count": new_off_count,
                    "debug": state["debug"],
                }
                if anchor_pick_overrides:
                    for _k, _v in anchor_pick_overrides.items():
                        if _k == "messages":
                            continue
                        _ret[_k] = _v
                return _ret
            debug_trace.append({
                "wrapper": "dean_node_v2.exit_intent_modal_triggered",
                "category": preflight.category,
                "evidence": preflight.evidence[:120],
            })
            debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms_e})
            # : same merge as the rapport_decline branch above.
            _ret = {
                "exit_intent_pending": True,
                "help_abuse_count": new_help_count,
                "off_topic_count": new_off_count,
                "student_reached_answer": bool(state.get("student_reached_answer", False)),
                "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
                "student_reach_path": str(state.get("student_reach_path", "") or ""),
                "debug": state["debug"],
            }
            if anchor_pick_overrides:
                for _k, _v in anchor_pick_overrides.items():
                    if _k == "messages":
                        continue
                    _ret[_k] = _v
            return _ret

        # Force hint advance if strike-4 fired (help_abuse threshold).
        # cap at max_hints+1 (was 3
        # hardcoded). When the next strike fires after hint=3, level
        # bumps to 4 — after_dean routes to memory_update with
        # → no termination, infinite stonewall ( Sim 4).
        prev_hint_level_p = int(state.get("hint_level", 0) or 0)
        new_hint_level = prev_hint_level_p
        rule_hint_advance_count_pf = int(state.get("rule_hint_advance_count", 0) or 0)
        if preflight.should_force_hint_advance:
            max_hints_p = int(state.get("max_hints", 3) or 3)
            new_hint_level = min(max_hints_p + 1, new_hint_level + 1)
            # — diagnostic counter. Only count if level actually moved.
            if new_hint_level > prev_hint_level_p:
                rule_hint_advance_count_pf += 1
            # : log hint_advance event so Teacher can read the trigger
            # in CONVERSATION HISTORY and acknowledge the level-up in its draft.
            from conversation.snapshots import log_system_event
            log_system_event(
                state, "hint_advance",
                from_level=prev_hint_level_p,
                to_level=new_hint_level,
                trigger="help_abuse_strike_4",
            )

        # Build a TurnPlan for Teacher's redirect
        from conversation.llm_client import make_anthropic_client, resolve_model
        from config import cfg as _cfg

        plan = TurnPlan(
            scenario=f"preflight:{preflight.category}",
            hint_text=state.get("locked_question", "") or "",  # for redirect, hint_text is the anchor question
            mode=preflight.suggested_mode,
            tone=preflight.suggested_tone,
            forbidden_terms=[state.get("locked_answer", "") or ""],
            shape_spec={"max_sentences": 3, "exactly_one_question": True},
        )
        client = make_anthropic_client()
        teacher_v2 = TeacherV2(client, model=resolve_model(_cfg.models.teacher))
        # pass snapshots + events from state.debug
        # so Teacher sees system-state annotations in history
        _debug_obj = state.get("debug") or {}
        inputs = TeacherPromptInputs(
            chunks=[],  # redirect/nudge/confirm_end don't use chunks
            history=state.get("messages", []),
            locked_subsection=locked.get("subsection") or "",
            locked_question=state.get("locked_question") or "",
            domain_name=getattr(_cfg.domain, "name", "this subject"),
            domain_short=getattr(_cfg.domain, "short", "subject"),
            student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
            snapshots=_debug_obj.get("per_turn_snapshots", []) or [],
            system_events=_debug_obj.get("system_events", []) or [],
        )
        fire_activity({
            "redirect": "Redirecting back to the topic",
            "nudge": "Nudging back on topic",
            "confirm_end": "Confirming session end",
            "honest_close": "Closing the session",
        }.get(preflight.suggested_mode, "Drafting response"))
        draft = teacher_v2.draft(plan, inputs)
        debug_trace.append({
            "wrapper": "teacher_v2.draft",
            "mode": draft.mode,
            "tone": draft.tone,
            "elapsed_ms": draft.elapsed_ms,
            "tokens_in": draft.input_tokens,
            "tokens_out": draft.output_tokens,
            "error": draft.error,
        })

        msg_text = draft.text or "(could you try again?)"
        msgs = list(state.get("messages", []))
        msgs.append({
            "role": "tutor",
            "content": msg_text,
            "phase": "tutoring",
            "metadata": {
                "preflight_category": preflight.category,
                "tone": draft.tone,
                "mode": draft.mode,
            },
        })
        # snapshot the tutor turn (preflight-redirect
        # path: single attempt, no verifier loop)
        state["messages"] = msgs
        from conversation.snapshots import snapshot_tutor_turn
        snapshot_tutor_turn(
            state,
            mode=draft.mode,
            tone=draft.tone,
            attempts=1,
        )

        # Honor end-session signal ( strike 4)
        new_phase = state.get("phase", "tutoring")
        if preflight.should_end_session:
            new_phase = "memory_update"
            msgs[-1]["metadata"]["is_closing"] = True
            # plain assignment (not setdefault, which is a no-op
            # when the key was initialized to False in state.py).
            state["session_ended_off_domain"] = True

        elapsed_ms = int((time.time() - t0) * 1000)
        debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms})

        # mem0 injection #2 hook: stamp the turn at which hint advanced
        # so the NEXT turn's dean_node_v2 entry can read learning-style cue
        # via mem0_inject.read_hint_advance_carryover and pass it as
        # carryover_notes to dean.plan.
        prev_hint_level = int(state.get("hint_level", 0) or 0)
        last_advance_at = int(state.get("last_hint_advance_at_turn", -1) or -1)
        if new_hint_level > prev_hint_level:
            last_advance_at = int(state.get("turn_count", 0) or 0)

        # : preflight-fired (redirect/nudge/confirm_end) return — same
        # anchor_pick_overrides merge pattern as the deflection branches above
        # and the bottom engaged-tutoring return.
        _ret = {
            "messages": msgs,
            "help_abuse_count": new_help_count,
            "off_topic_count": new_off_count,
            "hint_level": new_hint_level,
            "last_hint_advance_at_turn": last_advance_at,
            "phase": new_phase,
            # — propagate reach gate result so after_dean
            # routes to assessment_node when the student reached the answer.
            "student_reached_answer": bool(state.get("student_reached_answer", False)),
            "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
            "student_reach_path": str(state.get("student_reach_path", "") or ""),
            # — diagnostic counters. Carry
            # forward; rule_hint_advance_count may have been bumped above
            # if preflight strike-4 fired. dean_override + engaged_wrong
            # don't increment in this branch (we're in a deflection path).
            "engaged_wrong_count": int(state.get("engaged_wrong_count", 0) or 0),
            "dean_hint_override_count": int(state.get("dean_hint_override_count", 0) or 0),
            "rule_hint_advance_count": rule_hint_advance_count_pf,
            "session_ended_off_domain": bool(state.get("session_ended_off_domain", False)),
            "debug": state["debug"],
        }
        if anchor_pick_overrides:
            for _k, _v in anchor_pick_overrides.items():
                if _k == "messages":
                    continue
                _ret[_k] = _v
        return _ret

    # ── 2b. No pre-flight fire → Dean plans + Teacher drafts via retry ──
    from conversation.llm_client import make_anthropic_client, resolve_model
    from config import cfg as _cfg

    # reuse lock-time chunks by default. The unconditional per-turn
    # retrieve we used to do here busted the prompt-cache contract (chunks
    # change every turn → cache miss → expensive). Lock-time chunks already
    # cover the locked subsection. Dean can opt-in to exploration retrieval
    # via plan.needs_exploration when student detours into a tangent.
    chunks = list(state.get("retrieved_chunks", []) or [])
    debug_trace.append({
        "wrapper": "retriever.reused_lock_time_chunks",
        "n": len(chunks),
    })

    client = make_anthropic_client()
    dean_v2 = DeanV2(client, model=resolve_model(_cfg.models.dean))
    teacher_v2 = TeacherV2(client, model=resolve_model(_cfg.models.teacher))

    # ── mem0 carryover assembly ─────────────────────────
    # Injection #1: stashed on state["mem0_carryover_notes"] at lock time
    # by topic_lock_v2 — survives turn-to-turn until consumed.
    # Injection #2: fired here on every hint advance (1→2 or 2→3).
    from conversation.mem0_inject import (
        read_hint_advance_carryover,
        combine_carryover,
    )
    carryover_topic_lock = str(state.get("mem0_carryover_notes", "") or "")
    carryover_hint_advance = ""
    prev_hint_level = int(state.get("hint_level", 0) or 0)
    # Hint advance happens INSIDE Dean's planning when it bumps level — at
    # this point in the turn, we don't yet know whether Dean will advance.
    # Fire injection #2 SPECULATIVELY when the prior turn's plan already
    # bumped the level (i.e. current level > 1 and last_hint_advance_at_turn
    # equals current turn number) — this surfaces style cues for the next
    # plan decision. Cheap (mem0_safe never raises, top_k=1).
    last_advance_at = int(state.get("last_hint_advance_at_turn", -1) or -1)
    current_turn = int(state.get("turn_count", 0) or 0)
    if prev_hint_level >= 2 and last_advance_at == current_turn - 1:
        try:
            persistent = getattr(dean, "memory_client", None)
            carryover_hint_advance = read_hint_advance_carryover(
                state, persistent, locked,
            )
            if carryover_hint_advance:
                debug_trace.append({
                    "wrapper": "mem0_inject.hint_advance_carryover",
                    "carryover_chars": len(carryover_hint_advance),
                })
                # visible activity
                # signal that learning-style cues from prior sessions
                # are informing this hint. Fires only when prior memory
                # exists for this student; silently skipped otherwise.
                fire_activity(
                    "Loading your learning style from past sessions",
                    detail=(
                        f"Hint just advanced to level {prev_hint_level}. "
                        f"Pulled {len(carryover_hint_advance)} chars of "
                        "learning-style cues from prior sessions to "
                        "shape this turn's scaffolding (e.g. 'student "
                        "responds to clinical framing', 'prefers concrete "
                        "analogies before abstractions')."
                    ),
                )
        except Exception as e:
            debug_trace.append({
                "wrapper": "mem0_inject.hint_advance_carryover_error",
                "error": f"{type(e).__name__}: {str(e)[:160]}",
            })
    carryover_combined = combine_carryover(
        carryover_topic_lock, carryover_hint_advance,
    )

    # : PRE-PLAN low-effort hint advance.
    # Was: post-Teacher safety net advanced hint AFTER Dean had already planned
    # honest_close (because Dean saw cle=4 and read it as 'student won't engage
    # close out'). The hint advance only took effect on the NEXT turn — so the
    # student saw a "we didn't get there" goodbye even though the system was
    # supposed to escalate. Fix: bump hint_level + reset cle BEFORE Dean plans
    # so Dean plans a more concrete socratic question at the new hint level
    # instead of choosing a close mode.
    consecutive_low_pre = int(state.get("consecutive_low_effort_count", 0) or 0)
    pre_advanced_low_effort = False
    if (
        consecutive_low_pre >= 4
        and not state.get("student_reached_answer")
    ):
        max_hints_pre = int(state.get("max_hints", 3) or 3)
        prev_hint_pre = int(state.get("hint_level", 0) or 0)
        if prev_hint_pre < max_hints_pre:
            new_hint_pre = prev_hint_pre + 1
            state["hint_level"] = new_hint_pre
            state["consecutive_low_effort_count"] = 0
            pre_advanced_low_effort = True
            debug_trace.append({
                "wrapper": "dean_node_v2.low_effort_pre_plan_advance",
                "consecutive_low_effort_at_fire": consecutive_low_pre,
                "from_hint": prev_hint_pre,
                "to_hint": new_hint_pre,
            })
            from conversation.snapshots import log_system_event
            log_system_event(
                state, "hint_advance",
                from_level=prev_hint_pre,
                to_level=new_hint_pre,
                trigger="low_effort_streak_4_pre_plan",
            )
        # If hint already at cap, leave the post-plan safety net to handle
        # session end via memory_update routing.

    # Plan the turn — mem0 carryover + domain-aware
    # clinical scenario style passed through to Dean's prompt.
    fire_activity("Planning the next question")
    plan_result = dean_v2.plan(
        state, chunks,
        carryover_notes=carryover_combined,
        domain_name=getattr(_cfg.domain, "name", "this subject"),
        domain_short=getattr(_cfg.domain, "short", "subject"),
        clinical_scenario_style=getattr(
            _cfg.domain, "clinical_scenario_style", "",
        ),
    )
    # : defensive override — if pre-advance fired but Dean STILL
    # planned a close mode (honest_close / reach_close / etc), force socratic
    # with the new hint level. Belt-and-suspenders against the plan-LLM
    # interpreting "student kept saying idk" as "close session" even after
    # the advance state is in place.
    if pre_advanced_low_effort and plan_result.turn_plan.mode in (
        "honest_close", "reach_close", "close", "tutoring_cap_close"
    ):
        from dataclasses import replace as _replace_le
        plan_result.turn_plan = _replace_le(
            plan_result.turn_plan,
            mode="socratic",
            tone="encouraging",
            scenario=f"low_effort_advance:hint_{state.get('hint_level', 1)}",
        )
        debug_trace.append({
            "wrapper": "dean_node_v2.low_effort_force_socratic",
            "result": "overrode_close_mode_to_socratic_after_pre_advance",
        })

    # — cancel-modal override. If student just clicked
    # Cancel on the exit modal, force mode=soft_reset on this turn so
    # Teacher emits the bridging "fresh angle" message regardless of
    # what Dean otherwise planned. Clear the flag here (one-shot per
    # cancel).
    if state.get("cancel_modal_pending"):
        from dataclasses import replace as _replace
        # planned hint_text=locked_question (or a near-paraphrase)
        # which Teacher's soft_reset rendering would then repeat verbatim.
        # A neutral re-engagement framing forces fresh phrasing.
        plan_result.turn_plan = _replace(
            plan_result.turn_plan,
            mode="soft_reset",
            tone="encouraging",
            scenario="soft_reset_after_cancel",
            hint_text="Re-engage with a fresh angle on the locked subsection — do NOT echo locked_question verbatim.",
        )
        state["cancel_modal_pending"] = False
        debug_trace.append({
            "wrapper": "dean_node_v2.cancel_modal_soft_reset",
            "result": "forced_mode_soft_reset_with_hint_override",
        })
    debug_trace.append({
        "wrapper": "dean_v2.plan",
        "parse_attempts": plan_result.parse_attempts,
        "used_fallback": plan_result.used_fallback,
        "mode": plan_result.turn_plan.mode,
        "tone": plan_result.turn_plan.tone,
        "elapsed_ms": plan_result.elapsed_ms,
        "tokens_in": plan_result.input_tokens,
        "tokens_out": plan_result.output_tokens,
        "error": plan_result.error,
    })

    # Drive the retry loop (Teacher × N + Haiku quartet × N + Dean.replan + safe probe)
    # pass snapshots + events for enriched history
    _debug_obj_for_inputs = state.get("debug") or {}
    inputs = TeacherPromptInputs(
        chunks=chunks,
        history=state.get("messages", []),
        locked_subsection=locked.get("subsection") or "",
        locked_question=state.get("locked_question") or "",
        domain_name=getattr(_cfg.domain, "name", "this subject"),
        domain_short=getattr(_cfg.domain, "short", "subject"),
        student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
        snapshots=_debug_obj_for_inputs.get("per_turn_snapshots", []) or [],
        system_events=_debug_obj_for_inputs.get("system_events", []) or [],
        # surface phase-transition
        # signal so Teacher's first post-lock turn opens with a brief
        # bridging acknowledgment of the locked subsection.
        topic_just_locked=bool(state.get("topic_just_locked", False)),
    )
    aliases = state.get("locked_answer_aliases") or []
    prior_qs = []
    for m in reversed(state.get("messages", []) or []):
        if (m or {}).get("role") == "tutor":
            prior_qs.append(str(m.get("content", "") or ""))
            if len(prior_qs) >= 2:
                break

    # propagate session-level image_context onto the TurnPlan so
    # Teacher's prompt builder can ground in identified structures. Dean
    # may have populated it itself (when re-planning); we set it from
    # state when Dean left it None so image-initiated sessions don't
    # lose the image context across turns.
    final_plan = plan_result.turn_plan
    session_image_context = state.get("image_context")
    if session_image_context and not final_plan.image_context:
        final_plan.image_context = session_image_context

    # exploration retrieval gate. Dean opts in via plan.needs_exploration
    # when student went tangential. Append exploration chunks (don't replace
    # locked-time chunks — Teacher sees both contexts).
    new_exploration_count = int(state.get("exploration_count", 0) or 0)
    new_exploration_used = int(state.get("exploration_used", 0) or 0)
    exploration_max = int(state.get("exploration_max", 10) or 10)
    # — per-turn exploration flag (drives sidebar EXPLORING badge).
    new_currently_exploring = False
    new_exploration_query_last = str(state.get("exploration_query_last", "") or "")
    # Budget gate: cap is runaway-protection only (default 10 — see
    # state.py). Pacing pressure comes from urgency_tier in the Dean
    # prompt, not from this counter. If we're over the cap, log it and
    # reuse existing chunks; the Teacher still answers the student's
    # exploration question, just without fetching fresh material.
    over_budget = new_exploration_used >= exploration_max
    if final_plan.needs_exploration and final_plan.exploration_query and not over_budget:
        fire_activity("Searching textbook for related context")
        try:
            extra = retriever.retrieve(final_plan.exploration_query) if retriever else []
        except Exception as e:
            extra = []
            debug_trace.append({
                "wrapper": "retriever.exploration_retrieve_error",
                "error": str(e)[:160],
            })
        if extra:
            tagged = []
            for c in extra:
                row = dict(c)
                row["exploration"] = True
                tagged.append(row)
            chunks = list(chunks) + tagged
            inputs.chunks = chunks
            new_exploration_count += 1
            new_exploration_used += 1
            new_currently_exploring = True
            new_exploration_query_last = str(final_plan.exploration_query or "")[:120]
            debug_trace.append({
                "wrapper": "exploration_retrieval",
                "n_added": len(tagged),
                "exploration_count": new_exploration_count,
                "exploration_used": new_exploration_used,
                "exploration_max": exploration_max,
                "query": final_plan.exploration_query[:80],
            })
    elif final_plan.needs_exploration and over_budget:
        # Asked but past the runaway cap — fall through with existing
        # chunks. Teacher still answers; just no new retrieval.
        debug_trace.append({
            "wrapper": "exploration_retrieval.over_budget",
            "exploration_used": new_exploration_used,
            "exploration_max": exploration_max,
            "query": (final_plan.exploration_query or "")[:80],
        })
        new_currently_exploring = True  # behaviorally still an exploration turn
    else:
        # On-topic engaged turn (no exploration requested) — decay the
        # recent-tangent counter. exploration_used (the lifetime budget)
        # never decays.
        new_exploration_count = max(0, new_exploration_count - 1)
        new_currently_exploring = False
        new_exploration_query_last = ""

    # Layer-2 mode-aware label so the demo viewer can see WHY this turn
    # has the shape it does (preflight intervention, soft_reset after
    # cancel, hint-advance from low_effort_streak_4, etc.).
    _mode = final_plan.mode
    _tone = final_plan.tone
    _hint_now = int(state.get("hint_level", 0) or 0)
    _max_hints = int(state.get("max_hints", 3) or 3)
    if pre_advanced_low_effort:
        _label = f"Mode: socratic, hint {_hint_now}/{_max_hints} (advanced from low-effort streak)"
        _detail = (
            f"consecutive_low_effort hit 4. Pre-plan safety net advanced "
            f"the hint level so Teacher's draft will be more concrete on "
            f"this turn (not on the next). Counter reset to 0/4."
        )
    elif state.get("recent_cancel_at_turn") == int(state.get("turn_count", 0) or 0):
        _label = f"Mode: {_mode} (after exit-modal cancel)"
        _detail = (
            "Student clicked Cancel on the exit-confirm modal. Teacher is "
            "drafting a soft_reset bridging message — fresh angle, no "
            "echo of the locked question."
        )
    else:
        _label = f"Mode: {_mode}, tone: {_tone}, hint {_hint_now}/{_max_hints}"
        _detail = (
            f"Dean planned a {_mode}-mode turn with {_tone} tone. "
            "Teacher will now draft 1-3 attempts, each verified by the "
            "Haiku quartet. If all fail, Dean replans and Teacher tries once more."
        )
    fire_activity(_label, detail=_detail)
    turn_result = run_turn(
        teacher=teacher_v2,
        dean=dean_v2,
        turn_plan=final_plan,
        teacher_inputs=inputs,
        dean_state=state,
        dean_chunks=chunks,
        locked_answer=state.get("locked_answer") or "",
        locked_answer_aliases=aliases,
        prior_tutor_questions=prior_qs,
    )
    debug_trace.append({
        "wrapper": "retry_orchestrator.run_turn",
        "final_attempt": turn_result.final_attempt,
        "used_safe_generic_probe": turn_result.used_safe_generic_probe,
        "used_dean_replan": turn_result.used_dean_replan,
        "leak_cap_fallback_fired": turn_result.leak_cap_fallback_fired,
        "timed_out": turn_result.timed_out,
        "elapsed_ms": turn_result.elapsed_ms,
        "n_attempts": len(turn_result.attempts),
        "attempt_summaries": [
            {
                "attempt": a.attempt_num,
                "passed": a.all_passed,
                "failed_checks": a.failed_check_names(),
            }
            for a in turn_result.attempts
        ],
    })

    fire_activity(
        "Finalizing reply",
        detail=(
            f"Retry loop done in {turn_result.elapsed_ms}ms across "
            f"{len(turn_result.attempts)} attempt(s). "
            + ("Dean replanned once. " if turn_result.used_dean_replan else "")
            + ("Used safe-probe fallback." if turn_result.used_safe_generic_probe
               else "Final draft passed all checks.")
        ),
    )

    # — when retry exhausts (used_safe_generic_probe=True), do NOT
    # ship templated tutor text. Emit an error_card system message so
    # the frontend ErrorCard component renders it as a distinct UI
    # element with [Retry] instead of a fake tutor bubble.
    msgs = list(state.get("messages", []))
    if turn_result.used_safe_generic_probe or not (turn_result.final_text or "").strip():
        failed_summary = []
        for a in (turn_result.attempts or []):
            failed = a.failed_check_names()
            if failed:
                failed_summary.append(f"attempt {a.attempt_num}: {','.join(failed)}")
        err_msg = (
            "; ".join(failed_summary)[:200]
            if failed_summary
            else "Teacher returned empty drafts on all retry attempts"
        )
        msgs.append({
            "role": "system",
            "content": "",
            "phase": "tutoring",
            "metadata": {
                "kind": "error_card",
                "component": "Teacher.draft (retry exhausted)",
                "error_class": (
                    "LeakCapFallback" if turn_result.leak_cap_fallback_fired
                    else "RetryExhausted"
                ),
                "message": err_msg,
                "retry_handler": "tutoring_turn",
            },
        })
    else:
        msgs.append({
            "role": "tutor",
            "content": turn_result.final_text,
            "phase": "tutoring",
            "metadata": {
                "mode": turn_result.final_turn_plan.mode if turn_result.final_turn_plan else "socratic",
                "tone": turn_result.final_turn_plan.tone if turn_result.final_turn_plan else "neutral",
                "final_attempt": turn_result.final_attempt,
                "safe_probe": turn_result.used_safe_generic_probe,
            },
        })
        # snapshot the tutor turn (main tutoring path)
        state["messages"] = msgs
        from conversation.snapshots import snapshot_tutor_turn
        snapshot_tutor_turn(
            state,
            mode=turn_result.final_turn_plan.mode if turn_result.final_turn_plan else "socratic",
            tone=turn_result.final_turn_plan.tone if turn_result.final_turn_plan else "neutral",
            attempts=int(turn_result.final_attempt or 1),
        )

    elapsed_ms = int((time.time() - t0) * 1000)
    debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms})

    # Hint-level advance — Dean signals on substantive-but-wrong answers.
    # Cap at max_hints+1 so the next routing tick trips the hint-exhaustion
    # path to memory_update (edges.py:67 fix).
    prev_hint_level_engaged = int(state.get("hint_level", 0) or 0)
    new_hint_level_engaged = prev_hint_level_engaged
    last_advance_at_engaged = int(state.get("last_hint_advance_at_turn", -1) or -1)
    # — diagnostic counters. Carry forward from state, increment
    # below at the right branch site so we know which path fired.
    dean_hint_override_count_eng = int(state.get("dean_hint_override_count", 0) or 0)
    rule_hint_advance_count_eng = int(state.get("rule_hint_advance_count", 0) or 0)
    if final_plan.advance_hint_level and not state.get("student_reached_answer"):
        max_hints = int(state.get("max_hints", 3) or 3)
        new_hint_level_engaged = min(max_hints + 1, prev_hint_level_engaged + 1)
        last_advance_at_engaged = int(state.get("turn_count", 0) or 0)
        # — only count if the level actually moved (cap may pin it).
        if new_hint_level_engaged > prev_hint_level_engaged:
            dean_hint_override_count_eng += 1
        debug_trace.append({
            "wrapper": "dean_node_v2.hint_level_advance",
            "from": prev_hint_level_engaged,
            "to": new_hint_level_engaged,
            "trigger": "dean_signal",
        })
        # log hint_advance event
        from conversation.snapshots import log_system_event
        log_system_event(
            state, "hint_advance",
            from_level=prev_hint_level_engaged,
            to_level=new_hint_level_engaged,
        )

    # — deterministic safety net for consecutive low-effort
    # escalation. : changed from force-session-end to symmetric
    # hint-advance behavior (mirrors help_abuse strike-4). 4 passive "idk"s
    # in a row now bumps the hint level and resets the counter — gives the
    # student a more concrete scaffold instead of bailing out on them. The
    # session ends naturally only when hints are exhausted (hint_level=3 +
    # next strike triggers no further advance — Dean's plan or after_dean
    # handles the end on its own terms).
    consecutive_low = int(state.get("consecutive_low_effort_count", 0) or 0)
    if consecutive_low >= 4 and not state.get("student_reached_answer"):
        # cap at max_hints+1 to
        # capped at max_hints, which meant low_effort streaks could
        # never push hint past 3 → after_dean's "hint_level > max_hints"
        # termination never tripped → session continued indefinitely
        # ( Sim 4 stonewall).
        max_hints = int(state.get("max_hints", 3) or 3)
        prev_hint_low = new_hint_level_engaged
        new_hint_level_engaged = min(max_hints + 1, new_hint_level_engaged + 1)
        # — count rule-based advance if level actually moved.
        if new_hint_level_engaged > prev_hint_low:
            rule_hint_advance_count_eng += 1
        # Reset the counter for a fresh warning chain (same pattern as
        # help_abuse strike-4 reset in preflight.py).
        state["consecutive_low_effort_count"] = 0
        debug_trace.append({
            "wrapper": "dean_node_v2.low_effort_force_hint_advance",
            "consecutive_low_effort_at_fire": consecutive_low,
            "from_hint": prev_hint_low,
            "to_hint": new_hint_level_engaged,
        })
        from conversation.snapshots import log_system_event
        log_system_event(
            state, "hint_advance",
            from_level=prev_hint_low,
            to_level=new_hint_level_engaged,
            trigger="low_effort_streak_4",
        )

    # — engaged_wrong_count: increment when student gave a
    # substantive on-topic turn that didn't reach. We're in the
    # engaged-tutoring path here (preflight passed, reach gate ran)
    # so a non-reach engaged turn is exactly "tried, missed".
    engaged_wrong_count_eng = int(state.get("engaged_wrong_count", 0) or 0)
    if not state.get("student_reached_answer"):
        engaged_wrong_count_eng += 1

    final_return = {
        "messages": msgs,
        "help_abuse_count": new_help_count,  # reset to 0 on engagement
        "off_topic_count": new_off_count,
        "turn_count": int(state.get("turn_count", 0) or 0) + 1,
        "hint_level": new_hint_level_engaged,
        "last_hint_advance_at_turn": last_advance_at_engaged,
        # — propagate reach gate result so after_dean routes
        # to assessment_node when the student reached the answer.
        "student_reached_answer": bool(state.get("student_reached_answer", False)),
        "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
        "student_reach_path": str(state.get("student_reach_path", "") or ""),
        # exploration_count: recent-tangent frequency (decays on engaged turns).
        # exploration_used:  lifetime counter, capped at exploration_max as
        #                    runaway protection only.
        "exploration_count": new_exploration_count,
        "exploration_used": new_exploration_used,
        # — propagate consecutive_low_effort_count so
        # snapshot annotations reflect the latest streak in next turn
        "consecutive_low_effort_count": int(state.get("consecutive_low_effort_count", 0) or 0),
        # — clear cancel_modal_pending after one-shot
        # soft_reset turn so subsequent turns return to normal flow.
        # Also explicitly carry exit_intent_pending = False (cancel
        # cleared it; ensure LangGraph reducer doesn't revert).
        "cancel_modal_pending": False,
        "exit_intent_pending": bool(state.get("exit_intent_pending", False)),
        # — diagnostic counters.
        "engaged_wrong_count": engaged_wrong_count_eng,
        "dean_hint_override_count": dean_hint_override_count_eng,
        "rule_hint_advance_count": rule_hint_advance_count_eng,
        "currently_exploring": new_currently_exploring,
        "exploration_query_last": new_exploration_query_last,
        "debug": state["debug"],
    }
    # When an anchor_pick was just resolved on this same invocation, the
    # tutoring return above doesn't echo locked_question / locked_answer /
    # locked_topic — without merging in the handler's overrides, the
    # LangGraph reducer would drop those updates and the NEXT invocation
    # would see empty Q/A again. Apply overrides last so they land in the
    # node's output dict.
    if anchor_pick_overrides:
        for k, v in anchor_pick_overrides.items():
            # Don't clobber tutoring's messages with handler's empty list.
            if k == "messages":
                continue
            final_return[k] = v
    return final_return

# ─────────────────────────────────────────────────────────────────────────────
# assessment_node_v2 — wires DeanV2 + TeacherV2 into the assessment phase
# ─────────────────────────────────────────────────────────────────────────────

def assessment_node_v2(state: dict, dean, teacher, retriever) -> dict:
    """V2 assessment phase node — opt-in/clinical/close orchestration.

 Constructs DeanV2 + TeacherV2 lazily (matches dean_node_v2 pattern)
 and delegates to conversation.assessment_v2.assessment_node_v2 for
 the orchestration. `dean` and `teacher` are the legacy agents kept
 for parity with the dean_node_v2 signature; the v2 stack does not
 use them today (helpers like _coverage_gate aren't needed in
 assessment), so they're passed through but ignored.
"""
    from conversation.llm_client import make_anthropic_client, resolve_model
    from config import cfg as _cfg

    # Archive previous turn's trace before resetting (same pattern as
    # dean_node_v2). Keeps per-phase trace records distinct.
    prior_trace = list(state.get("debug", {}).get("turn_trace", []) or [])
    if prior_trace:
        att = list(state.get("debug", {}).get("all_turn_traces", []) or [])
        att.append({
            "turn": int(state.get("turn_count", 0) or 0),
            "phase": state.get("phase", "assessment"),
            "trace": prior_trace,
        })
        state.setdefault("debug", {})["all_turn_traces"] = att[-50:]
    state.setdefault("debug", {})["turn_trace"] = []

    client = make_anthropic_client()
    dean_v2_inst = DeanV2(client, model=resolve_model(_cfg.models.dean))
    teacher_v2_inst = TeacherV2(client, model=resolve_model(_cfg.models.teacher))

    return _assessment_node_v2_impl(
        state,
        dean=dean,
        teacher=teacher,
        retriever=retriever,
        dean_v2=dean_v2_inst,
        teacher_v2=teacher_v2_inst,
    )
