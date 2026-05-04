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
from pathlib import Path
from conversation.state import TutorState
from conversation.summarizer import maybe_summarize
from retrieval.topic_suggester import TopicSuggester
from config import cfg

_topic_suggester = TopicSuggester()


def rapport_node(state: TutorState, teacher, memory_manager) -> dict:
    """
    Phase 1 — Rapport.

    - Loads cross-session memory via memory_manager.load(student_id) (mem0/Qdrant).
      Returning students get a brief reference to one prior topic; new students
      get a fresh greeting.
    - weak_topics is still empty here (legacy slot reserved for future
      knowledge-tracing-derived weak concepts).
    - TopicSuggester provides initial topic suggestions from textbook_structure.json.
    - Teacher generates personalized greeting.
    - Transition phase to "tutoring".

    Note: teacher not dean — rapport never uses Dean.
    """
    # Only run once — skip if already past rapport phase
    if state.get("phase") != "rapport":
        return {}

    # Legacy slot — kept for knowledge-tracing wiring (D.3).
    weak_topics: list[dict] = []

    # Pull cross-session context from SQL (per L1 — session_summary +
    # open_thread now live in the sessions table, not mem0).
    #
    # Two derived signals fold into past_memories for the rapport prompt:
    #   1. "recent_summary" — most recent COMPLETED session's locked topic
    #      (sessions.ended_at IS NOT NULL AND status='completed')
    #   2. "open_thread"    — any session left unresolved
    #      (ended_at IS NULL OR status IN
    #         ('abandoned_no_lock','ended_off_domain','ended_turn_limit'))
    # Both are formatted as memory-shaped dicts so draft_rapport treats
    # them identically to a mem0 entry. Misconception + learning_style
    # narratives stay in mem0 (per L1) — those are tutor-internal,
    # NOT material for an opener.
    student_id = state.get("student_id", "") or ""
    memory_enabled = bool(state.get("memory_enabled", True))
    past_memories: list[dict] = []
    weak_subsections: list[dict] = []
    if student_id and memory_enabled:
        try:
            from memory.sqlite_store import SQLiteStore
            store = SQLiteStore()
            # Most recent completed session — surface as "session summary"
            recent = store.list_sessions(student_id, limit=1, completed_only=True)
            for s in recent:
                topic = (
                    s.get("locked_subsection_path")
                    or s.get("locked_topic_path")
                    or ""
                )
                if not topic:
                    continue
                tier = s.get("mastery_tier") or "not_assessed"
                past_memories.append({
                    "memory": (
                        f"[Recent session] Last covered: {topic}. "
                        f"Mastery tier: {tier}. "
                        f"Reach: {'yes' if s.get('reach_status') else 'partial/no'}."
                    )
                })
            # Open threads — sessions with no ended_at OR unresolved-status
            open_sessions = store.list_sessions(
                student_id,
                limit=3,
                status=("abandoned_no_lock", "ended_off_domain", "ended_turn_limit"),
            )
            in_progress = store.list_sessions(student_id, limit=3, status="in_progress")
            for s in (in_progress + open_sessions)[:3]:
                topic = (
                    s.get("locked_subsection_path")
                    or s.get("locked_topic_path")
                    or ""
                )
                if not topic:
                    continue
                past_memories.append({
                    "memory": (
                        f"[Open thread] Student was working on: {topic}. "
                        f"Status: {s.get('status', 'in_progress')}. "
                        f"Resume this topic if helpful."
                    )
                })
        except Exception:
            # SQL unreachable — degrade silently. Cold-start opener still works.
            pass

        # D.3: pull the student's weakest subsections so the rapport
        # opener can reference one *quantitatively* (not just "we worked
        # on X last time" but "X is at 32% mastery, want to revisit?").
        # Synthesize as additional bullets in past_session_memories so
        # the existing prompt rules apply unchanged — the LLM can still
        # only reference ONE item, no recap, no list.
        try:
            from memory.mastery_store import MasteryStore
            ms = MasteryStore()
            weak_subsections = ms.weak_subsections(
                student_id, threshold=0.5, limit=2
            )
            for w in weak_subsections:
                sub = w.get("subsection_title") or "?"
                pct = int(round(float(w.get("mastery", 0.0)) * 100))
                ch = w.get("chapter_num") or "?"
                outcome = w.get("last_outcome") or ""
                # Format as a "memory-shaped" dict so draft_rapport's
                # existing field-resolution path treats it identically
                # to a mem0 entry. The "Mastery cue:" prefix gives the
                # LLM a hook to recognize it as a quantitative signal.
                past_memories.append({
                    "memory": (
                        f"Mastery cue: {sub} (Ch{ch}) is at {pct}% mastery "
                        f"(last outcome: {outcome or 'unknown'}). The "
                        f"student would benefit from revisiting this "
                        f"subsection."
                    )
                })
        except Exception:
            weak_subsections = []

    # Initial suggestions: weak topics first for returning students,
    # explore picks for fresh students. suggest_for_student gracefully
    # falls back to plain suggest() when there's no mastery data.
    if student_id and memory_enabled:
        try:
            from memory.mastery_store import MasteryStore
            initial_suggestions = _topic_suggester.suggest_for_student(
                MasteryStore(), student_id, n=6
            )
        except Exception:
            initial_suggestions = _topic_suggester.suggest(n=6)
    else:
        initial_suggestions = _topic_suggester.suggest(n=6)

    greeting = teacher.draft_rapport(
        weak_topics,
        state=state,
        past_session_memories=past_memories,
        client_hour=state.get("client_hour"),
    )

    messages = list(state.get("messages", []))
    messages.append({"role": "tutor", "content": greeting, "phase": "rapport"})

    return {
        "weak_topics": weak_topics,
        "initial_suggestions": initial_suggestions,
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

    # Tier 1 #1.4 fix (e2e bug G1): if the latest student message is
    # whitespace-only or empty after stripping, do NOT route to the LLM
    # graph — Anthropic returns a 400 ('messages: text content blocks
    # must contain non-whitespace text'). Reply with a friendly nudge
    # instead so the student can retry. This is the input-sanitization
    # gap the e2e harness surfaced; before this guard, the suite
    # crashed with BadRequestError.
    latest_student_for_guard = ""
    for _m in reversed(state.get("messages", []) or []):
        if (_m or {}).get("role") == "student":
            latest_student_for_guard = str(_m.get("content", "") or "")
            break
    if latest_student_for_guard and not latest_student_for_guard.strip():
        msgs = list(state.get("messages", []))
        msgs.append({
            "role": "tutor",
            "content": (
                "Looks like your last message came through empty — "
                "could you type your question or response again?"
            ),
        })
        try:
            state["debug"].setdefault("turn_trace", []).append({
                "wrapper": "dean_node.whitespace_input_guard",
                "result": "skipped_llm_path",
            })
        except Exception:
            pass
        return {"messages": msgs}

    # Archive the previous turn's trace (if any) into all_turn_traces before
    # wiping turn_trace. state.py declares all_turn_traces but nothing was
    # writing it — debug history was being lost. Cap to last 50 turns so
    # long sessions don't blow up memory.
    prior_trace = list(state["debug"].get("turn_trace", []) or [])
    if prior_trace:
        att = list(state["debug"].get("all_turn_traces", []) or [])
        att.append({
            "turn": int(state.get("turn_count", 0)),
            "phase": state.get("phase", ""),
            "trace": prior_trace,
        })
        if len(att) > 50:
            att = att[-50:]
        state["debug"]["all_turn_traces"] = att

    # Reset turn_trace at start of each new student turn
    state["debug"]["turn_trace"] = []
    state["debug"]["current_node"] = "dean_node"

    # Invariant: tutoring should not continue without both anchors once topic is confirmed.
    if (
        state.get("phase") == "tutoring"
        and state.get("topic_confirmed", False)
        and (not state.get("locked_question") or not state.get("locked_answer"))
    ):
        state["debug"]["turn_trace"].append({
            "wrapper": "dean_node.invariant_violation",
            "result": (
                f"tutoring_without_anchors "
                f"locked_question={bool(state.get('locked_question'))} "
                f"locked_answer={bool(state.get('locked_answer'))}"
            ),
        })

    # Run the Dean-Teacher loop for this turn
    partial_update = dean.run_turn(state, teacher)

    # Increment turn_count ONLY on turns where real tutoring happened.
    # Early returns (topic scoping reprompt, anchor-fail reprompt, unselected
    # options) should NOT burn a tutoring turn budget from the student.
    topic_confirmed_now = bool(
        partial_update.get("topic_confirmed", state.get("topic_confirmed", False))
    )
    had_anchors = bool(state.get("locked_question")) and bool(state.get("locked_answer"))
    anchors_after = bool(partial_update.get("locked_question", state.get("locked_question"))) \
        and bool(partial_update.get("locked_answer", state.get("locked_answer")))
    is_tutoring_turn = topic_confirmed_now and (had_anchors or anchors_after)

    turn_count = state.get("turn_count", 0)
    if is_tutoring_turn:
        turn_count += 1
    partial_update["turn_count"] = turn_count

    # P4 observability: per-tutoring-turn groundedness accounting + invariant
    # check. A tutoring turn is "grounded" iff it has a locked_topic AND non-
    # empty retrieved_chunks when the Teacher drafted. We track both counts
    # directly (rather than deriving groundedness from coverage_gap_events)
    # because coverage-gap events don't include turns where retrieval was
    # never attempted but the state was inconsistent.
    debug = state["debug"]
    if is_tutoring_turn:
        locked = partial_update.get("locked_topic", state.get("locked_topic"))
        chunks = partial_update.get("retrieved_chunks", state.get("retrieved_chunks", []))
        grounded_now = bool(locked) and bool(chunks)
        if grounded_now:
            debug["grounded_turns"] = int(debug.get("grounded_turns", 0)) + 1
        else:
            debug["ungrounded_turns"] = int(debug.get("ungrounded_turns", 0)) + 1
            # Invariant violation: tutoring turn without grounded state.
            # This should only happen after a coverage-gap unlock (handled),
            # so log it with enough detail to investigate otherwise.
            debug.setdefault("invariant_violations", []).append({
                "turn": turn_count,
                "kind": "ungrounded_tutoring_turn",
                "has_locked_topic": bool(locked),
                "chunk_count": len(chunks) if chunks else 0,
            })

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

    # Activity log instrumentation (D.6 UX). Each LLM-bound step in the
    # assessment phase fires a short user-facing label so the activity
    # feed populates during clinical-question generation, follow-up
    # evaluation, and session-end mastery wrap-up — same pattern the
    # tutoring path uses via dean.run_turn.
    from conversation.teacher import fire_activity

    if reached and assessment_turn == 0:
        # Step 1: ask whether student wants the optional clinical application question.
        fire_activity("Reading your message")
        fire_activity("Preparing assessment prompt")
        opt_in_q = teacher.draft_clinical_opt_in(state)
        messages.append({"role": "tutor", "content": opt_in_q, "phase": "assessment"})
        return {
            "messages": messages,
            "assessment_turn": 1,
            "clinical_opt_in": None,
            "pending_user_choice": {
                "kind": "opt_in",
                "options": ["yes", "no"],
            },
            "phase": "assessment",
            "debug": state["debug"],
        }

    elif reached and assessment_turn == 1:
        # Step 2: parse yes/no on optional clinical question.
        fire_activity("Reading your message")
        student_msg = _latest_student_message(messages).strip().lower()
        intent = _classify_opt_in(student_msg)
        if intent == "yes":
            fire_activity("Drafting clinical scenario")
            state["dean_critique"] = ""
            draft = teacher.draft_clinical(state, dean_critique="")
            fire_activity("Reviewing clinical question")
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
            # Clinical drafts skip the tutoring-phase deterministic check, so
            # enforce banned-prefix rule explicitly here before emitting.
            clinical_q = _strip_banned_prefixes(clinical_q, state)
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
                "pending_user_choice": {},
                "phase": "assessment",
                "dean_critique": "",
                "dean_retry_count": 0,
                "debug": state["debug"],
            }

        if intent == "no":
            # Student opted out: skip clinical and go straight to mastery summary.
            state["clinical_opt_in"] = False
            # Change 5.1b: clinical_mastery_tier=not_assessed when student
            # declines the optional clinical question. core_mastery_tier
            # is set later by _close_session_with_dean / mastery scorer
            # based on tutoring outcome.
            state["clinical_mastery_tier"] = "not_assessed"
            return _close_session_with_dean(state, dean, messages)

        # Ambiguous — treat a long substantive reply as an implicit "yes" and
        # proceed into clinical so we don't loop on the opt-in prompt.
        if len(student_msg.split()) >= 6:
            state["debug"].setdefault("turn_trace", []).append({
                "wrapper": "assessment.opt_in_implicit_yes",
                "result": "long reply treated as yes",
            })
            fire_activity("Drafting clinical scenario")
            draft = teacher.draft_clinical(state, dean_critique="")
            fire_activity("Reviewing clinical question")
            quality = dean._quality_check_call(state, draft, phase="assessment")
            if quality.get("pass", False):
                clinical_q = draft
            else:
                revised = (quality.get("revised_teacher_draft") or "").strip()
                clinical_q = revised or dean._assessment_clinical_fallback(state)
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
                "pending_user_choice": {},
                "phase": "assessment",
                "dean_critique": "",
                "dean_retry_count": 0,
                "debug": state["debug"],
            }

        # Genuinely ambiguous short reply — ask again once.
        clarify_q = teacher.draft_clinical_opt_in(state)
        messages.append({"role": "tutor", "content": clarify_q, "phase": "assessment"})
        return {
            "messages": messages,
            "assessment_turn": 1,
            "clinical_opt_in": None,
            "pending_user_choice": {
                "kind": "opt_in",
                "options": ["yes", "no"],
            },
            "phase": "assessment",
            "debug": state["debug"],
        }

    elif reached and assessment_turn == 2:
        # Step 3: multi-turn clinical reasoning loop (max N turns).
        fire_activity("Reading your message")
        fire_activity("Evaluating your clinical reasoning")
        state["clinical_opt_in"] = True
        eval_result = dean._clinical_turn_call(state)

        # ============================================================
        # Change 5.1 (2026-04-30): clinical-phase counters.
        #
        # Symmetric with tutoring's help_abuse / off_topic but with
        # threshold=2 (clinical only has 3 total turns). At strike 2,
        # END THE CLINICAL PHASE ONLY (clinical_mastery_tier=not_assessed)
        # but keep the session — the student earned tutoring credit.
        # ============================================================
        clinical_student_state = str(eval_result.get("student_state", "")).strip().lower()
        clinical_threshold_strikes = int(getattr(cfg.dean, "clinical_strike_threshold", 2))

        if clinical_student_state == "low_effort":
            state["clinical_low_effort_count"] = state.get("clinical_low_effort_count", 0) + 1
            state["total_low_effort_turns"] = state.get("total_low_effort_turns", 0) + 1
        else:
            state["clinical_low_effort_count"] = 0

        if clinical_student_state in {"irrelevant", "off_topic"}:
            state["clinical_off_topic_count"] = state.get("clinical_off_topic_count", 0) + 1
            state["total_off_topic_turns"] = state.get("total_off_topic_turns", 0) + 1
        else:
            state["clinical_off_topic_count"] = 0

        clinical_cap_triggered = (
            state["clinical_low_effort_count"] >= clinical_threshold_strikes
            or state["clinical_off_topic_count"] >= clinical_threshold_strikes
        )
        if clinical_cap_triggered:
            fire_activity("Clinical phase capped: low engagement detected")
            state["debug"].setdefault("turn_trace", []).append({
                "wrapper": "assessment.clinical_cap",
                "result": (
                    f"clinical_strike_threshold ({clinical_threshold_strikes}) reached; "
                    f"ending clinical phase only "
                    f"(low_effort={state['clinical_low_effort_count']}, "
                    f"off_topic={state['clinical_off_topic_count']}). "
                    f"Tutoring progress preserved."
                ),
            })
            # Keep core_mastery_tier as whatever tutoring earned (don't
            # touch it). Mark clinical as not_assessed.
            state["clinical_mastery_tier"] = "not_assessed"
            state["clinical_completed"] = False
            return _close_session_with_dean(state, dean, messages)

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
            fire_activity(
                f"Clinical reasoning verified ({int(state['clinical_confidence']*100)}% confidence)"
            )
            fire_activity("Generating closing summary")
            state["clinical_completed"] = True
            return _close_session_with_dean(state, dean, messages)

        if clinical_turn_count < clinical_max_turns:
            # Verdict label so the user sees "we're continuing because the
            # answer wasn't fully there yet" rather than going straight to
            # follow-up with no signal of why.
            student_state = (
                state.get("clinical_state") or "incomplete"
            )
            remaining = clinical_max_turns - clinical_turn_count
            fire_activity(
                f"Reasoning {student_state} — asking a follow-up "
                f"({remaining} turn{'s' if remaining != 1 else ''} remaining)"
            )
            fire_activity("Drafting follow-up question")
            follow_up = str(eval_result.get("feedback_message", "") or "").strip()
            if not follow_up:
                follow_up = dean._assessment_clinical_followup_fallback(state)
            follow_up = _strip_banned_prefixes(follow_up, state)
            # Post-filter the feedback_message through the same deterministic
            # leak check used for assessment-phase teacher drafts. The
            # _clinical_turn_call's feedback_message is a separate LLM
            # output that was previously not screened — observed in the
            # 2026-04-30 nidhi clinical session leaking "right coronary
            # artery, not the left" and citing `[7] and [10]`. If the
            # check fails, swap in a non-revealing redirect prompt.
            try:
                det = dean._deterministic_assessment_check(state, follow_up)
                if not det.get("pass", True):
                    state["debug"].setdefault("turn_trace", []).append({
                        "wrapper": "nodes.clinical_followup_leak_filter",
                        "result": f"FAIL: {det.get('reason_codes', [])}",
                        "original_excerpt": follow_up[:160],
                    })
                    follow_up = dean._assessment_clinical_followup_fallback(state)
                    # Re-strip + re-check the fallback (paranoid: if the
                    # fallback itself leaks, we accept it but log the issue).
                    follow_up = _strip_banned_prefixes(follow_up, state)
            except Exception as _e:
                state["debug"].setdefault("turn_trace", []).append({
                    "wrapper": "nodes.clinical_followup_leak_filter",
                    "result": f"check_error: {type(_e).__name__}",
                })
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
                "pending_user_choice": {},
                "debug": state["debug"],
            }

        # Max clinical turns reached: close with coaching summary.
        state["clinical_completed"] = False
        return _close_session_with_dean(state, dean, messages)

    else:
        # Reveal path (did not reach answer)
        state["clinical_opt_in"] = False
        # Change 5.1b: clinical never happened, mark as not_assessed.
        # core_mastery_tier is set later by _close_session_with_dean /
        # mastery scorer based on the tutoring outcome.
        state["clinical_mastery_tier"] = "not_assessed"
        return _close_session_with_dean(state, dean, messages)


