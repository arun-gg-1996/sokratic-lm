"""
conversation/nodes.py
----------------------
One function per LangGraph node. Each function:
  - Takes the full TutorState
  - Does one focused thing
  - Returns a partial state update dict (LangGraph merges it)

Nodes:
  rapport_node        — greet student, load memory, pick topic
  dean_node           — main Dean-Teacher loop for one student turn
  assessment_node     — clinical question or answer reveal
  memory_update_node  — flush session to mem0, clear state

The graph in graph.py wires these together with edges from edges.py.
"""

import json
import re
from pathlib import Path
from conversation.state import TutorState
from conversation.summarizer import maybe_summarize
from config import cfg


def rapport_node(state: TutorState, teacher, memory_manager) -> dict:
    """
    Phase 1 — Rapport.

    - Load student memory to seed weak_topics.
    - Teacher generates personalized greeting (teacher_rapport prompt).
    - Transition phase to "tutoring".

    Note: teacher not dean — rapport never uses Dean.
    """
    # Only run once — skip if already past rapport phase
    if state.get("phase") != "rapport":
        return {}

    student_id = state["student_id"]
    weak_topics = memory_manager.load(student_id)

    greeting = teacher.draft_rapport(weak_topics, state=state)

    messages = list(state.get("messages", []))
    messages.append({"role": "tutor", "content": greeting, "phase": "rapport"})

    return {
        "weak_topics": weak_topics,
        "messages": messages,
        "phase": "tutoring",
    }


def dean_node(state: TutorState, dean, teacher) -> dict:
    """
    Phase 2 — Socratic Tutoring (one full Dean-Teacher loop turn).

    - Resets turn_trace at start of each new student turn.
    - Dean runs run_turn() which orchestrates setup, Teacher draft, quality check.
    - Increments turn_count.
    - Fires summarizer if turn_count reaches the threshold.
    - Returns merged state update.
    """
    # Don't run if no student message yet (rapport just fired on same invoke)
    has_student_msg = any(m.get("role") == "student" for m in state.get("messages", []))
    if not has_student_msg:
        return {}

    # Reset turn_trace at start of each new student turn
    state["debug"]["turn_trace"] = []
    state["debug"]["current_node"] = "dean_node"

    # Run the Dean-Teacher loop for this turn
    partial_update = dean.run_turn(state, teacher)

    # Increment turn count
    turn_count = state.get("turn_count", 0) + 1
    partial_update["turn_count"] = turn_count

    # Fire summarizer when approaching the turn limit
    summarizer_trigger = state["max_turns"] - cfg.session.summarizer_keep_recent
    if turn_count >= summarizer_trigger:
        messages = partial_update.get("messages", state.get("messages", []))
        partial_update["messages"] = maybe_summarize(messages)

    if partial_update.get("student_reached_answer"):
        routing = "assessment_node (student_reached_answer)"
    elif partial_update.get("hint_level", state.get("hint_level", 1)) > state.get("max_hints", 3):
        routing = "assessment_node (hints_exhausted)"
    elif turn_count >= state.get("max_turns", 25):
        routing = "assessment_node (turn_limit)"
    else:
        routing = "END"
    state["debug"]["last_routing"] = routing
    partial_update["debug"] = state["debug"]

    return partial_update


