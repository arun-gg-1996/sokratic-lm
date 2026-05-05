from __future__ import annotations

from unittest.mock import MagicMock

from conversation import topic_lock_v2 as T
from conversation.lifecycle_v2 import after_dean
from retrieval.topic_mapper_llm import TopicMapperResult, TopicMatchCandidate
from retrieval.topic_matcher import TopicMatch


def _topic(label: str = "Conduction System of the Heart", idx: int = 1) -> TopicMatch:
    return TopicMatch(
        path=f"Chapter {idx}: The Cardiovascular System > Cardiac Muscle > {label}",
        chapter="The Cardiovascular System",
        section="Cardiac Muscle",
        subsection=label,
        difficulty="moderate",
        chunk_count=12,
        limited=False,
        score=0.92,
        teachable=True,
    )


def _state(**overrides):
    base = {
        "student_id": "alice",
        "thread_id": "alice_t1",
        "phase": "tutoring",
        "messages": [
            {"role": "tutor", "content": "What would you like to study?"},
            {"role": "student", "content": "SA node"},
        ],
        "topic_confirmed": False,
        "topic_options": [],
        "topic_question": "",
        "topic_selection": "",
        "locked_topic": None,
        "pending_user_choice": {},
        "prelock_loop_count": 0,
        "retrieved_chunks": [],
        "locked_question": "",
        "locked_answer": "",
        "locked_answer_aliases": [],
        "full_answer": "",
        "hint_level": 0,
        "max_hints": 3,
        "turn_count": 0,
        "max_turns": 25,
        "student_reached_answer": False,
        "debug": {"turn_trace": [], "retrieval_calls": 0},
    }
    base.update(overrides)
    return base


class FakeMatcher:
    def __init__(self, topics):
        self._entries = topics

    def sample_diverse(self, n=3, seed=None, min_chunk_count=5, exclude_paths=None):
        return self._entries[:n]

    def sample_related(self, retriever, query, n=3, min_chunk_count=3, exclude_paths=None):
        return self._entries[:n]


class FakeDean:
    def __init__(self):
        self.retriever = MagicMock()

    def _retrieve_on_topic_lock(self, state):
        state["retrieved_chunks"] = [
            {"score": 1.0, "subsection_title": state["locked_topic"]["subsection"], "text": "The SA node initiates the heartbeat."}
        ]
        state["debug"]["retrieval_calls"] = 1

    def _lock_anchors_call(self, state):
        return {
            "locked_question": "What initiates the heartbeat?",
            "locked_answer": "SA node",
            "locked_answer_aliases": ["sinoatrial node"],
            "full_answer": "The sinoatrial node initiates the heartbeat.",
        }

    def _build_topic_ack_message(self, state):
        return f"Got it - let's work on **{state['locked_topic']['subsection']}**.\n\n{state['locked_question']}"

    def _prelock_refuse_call(self, *args, **kwargs):
        return {"tutor_reply": "Pick a covered topic:"}


def _mapper_result(verdict: str, confidence: float, topic: TopicMatch | None = None):
    matches = []
    if topic is not None:
        matches.append(TopicMatchCandidate(
            path=f"{topic.chapter} > {topic.section} > {topic.subsection}",
            confidence=confidence,
            rationale="match",
        ))
    return TopicMapperResult(
        query="SA node",
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        student_intent="topic_request",
        deferred_question=None,
        top_matches=matches,
    )


def test_l9_strong_locks_topic_and_resets_prelock_counter(monkeypatch):
    topic = _topic()
    monkeypatch.setattr(T, "get_topic_matcher", lambda: FakeMatcher([topic]))
    monkeypatch.setattr(T, "_map_topic", lambda query, trace: _mapper_result("strong", 0.93, topic))

    result = T.run_topic_lock_v2(
        _state(), dean=FakeDean(), retriever=MagicMock(), latest_student="SA node"
    )

    assert result["topic_confirmed"] is True
    assert result["prelock_loop_count"] == 0
    assert result["locked_answer"] == "SA node"
    assert "What initiates the heartbeat?" in result["messages"][-1]["content"]


def test_none_route_increments_prelock_and_surfaces_cards(monkeypatch):
    topics = [_topic("Aorta", 1), _topic("Pulmonary Circulation", 2), _topic("Cardiac Cycle", 3)]
    monkeypatch.setattr(T, "get_topic_matcher", lambda: FakeMatcher(topics))
    monkeypatch.setattr(T, "_map_topic", lambda query, trace: _mapper_result("none", 0.0, None))

    result = T.run_topic_lock_v2(
        _state(), dean=FakeDean(), retriever=MagicMock(), latest_student="pizza"
    )

    assert result["topic_confirmed"] is False
    assert result["prelock_loop_count"] == 1
    assert result["pending_user_choice"]["kind"] == "topic"
    assert result["topic_options"] == ["Aorta", "Pulmonary Circulation", "Cardiac Cycle"]


