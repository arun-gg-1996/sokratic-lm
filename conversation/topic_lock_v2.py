"""
conversation/topic_lock_v2.py
-----------------------------
Track 4.7d: v2 pre-lock topic flow.

Owns L9/L10/L11/L22 while SOKRATIC_USE_V2_FLOW is enabled:
  * L9  - Topic Mapper LLM route decisions
  * L10 - confirm-and-lock for borderline-high matches
  * L11 - prelock_loop_count, separate from tutoring turn_count
  * L22 - cap-7 guided-pick cards plus explicit give-up/end

This module deliberately reuses the legacy Dean's tested retrieval,
coverage-gate, and anchor-lock helpers once a TOC node is chosen. The
new behavior is the pre-lock routing, not a rewrite of anchor extraction.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from config import cfg
from conversation.dean import (
    _coverage_gate,
    _format_topic_label,
    _replace_latest_student_message,
)
from conversation.llm_client import make_anthropic_client, resolve_model
from retrieval.topic_mapper_llm import TopicMapperResult, map_topic
from retrieval.topic_matcher import TopicMatch, get_topic_matcher


PRELOCK_CAP = 7
GUIDED_PICK_COUNT = 6
NORMAL_CARD_COUNT = 3
GIVE_UP_VALUE = "__sokratic_give_up__"
TOPIC_MAPPER_MODEL = "claude-haiku-4-5-20251001"


def run_topic_lock_v2(
    state: dict,
    *,
    dean: Any,
    retriever: Any,
    latest_student: str,
) -> dict:
    """Run one unlocked-topic v2 pre-lock turn and return a partial state.

    `nodes_v2.dean_node_v2` handles no-student and whitespace guards before
    calling this function, so every call here is a real student round-trip.
    """
    state.setdefault("debug", {}).setdefault("turn_trace", [])
    trace = state["debug"]["turn_trace"]
    messages = list(state.get("messages", []) or [])
    prior_count = int(state.get("prelock_loop_count", 0) or 0)
    prelock_count = min(PRELOCK_CAP, prior_count + 1)
    pending = state.get("pending_user_choice") or {}

    # NOTE: nodes_v2.dean_node_v2 already fires "Reading your message"
    # before delegating here, so do NOT fire it again — that produced
    # a visible duplicate row in the activity feed.
    from conversation.teacher import fire_activity

    trace.append({
        "wrapper": "topic_lock_v2.entry",
        "prelock_loop_count": prelock_count,
        "pending_kind": pending.get("kind") if isinstance(pending, dict) else "",
    })

    if _is_guided_pending(pending):
        selected = _match_choice(latest_student, pending.get("options", []) or [])
        if _is_give_up(latest_student, pending) or not selected:
            return _give_up(state, messages, prelock_count)
        topic = _topic_from_pending(pending, selected)
        if topic is not None:
            return _lock_topic(
                state, dean=dean, retriever=retriever, topic=topic,
                selected_label=selected, messages=messages,
                prelock_count=prelock_count, source="guided_pick",
            )
        return _give_up(state, messages, prelock_count)

    if isinstance(pending, dict) and pending.get("kind") == "confirm_topic":
        if _is_yes(latest_student):
            topic = _topic_from_pending(pending, "__candidate__")
            selected_label = pending.get("candidate_label") or (topic.label if topic else "that topic")
            if topic is not None:
                return _lock_topic(
                    state, dean=dean, retriever=retriever, topic=topic,
                    selected_label=str(selected_label), messages=messages,
                    prelock_count=prelock_count, source="confirm_topic",
                )
        elif _is_no(latest_student):
            # M3: record the rejected subsection so the next _map_topic call
            # excludes it. State field clears on new session — in-session only.
            rejected = list(state.get("rejected_topic_paths", []) or [])
            topic_meta = (pending.get("topic_meta") or {}).get("__candidate__") or {}
            rej_path = str(topic_meta.get("path", "") or "").strip()
            if rej_path and rej_path not in rejected:
                rejected.append(rej_path)
            state["rejected_topic_paths"] = rejected
            messages.append({
                "role": "tutor",
                "content": "Ok, what topic would you like to work on instead?",
                "phase": "tutoring",
            })
            return _base_update(
                state, messages, prelock_count,
                topic_options=[], topic_question="", pending_user_choice={},
                student_state="question",
            )
        # Typed text instead of clicking Yes/No is a fresh topic query.

    if isinstance(pending, dict) and pending.get("kind") == "topic":
        options = list(pending.get("options", []) or [])
        selected = _match_choice(latest_student, options)
        if selected:
            topic = _topic_from_pending(pending, selected)
            if topic is not None:
                return _lock_topic(
                    state, dean=dean, retriever=retriever, topic=topic,
                    selected_label=selected, messages=messages,
                    prelock_count=prelock_count, source="topic_card",
                )
        # Non-selection text falls through to the L9 mapper.

    # M4 — anchor_pick (My Mastery prelock UX). Student is choosing WHICH
    # anchor question variation to work on. Each variation already has its
    # own locked_question / locked_answer / aliases / full_answer in the
    # pending.anchor_meta dict — set them on state and route into tutoring.
    if isinstance(pending, dict) and pending.get("kind") == "anchor_pick":
        options = list(pending.get("options", []) or [])
        anchor_meta = pending.get("anchor_meta") or {}
        # Match by exact question text first, then by 1-based index.
        chosen_q: Optional[str] = None
        for opt in options:
            if opt and opt.strip() == latest_student.strip():
                chosen_q = opt
                break
        if chosen_q is None:
            try:
                idx = int(latest_student.strip()) - 1
                if 0 <= idx < len(options):
                    chosen_q = options[idx]
            except (TypeError, ValueError):
                pass
        if chosen_q and isinstance(anchor_meta, dict) and chosen_q in anchor_meta:
            v = anchor_meta[chosen_q]
            locked_q = str(v.get("question") or "").strip()
            locked_a = str(v.get("answer") or "").strip()
            full_a = str(v.get("full_answer") or v.get("answer") or "").strip()
            aliases = [
                str(a) for a in (v.get("aliases") or [])
                if isinstance(a, str) and str(a).strip()
            ]
            # Mirror onto state so downstream code reading state directly
            # in this same invocation sees them.
            state["locked_question"] = locked_q
            state["locked_answer"] = locked_a
            state["full_answer"] = full_a
            state["locked_answer_aliases"] = aliases
            state["topic_just_locked"] = True
            trace.append({
                "wrapper": "topic_lock_v2.anchor_pick_resolved",
                "chosen": chosen_q[:80],
                "locked_question_len": len(locked_q),
                "locked_answer_len": len(locked_a),
            })
            # CRITICAL — _base_update only puts a fixed set of keys into
            # the return dict (messages/phase/topic_confirmed/prelock_loop_count/debug).
            # Anything else MUST be passed via **extra or LangGraph's
            # reducer drops the state mutation. Earlier version assigned
            # these to state but didn't pass them through, so the next
            # invocation read them as empty → empty Q/A → SAFE_PROBE loop.
            return _base_update(
                state, messages, prelock_count,
                # M4 — topic IS confirmed once the student picked an anchor.
                # Without this, _base_update defaults to False and the
                # sidebar derivePhase falls back to "rapport" + shows the
                # pre-lock counter even though tutoring has started.
                topic_confirmed=True,
                topic_options=[], topic_question="",
                pending_user_choice={},
                student_state="answer",
                locked_topic=state.get("locked_topic") or {},
                locked_question=locked_q,
                locked_answer=locked_a,
                full_answer=full_a,
                locked_answer_aliases=aliases,
                topic_just_locked=True,
            )
        # M4 — pivot path: student typed something instead of picking
        # one of the anchor cards. Treat as a NEW topic query: clear the
        # prelock state so the topic mapper can resolve from scratch.
        # Without this, the student would be stuck on the prior subsection's
        # cards or fall through to tutoring with empty Q/A.
        trace.append({
            "wrapper": "topic_lock_v2.anchor_pick_pivot",
            "typed": latest_student[:80],
            "had_subsection": str((state.get("locked_topic") or {}).get("subsection") or "")[:60],
        })
        state["locked_topic"] = None
        state["topic_confirmed"] = False
        state["topic_selection"] = ""
        state["locked_question"] = ""
        state["locked_answer"] = ""
        state["full_answer"] = ""
        state["locked_answer_aliases"] = []
        state["retrieved_chunks"] = []
        state["topic_just_locked"] = False
        # pending will be cleared by the _map_topic path that runs below.
        state["pending_user_choice"] = {}

    if prelock_count >= PRELOCK_CAP:
        fire_activity("Showing a guided picker")
        return _render_guided_pick(state, messages, retriever, latest_student, prelock_count)

    fire_activity("Resolving topic to the textbook")
    rejected_for_resolver = list(state.get("rejected_topic_paths", []) or [])
    result = _map_topic(latest_student, trace, rejected_paths=rejected_for_resolver)
    decision = result.route_decision()
    trace.append({
        "wrapper": "topic_lock_v2.map_topic",
        "decision": decision,
        "verdict": result.verdict,
        "confidence": result.confidence,
        "n_matches": len(result.top_matches),
        "elapsed_ms": result.elapsed_ms,
        "tokens_in": result.input_tokens,
        "tokens_out": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cache_creation_tokens": result.cache_creation_tokens,
    })

    topics = [_topic_from_candidate(c.path) for c in result.top_matches]
    topics = [t for t in topics if t is not None]

    if decision == "lock_immediately" and topics:
        return _lock_topic(
            state, dean=dean, retriever=retriever, topic=topics[0],
            selected_label=_format_topic_label(topics[0]), messages=messages,
            prelock_count=prelock_count, source="l9_strong",
        )

    if decision == "confirm_and_lock" and topics:
        return _render_confirm(state, messages, topics[0], result, prelock_count)

    if decision == "show_top_matches" and topics:
        intro = "I found a few close matches. Which one did you mean?"
        return _render_topic_cards(state, messages, topics[:NORMAL_CARD_COUNT], intro, prelock_count)

    return _render_refuse_cards(
        state, dean=dean, messages=messages, retriever=retriever,
        rejected_topic=latest_student, refuse_reason=decision,
        prelock_count=prelock_count,
    )


def _map_topic(
    query: str,
    trace: list[dict],
    *,
    rejected_paths: Optional[list[str]] = None,
) -> TopicMapperResult:
    try:
        client = make_anthropic_client()
        return map_topic(
            query,
            client=client,
            model=resolve_model(getattr(getattr(cfg, "models", object()), "topic_mapper", TOPIC_MAPPER_MODEL)),
            rejected_paths=rejected_paths,
        )
    except Exception as e:
        trace.append({
            "wrapper": "topic_lock_v2.map_topic_error",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })
        return TopicMapperResult(
            query=query,
            verdict="none",
            confidence=0.0,
            student_intent="topic_request",
            deferred_question=None,
            top_matches=[],
            raw_response=f"<caller_error: {type(e).__name__}>",
        )


def _lock_topic(
    state: dict,
    *,
    dean: Any,
    retriever: Any,
    topic: TopicMatch,
    selected_label: str,
    messages: list[dict],
    prelock_count: int,
    source: str,
) -> dict:
    """Lock a chosen TOC topic, run retrieval + coverage + anchors, then ack."""
    from conversation.teacher import fire_activity
    fire_activity(f"Topic locked: {selected_label}")

    if source in {"topic_card", "guided_pick", "confirm_topic"}:
        messages = _replace_latest_student_message(messages, selected_label)

    state["messages"] = messages
    state["topic_confirmed"] = True
    state["topic_selection"] = selected_label
    state["locked_topic"] = _topic_meta(topic)
    state["topic_options"] = []
    state["topic_question"] = ""
    state["pending_user_choice"] = {}
    state["retrieved_chunks"] = []
    state["locked_question"] = ""
    state["locked_answer"] = ""
    state["locked_answer_aliases"] = []
    state["full_answer"] = ""
    state["hint_level"] = 0
    state["topic_just_locked"] = True
    state.setdefault("debug", {})["retrieval_calls"] = 0
    trace = state["debug"].setdefault("turn_trace", [])
    trace.append({
        "wrapper": "topic_lock_v2.topic_locked",
        "source": source,
        "topic_path": topic.path,
        "label": selected_label,
        "prelock_loop_count": prelock_count,
    })

    fire_activity("Loading textbook context")
    try:
        dean._retrieve_on_topic_lock(state)
    except Exception as e:
        trace.append({
            "wrapper": "topic_lock_v2.retrieve_on_lock_error",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })

    gate = _coverage_gate(state, retriever=retriever)
    if gate is not None:
        return _coverage_refusal(
            state, dean=dean, messages=list(state.get("messages", []) or []),
            gate=gate, prelock_count=prelock_count,
        )

    fire_activity("Setting up the anchor question")
    try:
        anchors = dean._lock_anchors_call(state)
    except Exception as e:
        trace.append({
            "wrapper": "topic_lock_v2.lock_anchors_error",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })
        anchors = {}

    state["locked_question"] = str(anchors.get("locked_question", "") or "").strip()
    state["locked_answer"] = str(anchors.get("locked_answer", "") or "").strip()
    raw_aliases = anchors.get("locked_answer_aliases", []) or []
    state["locked_answer_aliases"] = [str(a) for a in raw_aliases if isinstance(a, str) and a.strip()]
    state["full_answer"] = str(anchors.get("full_answer", "") or "").strip() or state["locked_answer"]

    if not state["locked_question"] or not state["locked_answer"]:
        return _anchor_refusal(
            state, dean=dean, messages=list(state.get("messages", []) or []),
            failed_topic=topic, prelock_count=prelock_count,
        )

    if state.get("locked_topic") and not state["debug"].get("locked_topic_snapshot"):
        state["debug"]["locked_topic_snapshot"] = dict(state["locked_topic"])
    trace.append({
        "wrapper": "topic_lock_v2.anchors_locked",
        "locked_question": state["locked_question"],
        "locked_answer": state["locked_answer"],
    })

    # L6 injection #1 — read mem0 once at lock-time and stash on state so
    # the next dean.plan() call (Track 4.7e tutoring loop) can pass it as
    # carryover_notes. Safe wrapper: never raises, returns "" on any
    # mem0/network/stub failure.
    try:
        from conversation.mem0_inject import read_topic_lock_carryover
        persistent = getattr(dean, "memory_client", None)
        carryover = read_topic_lock_carryover(state, persistent, state.get("locked_topic") or {})
        if carryover:
            state["mem0_carryover_notes"] = carryover
            trace.append({
                "wrapper": "topic_lock_v2.mem0_carryover_seeded",
                "carryover_chars": len(carryover),
            })
    except Exception as e:
        trace.append({
            "wrapper": "topic_lock_v2.mem0_carryover_error",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        })

    try:
        ack = dean._build_topic_ack_message(state)
    except Exception:
        ack = f"Got it - let's work on **{selected_label}**.\n\n{state['locked_question']}"
    messages = list(state.get("messages", []) or [])
    messages.append({
        "role": "tutor",
        "content": ack,
        "phase": "tutoring",
        "metadata": {"mode": "topic_ack", "source": "topic_lock_v2"},
    })
    state["topic_just_locked"] = False

    return {
        "messages": messages,
        "topic_confirmed": True,
        "topic_options": [],
        "topic_question": "",
        "topic_selection": selected_label,
        "locked_topic": state.get("locked_topic"),
        "topic_just_locked": False,
        "pending_user_choice": {},
        "retrieved_chunks": state.get("retrieved_chunks", []),
        "locked_question": state.get("locked_question", ""),
        "locked_answer": state.get("locked_answer", ""),
        "locked_answer_aliases": state.get("locked_answer_aliases", []),
        "full_answer": state.get("full_answer", ""),
        "hint_level": 0,
        "student_state": "question",
        "prelock_loop_count": 0,
        "mem0_carryover_notes": state.get("mem0_carryover_notes", ""),
        "debug": state["debug"],
    }


def _coverage_refusal(
    state: dict,
    *,
    dean: Any,
    messages: list[dict],
    gate: dict,
    prelock_count: int,
) -> dict:
    rejected = list(state.get("rejected_topic_paths", []) or [])
    rejected_path = gate.get("rejected_path") or ""
    if rejected_path and rejected_path not in rejected:
        rejected.append(rejected_path)
    intro = "Let's pick a topic with stronger textbook coverage:"
    try:
        refuse = dean._prelock_refuse_call(
            state,
            rejected_topic=gate.get("topic_label", "") or "",
            failure_count=int(gate.get("failure_count", 0) or 0),
            refuse_reason=gate.get("reason", "") or "",
        )
        intro = refuse.get("tutor_reply") or intro
    except Exception:
        pass
    options = list(gate.get("options", []) or [])
    pending = dict(gate.get("pending_user_choice", {}) or {})
    msg = _with_numbered_options(intro, options)
    messages.append({"role": "tutor", "content": msg, "phase": "tutoring"})
    return _base_update(
        state, messages, prelock_count,
        topic_confirmed=False,
        topic_options=options,
        topic_question=intro,
        topic_selection="",
        locked_topic=None,
        pending_user_choice=pending,
        retrieved_chunks=[],
        locked_question="",
        locked_answer="",
        locked_answer_aliases=[],
        full_answer="",
        hint_level=0,
        student_state="question",
        rejected_topic_paths=rejected,
    )


def _anchor_refusal(
    state: dict,
    *,
    dean: Any,
    messages: list[dict],
    failed_topic: TopicMatch,
    prelock_count: int,
) -> dict:
    matcher = get_topic_matcher()
    rejected = list(state.get("rejected_topic_paths", []) or [])
    if failed_topic.path and failed_topic.path not in rejected:
        rejected.append(failed_topic.path)
    exclude = set(rejected)
    query = state.get("topic_selection") or failed_topic.label
    try:
        alternatives = matcher.sample_related(dean.retriever, query, n=NORMAL_CARD_COUNT, exclude_paths=exclude)
    except Exception:
        alternatives = matcher.sample_diverse(NORMAL_CARD_COUNT, exclude_paths=exclude)
    intro = "Let's try a different angle. Pick one of these or type a more specific term:"
    try:
        fail = dean._prelock_anchor_fail_call(state, state.get("topic_selection", "") or "")
        intro = fail.get("tutor_reply") or intro
    except Exception:
        pass
    update = _topic_card_update(
        state, messages, alternatives, intro, prelock_count,
        allow_custom=True, mode="normal",
    )
    update.update({
        "topic_confirmed": False,
        "topic_selection": "",
        "locked_topic": None,
        "retrieved_chunks": [],
        "locked_question": "",
        "locked_answer": "",
        "locked_answer_aliases": [],
        "full_answer": "",
        "rejected_topic_paths": rejected,
    })
    return update


def _render_confirm(
    state: dict,
    messages: list[dict],
    topic: TopicMatch,
    result: TopicMapperResult,
    prelock_count: int,
) -> dict:
    label = _format_topic_label(topic)
    msg = f"It sounds like you mean **{label}** - is that right?"
    messages.append({"role": "tutor", "content": msg, "phase": "tutoring"})
    pending = {
        "kind": "confirm_topic",
        "options": ["Yes", "No"],
        "candidate_label": label,
        "topic_meta": {"__candidate__": _topic_meta(topic, score=result.confidence)},
    }
    return _base_update(
        state, messages, prelock_count,
        topic_options=[],
        topic_question=msg,
        pending_user_choice=pending,
        student_state="question",
    )


def _render_topic_cards(
    state: dict,
    messages: list[dict],
    topics: list[TopicMatch],
    intro: str,
    prelock_count: int,
) -> dict:
    return _topic_card_update(
        state, messages, topics, intro, prelock_count,
        allow_custom=True, mode="normal",
    )


def _render_refuse_cards(
    state: dict,
    *,
    dean: Any,
    messages: list[dict],
    retriever: Any,
    rejected_topic: str,
    refuse_reason: str,
    prelock_count: int,
) -> dict:
    matcher = get_topic_matcher()
    rejected = set(state.get("rejected_topic_paths", []) or [])
    topics = matcher.sample_diverse(NORMAL_CARD_COUNT, min_chunk_count=5, exclude_paths=rejected)
    # M3: when this fires AFTER one or more rejections, the message implies
    # "your prior tries were rejected" — pivot the wording so the student
    # doesn't think the system never understood them at all.
    if rejected:
        intro = (
            "I couldn't find another clear match for that topic. "
            "Try rephrasing more specifically, or browse these:"
        )
    else:
        intro = "I could not find a strong textbook match for that. Pick one of these or type a more specific topic:"
    try:
        refuse = dean._prelock_refuse_call(
            state,
            rejected_topic=rejected_topic,
            failure_count=len(rejected),
            refuse_reason=refuse_reason,
        )
        intro = refuse.get("tutor_reply") or intro
    except Exception:
        pass
    return _topic_card_update(
        state, messages, topics, intro, prelock_count,
        allow_custom=True, mode="normal",
    )


def _render_guided_pick(
    state: dict,
    messages: list[dict],
    retriever: Any,
    query: str,
    prelock_count: int,
) -> dict:
    matcher = get_topic_matcher()
    rejected = set(state.get("rejected_topic_paths", []) or [])
    # M3: rerank against the ORIGINAL query (BM25 via matcher.match) instead of
    # random sample_diverse — picking unrelated chapters at cap-7 is bad UX.
    match_result = matcher.match(query, k=GUIDED_PICK_COUNT * 3)
    topics = [m for m in match_result.matches
              if m.path not in rejected and m.chunk_count >= 8][:GUIDED_PICK_COUNT]
    if not topics:
        # Final safety net: relaxed sample_diverse (chunk_count >= 5) still
        # excluding rejected. Should be rare in practice.
        topics = matcher.sample_diverse(
            GUIDED_PICK_COUNT, min_chunk_count=5, exclude_paths=rejected,
        )
    intro = "Let's choose a starting point so we can begin. Pick one of these focus areas, or end the session."
    return _topic_card_update(
        state, messages, topics, intro, prelock_count,
        allow_custom=False, mode="guided_pick",
        end_session_label="Give up / End session",
        end_session_value=GIVE_UP_VALUE,
    )


def _topic_card_update(
    state: dict,
    messages: list[dict],
    topics: list[TopicMatch],
    intro: str,
    prelock_count: int,
    *,
    allow_custom: bool,
    mode: str,
    end_session_label: Optional[str] = None,
    end_session_value: Optional[str] = None,
) -> dict:
    options, meta = _options_and_meta(topics)
    msg = _with_numbered_options(intro, options)
    messages.append({"role": "tutor", "content": msg, "phase": "tutoring"})
    pending = {
        "kind": "topic",
        "options": options,
        "topic_meta": meta,
        "allow_custom": allow_custom,
        "mode": mode,
    }
    if end_session_label and end_session_value:
        pending["end_session_label"] = end_session_label
        pending["end_session_value"] = end_session_value
    return _base_update(
        state, messages, prelock_count,
        topic_options=options,
        topic_question=intro,
        pending_user_choice=pending,
        student_state="question",
    )


def _give_up(state: dict, messages: list[dict], prelock_count: int) -> dict:
    messages.append({
        "role": "tutor",
        "content": "No problem - we can stop here for now. This session will not be graded because we did not lock a topic.",
        "phase": "memory_update",
        "metadata": {"is_closing": True, "mode": "honest_close", "tone": "neutral"},
    })
    return _base_update(
        state, messages, prelock_count,
        phase="memory_update",
        topic_confirmed=False,
        topic_options=[],
        topic_question="",
        topic_selection="",
        locked_topic=None,
        pending_user_choice={},
        retrieved_chunks=[],
        locked_question="",
        locked_answer="",
        locked_answer_aliases=[],
        full_answer="",
        hint_level=0,
        core_mastery_tier="not_assessed",
        clinical_mastery_tier="not_assessed",
        mastery_tier="not_assessed",
        student_state="question",
    )


def _base_update(state: dict, messages: list[dict], prelock_count: int, **extra: Any) -> dict:
    update = {
        "messages": messages,
        "phase": extra.pop("phase", state.get("phase", "tutoring")),
        "topic_confirmed": extra.pop("topic_confirmed", False),
        "prelock_loop_count": prelock_count,
        "debug": state.get("debug", {}),
    }
    update.update(extra)
    return update


def _topic_from_candidate(path: str) -> Optional[TopicMatch]:
    matcher = get_topic_matcher()
    entries = list(getattr(matcher, "_entries", []) or [])
    raw = (path or "").strip()
    if not raw:
        return None
    raw_norm = _norm(raw)
    for e in entries:
        if _norm(e.path) == raw_norm:
            return e
    parts = [p.strip() for p in raw.split(">")]
    if len(parts) >= 3:
        chapter, section, subsection = parts[-3], parts[-2], parts[-1]
        for e in entries:
            if (
                _norm(e.chapter) == _norm(chapter)
                and _norm(e.section) == _norm(section)
                and _norm(e.subsection) == _norm(subsection)
            ):
                return e
    return None


def _topic_from_pending(pending: dict, label: str) -> Optional[TopicMatch]:
    meta_by_label = pending.get("topic_meta") or {}
    meta = meta_by_label.get(label)
    if meta is None and label == "__candidate__":
        meta = meta_by_label.get("__candidate__")
    if not isinstance(meta, dict):
        return None
    return TopicMatch(
        path=str(meta.get("path", "") or ""),
        chapter=str(meta.get("chapter", "") or ""),
        section=str(meta.get("section", "") or ""),
        subsection=str(meta.get("subsection", "") or ""),
        difficulty=str(meta.get("difficulty", "moderate") or "moderate"),
        chunk_count=int(meta.get("chunk_count", 0) or 0),
        limited=bool(meta.get("limited", False)),
        score=float(meta.get("score", 0.0) or 0.0),
        teachable=bool(meta.get("teachable", True)),
    )


def _topic_meta(topic: TopicMatch, score: Optional[float] = None) -> dict:
    return {
        "path": topic.path,
        "chapter": topic.chapter,
        "section": topic.section,
        "subsection": topic.subsection,
        "difficulty": topic.difficulty,
        "chunk_count": topic.chunk_count,
        "limited": topic.limited,
        "score": topic.score if score is None else score,
        "teachable": topic.teachable,
    }


def _options_and_meta(topics: list[TopicMatch]) -> tuple[list[str], dict[str, dict]]:
    options: list[str] = []
    meta: dict[str, dict] = {}
    seen: set[str] = set()
    for topic in topics:
        label = _format_topic_label(topic)
        if label in seen:
            label = f"{label} ({topic.section})"
        seen.add(label)
        options.append(label)
        meta[label] = _topic_meta(topic)
    return options, meta


def _with_numbered_options(intro: str, options: list[str]) -> str:
    if not options:
        return intro
    numbered = "\n".join(f"  {i + 1}. {opt}" for i, opt in enumerate(options))
    return f"{intro}\n\n{numbered}"


def _match_choice(student_text: str, options: list[str]) -> str:
    txt = _norm(student_text)
    if not txt or not options:
        return ""
    m = re.search(r"(?:#|option\s*)?([1-9])\b", txt)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(options):
            return options[idx - 1]
    ordinals = {
        "first": 1, "1st": 1, "one": 1,
        "second": 2, "2nd": 2, "two": 2,
        "third": 3, "3rd": 3, "three": 3,
        "fourth": 4, "4th": 4, "four": 4,
        "fifth": 5, "5th": 5, "five": 5,
        "sixth": 6, "6th": 6, "six": 6,
        "seventh": 7, "7th": 7, "seven": 7,
        "eighth": 8, "8th": 8, "eight": 8,
        "ninth": 9, "9th": 9, "nine": 9,
    }
    for word, idx in ordinals.items():
        if re.search(rf"\b{re.escape(word)}\b", txt) and 1 <= idx <= len(options):
            return options[idx - 1]
    normalized = [(_norm(o), o) for o in options]
    for norm, original in normalized:
        if txt == norm:
            return original
    hits = []
    for norm, original in normalized:
        if len(txt) >= 8 and txt in norm:
            hits.append(original)
        elif len(norm) >= 8 and norm in txt:
            hits.append(original)
    return hits[0] if len(set(hits)) == 1 else ""


def _is_guided_pending(pending: Any) -> bool:
    return isinstance(pending, dict) and pending.get("kind") == "topic" and pending.get("mode") == "guided_pick"


def _is_give_up(text: str, pending: dict) -> bool:
    val = str(pending.get("end_session_value") or GIVE_UP_VALUE)
    label = str(pending.get("end_session_label") or "")
    txt = _norm(text)
    return txt in {_norm(val), _norm(label), "give up", "end session", "stop", "quit"}


def _is_yes(text: str) -> bool:
    return _norm(text) in {"yes", "y", "yeah", "yep", "correct", "right", "that is right", "sounds right"}


def _is_no(text: str) -> bool:
    return _norm(text) in {"no", "n", "nope", "not really", "wrong", "something else"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9# ]+", " ", (text or "").lower())).strip()
