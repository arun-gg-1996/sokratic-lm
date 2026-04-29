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

    # Pull cross-session memories from mem0. Empty list for new students,
    # if memory is disabled for this session (frontend toggle), or if
    # Qdrant/mem0 is unavailable (memory_manager swallows all errors).
    #
    # Two TARGETED queries instead of one generic "topics + misconceptions
    # + outcomes" sweep. Why: the rapport opener should reference at most
    # ONE prior topic or open thread (per the prompt rules in
    # config/base.yaml::teacher_rapport). Misconceptions and learning-
    # style cues are tutor-internal — not material for an opener — and
    # surfacing them in the rapport prompt creates noise the LLM has to
    # ignore. This used to be one generic search returning a mixed bag;
    # now we filter at the mem0 layer to:
    #   1. session_summary  → "the most recent session's topic"
    #   2. open_thread      → "any unresolved thread to offer continuation"
    # Both filters require the metadata payload added in commit
    # 'feat(memory): tag mem0 entries with category + topic metadata'.
    student_id = state.get("student_id", "") or ""
    memory_enabled = bool(state.get("memory_enabled", True))
    past_memories: list[dict] = []
    weak_subsections: list[dict] = []
    if student_id and memory_enabled:
        try:
            recent_summaries = memory_manager.load(
                student_id,
                query="most recent session topic",
                filters={"category": "session_summary"},
            )
        except Exception:
            recent_summaries = []
        try:
            open_threads = memory_manager.load(
                student_id,
                query="unresolved thread to resume",
                filters={"category": "open_thread"},
            )
        except Exception:
            open_threads = []
        # Cap each list and merge. The rapport prompt only needs a few
        # bullets — more memory just dilutes the LLM's attention. Keep
        # open_threads first (more rapport-actionable than completed
        # summaries) and trim to 6 total entries.
        past_memories = (open_threads[:3] + recent_summaries[:3])[:6]

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

    if reached and assessment_turn == 0:
        # Step 1: ask whether student wants the optional clinical application question.
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
        student_msg = _latest_student_message(messages).strip().lower()
        intent = _classify_opt_in(student_msg)
        if intent == "yes":
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
            return _close_session_with_dean(state, dean, messages)

        # Ambiguous — treat a long substantive reply as an implicit "yes" and
        # proceed into clinical so we don't loop on the opt-in prompt.
        if len(student_msg.split()) >= 6:
            state["debug"].setdefault("turn_trace", []).append({
                "wrapper": "assessment.opt_in_implicit_yes",
                "result": "long reply treated as yes",
            })
            draft = teacher.draft_clinical(state, dean_critique="")
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
            return _close_session_with_dean(state, dean, messages)

        if clinical_turn_count < clinical_max_turns:
            follow_up = str(eval_result.get("feedback_message", "") or "").strip()
            if not follow_up:
                follow_up = dean._assessment_clinical_followup_fallback(state)
            follow_up = _strip_banned_prefixes(follow_up, state)
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
        return _close_session_with_dean(state, dean, messages)


def memory_update_node(state: TutorState, dean, memory_manager) -> dict:
    """
    Phase 4 — Memory Update.

    Calls memory_manager.flush() which writes 5 categories of mem0 entries
    for the session: session summary, misconceptions, open thread (if not
    reached), topics covered, and learning style cues. See
    memory/memory_manager.py for category formats.

    Falls back to no-op if mem0 / Qdrant is unavailable (memory_manager
    handles that internally) — never blocks session-end.

    Returns phase = "memory_update" to signal session end.
    """
    state["debug"]["current_node"] = "memory_update_node"
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

    # D.3: per-concept knowledge tracing. Update the locked subsection's
    # mastery score using the deterministic heuristic on
    # (outcome, hints, turns). The store is the source of truth for the
    # /mastery dashboard and feeds the topic suggester + rapport opener
    # on the NEXT session start. Wrapped in try/except so a mastery
    # failure never blocks session end.
    mastery_status = "skipped"
    if student_id:
        try:
            from memory.mastery_store import MasteryStore, score_session
            locked_path = (state.get("locked_topic") or {}).get("path", "") or ""
            if locked_path:
                ms = MasteryStore()
                score = score_session(state)
                outcome = (
                    "reached" if state.get("student_reached_answer")
                    else "not_reached"
                )
                rec = ms.update(student_id, locked_path, score, outcome)
                mastery_status = (
                    f"updated mastery={rec.get('mastery')} "
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

    return {
        "phase": "memory_update",
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