def assessment_node(state: TutorState, dean, teacher) -> dict:
    """
    Phase 3 — Assessment.

    Assessment flow controlled by assessment_turn:

    If student_reached_answer == True:
      assessment_turn == 0: Ask whether student wants optional clinical question.
                             Set assessment_turn = 1. Return END (wait for yes/no).
      assessment_turn == 1: If yes -> ask clinical question (Dean quality-gated), set assessment_turn = 2.
                             If no  -> skip clinical and write mastery summary, set assessment_turn = 3.
      assessment_turn == 2: Clinical multi-turn loop (max 2-3 turns):
                             - Dean evaluates student's clinical response.
                             - If correct with confidence threshold: write mastery summary, set 3.
                             - Else: give targeted feedback + follow-up question and stay at 2.
                             - If max clinical turns reached: close with coaching summary, set 3.

    If student_reached_answer == False (hints exhausted or turn limit):
      Dean reveals the answer directly.
      Topic added to weak_topics with failure_count++.
      Set assessment_turn = 3.
    """
    messages = list(state.get("messages", []))
    state["debug"]["current_node"] = "assessment_node"
    reached = state.get("student_reached_answer", False)
    assessment_turn = state.get("assessment_turn", 0)

    if reached and assessment_turn == 0:
        # Step 1: ask whether student wants the optional clinical application question.
        opt_in_q = teacher.draft_clinical_opt_in(state)
        messages.append({"role": "tutor", "content": opt_in_q, "phase": "assessment"})
        return {
            "messages": messages,
            "assessment_turn": 1,
            "clinical_opt_in": None,
            "phase": "assessment",
            "debug": state["debug"],
        }

    elif reached and assessment_turn == 1:
        # Step 2: parse yes/no on optional clinical question.
        student_msg = _latest_student_message(messages)
        if _is_affirmative(student_msg):
            state["dean_critique"] = ""
            draft = teacher.draft_clinical(state, dean_critique="")
            quality = dean._quality_check_call(state, draft, phase="assessment")
            if quality.get("pass", False):
                clinical_q = draft
            else:
                revised = (quality.get("revised_teacher_draft") or "").strip()
                if revised:
                    clinical_q = revised
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.revised_teacher_draft_applied",
                        "result": "Applied Dean revised_teacher_draft for clinical question",
                    })
                else:
                    clinical_q = dean._assessment_clinical_fallback(state)
                    state["debug"]["interventions"] += 1
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.assessment_fallback",
                        "result": "Clinical question fallback used (no valid revised_teacher_draft)",
                    })
            max_clinical_turns = int(getattr(cfg.session, "clinical_max_turns", 3))
            messages.append({"role": "tutor", "content": clinical_q, "phase": "assessment"})
            return {
                "messages": messages,
                "assessment_turn": 2,
                "clinical_opt_in": True,
                "clinical_turn_count": 0,
                "clinical_max_turns": max_clinical_turns,
                "clinical_completed": False,
                "clinical_state": None,
                "clinical_confidence": 0.0,
                "clinical_history": [],
                "phase": "assessment",
                "dean_critique": "",
                "dean_retry_count": 0,
                "debug": state["debug"],
            }

        if _is_negative(student_msg):
            # Student opted out: skip clinical and go straight to mastery summary.
            state["clinical_opt_in"] = False
            tiers = _compute_mastery_tiers(state)
            state.update(tiers)
            weak_topics = list(state.get("weak_topics", []))
            if state.get("mastery_tier") in {"developing", "needs_review"}:
                weak_topics = _upsert_weak_topic(
                    weak_topics,
                    _session_topic_name(state),
                    difficulty="moderate" if state.get("mastery_tier") == "developing" else "hard",
                    bump=1,
                )
                state["weak_topics"] = weak_topics
            mastery_msg = dean._assessment_call(state)
            messages.append({"role": "tutor", "content": mastery_msg, "phase": "memory_update"})
            _log_conversation(state, messages)
            return {
                "messages": messages,
                "assessment_turn": 3,
                "clinical_opt_in": False,
                "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
                "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
                "mastery_tier": state.get("mastery_tier", "not_assessed"),
                "weak_topics": state.get("weak_topics", []),
                "phase": "assessment",
                "debug": state["debug"],
            }

        # Ambiguous response — ask again.
        clarify_q = teacher.draft_clinical_opt_in(state)
        messages.append({"role": "tutor", "content": clarify_q, "phase": "assessment"})
        return {
            "messages": messages,
            "assessment_turn": 1,
            "clinical_opt_in": None,
            "phase": "assessment",
            "debug": state["debug"],
        }

    elif reached and assessment_turn == 2:
        # Step 3: multi-turn clinical reasoning loop (max N turns).
        state["clinical_opt_in"] = True
        eval_result = dean._clinical_turn_call(state)

        clinical_turn_count = int(state.get("clinical_turn_count", 0)) + 1
        clinical_max_turns = int(state.get("clinical_max_turns", getattr(cfg.session, "clinical_max_turns", 3)))
        clinical_history = list(state.get("clinical_history", []))
        clinical_history.append({
            "turn": clinical_turn_count,
            "state": eval_result.get("student_state", "incorrect"),
            "confidence": float(eval_result.get("confidence_score", 0.0)),
            "pass": bool(eval_result.get("pass", False)),
        })

        state["clinical_turn_count"] = clinical_turn_count
        state["clinical_history"] = clinical_history
        state["clinical_state"] = eval_result.get("student_state", "incorrect")
        state["clinical_confidence"] = float(eval_result.get("confidence_score", 0.0))

        clinical_threshold = float(getattr(getattr(cfg, "thresholds", object()), "clinical_reached_confidence", 0.72))
        clinical_pass = bool(eval_result.get("pass", False)) and state["clinical_confidence"] >= clinical_threshold

        if clinical_pass:
            state["clinical_completed"] = True
            tiers = _compute_mastery_tiers(state)
            state.update(tiers)
            weak_topics = list(state.get("weak_topics", []))
            if state.get("mastery_tier") in {"developing", "needs_review"}:
                weak_topics = _upsert_weak_topic(
                    weak_topics,
                    _session_topic_name(state),
                    difficulty="moderate" if state.get("mastery_tier") == "developing" else "hard",
                    bump=1,
                )
                state["weak_topics"] = weak_topics
            mastery_msg = dean._assessment_call(state)
            messages.append({"role": "tutor", "content": mastery_msg, "phase": "memory_update"})
            _log_conversation(state, messages)
            return {
                "messages": messages,
                "assessment_turn": 3,
                "phase": "assessment",
                "clinical_opt_in": True,
                "clinical_completed": True,
                "clinical_turn_count": clinical_turn_count,
                "clinical_max_turns": clinical_max_turns,
                "clinical_state": state.get("clinical_state"),
                "clinical_confidence": state.get("clinical_confidence", 0.0),
                "clinical_history": clinical_history,
                "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
                "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
                "mastery_tier": state.get("mastery_tier", "not_assessed"),
                "weak_topics": state.get("weak_topics", []),
                "debug": state["debug"],
            }

        if clinical_turn_count < clinical_max_turns:
            follow_up = str(eval_result.get("feedback_message", "") or "").strip()
            if not follow_up:
                follow_up = dean._assessment_clinical_followup_fallback(state)
            messages.append({"role": "tutor", "content": follow_up, "phase": "assessment"})
            return {
                "messages": messages,
                "assessment_turn": 2,
                "phase": "assessment",
                "clinical_opt_in": True,
                "clinical_completed": False,
                "clinical_turn_count": clinical_turn_count,
                "clinical_max_turns": clinical_max_turns,
                "clinical_state": state.get("clinical_state"),
                "clinical_confidence": state.get("clinical_confidence", 0.0),
                "clinical_history": clinical_history,
                "debug": state["debug"],
            }

        # Max clinical turns reached: close with coaching summary.
        state["clinical_completed"] = False
        tiers = _compute_mastery_tiers(state)
        state.update(tiers)
        weak_topics = list(state.get("weak_topics", []))
        if state.get("mastery_tier") in {"developing", "needs_review"}:
            weak_topics = _upsert_weak_topic(
                weak_topics,
                _session_topic_name(state),
                difficulty="moderate" if state.get("mastery_tier") == "developing" else "hard",
                bump=1,
            )
            state["weak_topics"] = weak_topics
        mastery_msg = dean._assessment_call(state)
        messages.append({"role": "tutor", "content": mastery_msg, "phase": "memory_update"})
        _log_conversation(state, messages)
        return {
            "messages": messages,
            "assessment_turn": 3,
            "phase": "assessment",
            "clinical_opt_in": True,
            "clinical_completed": False,
            "clinical_turn_count": clinical_turn_count,
            "clinical_max_turns": clinical_max_turns,
            "clinical_state": state.get("clinical_state"),
            "clinical_confidence": state.get("clinical_confidence", 0.0),
            "clinical_history": clinical_history,
            "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
            "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
            "mastery_tier": state.get("mastery_tier", "not_assessed"),
            "weak_topics": state.get("weak_topics", []),
            "debug": state["debug"],
        }

    else:
        # Reveal path (did not reach answer)
        state["clinical_opt_in"] = False
        tiers = _compute_mastery_tiers(state)
        state.update(tiers)
        reveal_msg = dean._assessment_call(state)
        messages.append({"role": "tutor", "content": reveal_msg, "phase": "memory_update"})

        # Increment failure_count for this topic in weak_topics (did_not_reach path).
        weak_topics = list(state.get("weak_topics", []))
        topic_name = _session_topic_name(state)
        weak_topics = _upsert_weak_topic(weak_topics, topic_name, difficulty="hard", bump=1)

        _log_conversation(state, messages)

        return {
            "messages": messages,
            "assessment_turn": 3,
            "phase": "assessment",
            "clinical_opt_in": False,
            "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
            "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
            "mastery_tier": state.get("mastery_tier", "not_assessed"),
            "weak_topics": weak_topics,
            "debug": state["debug"],
        }


