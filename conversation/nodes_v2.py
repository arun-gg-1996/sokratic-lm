"""
conversation/nodes_v2.py
────────────────────────
LangGraph node functions implementing the L43-L62 tutor flow rewrite
(Track 4.7b).

Composes the new modules built in Tracks 4.1-4.6:

  preflight        ── 3 parallel Haiku checks (L44/L55/L56/L58)
  dean_v2          ── single-call TurnPlan emitter (L46/L47/L51/L53)
  teacher_v2       ── single mode-dispatched draft (L49/L52/L54)
  retry_orchestrator ── bounded retry + safe-generic-probe (L50/L62)
  classifiers      ── 4 Haiku self-policing checks (L48/L59/L60/L61)
  mem0_safe        ── safe read/write wrappers (L5)
  observation_extractor ── single Haiku extraction at session end (L4)
  SQLite store     ── per-domain session + mastery (L1/L2/L3/L21)

Live behind a feature flag (SOKRATIC_USE_V2_FLOW=1). When the flag is
unset/0, the legacy nodes still run unchanged. Flag-on enables A/B
comparison via Nidhi's existing 8 e2e scenarios.

Scope of this commit
--------------------
* dean_node_v2:
    0. If topic is not locked yet → topic_lock_v2 handles L9/L10/L11/L22
    1. Run preflight on the latest student message
    2. If preflight fires → teacher_v2.draft(redirect/nudge/confirm_end)
       (Dean SKIPPED, hint/strike counters updated per L55/L56/L58)
    3. Else → fetch chunks → dean_v2.plan() → retry_orchestrator.run_turn()
    4. Update state with the final text + hint_level/strikes/etc.

* rapport_node_v2 — uses teacher_v2.draft(mode="rapport") with carryover
  notes derived from SQL (already wired in legacy rapport_node via
  Track 4.7a SQL-read fix).

* assessment_node_v2 — clinical opt-in + clinical phase via teacher_v2.

NOT in scope of this commit (defer to follow-ups)
-------------------------------------------------
* Clinical phase scenario generation (L74) — Track Clinical
* L6 mem0 read injection points (#1 + #2) — Track 4.7e

Why this scoping
----------------
Locked-topic per-turn tutoring is the single biggest surface area
(every tutoring turn). Validating the v2 stack on this case via
Nidhi's e2e scenarios is the most useful confidence signal. The
unlocked / pre-lock paths are bounded (1-7 turns/session).
"""
from __future__ import annotations

import os
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


