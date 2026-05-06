"""
tests/test_assessment_v2.py
───────────────────────────
Coverage for the v2 assessment phase orchestrator (Track 4.7e).

Tests the dispatch matrix in conversation.assessment_v2.assessment_node_v2:

  * reached=False                       → reveal-and-close
  * reached=True, assessment_turn=0     → opt-in question rendered
  * reached=True, assessment_turn=1, "yes" → enter clinical phase
  * reached=True, assessment_turn=1, "no"  → reach-and-close (L65)
  * reached=True, assessment_turn=1, ambiguous → re-ask
  * reached=True, assessment_turn=2     → clinical loop turn via run_turn
  * clinical_turn_count > cap           → clinical close (L67)
  * dean_v2 plan failure on opt-in yes  → safe close fallback

All LLM clients are mocked; we never touch the network.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conversation import assessment_v2 as A
from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _state(**overrides) -> dict:
    """Minimal TutorState dict with sensible defaults for assessment tests."""
    base = {
        "messages": [],
        "student_id": "test_student",
        "student_reached_answer": False,
        "assessment_turn": 0,
        "clinical_opt_in": None,
        "clinical_turn_count": 0,
        "clinical_max_turns": A.CLINICAL_TURN_CAP,
        "clinical_completed": False,
        "clinical_history": [],
        "locked_topic": {
            "path": "Ch20|Heart|Coronary Circulation",
            "chapter": "Heart",
            "section": "Heart",
            "subsection": "Coronary Circulation",
        },
        "locked_question": "What artery supplies the left ventricle?",
        "locked_answer": "left anterior descending artery",
        "locked_answer_aliases": ["LAD"],
        "full_answer": "The left anterior descending artery supplies the left ventricle.",
        "retrieved_chunks": [{"text": "Anchor chunk text.", "subsection_title": "Coronary Circulation"}],
        "phase": "assessment",
        "hint_level": 1,
        "turn_count": 5,
        "client_hour": 14,
        "debug": {"turn_trace": [], "all_turn_traces": []},
        "pending_user_choice": {},
    }
    base.update(overrides)
    return base


def _teacher_returns(text: str = "Tutor response.") -> MagicMock:
    teacher = MagicMock()
    teacher.draft.return_value = SimpleNamespace(
        text=text, mode="opt_in", tone="neutral",
        elapsed_ms=12, input_tokens=50, output_tokens=20,
        cache_read_tokens=0, error=None,
    )
    return teacher


def _dean_returns(plan: TurnPlan) -> MagicMock:
    dean = MagicMock()
    dean.plan.return_value = SimpleNamespace(
        turn_plan=plan, elapsed_ms=20, input_tokens=200, output_tokens=80,
        cache_read_tokens=0, raw_response="{}", parse_attempts=1,
        used_fallback=False, error=None,
    )
    return dean


# ─────────────────────────────────────────────────────────────────────────────
# 1. Reveal-and-close (didn't reach)
# ─────────────────────────────────────────────────────────────────────────────


def test_reveal_close_when_not_reached():
    """F8 (POST_DEMO_FIXES.md, 2026-05-06): post-B4/M1, the close
    rendering moved from assessment_v2 to memory_update_node. So
    `_render_reveal_close` no longer appends a tutor message — it just
    sets close_reason + routes to memory_update. Tests now assert the
    routing, not the message text.
    """
    state = _state(student_reached_answer=False)
    teacher = _teacher_returns("The answer was X. Tough one — revisit later.")
    dean = MagicMock()  # not used for reveal path

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=dean, teacher_v2=teacher,
    )

    assert result["phase"] == "memory_update"
    assert result["clinical_opt_in"] is False
    assert result["clinical_mastery_tier"] == "not_assessed"
    assert result["assessment_turn"] == 3
    # close_reason is set so memory_update_node can render the right close.
    assert result["close_reason"] in {"hints_exhausted", "tutoring_cap"}
    # No new tutor message appended — close text comes from memory_update_node.
    assert result["messages"] == state["messages"]
    # Dean should not have been called
    dean.plan.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Opt-in question rendering (assessment_turn=0)
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_in_rendered_when_reached_first_entry():
    state = _state(student_reached_answer=True, assessment_turn=0)
    teacher = _teacher_returns("Want to try a quick clinical question, or wrap up?")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=MagicMock(), teacher_v2=teacher,
    )

    assert result["assessment_turn"] == 1
    assert result["clinical_opt_in"] is None
    assert result["pending_user_choice"]["kind"] == "opt_in"
    assert result["pending_user_choice"]["options"] == ["Yes", "No"]
    msgs = result["messages"]
    assert msgs[-1]["role"] == "tutor"
    assert msgs[-1]["metadata"]["mode"] == "opt_in"
    assert "clinical" in msgs[-1]["content"].lower() or "wrap" in msgs[-1]["content"].lower()
    teacher.draft.assert_called_once()
    plan_arg = teacher.draft.call_args[0][0]
    assert plan_arg.mode == "opt_in"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Opt-in "no" → reach-and-close (L65)
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_in_no_routes_to_reach_close():
    """F8 (POST_DEMO_FIXES.md, 2026-05-06): post-B4/M1, opt-in 'no' path
    routes to memory_update without rendering its own close text — the
    close LLM in memory_update_node owns that.
    """
    state = _state(
        student_reached_answer=True,
        assessment_turn=1,
        messages=[
            {"role": "tutor", "content": "Want to try clinical?"},
            {"role": "student", "content": "no"},
        ],
    )
    teacher = _teacher_returns("Great work — see you next session.")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=MagicMock(), teacher_v2=teacher,
    )

    assert result["phase"] == "memory_update"
    assert result["clinical_opt_in"] is False
    assert result["clinical_mastery_tier"] == "not_assessed"
    assert result["assessment_turn"] == 3
    # close_reason = reach_skipped (student reached but declined clinical bonus)
    assert result["close_reason"] == "reach_skipped"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Opt-in "yes" → enter clinical phase
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_in_yes_enters_clinical_phase():
    state = _state(
        student_reached_answer=True,
        assessment_turn=1,
        messages=[
            {"role": "tutor", "content": "Want to try clinical?"},
            {"role": "student", "content": "yes"},
        ],
    )
    clinical_plan = TurnPlan(
        scenario="clinical_scenario_minted",
        hint_text="",
        mode="clinical",
        tone="neutral",
        forbidden_terms=["LAD"],
        permitted_terms=["coronary"],
        shape_spec={"max_sentences": 4, "exactly_one_question": True},
        carryover_notes="",
        clinical_scenario="A 55yo presents with chest pain. Which artery is most likely involved?",
        clinical_target="LAD or RCA depending on EKG",
        apply_redaction=False,
    )
    dean = _dean_returns(clinical_plan)
    teacher = _teacher_returns("First clinical question rendered.")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=dean, teacher_v2=teacher,
    )

    assert result["assessment_turn"] == 2
    assert result["clinical_opt_in"] is True
    assert result["clinical_turn_count"] == 0
    assert result["clinical_max_turns"] == A.CLINICAL_TURN_CAP
    assert result["phase"] == "assessment"
    msgs = result["messages"]
    assert msgs[-1]["metadata"]["mode"] == "clinical"
    assert msgs[-1]["metadata"]["clinical_scenario"] == clinical_plan.clinical_scenario
    history = result["clinical_history"]
    assert len(history) == 1
    assert history[0]["scenario"] == clinical_plan.clinical_scenario
    dean.plan.assert_called_once()


def test_opt_in_yes_falls_back_when_dean_does_not_emit_clinical():
    """Dean returns a non-clinical plan → fall back to reach-close."""
    state = _state(
        student_reached_answer=True,
        assessment_turn=1,
        messages=[
            {"role": "tutor", "content": "Want to try clinical?"},
            {"role": "student", "content": "yes"},
        ],
    )
    socratic_plan = TurnPlan(
        scenario="dean_drifted",
        hint_text="something",
        mode="socratic",
        tone="neutral",
        forbidden_terms=[],
        permitted_terms=[],
        shape_spec={"max_sentences": 3, "exactly_one_question": True},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )
    dean = _dean_returns(socratic_plan)
    teacher = _teacher_returns("Closing.")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=dean, teacher_v2=teacher,
    )

    assert result["phase"] == "memory_update"
    assert result["clinical_mastery_tier"] == "not_assessed"
    assert result["assessment_turn"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 5. Opt-in ambiguous → re-ask
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_in_ambiguous_reask_keeps_state():
    state = _state(
        student_reached_answer=True,
        assessment_turn=1,
        messages=[
            {"role": "tutor", "content": "Want to try clinical?"},
            {"role": "student", "content": "what does that mean exactly"},
        ],
    )
    teacher = _teacher_returns("Just to confirm — clinical question or wrap up?")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=MagicMock(), teacher_v2=teacher,
    )

    assert result["assessment_turn"] == 1
    assert result["clinical_opt_in"] is None
    assert result["pending_user_choice"]["kind"] == "opt_in"
    msgs = result["messages"]
    assert msgs[-1]["metadata"]["mode"] == "opt_in"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Clinical loop (assessment_turn=2)
# ─────────────────────────────────────────────────────────────────────────────


def test_clinical_loop_runs_turn_via_orchestrator(monkeypatch):
    """assessment_turn=2 should call DeanV2.plan + run_turn.

    F8 (POST_DEMO_FIXES.md, 2026-05-06): student message changed so it
    does NOT contain the locked answer or its aliases — otherwise
    `_clinical_response_hits_locked_target` would short-circuit to
    memory_update with assessment_turn=3, defeating the test intent.

    Also mocks N3's `haiku_intent_classify_unified` (called by
    `_run_clinical_preflight`) so we don't hit Bedrock.
    """
    # N3 — mock the unified classifier called from _run_clinical_preflight
    from conversation import preflight_classifier as PC
    monkeypatch.setattr(
        PC, "haiku_intent_classify_unified",
        lambda msg, **kw: {"verdict": "on_topic_engaged", "evidence": "", "rationale": "test"},
    )
    state = _state(
        student_reached_answer=True,
        assessment_turn=2,
        clinical_opt_in=True,
        clinical_turn_count=1,
        clinical_history=[
            {"turn": 0, "role": "tutor", "scenario": "A 55yo with chest pain..."},
        ],
        messages=[
            {"role": "tutor", "content": "A 55yo with chest pain..."},
            # F8: avoid locked_answer + aliases ("LAD") in student msg.
            {"role": "student", "content": "Maybe ischemia of the anterior wall — what EKG leads should I focus on?"},
        ],
    )
    next_plan = TurnPlan(
        scenario="continue_clinical",
        hint_text="probe ekg pattern",
        mode="clinical",
        tone="encouraging",
        forbidden_terms=[],
        permitted_terms=[],
        shape_spec={"max_sentences": 3, "exactly_one_question": True},
        carryover_notes="",
        clinical_scenario="A 55yo with chest pain...",
        clinical_target="LAD or RCA",
        apply_redaction=False,
    )
    dean = _dean_returns(next_plan)
    teacher = _teacher_returns("Good — what EKG leads would localize this?")

    # Stub out run_turn so we don't hit the real Haiku quartet.
    fake_result = SimpleNamespace(
        final_text="Good — what EKG leads would localize this?",
        final_attempt=1,
        used_safe_generic_probe=False,
        used_dean_replan=False,
        leak_cap_fallback_fired=False,
        timed_out=False,
        elapsed_ms=100,
        attempts=[],
        final_turn_plan=next_plan,
    )
    monkeypatch.setattr(A, "run_turn", lambda **kwargs: fake_result)

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=dean, teacher_v2=teacher,
    )

    assert result["assessment_turn"] == 2
    assert result["clinical_opt_in"] is True
    assert result["clinical_turn_count"] == 2  # incremented from 1
    assert result["phase"] == "assessment"
    msgs = result["messages"]
    assert msgs[-1]["metadata"]["mode"] == "clinical"
    assert "EKG" in msgs[-1]["content"]
    dean.plan.assert_called_once()


def test_clinical_loop_caps_at_max_turns():
    """Per L67, clinical_turn_count > CLINICAL_TURN_CAP triggers clinical
    close.

    F8 (POST_DEMO_FIXES.md, 2026-05-06): renamed from `_caps_at_seven_turns`
    after N3 bumped the cap 7 → 15. Test now references the constant
    so it tracks future cap moves. Also: post-B4/M1 the cap-close routes
    via `_render_clinical_close` which calls memory_update_node — the
    closing tutor text is no longer rendered here.
    """
    state = _state(
        student_reached_answer=True,
        assessment_turn=2,
        clinical_opt_in=True,
        clinical_turn_count=A.CLINICAL_TURN_CAP,  # next bump triggers cap
        messages=[
            {"role": "tutor", "content": "Last clinical Q"},
            {"role": "student", "content": "uh I dunno"},
        ],
    )
    teacher = _teacher_returns("Nice work — let's wrap.")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=MagicMock(), teacher_v2=teacher,
    )

    assert result["phase"] == "memory_update"
    assert result["assessment_turn"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 7. _classify_opt_in primitive — string parsing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg,expected", [
    ("yes", "yes"),
    ("Yes", "yes"),
    ("y", "yes"),
    ("yeah", "yes"),
    ("sure", "yes"),
    ("ok", "yes"),
    ("no", "no"),
    ("No", "no"),
    ("n", "no"),
    ("nope", "no"),
    ("skip", "no"),
    ("pass", "no"),
    ("", "ambiguous"),
    ("what is this", "ambiguous"),
    ("I have a question about anatomy instead", "ambiguous"),  # legacy ≥6-word implicit-yes is dropped per L73
])
def test_classify_opt_in_primitive(msg, expected):
    assert A._classify_opt_in(msg) == expected


# ─────────────────────────────────────────────────────────────────────────────
# 8. Teacher draft error → deterministic fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_in_when_teacher_errors_routes_with_empty_content():
    """F8 (POST_DEMO_FIXES.md, 2026-05-06): per the M-FB rule
    ('no templated tutor-text fallback'), `_safe_teacher_draft` now
    returns "" when teacher.draft raises — the frontend renders an
    error card, NOT a fake tutor reply. The routing (assessment_turn=1
    + pending_user_choice='opt_in') still completes; only the message
    content is empty.
    """
    state = _state(student_reached_answer=True, assessment_turn=0)
    teacher = MagicMock()
    teacher.draft.side_effect = RuntimeError("network down")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=MagicMock(), teacher_v2=teacher,
    )

    # Still routes to opt-in turn even though teacher errored.
    assert result["assessment_turn"] == 1
    assert result["pending_user_choice"]["kind"] == "opt_in"
    msgs = result["messages"]
    assert msgs[-1]["role"] == "tutor"
    assert msgs[-1]["metadata"]["mode"] == "opt_in"
    # M-FB: no templated fallback text on LLM failure — content is empty
    # so the frontend renders an error card.
    assert msgs[-1]["content"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# 9. Reveal close uses honest tone + reveals locked answer in fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_reveal_close_does_not_render_text_in_assessment_v2():
    """F8 (POST_DEMO_FIXES.md, 2026-05-06): post-B4/M1 the close text is
    rendered by memory_update_node, not assessment_v2. The reveal-close
    path here just sets close_reason + locks the locked_answer for the
    downstream close LLM to surface. Renamed from
    `test_reveal_close_fallback_includes_locked_answer` because the
    'fallback' concept doesn't apply — assessment_v2 never calls
    teacher.draft on this path.
    """
    state = _state(student_reached_answer=False)
    teacher = MagicMock()
    teacher.draft.side_effect = RuntimeError("teacher down")

    result = A.assessment_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(),
        retriever=MagicMock(), dean_v2=MagicMock(), teacher_v2=teacher,
    )

    # No tutor message appended on this path — memory_update_node owns it.
    assert result["messages"] == state["messages"]
    # close_reason + tier set so memory_update can produce the right close.
    assert result["close_reason"] in {"hints_exhausted", "tutoring_cap"}
    assert result["clinical_mastery_tier"] == "not_assessed"
    # teacher.draft was never called (per the new no-render contract).
    teacher.draft.assert_not_called()
