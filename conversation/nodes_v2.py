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

    # If topic isn't locked yet, Track 4.7d owns the v2 pre-lock path:
    # L9 topic_mapper_llm, L10 confirm-and-lock, L11 prelock counter,
    # and L22 guided-pick at cap 7.
    locked = state.get("locked_topic") or {}
    if not locked or not locked.get("path"):
        return run_topic_lock_v2(
            state,
            dean=dean,
            retriever=retriever,
            latest_student=latest_student,
        )

    # ── 1. Pre-flight Haiku layer ────────────────────────────────────────
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
            domain_name=getattr(_cfg.domain, "name", "human anatomy"),
            domain_short=getattr(_cfg.domain, "short", "anatomy"),
            student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
        )
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

        return {
            "messages": msgs,
            "help_abuse_count": new_help_count,
            "off_topic_count": new_off_count,
            "hint_level": new_hint_level,
            "phase": new_phase,
            "debug": state["debug"],
        }

    # ── 2b. No pre-flight fire → Dean plans + Teacher drafts via retry ──
    from conversation.llm_client import make_anthropic_client, resolve_model
    from config import cfg as _cfg

    # Fetch chunks for this turn — reuse existing retriever
    try:
        chunks = retriever.retrieve(latest_student) if retriever else []
    except Exception as e:
        chunks = []
        debug_trace.append({"wrapper": "retriever.retrieve_error", "error": str(e)[:160]})

    client = make_anthropic_client()
    dean_v2 = DeanV2(client, model=resolve_model(_cfg.models.dean))
    teacher_v2 = TeacherV2(client, model=resolve_model(_cfg.models.teacher))

    # Plan the turn
    plan_result = dean_v2.plan(
        state, chunks,
        carryover_notes="",  # L6 wire (Track 4.7e) populates this
        domain_name=getattr(_cfg.domain, "name", "human anatomy"),
        domain_short=getattr(_cfg.domain, "short", "anatomy"),
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
        domain_name=getattr(_cfg.domain, "name", "human anatomy"),
        domain_short=getattr(_cfg.domain, "short", "anatomy"),
        student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
    )
    aliases = state.get("locked_answer_aliases") or []
    prior_qs = []
    for m in reversed(state.get("messages", []) or []):
        if (m or {}).get("role") == "tutor":
            prior_qs.append(str(m.get("content", "") or ""))
            if len(prior_qs) >= 2:
                break

    turn_result = run_turn(
        teacher=teacher_v2,
        dean=dean_v2,
        turn_plan=plan_result.turn_plan,
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

    # Append the final text to messages
    msgs = list(state.get("messages", []))
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

    return {
        "messages": msgs,
        "help_abuse_count": new_help_count,  # reset to 0 on engagement
        "off_topic_count": new_off_count,
        "turn_count": int(state.get("turn_count", 0) or 0) + 1,
        "debug": state["debug"],
    }