# M1 — close-reason buckets. Drives both the close-LLM tone AND the
# save/no-save decision at memory_update_node.
_NO_SAVE_REASONS = {"exit_intent", "off_domain_strike"}
_VALID_CLOSE_REASONS = {
    "reach_full", "reach_skipped", "clinical_cap",
    "hints_exhausted", "tutoring_cap",
    "off_domain_strike", "exit_intent",
}


def _derive_close_reason(state: TutorState) -> str:
    """M1 — pick the close reason from state signals at session end.

    Priority order:
      1. exit_intent_pending or session_ended_off_domain → explicit triggers
      2. clinical_completed + clinical reach → reach_full
      3. clinical_completed + cap hit → clinical_cap
      4. tutoring student_reached_answer + opt_in_no → reach_skipped
      5. hint_level > max_hints → hints_exhausted
      6. turn_count >= max_turns → tutoring_cap
      7. else → tutoring_cap (defensive)
    """
    # Explicit triggers from upstream
    if state.get("exit_intent_pending"):
        return "exit_intent"
    if state.get("session_ended_off_domain"):
        return "off_domain_strike"

    # Clinical phase outcomes
    if state.get("clinical_completed"):
        clinical_state = state.get("clinical_state") or ""
        if clinical_state in {"correct", "partial_correct"}:
            return "reach_full"
        return "clinical_cap"

    # Tutoring outcomes
    if state.get("student_reached_answer") and state.get("clinical_opt_in") is False:
        return "reach_skipped"

    hint_level = int(state.get("hint_level", 0) or 0)
    max_hints = int(state.get("max_hints", 0) or 0)
    if hint_level > max_hints:
        return "hints_exhausted"

    turn_count = int(state.get("turn_count", 0) or 0)
    max_turns = int(state.get("max_turns", 0) or 0)
    if max_turns and turn_count >= max_turns:
        return "tutoring_cap"

    return "tutoring_cap"