def test_borderline_high_confirm_yes_locks(monkeypatch):
    topic = _topic()
    matcher = FakeMatcher([topic])
    monkeypatch.setattr(T, "get_topic_matcher", lambda: matcher)
    monkeypatch.setattr(T, "_map_topic", lambda query, trace: _mapper_result("borderline", 0.8, topic))

    first = T.run_topic_lock_v2(
        _state(), dean=FakeDean(), retriever=MagicMock(), latest_student="conduction"
    )
    assert first["pending_user_choice"]["kind"] == "confirm_topic"

    second_state = _state(
        pending_user_choice=first["pending_user_choice"],
        messages=first["messages"] + [{"role": "student", "content": "Yes"}],
        prelock_loop_count=first["prelock_loop_count"],
    )
    second = T.run_topic_lock_v2(
        second_state, dean=FakeDean(), retriever=MagicMock(), latest_student="Yes"
    )
    assert second["topic_confirmed"] is True
    assert second["prelock_loop_count"] == 0


def test_borderline_high_confirm_no_reprompts_without_lock(monkeypatch):
    topic = _topic()
    monkeypatch.setattr(T, "get_topic_matcher", lambda: FakeMatcher([topic]))
    monkeypatch.setattr(T, "_map_topic", lambda query, trace: _mapper_result("borderline", 0.8, topic))

    first = T.run_topic_lock_v2(
        _state(), dean=FakeDean(), retriever=MagicMock(), latest_student="conduction"
    )
    second_state = _state(
        pending_user_choice=first["pending_user_choice"],
        messages=first["messages"] + [{"role": "student", "content": "No"}],
        prelock_loop_count=first["prelock_loop_count"],
    )
    second = T.run_topic_lock_v2(
        second_state, dean=FakeDean(), retriever=MagicMock(), latest_student="No"
    )

    assert second["topic_confirmed"] is False
    assert second["pending_user_choice"] == {}
    assert "what topic" in second["messages"][-1]["content"].lower()


def test_cap_7_renders_guided_pick_without_custom_escape(monkeypatch):
    topics = [_topic(f"Topic {i}", i) for i in range(1, 7)]
    monkeypatch.setattr(T, "get_topic_matcher", lambda: FakeMatcher(topics))

    result = T.run_topic_lock_v2(
        _state(prelock_loop_count=6),
        dean=FakeDean(),
        retriever=MagicMock(),
        latest_student="still vague",
    )

    pending = result["pending_user_choice"]
    assert result["prelock_loop_count"] == 7
    assert pending["mode"] == "guided_pick"
    assert pending["allow_custom"] is False
    assert pending["end_session_label"] == "Give up / End session"
    assert len(result["topic_options"]) == 6


def test_cap_7_still_honors_existing_card_pick(monkeypatch):
    topic = _topic()
    _, meta = T._options_and_meta([topic])
    pending = {
        "kind": "topic",
        "options": list(meta.keys()),
        "topic_meta": meta,
        "allow_custom": True,
        "mode": "normal",
    }

    result = T.run_topic_lock_v2(
        _state(prelock_loop_count=6, pending_user_choice=pending),
        dean=FakeDean(),
        retriever=MagicMock(),
        latest_student="1",
    )

    assert result["topic_confirmed"] is True
    assert result["prelock_loop_count"] == 0


def test_guided_pick_give_up_routes_to_memory_update(monkeypatch):
    topics = [_topic(f"Topic {i}", i) for i in range(1, 7)]
    _, meta = T._options_and_meta(topics)
    pending = {
        "kind": "topic",
        "mode": "guided_pick",
        "options": list(meta.keys()),
        "topic_meta": meta,
        "allow_custom": False,
        "end_session_label": "Give up / End session",
        "end_session_value": T.GIVE_UP_VALUE,
    }

    result = T.run_topic_lock_v2(
        _state(prelock_loop_count=7, pending_user_choice=pending),
        dean=FakeDean(),
        retriever=MagicMock(),
        latest_student=T.GIVE_UP_VALUE,
    )

    assert result["phase"] == "memory_update"
    assert result["pending_user_choice"] == {}
    assert result["messages"][-1]["metadata"]["is_closing"] is True


def test_after_dean_routes_memory_update_phase():
    state = _state(phase="memory_update")
    assert after_dean(state) == "memory_update_node"