def memory_update_node(state: TutorState, dean, memory_manager) -> dict:
    """
    Phase 4 — Memory Update.

    - Dean generates session summary via _memory_summary_call.
    - memory_manager.flush() writes summary to mem0.
    - Returns phase = "memory_update" to signal session end.
    """
    state["debug"]["current_node"] = "memory_update_node"

    summary = dean._memory_summary_call(state)
    persisted = memory_manager.flush(state["student_id"], state, summary_text=summary)
    flush_status = getattr(
        memory_manager,
        "last_flush_status",
        "persisted_to_mem0_qdrant" if persisted else "memory_persist_skipped_or_failed",
    )
    state["debug"]["turn_trace"].append({
        "wrapper": "memory_manager.flush",
        "result": flush_status,
    })

    return {
        "phase": "memory_update",
        "debug": state["debug"],
    }


def _log_conversation(state: TutorState, messages: list[dict]) -> None:
    """Save full conversation to data/artifacts/conversations/{student_id}_turn_{n}.json."""
    try:
        out_dir = Path(cfg.paths.artifacts) / "conversations"
        out_dir.mkdir(parents=True, exist_ok=True)
        conv_data = {
            "student_id": state["student_id"],
            "locked_answer": state.get("locked_answer", ""),
            "student_reached_answer": state.get("student_reached_answer", False),
            "hint_level": state.get("hint_level", 1),
            "turn_count": state.get("turn_count", 0),
            "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
            "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
            "mastery_tier": state.get("mastery_tier", "not_assessed"),
            "clinical_turn_count": state.get("clinical_turn_count", 0),
            "clinical_completed": state.get("clinical_completed", False),
            "weak_topics": state.get("weak_topics", []),
            "messages": messages,
            "debug": {k: v for k, v in state.get("debug", {}).items() if k != "turn_trace"},
        }
        turn = state.get("turn_count", 0)
        out_path = out_dir / f"{state['student_id']}_turn_{turn}.json"
        out_path.write_text(json.dumps(conv_data, indent=2))
    except Exception:
        pass  # non-fatal