def _draft_close_message(state: TutorState, close_reason: str) -> dict:
    """M1 — single Sonnet close call. Returns dict with message + takeaways.

    Output shape (from the close-mode prompt JSON):
      {
        "message":      "<tutor goodbye>",
        "demonstrated": "<short>",
        "needs_work":   "<short>",
      }

    Empty fields on LLM failure (no templated tutor text per M-FB).
    """
    import json as _json
    from conversation.teacher_v2 import TeacherV2, TeacherPromptInputs
    from conversation.turn_plan import TurnPlan
    from conversation.llm_client import make_anthropic_client, resolve_model
    from conversation.teacher import fire_activity
    from config import cfg as _cfg

    fire_activity("Reflecting on your progress")

    locked = state.get("locked_topic") or (state.get("debug") or {}).get("locked_topic_snapshot") or {}
    plan = TurnPlan(
        scenario=f"close:{close_reason}",
        hint_text="",
        mode="close",
        tone="neutral",
        forbidden_terms=[],
        permitted_terms=[],
        # close JSON output is unconstrained sentences; loose shape
        shape_spec={"max_sentences": 6, "exactly_one_question": False},
        carryover_notes=f"close_reason: {close_reason}",
    )
    inputs = TeacherPromptInputs(
        chunks=[],
        history=state.get("messages", []),
        locked_subsection=str(locked.get("subsection") or ""),
        locked_question=str(state.get("locked_question") or ""),
        domain_name=getattr(_cfg.domain, "name", "this subject"),
        domain_short=getattr(_cfg.domain, "short", "subject"),
        student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
    )
    client = make_anthropic_client()
    teacher_v2 = TeacherV2(client, model=resolve_model(_cfg.models.teacher))

    # Single attempt with internal silent retry — M-FB pattern.
    text = ""
    error = ""
    for attempt in (1, 2):
        try:
            draft = teacher_v2.draft(plan, inputs)
            text = (draft.text or "").strip()
            if text:
                error = ""
                break
            error = "empty_text"
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:120]}"

    state["debug"]["turn_trace"].append({
        "wrapper": "teacher_v2.close_draft",
        "close_reason": close_reason,
        "attempts": attempt,
        "error": error,
        "text_len": len(text),
    })
    if not text:
        return {"message": "", "demonstrated": "", "needs_work": "", "_error": error}

    # Strip ```json fences if present
    s = text
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        parsed = _json.loads(s)
    except _json.JSONDecodeError:
        # Fallback: try to extract first JSON object substring
        import re as _re
        m = _re.search(r"\{.*\}", s, _re.DOTALL)
        parsed = None
        if m:
            try:
                parsed = _json.loads(m.group(0))
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        # If JSON parse fails, treat the whole text as the message and
        # leave takeaways empty (no fake/templated takeaways).
        return {
            "message": text,
            "demonstrated": "",
            "needs_work": "",
            "_error": "json_parse_fail",
        }
    return {
        "message": str(parsed.get("message") or "").strip(),
        "demonstrated": str(parsed.get("demonstrated") or "").strip(),
        "needs_work": str(parsed.get("needs_work") or "").strip(),
        "_error": "",
    }


