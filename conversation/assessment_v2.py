"""
conversation/assessment_v2.py
─────────────────────────────
Track 4.7e: v2 assessment phase.

Owns the post-tutoring assessment phase under SOKRATIC_USE_V2_FLOW:
  * L65 — opt-in "No" path: brief reach-confirmation + textbook-answer
          confirmation, then graceful close
  * L67 — clinical_turn_count caps at 7 (separate from tutoring's 25 and
          pre-lock's 7)
  * L70 — pre-flight Haiku DURING clinical: counters tick, but escalation
          is suppressed (no hint advance, no terminate) — natural turn cap
          is the only termination trigger
  * L71 — clinical retrieval = anchor chunks only (state.retrieved_chunks
          set at lock time; no exploration retrieval here)
  * L72 — clinical reuses the tutor pipeline (run_turn from
          retry_orchestrator) with TurnPlan.mode = "clinical"
  * L73 — opt-in UX: TurnPlan(mode="opt_in", tone="neutral"), Yes/No
          buttons + free-text fallback handled in this node
  * L74 — clinical scenario lazy-generated via DeanV2 on opt-in Yes
  * L75 — Dean plans (TurnPlan minting), Teacher renders (one Sonnet
          model serving both via different prompts)

Reveal path (student did NOT reach answer):
  Reveal the locked answer + close. clinical_mastery_tier = not_assessed.
  Re-uses TeacherV2 honest_close mode with the answer surfaced via
  hint_text so the student sees what they missed.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from conversation.retry_orchestrator import run_turn
from conversation.teacher_v2 import TeacherPromptInputs, TeacherV2
from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# L67 — clinical phase has its own 7-turn budget, separate from tutoring
CLINICAL_TURN_CAP = 7

# Sentinel labels used in pending_user_choice for clinical opt-in
OPT_IN_OPTIONS = ["Yes", "No"]

# Max re-ask attempts when opt-in classifier returns "ambiguous". Beyond
# this we close the session (per L65) instead of looping. Surfaced as a
# constant so tests + the audit doc can reference it.
OPT_IN_REASK_CAP = 2


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def assessment_node_v2(
    state: dict,
    *,
    dean,
    teacher,
    retriever,
    dean_v2: Any,
    teacher_v2: TeacherV2,
) -> dict:
    """v2 assessment phase orchestrator.

    Dispatch by (student_reached_answer, assessment_turn):
      reached=False                        → reveal-and-close
      reached=True,  assessment_turn=0     → render opt-in question
      reached=True,  assessment_turn=1     → handle Yes/No/typed-text response
      reached=True,  assessment_turn=2     → run one clinical loop turn
                                             via run_turn() with mode=clinical

    `dean` and `teacher` are the LEGACY agents — passed through for
    compatibility with helpers that still need them (e.g. _coverage_gate,
    _replace_latest_student_message). All NEW behavior runs through
    `dean_v2` (DeanV2) + `teacher_v2` (TeacherV2).
    """
    state.setdefault("debug", {}).setdefault("turn_trace", [])
    state["debug"]["current_node"] = "assessment_node_v2"
    trace = state["debug"]["turn_trace"]

    reached = bool(state.get("student_reached_answer", False))
    assessment_turn = int(state.get("assessment_turn", 0) or 0)

    trace.append({
        "wrapper": "assessment_v2.entry",
        "reached": reached,
        "assessment_turn": assessment_turn,
        "clinical_turn_count": int(state.get("clinical_turn_count", 0) or 0),
    })

    if not reached:
        return _render_reveal_close(state, teacher_v2)

    if assessment_turn == 0:
        return _render_opt_in(state, teacher_v2)

    if assessment_turn == 1:
        return _handle_opt_in_response(
            state,
            dean_v2=dean_v2,
            teacher_v2=teacher_v2,
            retriever=retriever,
        )

    # assessment_turn >= 2 → clinical loop
    return _run_clinical_turn(
        state,
        dean=dean,
        teacher=teacher,
        dean_v2=dean_v2,
        teacher_v2=teacher_v2,
        retriever=retriever,
    )


# ─────────────────────────────────────────────────────────────────────────────
# L73 — render opt-in question (TurnPlan mode="opt_in", neutral tone)
# ─────────────────────────────────────────────────────────────────────────────


def _render_opt_in(state: dict, teacher_v2: TeacherV2) -> dict:
    """First entry into assessment with reached=True. Construct an opt-in
    TurnPlan manually (no Dean ceremony needed — opt-in is short and
    deterministic), render via Teacher, present Yes/No pending choice.
    """
    locked = state.get("locked_topic") or {}
    plan = TurnPlan.minimal_fallback(
        scenario="clinical_opt_in_offer",
        hint_text="",
        tone="neutral",
    )
    # Override mode after construction (minimal_fallback returns socratic)
    plan = TurnPlan(
        scenario="clinical_opt_in_offer",
        hint_text="",
        mode="opt_in",
        tone="neutral",
        forbidden_terms=plan.forbidden_terms,
        permitted_terms=plan.permitted_terms,
        shape_spec={"max_sentences": 2, "exactly_one_question": True},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )

    inputs = _teacher_inputs(state, locked)
    text = _safe_teacher_draft(
        teacher_v2, plan, inputs,
        fallback_text=(
            "You reached the core answer — would you like a quick clinical-application "
            "question to apply it, or wrap up here?"
        ),
        trace=state["debug"]["turn_trace"],
    )

    messages = list(state.get("messages", []) or [])
    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "assessment",
        "metadata": {"mode": "opt_in", "tone": "neutral", "source": "assessment_v2"},
    })
    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.opt_in_rendered",
        "preview": text[:120],
    })

    return {
        "messages": messages,
        "assessment_turn": 1,
        "clinical_opt_in": None,
        "phase": "assessment",
        "pending_user_choice": {
            "kind": "opt_in",
            "options": OPT_IN_OPTIONS,
        },
        "debug": state["debug"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Handle opt-in response (Yes / No / typed-text fallback per L73)
# ─────────────────────────────────────────────────────────────────────────────


def _handle_opt_in_response(
    state: dict,
    *,
    dean_v2: Any,
    teacher_v2: TeacherV2,
    retriever: Any,
) -> dict:
    messages = list(state.get("messages", []) or [])
    student_msg = _latest_student(messages)

    intent = _classify_opt_in(student_msg)
    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.opt_in_response",
        "intent": intent,
        "student_msg_preview": student_msg[:80],
    })

    if intent == "yes":
        return _enter_clinical_phase(
            state,
            messages=messages,
            dean_v2=dean_v2,
            teacher_v2=teacher_v2,
            retriever=retriever,
        )

    if intent == "no":
        # L65 — answer-confirmation graceful close
        return _render_reach_close(state, teacher_v2, messages=messages)

    # Ambiguous: re-ask, but cap re-asks to prevent infinite loops
    # (sanity-check observation 2026-05-03 — the simulator typing
    # substantive responses kept landing in ambiguous and looping).
    # After OPT_IN_REASK_CAP attempts, treat as "no" and close
    # gracefully via L65.
    reask_count = int(state.get("opt_in_reask_count", 0) or 0)
    if reask_count >= OPT_IN_REASK_CAP:
        state["debug"]["turn_trace"].append({
            "wrapper": "assessment_v2.opt_in_reask_cap",
            "result": f"reask_count={reask_count} >= cap {OPT_IN_REASK_CAP}; treating as no",
        })
        return _render_reach_close(state, teacher_v2, messages=messages)
    state["opt_in_reask_count"] = reask_count + 1
    return _render_opt_in_clarify(state, teacher_v2, messages=messages)


def _render_opt_in_clarify(
    state: dict,
    teacher_v2: TeacherV2,
    *,
    messages: list[dict],
) -> dict:
    locked = state.get("locked_topic") or {}
    plan = TurnPlan(
        scenario="clinical_opt_in_clarify",
        hint_text="",
        mode="opt_in",
        tone="neutral",
        forbidden_terms=[],
        permitted_terms=[],
        shape_spec={"max_sentences": 2, "exactly_one_question": True},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )
    inputs = _teacher_inputs(state, locked)
    text = _safe_teacher_draft(
        teacher_v2, plan, inputs,
        fallback_text=(
            "Just to confirm — would you like a clinical-application question, "
            "or wrap up here?"
        ),
        trace=state["debug"]["turn_trace"],
    )
    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "assessment",
        "metadata": {"mode": "opt_in", "tone": "neutral", "source": "assessment_v2"},
    })
    return {
        "messages": messages,
        "assessment_turn": 1,
        "clinical_opt_in": None,
        "phase": "assessment",
        "pending_user_choice": {
            "kind": "opt_in",
            "options": OPT_IN_OPTIONS,
        },
        "debug": state["debug"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# L74 — enter clinical phase: lazy-generate scenario via DeanV2
# ─────────────────────────────────────────────────────────────────────────────


def _enter_clinical_phase(
    state: dict,
    *,
    messages: list[dict],
    dean_v2: Any,
    teacher_v2: TeacherV2,
    retriever: Any,
) -> dict:
    """L74 — student opted IN. Generate clinical scenario via DeanV2,
    render the first clinical question via TeacherV2, set up state for
    the multi-turn clinical loop.

    Per L71, clinical retrieval = anchor chunks only — we re-use
    state["retrieved_chunks"] from lock time without firing additional
    retrieval.
    """
    locked = state.get("locked_topic") or {}
    chunks = list(state.get("retrieved_chunks", []) or [])

    # Mint clinical TurnPlan via DeanV2.plan() — Dean reads state context
    # (assessment_turn=1, locked anchors, history showing the reach event)
    # and is expected to emit mode="clinical" + clinical_scenario + target.
    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.clinical_scenario_gen_start",
        "locked_path": str(locked.get("path", "") or ""),
        "chunk_count": len(chunks),
    })
    t0 = time.time()
    try:
        # Mark state so Dean's prompt context indicates we're entering clinical.
        # DeanV2.plan() infers mode from context; the marker steers it.
        state["_clinical_scenario_request"] = True
        plan_result = dean_v2.plan(
            state, chunks,
            **_dean_domain_kwargs(),
        )
    except Exception as e:
        state["debug"]["turn_trace"].append({
            "wrapper": "assessment_v2.clinical_scenario_gen_error",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })
        plan_result = None
    finally:
        state.pop("_clinical_scenario_request", None)
    elapsed_ms = int((time.time() - t0) * 1000)

    plan = getattr(plan_result, "turn_plan", None) if plan_result else None
    if plan is None or plan.mode != "clinical" or not plan.clinical_scenario:
        # Dean either failed to plan or didn't switch to clinical mode.
        # Fall back to a deterministic neutral close — never block session-end.
        state["debug"]["turn_trace"].append({
            "wrapper": "assessment_v2.clinical_scenario_gen_fallback",
            "reason": "dean_did_not_emit_clinical_plan",
            "elapsed_ms": elapsed_ms,
        })
        return _render_reach_close(state, teacher_v2, messages=messages)

    # Render the first clinical question via TeacherV2.
    inputs = _teacher_inputs(state, locked, chunks=chunks)
    text = _safe_teacher_draft(
        teacher_v2, plan, inputs,
        fallback_text=plan.clinical_scenario,
        trace=state["debug"]["turn_trace"],
    )

    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "assessment",
        "metadata": {
            "mode": "clinical",
            "tone": plan.tone,
            "source": "assessment_v2",
            "clinical_scenario": plan.clinical_scenario,
        },
    })

    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.clinical_phase_entered",
        "clinical_scenario_preview": (plan.clinical_scenario or "")[:120],
        "elapsed_ms": elapsed_ms,
    })

    return {
        "messages": messages,
        "assessment_turn": 2,
        "clinical_opt_in": True,
        "clinical_turn_count": 0,
        "clinical_max_turns": CLINICAL_TURN_CAP,
        "clinical_completed": False,
        "clinical_state": None,
        "clinical_confidence": 0.0,
        "clinical_history": [{
            "turn": 0,
            "role": "tutor",
            "scenario": plan.clinical_scenario,
            "target": plan.clinical_target,
        }],
        "phase": "assessment",
        "pending_user_choice": {},
        "debug": state["debug"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# L72 — clinical loop turn (reuses run_turn from retry_orchestrator)
# ─────────────────────────────────────────────────────────────────────────────


def _run_clinical_turn(
    state: dict,
    *,
    dean: Any,
    teacher: Any,
    dean_v2: Any,
    teacher_v2: TeacherV2,
    retriever: Any,
) -> dict:
    """Run one clinical loop turn via the same pipeline as tutoring (L72).

    L70: pre-flight Haiku trio runs (counters increment for telemetry) but
    cannot escalate (no hint advance, no terminate) — natural cap (L67) is
    the only termination trigger.

    L67: cap at CLINICAL_TURN_CAP = 7. After cap, render close + route to
    memory_update.
    """
    messages = list(state.get("messages", []) or [])
    locked = state.get("locked_topic") or {}
    chunks = list(state.get("retrieved_chunks", []) or [])

    clinical_turn_count = int(state.get("clinical_turn_count", 0) or 0) + 1
    clinical_history = list(state.get("clinical_history", []) or [])
    clinical_history.append({
        "turn": clinical_turn_count,
        "role": "student",
        "content": _latest_student(messages),
    })

    # L67 — cap check BEFORE running the turn so we don't burn an extra LLM
    # call on a turn that would just close anyway.
    if clinical_turn_count > CLINICAL_TURN_CAP:
        state["clinical_turn_count"] = CLINICAL_TURN_CAP
        state["clinical_completed"] = False
        state["clinical_history"] = clinical_history
        state["debug"]["turn_trace"].append({
            "wrapper": "assessment_v2.clinical_cap_reached",
            "cap": CLINICAL_TURN_CAP,
        })
        # clinical_mastery_tier intentionally NOT set to not_assessed here
        # — the mastery scorer at memory_update derives it from history.
        return _render_clinical_close(state, teacher_v2, messages=messages)

    # L72 — call the same retry orchestrator used for tutoring. Dean
    # plans first (mints a fresh clinical TurnPlan continuing the scenario),
    # then run_turn drives Teacher draft + 4 Haiku checks + 1 Dean replan
    # + safe-generic-probe fallback (L50/L62). Pre-flight is owned by
    # dean_node_v2 in tutoring; in the clinical loop we skip pre-flight
    # entirely per L70 (counters ticking is informational; the natural
    # 7-turn cap (L67) is the only termination trigger).
    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.run_turn_start",
        "clinical_turn": clinical_turn_count,
    })
    try:
        # Continuation plan — Dean sees the existing clinical scenario
        # in conversation history + state, evaluates student's response,
        # and emits next clinical TurnPlan.
        state["_clinical_continuation"] = True
        plan_result = dean_v2.plan(
            state, chunks,
            **_dean_domain_kwargs(),
        )
        state.pop("_clinical_continuation", None)
        clinical_plan = getattr(plan_result, "turn_plan", None)
        if clinical_plan is None:
            raise RuntimeError("dean_v2.plan returned no turn_plan")
        # Force clinical mode if Dean drifted (e.g. emitted socratic).
        if clinical_plan.mode != "clinical":
            scenario = _last_scenario_from_history(state)
            clinical_plan = TurnPlan(
                scenario=clinical_plan.scenario,
                hint_text=clinical_plan.hint_text,
                mode="clinical",
                tone=clinical_plan.tone,
                forbidden_terms=clinical_plan.forbidden_terms,
                permitted_terms=clinical_plan.permitted_terms,
                shape_spec=clinical_plan.shape_spec,
                carryover_notes=clinical_plan.carryover_notes,
                clinical_scenario=scenario,
                clinical_target=clinical_plan.clinical_target,
                apply_redaction=False,
            )

        teacher_inputs = _teacher_inputs(state, locked, chunks=chunks)
        prior_qs = [m.get("content", "") for m in messages
                    if (m or {}).get("role") == "tutor"][-2:]
        turn_result = run_turn(
            teacher=teacher_v2,
            dean=dean_v2,
            turn_plan=clinical_plan,
            teacher_inputs=teacher_inputs,
            dean_state=state,
            dean_chunks=chunks,
            locked_answer=str(state.get("locked_answer", "") or ""),
            locked_answer_aliases=list(state.get("locked_answer_aliases", []) or []),
            prior_tutor_questions=prior_qs,
        )
    except Exception as e:
        state.pop("_clinical_continuation", None)
        state["debug"]["turn_trace"].append({
            "wrapper": "assessment_v2.run_turn_error",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })
        # Hard fallback — close gracefully.
        return _render_clinical_close(state, teacher_v2, messages=messages)

    # L70 — counters from preflight already ticked inside dean_node_v2 (we
    # don't run preflight here per L72's clarification). We explicitly DO
    # NOT honor should_force_hint_advance / should_end_session in clinical
    # context — natural cap is the only termination.
    text = (getattr(turn_result, "final_text", "") or "").strip()
    if not text:
        text = (
            "Let's keep working — what's your reasoning on the clinical "
            "scenario so far?"
        )

    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "assessment",
        "metadata": {
            "mode": "clinical",
            "source": "assessment_v2",
            "clinical_turn": clinical_turn_count,
        },
    })
    clinical_history.append({
        "turn": clinical_turn_count,
        "role": "tutor",
        "content": text,
    })

    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.clinical_turn_rendered",
        "clinical_turn": clinical_turn_count,
        "preview": text[:120],
    })

    return {
        "messages": messages,
        "assessment_turn": 2,
        "phase": "assessment",
        "clinical_opt_in": True,
        "clinical_completed": False,
        "clinical_turn_count": clinical_turn_count,
        "clinical_max_turns": CLINICAL_TURN_CAP,
        "clinical_history": clinical_history,
        "pending_user_choice": {},
        "debug": state["debug"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# L65 — opt-in No path: brief reach-confirmation + answer reveal
# ─────────────────────────────────────────────────────────────────────────────


def _render_reach_close(
    state: dict,
    teacher_v2: TeacherV2,
    *,
    messages: Optional[list[dict]] = None,
) -> dict:
    """L65 — student declined clinical bonus. Confirm what they reached
    and what the textbook answer is, then close.
    """
    if messages is None:
        messages = list(state.get("messages", []) or [])
    locked = state.get("locked_topic") or {}
    locked_q = (state.get("locked_question") or "").strip()
    locked_a = (state.get("locked_answer") or state.get("full_answer") or "").strip()

    # Use honest_close mode for the prose — but feed it the reach context
    # via hint_text so Teacher can confirm the answer naturally.
    confirmation_hint = (
        f"Student reached the core answer for: '{locked_q}'. "
        f"Confirm what they got right and the textbook answer "
        f"({locked_a or 'see textbook'}) before closing. Brief and warm."
    )
    plan = TurnPlan(
        scenario="opt_in_no_reach_confirm_close",
        hint_text=confirmation_hint,
        mode="honest_close",
        tone="encouraging",
        forbidden_terms=[],
        permitted_terms=[],
        shape_spec={"max_sentences": 3, "exactly_one_question": False},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )
    inputs = _teacher_inputs(state, locked)
    fallback = _build_reach_close_fallback(locked, locked_q, locked_a)
    text = _safe_teacher_draft(
        teacher_v2, plan, inputs, fallback_text=fallback,
        trace=state["debug"]["turn_trace"],
    )

    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "memory_update",
        "metadata": {
            "mode": "honest_close",
            "tone": "encouraging",
            "source": "assessment_v2",
            "is_closing": True,
        },
    })
    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.reach_close_rendered",
        "preview": text[:120],
    })

    return {
        "messages": messages,
        "assessment_turn": 3,
        "clinical_opt_in": False,
        "clinical_mastery_tier": "not_assessed",
        "phase": "memory_update",
        "pending_user_choice": {},
        "debug": state["debug"],
    }


def _build_reach_close_fallback(
    locked: dict, locked_q: str, locked_a: str,
) -> str:
    sub = (locked.get("subsection") or "").strip()
    bits = []
    if locked_q:
        bits.append(f"You reached the answer to '{locked_q}'.")
    if locked_a:
        bits.append(f"The textbook answer: {locked_a}")
    if sub:
        bits.append(f"Great work on **{sub}** — see you next session.")
    else:
        bits.append("Great work — see you next session.")
    return " ".join(bits)


# ─────────────────────────────────────────────────────────────────────────────
# Reveal-and-close path (student did not reach answer)
# ─────────────────────────────────────────────────────────────────────────────


def _render_reveal_close(state: dict, teacher_v2: TeacherV2) -> dict:
    """Student didn't reach the answer (hints exhausted or turn cap).
    Reveal the answer + close. clinical_mastery_tier = not_assessed.
    """
    messages = list(state.get("messages", []) or [])
    locked = state.get("locked_topic") or {}
    locked_q = (state.get("locked_question") or "").strip()
    locked_a = (state.get("locked_answer") or state.get("full_answer") or "").strip()

    reveal_hint = (
        f"The locked question was: '{locked_q}'. "
        f"Reveal the textbook answer ({locked_a or 'see textbook'}) "
        f"plainly, acknowledge it was a tough one, and close honestly."
    )
    plan = TurnPlan(
        scenario="reveal_and_close_no_reach",
        hint_text=reveal_hint,
        mode="honest_close",
        tone="honest",
        forbidden_terms=[],
        permitted_terms=[],
        shape_spec={"max_sentences": 3, "exactly_one_question": False},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )
    inputs = _teacher_inputs(state, locked)
    fallback = _build_reveal_close_fallback(locked, locked_q, locked_a)
    text = _safe_teacher_draft(
        teacher_v2, plan, inputs, fallback_text=fallback,
        trace=state["debug"]["turn_trace"],
    )

    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "memory_update",
        "metadata": {
            "mode": "honest_close",
            "tone": "honest",
            "source": "assessment_v2",
            "is_closing": True,
        },
    })
    state["debug"]["turn_trace"].append({
        "wrapper": "assessment_v2.reveal_close_rendered",
        "preview": text[:120],
    })

    return {
        "messages": messages,
        "assessment_turn": 3,
        "clinical_opt_in": False,
        "clinical_mastery_tier": "not_assessed",
        "phase": "memory_update",
        "pending_user_choice": {},
        "debug": state["debug"],
    }


def _build_reveal_close_fallback(
    locked: dict, locked_q: str, locked_a: str,
) -> str:
    sub = (locked.get("subsection") or "").strip()
    bits = []
    if locked_q and locked_a:
        bits.append(f"The answer to '{locked_q}': {locked_a}.")
    elif locked_a:
        bits.append(f"The textbook answer: {locked_a}.")
    if sub:
        bits.append(f"Tough one — revisit **{sub}** from My Mastery when you're ready.")
    else:
        bits.append("Tough one — revisit this topic from My Mastery when you're ready.")
    return " ".join(bits)


# ─────────────────────────────────────────────────────────────────────────────
# Clinical close — natural cap (L67) reached
# ─────────────────────────────────────────────────────────────────────────────


def _render_clinical_close(
    state: dict,
    teacher_v2: TeacherV2,
    *,
    messages: list[dict],
) -> dict:
    """Clinical phase ended via L67 turn cap. Brief acknowledgment +
    route to memory_update so mastery scorer derives clinical_mastery_tier
    from clinical_history.
    """
    locked = state.get("locked_topic") or {}
    plan = TurnPlan(
        scenario="clinical_phase_natural_close",
        hint_text=(
            "Clinical phase wrap: acknowledge the work done, no answer reveal, "
            "brief and warm. Route is memory_update next."
        ),
        mode="honest_close",
        tone="neutral",
        forbidden_terms=[],
        permitted_terms=[],
        shape_spec={"max_sentences": 2, "exactly_one_question": False},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )
    inputs = _teacher_inputs(state, locked)
    fallback = (
        "Nice work on the clinical reasoning — let's wrap here. Your mastery "
        "and any open threads will appear in My Mastery."
    )
    text = _safe_teacher_draft(
        teacher_v2, plan, inputs, fallback_text=fallback,
        trace=state["debug"]["turn_trace"],
    )
    messages.append({
        "role": "tutor",
        "content": text,
        "phase": "memory_update",
        "metadata": {
            "mode": "honest_close",
            "tone": "neutral",
            "source": "assessment_v2",
            "is_closing": True,
        },
    })
    return {
        "messages": messages,
        "assessment_turn": 3,
        "phase": "memory_update",
        "clinical_completed": False,
        "pending_user_choice": {},
        "debug": state["debug"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _teacher_inputs(
    state: dict,
    locked: dict,
    *,
    chunks: Optional[list[dict]] = None,
) -> TeacherPromptInputs:
    """Build TeacherPromptInputs from state. Defaults sourced from
    state where possible; safe fallbacks otherwise."""
    # L78 — generic fallbacks so a missing cfg.domain.* slot still yields
    # a parseable prompt; production callers always override via cfg.
    domain_name = "this subject"
    domain_short = "subject"
    try:
        from config import cfg as _cfg
        domain_name = getattr(_cfg.domain, "name", domain_name)
        domain_short = getattr(_cfg.domain, "short", domain_short)
    except Exception:
        pass

    return TeacherPromptInputs(
        chunks=chunks if chunks is not None else list(state.get("retrieved_chunks", []) or []),
        history=list(state.get("messages", []) or []),
        locked_subsection=str(locked.get("subsection", "") or ""),
        locked_question=str(state.get("locked_question", "") or ""),
        domain_name=domain_name,
        domain_short=domain_short,
        student_descriptor="student",
        time_of_day=_time_of_day(state),
    )


def _time_of_day(state: dict) -> str:
    hour = state.get("client_hour")
    try:
        h = int(hour) if hour is not None else None
    except (TypeError, ValueError):
        h = None
    if h is None:
        return "afternoon"
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"


def _safe_teacher_draft(
    teacher_v2: TeacherV2,
    plan: TurnPlan,
    inputs: TeacherPromptInputs,
    *,
    fallback_text: str,
    trace: list[dict],
) -> str:
    """Call teacher_v2.draft() with a deterministic fallback on failure."""
    try:
        result = teacher_v2.draft(plan, inputs)
        text = (getattr(result, "text", "") or "").strip()
        if not text:
            trace.append({
                "wrapper": "assessment_v2.teacher_draft_empty",
                "mode": plan.mode,
            })
            return fallback_text
        return text
    except Exception as e:
        trace.append({
            "wrapper": "assessment_v2.teacher_draft_error",
            "mode": plan.mode,
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })
        return fallback_text


def _latest_student(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if (m or {}).get("role") == "student":
            return str((m or {}).get("content", "") or "")
    return ""


def _dean_domain_kwargs() -> dict:
    """Per L78 — pull domain-aware kwargs (name, short, clinical_scenario_style)
    from the active cfg so Dean's prompt is rendered in domain-appropriate
    framing. Returns generic fallbacks if cfg is missing so unit tests
    that bypass cfg keep working."""
    try:
        from config import cfg as _cfg
        return {
            "domain_name": getattr(_cfg.domain, "name", "this subject"),
            "domain_short": getattr(_cfg.domain, "short", "subject"),
            "clinical_scenario_style": getattr(
                _cfg.domain, "clinical_scenario_style", "",
            ),
        }
    except Exception:
        return {"domain_name": "this subject", "domain_short": "subject"}


def _last_scenario_from_history(state: dict) -> str:
    """Walk clinical_history backwards to find the most recent
    `scenario` field set by _enter_clinical_phase. Empty if none."""
    history = state.get("clinical_history") or []
    for entry in reversed(history):
        if isinstance(entry, dict) and entry.get("scenario"):
            return str(entry["scenario"])
    return ""


_YES_TOKENS = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay",
    "absolutely", "definitely", "totally",
    "let's", "lets",   # "let's do it" / "lets go" — first token
    "i'd", "i'll",     # "I'd love to" / "I'll try"
    "go", "go ahead",
}
_NO_TOKENS = {
    "no", "n", "nope", "nah", "not", "skip", "pass", "stop",
    "wrap", "done", "end", "later", "exit",
}
_NEGATION_TOKENS = {"not", "no", "n't", "nope", "nah"}


def _classify_opt_in(student_msg: str) -> str:
    """Return 'yes', 'no', or 'ambiguous'.

    Per L73 — the UI's primary path is Yes/No buttons. This classifier
    handles the free-text fallback. Strategy:

      1. Exact-match canonical strings first (highest precision)
      2. Otherwise look for a YES/NO token anywhere in the message,
         penalised by negation. Long substantive affirmations like
         "Yes, I'd love to try the bonus question!" must classify as
         "yes" — the legacy ≥6-word implicit-yes heuristic was too
         coarse, but pure exact-match was too strict and produced
         infinite re-ask loops on simulator transcripts (sanity-check
         observation 2026-05-03).

    Returns "ambiguous" only when there's no clear yes/no signal OR
    when both signals are present (genuinely unclear).
    """
    txt = re.sub(r"\s+", " ", (student_msg or "").strip().lower())
    if not txt:
        return "ambiguous"

    # Exact canonical phrases — highest confidence, short-circuit.
    if txt in {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "let's do it"}:
        return "yes"
    if txt in {"no", "n", "nope", "not really", "skip", "pass", "stop"}:
        return "no"

    # Token search — split on word-ish boundaries.
    tokens = re.findall(r"[a-z']+", txt)
    token_set = set(tokens)

    has_yes = bool(token_set & _YES_TOKENS)
    has_no = bool(token_set & _NO_TOKENS)
    has_negation = bool(token_set & _NEGATION_TOKENS)

    # "no thanks" / "not really" — no wins regardless of any yes-token.
    if has_no:
        return "no"
    # Plain yes signal with no negation
    if has_yes and not has_negation:
        return "yes"
    return "ambiguous"