def _latest_student_message(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "student":
            return str(msg.get("content", "")).strip()
    return ""


def _is_affirmative(text: str) -> bool:
    txt = text.lower().strip()
    patterns = (
        r"\byes\b", r"\byeah\b", r"\byep\b", r"\bsure\b", r"\bok\b", r"\bokay\b",
        r"\bplease do\b", r"\bgo ahead\b", r"\blet'?s do\b", r"\bwhy not\b",
    )
    return any(re.search(p, txt) for p in patterns)


def _is_negative(text: str) -> bool:
    txt = text.lower().strip()
    patterns = (
        r"\bno\b", r"\bnope\b", r"\bnot now\b", r"\bskip\b", r"\bpass\b", r"\blater\b",
        r"\bdon'?t\b", r"\bno thanks\b", r"\bnot really\b",
    )
    return any(re.search(p, txt) for p in patterns)


def _session_topic_name(state: TutorState) -> str:
    selected = str(state.get("topic_selection", "") or "").strip()
    if selected:
        return selected[:120]
    for msg in state.get("messages", []):
        if msg.get("role") == "student":
            return str(msg.get("content", "") or "").strip()[:120]
    return ""


def _upsert_weak_topic(weak_topics: list[dict], topic_name: str, difficulty: str = "moderate", bump: int = 1) -> list[dict]:
    if not topic_name:
        return weak_topics
    out = list(weak_topics or [])
    key = topic_name.strip().lower()
    for wt in out:
        wt_topic = str(wt.get("topic", "")).strip().lower()
        if wt_topic == key:
            wt["failure_count"] = int(wt.get("failure_count", 0)) + int(max(bump, 1))
            if difficulty and wt.get("difficulty") != "hard":
                wt["difficulty"] = difficulty
            return out
    out.append({
        "topic": topic_name,
        "difficulty": difficulty or "moderate",
        "failure_count": int(max(bump, 1)),
    })
    return out


def _tier_from_score(score: float) -> str:
    if score >= 0.85:
        return "strong"
    if score >= 0.70:
        return "proficient"
    if score >= 0.55:
        return "developing"
    return "needs_review"


def _compute_mastery_tiers(state: TutorState) -> dict:
    """
    Tiered grading based on BOTH:
    - core tutoring performance
    - clinical application performance (when attempted)
    """
    reached = bool(state.get("student_reached_answer", False))
    hint_level = int(state.get("hint_level", 1))
    max_hints = int(state.get("max_hints", 3))
    mastery_conf = float(state.get("student_mastery_confidence", 0.0))

    if not reached:
        return {
            "core_mastery_tier": "needs_review",
            "clinical_mastery_tier": "not_assessed",
            "mastery_tier": "needs_review",
        }

    hint_score_map = {
        1: 1.00,
        2: 0.80,
        3: 0.62,
    }
    if hint_level > max_hints:
        core_hint_score = 0.30
    else:
        core_hint_score = hint_score_map.get(hint_level, 0.55)

    core_score = (0.65 * core_hint_score) + (0.35 * mastery_conf)
    core_tier = _tier_from_score(core_score)

    clinical_opt_in = state.get("clinical_opt_in")
    clinical_history = list(state.get("clinical_history", []))
    clinical_completed = bool(state.get("clinical_completed", False))

    if clinical_opt_in is not True:
        return {
            "core_mastery_tier": core_tier,
            "clinical_mastery_tier": "not_assessed",
            "mastery_tier": core_tier,
        }

    if not clinical_history:
        clinical_tier = "developing"
        clinical_score = 0.52
    else:
        avg_conf = sum(float(x.get("confidence", 0.0)) for x in clinical_history) / max(len(clinical_history), 1)
        final_state = str(clinical_history[-1].get("state", "incorrect"))
        final_pass = bool(clinical_history[-1].get("pass", False))
        turns = int(state.get("clinical_turn_count", len(clinical_history)))

        if final_state == "correct" and final_pass and clinical_completed:
            clinical_score = min(1.0, avg_conf + (0.08 if turns <= 2 else 0.0))
        elif final_state == "partial_correct":
            clinical_score = min(0.69, avg_conf)
        else:
            clinical_score = min(0.49, avg_conf)
        clinical_tier = _tier_from_score(clinical_score)

    tier_to_num = {
        "needs_review": 1.0,
        "developing": 2.0,
        "proficient": 3.0,
        "strong": 4.0,
    }
    combined = (0.60 * tier_to_num[core_tier]) + (0.40 * tier_to_num[clinical_tier])
    if combined >= 3.5:
        mastery_tier = "strong"
    elif combined >= 2.7:
        mastery_tier = "proficient"
    elif combined >= 1.9:
        mastery_tier = "developing"
    else:
        mastery_tier = "needs_review"

    return {
        "core_mastery_tier": core_tier,
        "clinical_mastery_tier": clinical_tier,
        "mastery_tier": mastery_tier,
    }