def memory_update_node(state: TutorState, dean, memory_manager) -> dict:
    """
    Phase 4 — Memory Update + Close.

    M1 redesign:
      1. Determine close_reason from state signals
      2. Draft the LLM close message (structured JSON)
      3. Append close message to chat history
      4. If close_reason is no-save (exit_intent, off_domain_strike):
         skip mem0 / mastery / sqlite writes
      5. Otherwise: full save + record key_takeaways from the close JSON
      6. Stamp session_ended=True so frontend disables input

    No templated tutor fallbacks (M-FB). On close-LLM failure, the message
    is empty (frontend renders an error card).
    """
    state["debug"]["current_node"] = "memory_update_node"
    from conversation.teacher import fire_activity

    # ── M1: derive close reason + draft close message ─────────────────────
    close_reason = state.get("close_reason") or _derive_close_reason(state)
    state["close_reason"] = close_reason
    close_payload = _draft_close_message(state, close_reason)
    close_message = close_payload.get("message") or ""
    takeaways = {
        "demonstrated": close_payload.get("demonstrated") or "",
        "needs_work": close_payload.get("needs_work") or "",
        "close_reason": close_reason,
    }

    # Append close message to chat (only if non-empty — avoid blank
    # tutor bubble; frontend will surface the error card instead).
    new_messages = list(state.get("messages", []) or [])
    if close_message:
        new_messages.append({
            "role": "tutor",
            "content": close_message,
            "phase": "memory_update",
            "metadata": {
                "mode": "close",
                "close_reason": close_reason,
                "source": "memory_update_node",
            },
        })
    else:
        # M-FB: surface error card payload — frontend renders distinct UI
        new_messages.append({
            "role": "system",
            "content": "",
            "phase": "memory_update",
            "metadata": {
                "kind": "error_card",
                "component": "Teacher.close_draft",
                "error_class": "EmptyOrParseFail",
                "message": close_payload.get("_error") or "close draft empty",
                "retry_handler": "close",
            },
        })

    # ── M1: no-save bucket → skip all persistence ─────────────────────────
    if close_reason in _NO_SAVE_REASONS:
        state["debug"]["turn_trace"].append({
            "wrapper": "memory_update_node.no_save",
            "close_reason": close_reason,
            "result": "skipped_save_per_M1_design",
        })
        return {
            "phase": "memory_update",
            "messages": new_messages,
            "session_ended": True,
            "close_reason": close_reason,
            "exit_intent_pending": False,
            "debug": state["debug"],
        }

    # ── Save bucket — existing mem0 + mastery + sqlite flow ───────────────
    fire_activity("Saving session memory")
    student_id = state.get("student_id", "") or ""
    flush_status = "skipped_no_student_id"
    flushed = False
    if student_id:
        try:
            flushed = memory_manager.flush(student_id, state, summary_text="")
            flush_status = getattr(memory_manager, "last_flush_status", "ok")
        except Exception as e:
            flush_status = f"error: {type(e).__name__}: {str(e)[:80]}"
    state["debug"]["turn_trace"].append({
        "wrapper": "memory_manager.flush",
        "result": flush_status,
        "flushed": flushed,
    })

    # D.3: per-concept knowledge tracing via LLM-based scoring.
    # Updates two signals per session for the locked subsection:
    #   mastery     — point estimate (BKT-style P(L))
    #   confidence  — coverage estimate (how thoroughly probed)
    # See memory/mastery_store.py module docstring for the literature
    # grounding (extends Corbett & Anderson 1995 BKT).
    #
    # No heuristic fallback by design: the LLM is the model. If the
    # call fails after retries, this session's mastery simply doesn't
    # update (logged in turn_trace) — degrades visibly rather than
    # silently substituting a worse signal.
    mastery_status = "skipped"
    judgment: dict | None = None  # captured for SQLite dual-write below
    if student_id:
        try:
            from memory.mastery_store import MasteryStore, score_session_llm
            from config import cfg as _cfg
            import anthropic
            # Same sticky-snapshot fallback as MemoryManager._topic_metadata
            # — state["locked_topic"] can end up None at this node even
            # when a topic was locked earlier; debug.locked_topic_snapshot
            # is the dean's write-once record that survives merges.
            locked = state.get("locked_topic") or {}
            if not locked:
                locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}
            locked_path = str(locked.get("path", "") or "")
            if locked_path:
                fire_activity("Scoring concept mastery")
                ms = MasteryStore()
                priors = ms.recent_rationales(student_id, locked_path, limit=3)
                from conversation.llm_client import make_anthropic_client, resolve_model
                client = make_anthropic_client()
                judgment = score_session_llm(
                    state,
                    prior_rationales=priors,
                    client=client,
                    model=resolve_model(_cfg.models.dean),
                )
                if judgment is None:
                    mastery_status = "llm_scorer_failed_skipped_update"
                else:
                    outcome = (
                        "reached" if state.get("student_reached_answer")
                        else "not_reached"
                    )
                    rec = ms.update(
                        student_id=student_id,
                        subsection_path=locked_path,
                        mastery_score=float(judgment["mastery"]),
                        confidence_score=float(judgment["confidence"]),
                        outcome=outcome,
                        rationale=str(judgment.get("rationale", "")),
                    )
                    mastery_status = (
                        f"updated mastery={rec.get('mastery')} "
                        f"confidence={rec.get('confidence')} "
                        f"sessions={rec.get('sessions')}"
                    )
            else:
                mastery_status = "skipped_no_locked_topic"
        except Exception as e:
            mastery_status = f"error: {type(e).__name__}: {str(e)[:80]}"
    state["debug"]["turn_trace"].append({
        "wrapper": "mastery_store.update",
        "result": mastery_status,
    })

    # L21 + L1/L2/L3: dual-write the session-end row + subsection_mastery
    # into the per-domain SQLite store. This populates the new data layer
    # alongside the legacy JSON MasteryStore. Reads switch to SQLite in
    # the next commit (track 1.9). Best-effort — never blocks return.
    # M1 — also writes key_takeaways from the close-LLM JSON.
    state["_close_takeaways_pending"] = takeaways
    sqlite_status = _persist_session_end_to_sqlite(state, judgment)
    state.pop("_close_takeaways_pending", None)
    state["debug"]["turn_trace"].append({
        "wrapper": "sqlite_store.session_end",
        "result": sqlite_status,
    })

    return {
        "phase": "memory_update",
        "messages": new_messages,
        "session_ended": True,
        "close_reason": close_reason,
        "exit_intent_pending": False,
        "debug": state["debug"],
    }