def use_v2_flow() -> bool:
    """Feature flag — flip via env var at process start."""
    return os.environ.get("SOKRATIC_USE_V2_FLOW", "0").strip() == "1"


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
      state:     TutorState dict
      dean:      legacy DeanAgent — used for topic-locking + retrieval
      teacher:   legacy TeacherAgent — kept for legacy fallback paths
      retriever: ChunkRetriever — for fetching chunks at lock time

    Returns:
      Partial state dict for LangGraph reducer (messages, phase, hint_level,
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
    from conversation.teacher import fire_activity
    fire_activity("Reading your message")

    # If topic isn't locked yet, Track 4.7d owns the v2 pre-lock path:
    # L9 topic_mapper_llm, L10 confirm-and-lock, L11 prelock counter,
    # L22 guided-pick at cap 7, and M4 (B6) anchor_pick.
    #
    # M4: even when locked_topic.path is set (from _apply_prelock), we
    # MUST route to topic_lock_v2 if there's a pending anchor_pick — that
    # handler resolves the student's anchor selection into the actual
    # locked_question/locked_answer/aliases. Without this gate, dean_node_v2
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
        # echo locked_question etc, so without this they'd get dropped).
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

    # ── 0. L53 reach-answer gate (Track 4.7g) ─────────────────────────────
    # Mirrors legacy dean.run_turn() lines 1741-1783 — fires the SAME
    # gate (Step A.1 token-overlap → Step A.2 K-of-N partial reach →
    # Step B LLM paraphrase) on the student's latest message before any
    # planning. Stamps state so:
    #   * after_dean() can route to assessment_node when reached=True
    #   * Dean.plan() and Teacher inputs can see student_reached_answer
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
            gate_result = dean.reached_answer_gate(state, latest_student)
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

    # ── 1. Pre-flight Haiku layer ────────────────────────────────────────
    fire_activity("Checking message intent")
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

    # ── 2a. Pre-flight fired → Dean SKIPPED, Teacher renders redirect ───
    if preflight.fired:
        # M1 — DEFLECTION SHORT-CIRCUIT: when preflight detects the student
        # wants to end, do NOT have Teacher draft a confirm_end message
        # (that's templated tutor text per M-FB). Instead, stamp
        # exit_intent_pending=True; the frontend (useWebSocket → store →
        # ChatView) renders ExitConfirmModal directly. Modal pops, no
        # tutor bubble added to transcript.
        if preflight.category == "deflection":
            elapsed_ms_e = int((time.time() - t0) * 1000)
            debug_trace.append({
                "wrapper": "dean_node_v2.exit_intent_modal_triggered",
                "category": preflight.category,
                "evidence": preflight.evidence[:120],
            })
            debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms_e})
            return {
                "exit_intent_pending": True,
                "help_abuse_count": new_help_count,
                "off_topic_count": new_off_count,
                "student_reached_answer": bool(state.get("student_reached_answer", False)),
                "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
                "student_reach_path": str(state.get("student_reach_path", "") or ""),
                "debug": state["debug"],
            }

        # Force hint advance if L55 strike-4 fired
        new_hint_level = int(state.get("hint_level", 0) or 0)
        if preflight.should_force_hint_advance:
            new_hint_level = min(3, new_hint_level + 1)

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
        inputs = TeacherPromptInputs(
            chunks=[],  # redirect/nudge/confirm_end don't use chunks
            history=state.get("messages", []),
            locked_subsection=locked.get("subsection") or "",
            locked_question=state.get("locked_question") or "",
            domain_name=getattr(_cfg.domain, "name", "this subject"),
            domain_short=getattr(_cfg.domain, "short", "subject"),
            student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
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

        # Honor end-session signal (L56 strike 4)
        new_phase = state.get("phase", "tutoring")
        if preflight.should_end_session:
            new_phase = "memory_update"
            msgs[-1]["metadata"]["is_closing"] = True
            state.setdefault("session_ended_off_domain", True)

        elapsed_ms = int((time.time() - t0) * 1000)
        debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms})

        # L6 mem0 injection #2 hook: stamp the turn at which hint advanced
        # so the NEXT turn's dean_node_v2 entry can read learning-style cue
        # via mem0_inject.read_hint_advance_carryover and pass it as
        # carryover_notes to dean.plan().
        prev_hint_level = int(state.get("hint_level", 0) or 0)
        last_advance_at = int(state.get("last_hint_advance_at_turn", -1) or -1)
        if new_hint_level > prev_hint_level:
            last_advance_at = int(state.get("turn_count", 0) or 0)

        return {
            "messages": msgs,
            "help_abuse_count": new_help_count,
            "off_topic_count": new_off_count,
            "hint_level": new_hint_level,
            "last_hint_advance_at_turn": last_advance_at,
            "phase": new_phase,
            # Track 4.7g — propagate reach gate result so after_dean
            # routes to assessment_node when the student reached the answer.
            "student_reached_answer": bool(state.get("student_reached_answer", False)),
            "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
            "student_reach_path": str(state.get("student_reach_path", "") or ""),
            "debug": state["debug"],
        }

    # ── 2b. No pre-flight fire → Dean plans + Teacher drafts via retry ──
    from conversation.llm_client import make_anthropic_client, resolve_model
    from config import cfg as _cfg

    # M6 — reuse lock-time chunks by default. The unconditional per-turn
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

    # ── L6 mem0 carryover assembly (Track 4.7f) ─────────────────────────
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
        except Exception as e:
            debug_trace.append({
                "wrapper": "mem0_inject.hint_advance_carryover_error",
                "error": f"{type(e).__name__}: {str(e)[:160]}",
            })
    carryover_combined = combine_carryover(
        carryover_topic_lock, carryover_hint_advance,
    )

    # Plan the turn — Track 4.7f mem0 carryover + L78 domain-aware
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
    inputs = TeacherPromptInputs(
        chunks=chunks,
        history=state.get("messages", []),
        locked_subsection=locked.get("subsection") or "",
        locked_question=state.get("locked_question") or "",
        domain_name=getattr(_cfg.domain, "name", "this subject"),
        domain_short=getattr(_cfg.domain, "short", "subject"),
        student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
    )
    aliases = state.get("locked_answer_aliases") or []
    prior_qs = []
    for m in reversed(state.get("messages", []) or []):
        if (m or {}).get("role") == "tutor":
            prior_qs.append(str(m.get("content", "") or ""))
            if len(prior_qs) >= 2:
                break

    # L77 — propagate session-level image_context onto the TurnPlan so
    # Teacher's prompt builder can ground in identified structures. Dean
    # may have populated it itself (when re-planning); we set it from
    # state when Dean left it None so image-initiated sessions don't
    # lose the image context across turns.
    final_plan = plan_result.turn_plan
    session_image_context = state.get("image_context")
    if session_image_context and not final_plan.image_context:
        final_plan.image_context = session_image_context

    # M6 — exploration retrieval gate. Dean opts in via plan.needs_exploration
    # when student went tangential. Append exploration chunks (don't replace
    # locked-time chunks — Teacher sees both contexts).
    new_exploration_count = int(state.get("exploration_count", 0) or 0)
    if final_plan.needs_exploration and final_plan.exploration_query:
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
            debug_trace.append({
                "wrapper": "exploration_retrieval",
                "n_added": len(tagged),
                "exploration_count": new_exploration_count,
                "query": final_plan.exploration_query[:80],
            })
    else:
        # On-topic engaged turn (no exploration requested) — decay the count.
        new_exploration_count = max(0, new_exploration_count - 1)

    fire_activity("Drafting tutoring question")
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

    fire_activity("Reviewing draft for accuracy")

    # M-FB — when retry exhausts (used_safe_generic_probe=True), do NOT
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

    elapsed_ms = int((time.time() - t0) * 1000)
    debug_trace.append({"wrapper": "dean_node_v2.total_elapsed_ms", "value": elapsed_ms})

    # Hint-level advance — Dean signals on substantive-but-wrong answers.
    # Cap at max_hints+1 so the next routing tick trips the hint-exhaustion
    # path to memory_update (per M1's edges.py:67 fix).
    prev_hint_level_engaged = int(state.get("hint_level", 0) or 0)
    new_hint_level_engaged = prev_hint_level_engaged
    last_advance_at_engaged = int(state.get("last_hint_advance_at_turn", -1) or -1)
    if final_plan.advance_hint_level and not state.get("student_reached_answer"):
        max_hints = int(state.get("max_hints", 3) or 3)
        new_hint_level_engaged = min(max_hints + 1, prev_hint_level_engaged + 1)
        last_advance_at_engaged = int(state.get("turn_count", 0) or 0)
        debug_trace.append({
            "wrapper": "dean_node_v2.hint_level_advance",
            "from": prev_hint_level_engaged,
            "to": new_hint_level_engaged,
            "trigger": "dean_signal",
        })

    final_return = {
        "messages": msgs,
        "help_abuse_count": new_help_count,  # reset to 0 on engagement
        "off_topic_count": new_off_count,
        "turn_count": int(state.get("turn_count", 0) or 0) + 1,
        "hint_level": new_hint_level_engaged,
        "last_hint_advance_at_turn": last_advance_at_engaged,
        # Track 4.7g — propagate reach gate result so after_dean routes
        # to assessment_node when the student reached the answer.
        "student_reached_answer": bool(state.get("student_reached_answer", False)),
        "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
        "student_reach_path": str(state.get("student_reach_path", "") or ""),
        # M6 — exploration count incremented on tangent / decayed on on-topic
        "exploration_count": new_exploration_count,
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