def _log_conversation(state: TutorState, messages: list[dict]) -> None:
    """Save full conversation to data/artifacts/conversations/{student_id}_turn_{n}.json."""
    try:
        out_dir = Path(cfg.paths.artifacts) / "conversations"
        out_dir.mkdir(parents=True, exist_ok=True)
        dbg = state.get("debug", {}) or {}
        locked_topic = state.get("locked_topic") or dbg.get("locked_topic_snapshot")
        tutoring_turns = int(state.get("turn_count", 0))
        coverage_gap_events = int(dbg.get("coverage_gap_events", 0))
        retrieval_calls = int(dbg.get("retrieval_calls", 0))
        grounded_turns = int(dbg.get("grounded_turns", 0))
        ungrounded_turns = int(dbg.get("ungrounded_turns", 0))
        invariant_violations = list(dbg.get("invariant_violations", []) or [])
        topic_locked_to_toc = locked_topic is not None
        if tutoring_turns <= 0 or not topic_locked_to_toc or retrieval_calls < 1:
            groundedness_score = 0.0
        else:
            # Prefer explicit grounded/ungrounded counters when populated
            # (P4 observability). Falls back to coverage_gap_events for
            # older sessions that only have that signal.
            counted = grounded_turns + ungrounded_turns
            if counted > 0:
                groundedness_score = grounded_turns / float(counted)
            else:
                groundedness_score = max(
                    0.0, 1.0 - (coverage_gap_events / float(tutoring_turns))
                )
        conv_data = {
            "student_id": state["student_id"],
            "locked_question": state.get("locked_question", ""),
            "locked_answer": state.get("locked_answer", ""),
            "student_reached_answer": state.get("student_reached_answer", False),
            "hint_level": state.get("hint_level", 1),
            "turn_count": tutoring_turns,
            "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
            "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
            "mastery_tier": state.get("mastery_tier", "not_assessed"),
            "clinical_turn_count": state.get("clinical_turn_count", 0),
            "clinical_completed": state.get("clinical_completed", False),
            "weak_topics": state.get("weak_topics", []),
            "topic_locked_to_toc": topic_locked_to_toc,
            "locked_topic": locked_topic,
            "retrieval_calls": retrieval_calls,
            "coverage_gap_events": coverage_gap_events,
            "grounded_turns": grounded_turns,
            "ungrounded_turns": ungrounded_turns,
            "invariant_violations": invariant_violations,
            "groundedness_score": round(groundedness_score, 4),
            "messages": messages,
            "debug": {k: v for k, v in dbg.items() if k != "turn_trace"},
        }
        turn = state.get("turn_count", 0)
        out_path = out_dir / f"{state['student_id']}_turn_{turn}.json"
        out_path.write_text(json.dumps(conv_data, indent=2))
    except Exception:
        pass  # non-fatal


def _close_session_with_dean(state: TutorState, dean, messages: list[dict]) -> dict:
    """
    Single closeout path for both reached and unreached outcomes.
    Uses Dean's batched close-session evaluator and updates weak_topics from final tier.
    """
    closeout = dean._close_session_call(state)
    state["core_mastery_tier"] = closeout.get("core_mastery_tier", "not_assessed")
    state["clinical_mastery_tier"] = closeout.get("clinical_mastery_tier", "not_assessed")
    state["mastery_tier"] = closeout.get("mastery_tier", "not_assessed")
    state["grading_rationale"] = closeout.get("grading_rationale", "")
    state["session_memory_summary"] = closeout.get("memory_summary", "")

    # Strict-groundedness grading guard: a session that never locked to a TOC
    # node, or had zero successful retrieval calls, cannot be graded on
    # mastery — any tier Dean returned would be based on ungrounded content.
    # Override to `ungraded` so downstream memory/export treats it correctly.
    #
    # `locked_topic` can be transiently cleared by partial updates during the
    # session (e.g. a retry path), so we also accept the sticky snapshot
    # written by dean.anchors_locked as evidence of a successful lock. If
    # either signal says the session was grounded, we trust it.
    dbg = state.get("debug", {}) or {}
    locked_topic = state.get("locked_topic")
    locked_topic_snapshot = dbg.get("locked_topic_snapshot")
    topic_confirmed = bool(state.get("topic_confirmed"))
    was_grounded = bool(locked_topic or locked_topic_snapshot)
    retrieval_calls = int(dbg.get("retrieval_calls", 0))
    if not was_grounded or retrieval_calls == 0:
        state["core_mastery_tier"] = "ungraded"
        state["clinical_mastery_tier"] = "ungraded"
        state["mastery_tier"] = "ungraded"
        state["grading_rationale"] = (
            "Session was not grounded to a textbook topic — "
            "ungraded by policy (no TOC lock or no retrieval calls)."
        )
        state["debug"]["turn_trace"].append({
            "wrapper": "nodes.grading_guard",
            "result": "forced_ungraded",
            "locked_topic_present": bool(locked_topic),
            "locked_topic_snapshot_present": bool(locked_topic_snapshot),
            "topic_confirmed": topic_confirmed,
            "retrieval_calls": retrieval_calls,
        })

    # Update weak topics for developing/needs_review outcomes.
    # `ungraded` sessions deliberately skip weak-topic updates — we don't
    # want random student inputs polluting long-term memory.
    weak_topics = list(state.get("weak_topics", []))
    if state.get("mastery_tier") in {"developing", "needs_review"}:
        topic_name = _session_topic_name(state)
        if topic_name:
            difficulty = "moderate" if state.get("mastery_tier") == "developing" else "hard"
            weak_topics = _upsert_weak_topic(weak_topics, topic_name, difficulty=difficulty, bump=1)
            state["weak_topics"] = weak_topics

    final_msg = str(closeout.get("student_facing_message", "") or "").strip()
    if final_msg:
        messages.append({"role": "tutor", "content": final_msg, "phase": "memory_update"})
    _log_conversation(state, messages)

    return {
        "messages": messages,
        "assessment_turn": 3,
        "phase": "assessment",
        "pending_user_choice": {},
        "clinical_opt_in": state.get("clinical_opt_in"),
        "clinical_completed": state.get("clinical_completed", False),
        "clinical_turn_count": state.get("clinical_turn_count", 0),
        "clinical_max_turns": state.get("clinical_max_turns", 3),
        "clinical_state": state.get("clinical_state"),
        "clinical_confidence": state.get("clinical_confidence", 0.0),
        "clinical_history": state.get("clinical_history", []),
        "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
        "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
        "mastery_tier": state.get("mastery_tier", "not_assessed"),
        "grading_rationale": state.get("grading_rationale", ""),
        "session_memory_summary": state.get("session_memory_summary", ""),
        "weak_topics": state.get("weak_topics", []),
        "debug": state["debug"],
    }


def _latest_student_message(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "student":
            return str(msg.get("content", "")).strip()
    return ""


_OPT_IN_YES_PREFIXES = (
    "yes", "yeah", "yep", "yup", "sure", "ok ", "okay", "alright",
    "go ahead", "let's", "lets go", "i'll try", "ill try", "i would",
    "absolutely", "definitely", "please", "ready", "sounds good",
)
_OPT_IN_NO_PREFIXES = (
    "no", "nope", "nah", "skip", "pass", "not now", "maybe later",
    "i'm good", "im good", "not really", "not today",
)


_BANNED_LEAD_PREFIXES = (
    "i can see", "i notice", "i hear you", "that's okay", "that is okay",
    "i understand", "i see that", "it sounds like", "i hear that",
)


def _strip_banned_prefixes(text: str, state: dict) -> str:
    """
    Remove banned empathy/filler lead-ins from clinical drafts.

    The tutoring-phase `_deterministic_tutoring_check` enforces this list,
    but clinical drafts bypass that check — this function re-applies the rule
    post-hoc on the assessment path. If the first sentence starts with a
    banned prefix, drop just that sentence and keep the rest. If stripping
    empties the text, return the original (fail-open — never emit nothing).
    """
    import re as _re
    if not text:
        return text
    lowered = text.strip().lower()
    if not any(lowered.startswith(p) for p in _BANNED_LEAD_PREFIXES):
        return text
    # Split on sentence boundary, drop the first sentence, keep the rest.
    parts = _re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)
    remaining = parts[1].strip() if len(parts) >= 2 else ""
    if not remaining:
        return text  # fail-open: don't emit empty
    try:
        state.setdefault("debug", {}).setdefault("turn_trace", []).append({
            "wrapper": "assessment._strip_banned_prefix",
            "result": f"stripped_leading_sentence: {parts[0][:80]!r}",
        })
    except Exception:
        pass
    return remaining


def _classify_opt_in(msg: str) -> str:
    """Return 'yes', 'no', or 'ambiguous' for an opt-in reply."""
    m = (msg or "").strip().lower()
    if not m:
        return "ambiguous"
    if m in {"y"}: return "yes"
    if m in {"n"}: return "no"
    if any(m.startswith(p) for p in _OPT_IN_YES_PREFIXES):
        return "yes"
    if any(m.startswith(p) for p in _OPT_IN_NO_PREFIXES):
        return "no"
    return "ambiguous"


def _session_topic_name(state: TutorState) -> str:
    """
    Canonical TOC-node name for this session — used for weak_topics and
    memory write-back. Prefers the locked TOC entry (set by dean.run_turn
    after TopicMatcher resolves the student's request) so we never persist
    a raw free-text selection or a menu-option label.
    Returns "" when the session was never grounded to a TOC node; callers
    should skip weak-topic updates in that case (P0.6 grading guard).
    """
    locked = state.get("locked_topic") or {}
    node = (locked.get("subsection") or locked.get("section") or locked.get("chapter") or "").strip()
    if node:
        return node[:120]
    # No TOC lock — fall back to topic_selection only when it looks like a
    # canonical label (short, no numeric prefix). Otherwise return empty so
    # the caller treats the session as ungraded.
    selected = str(state.get("topic_selection", "") or "").strip()
    if selected and len(selected.split()) <= 10 and not selected[:1].isdigit():
        return selected[:120]
    return ""


def _upsert_weak_topic(weak_topics: list[dict], topic_name: str, difficulty: str = "moderate", bump: int = 1) -> list[dict]:
    if not topic_name:
        return weak_topics
    out = list(weak_topics or [])
    key = " ".join(topic_name.strip().lower().split())
    for wt in out:
        wt_topic = " ".join(str(wt.get("topic", "")).strip().lower().split())
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

def _persist_session_end_to_sqlite(
    state: TutorState,
    judgment: dict | None,
) -> str:
    """Update the L21 SQLite session row + upsert subsection_mastery (per L1, L2, L3).

    Coexists with the legacy MasteryStore JSON write — dual-write phase. The
    next commit (track 1.9) flips reads to SQLite and drops the JSON path.

    Returns a short status string for turn_trace.
    """
    thread_id = state.get("thread_id") or ""
    student_id = state.get("student_id") or ""
    if not thread_id or not student_id:
        return "skipped_no_thread_or_student"

    try:
        from memory.sqlite_store import (
            SQLiteStore,
            normalize_subsection_path,
            score_to_tier,
        )
    except Exception as e:
        return f"sqlite_import_error: {type(e).__name__}: {str(e)[:80]}"

    # --- Resolve final session metadata from state ---
    locked = state.get("locked_topic") or {}
    if not locked:
        locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}
    legacy_path = str(locked.get("path") or "")
    canonical_path = normalize_subsection_path(legacy_path) if legacy_path else ""
    locked_question = state.get("locked_question") or None
    locked_answer = state.get("locked_answer") or None
    full_answer = state.get("full_answer") or None

    # Determine status (per L21 enum). Heuristics — L43-L62 tutor refactor
    # will tighten these with explicit termination signals.
    reach = state.get("student_reached_answer")
    turn_count = len([m for m in (state.get("messages") or []) if m.get("role") == "student"])
    if not canonical_path:
        status = "abandoned_no_lock"
    elif reach is True:
        status = "completed"
    elif state.get("session_ended_off_domain"):
        status = "ended_off_domain"
    elif state.get("session_ended_by_student"):
        status = "ended_by_student"
    elif turn_count >= 25:
        status = "ended_turn_limit"
    else:
        status = "completed"

    # Mastery tier projection from the LLM judgment (if scoring ran).
    score = float(judgment["mastery"]) if judgment and "mastery" in judgment else None
    tier = score_to_tier(score) if score is not None else "not_assessed"

    # --- Write the session row + upsert subsection_mastery ---
    try:
        store = SQLiteStore()
        store.update_session(
            thread_id,
            ended_at=None and None,  # sentinel: end_session sets ended_at via utc_now
        )
        # M1 — pull takeaways from the close LLM JSON (set by memory_update_node)
        takeaways_payload = state.get("_close_takeaways_pending") or {}
        end_kwargs = dict(
            status=status,
            locked_topic_path=canonical_path or None,
            locked_subsection_path=canonical_path or None,
            locked_question=locked_question,
            locked_answer=locked_answer,
            full_answer=full_answer,
            reach_status=bool(reach) if reach is not None else None,
            mastery_tier=tier,
            core_mastery_tier=tier,
            clinical_mastery_tier="not_assessed",
            core_score=score,
            clinical_score=None,
            hint_level_final=int(state.get("hint_level") or 0),
            turn_count=turn_count,
        )
        if takeaways_payload and (
            takeaways_payload.get("demonstrated") or takeaways_payload.get("needs_work")
        ):
            end_kwargs["key_takeaways"] = takeaways_payload
        store.end_session(thread_id, **end_kwargs)
        if canonical_path and score is not None:
            outcome = "reached" if reach else (
                "partial" if (state.get("student_reach_coverage") or 0.0) > 0 else "not_reached"
            )
            store.upsert_subsection_mastery(
                student_id,
                canonical_path,
                fresh_score=score,
                outcome=outcome,
            )
        return f"sqlite_session_end ok status={status} score={score} path_set={bool(canonical_path)}"
    except Exception as e:
        return f"sqlite_write_error: {type(e).__name__}: {str(e)[:120]}"
