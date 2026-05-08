"""
tests/test_preflight.py
───────────────────────
Tests for conversation/preflight.py — the pre-flight Haiku layer
(L44 + L45 + L55 + L56 + L58, Track 4.3).

Coverage:
  * haiku_help_abuse_check + haiku_deflection_check (NEW classifiers)
    — happy paths, error paths, evidence validation
  * run_preflight: all 3 pass → fired=False, Dean runs
  * run_preflight: each check fires individually → correct category +
    suggested_mode + suggested_tone + counter updates
  * Dispatch priority: deflection > off_domain > help_abuse > none
  * L55 strike-4 → should_force_hint_advance=True
  * L56 strike-4 → should_end_session=True + suggested_mode=honest_close
  * Tone escalation per strike count
  * Counter reset semantics on legitimate engagement
"""
from __future__ import annotations

import json

import pytest

from conversation import classifiers as C
from conversation import preflight as P

# ─────────────────────────────────────────────────────────────────────────────
# Shared mock-Haiku fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_haiku(monkeypatch):
    """Configure both classifier module _haiku_call AND the unified-intent
    classifier _haiku_call to return canned responses. Each test sets
    verdicts via .set_verdict().

    updated to also patch the M7
    unified intent classifier (`conversation.preflight_classifier`).
    `run_preflight` now calls the unified path; tests that run through
    `run_preflight` need a mock for the unified _haiku_call too.
    """
    state = {
        "help_abuse": "legitimate_engagement",
        "off_domain": "in_domain",
        "deflection": "continuing",
        # M7 unified verdict — set this in tests that exercise
        # run_preflight end-to-end. Falls back to on_topic_engaged.
        "unified": "on_topic_engaged",
        "raise_exc": None,
    }

    def _classify_system(blocks):
        text = blocks[0]["text"] if blocks else ""
        # M7 unified system prompt has the distinctive header.
        if "intent classifier for a Socratic tutoring system" in text:
            return "unified"
        if "HELP-ABUSE patterns" in text:
            return "help_abuse"
        if "DEFLECTION patterns" in text:
            return "deflection"
        if "DOMAIN" in text or "domain" in text:
            return "off_domain"
        return "unknown"

    def fake(system_blocks, user_text):
        if state["raise_exc"]:
            raise state["raise_exc"]
        which = _classify_system(system_blocks)
        # When the unified system fires, route help_abuse / off_domain /
        # deflection set_verdict() calls through the `unified` slot for
        # maximum back-compat with existing tests. If a test set
        # `unified=` explicitly, that wins.
        if which == "unified":
            verdict = state.get("unified")
            if verdict == "on_topic_engaged":
                # If the legacy keys signal a fail (e.g. test set
                # help_abuse="help_abuse"), promote that to the unified
                # verdict so existing tests work without rewrites.
                for legacy_key in ("help_abuse", "deflection"):
                    if state.get(legacy_key) not in (None, "", "legitimate_engagement", "continuing"):
                        verdict = state[legacy_key]
                        break
                if verdict == "on_topic_engaged" and state.get("off_domain") not in (None, "", "in_domain"):
                    verdict = "off_domain"
        else:
            verdict = state.get(which, "unknown")
        # Use the actual student message content as evidence so
        # _validate_evidence passes. user_text contains either
        # "STUDENT MESSAGE:" (legacy) or "STUDENT'S LATEST MESSAGE:"
        # (M7 unified) — handle both.
        marker = (
            "STUDENT'S LATEST MESSAGE:" if "STUDENT'S LATEST MESSAGE:" in user_text
            else "STUDENT MESSAGE:"
        )
        suffix = user_text.split(marker)[-1] if marker in user_text else "test"
        first_line = ""
        for line in suffix.splitlines():
            if line.strip():
                first_line = line.strip()
                break
        evidence = first_line[:40]
        is_fail = (
            (which == "help_abuse" and verdict == "help_abuse") or
            (which == "deflection" and verdict == "deflection") or
            (which == "off_domain" and verdict in ("off_domain", "substance",
                                                    "chitchat", "jailbreak",
                                                    "answer_demand")) or
            (which == "unified" and verdict in ("help_abuse", "deflection",
                                                 "off_domain", "low_effort"))
        )
        return json.dumps({
            "verdict": verdict,
            "evidence": evidence if is_fail else "",
            "rationale": "test",
        })

    monkeypatch.setattr(C, "_haiku_call", fake)
    # F8: also patch the unified-classifier module's _haiku_call.
    from conversation import preflight_classifier as PC
    monkeypatch.setattr(PC, "_haiku_call", fake)

    class Setter:
        def set_verdict(self, **kwargs):
            state.update(kwargs)

        def raise_exception(self, exc):
            state["raise_exc"] = exc

    return Setter()

# ─────────────────────────────────────────────────────────────────────────────
# haiku_help_abuse_check
# ─────────────────────────────────────────────────────────────────────────────

def test_help_abuse_check_pass_for_engagement(mock_haiku):
    mock_haiku.set_verdict(help_abuse="legitimate_engagement")
    out = P.haiku_help_abuse_check("maybe the SA node?")
    assert out["verdict"] == "legitimate_engagement"

def test_help_abuse_check_catches_demand(mock_haiku):
    mock_haiku.set_verdict(help_abuse="help_abuse")
    out = P.haiku_help_abuse_check("just tell me the answer please")
    assert out["verdict"] == "help_abuse"

def test_help_abuse_check_handles_empty():
    out = P.haiku_help_abuse_check("")
    assert out["verdict"] == "legitimate_engagement"

def test_help_abuse_check_handles_haiku_error(mock_haiku):
    mock_haiku.raise_exception(RuntimeError("network down"))
    out = P.haiku_help_abuse_check("idk")
    # Safe default → legitimate_engagement (don't false-fire)
    assert out["verdict"] == "legitimate_engagement"
    assert "haiku_error" in out["_error"]

def test_help_abuse_evidence_validation(monkeypatch):
    """Hallucinated evidence must downgrade to legitimate_engagement."""
    bad_payload = json.dumps({
        "verdict": "help_abuse",
        "evidence": "completely_fake_phrase_not_in_message",
        "rationale": "x",
    })
    monkeypatch.setattr(C, "_haiku_call", lambda b, u: bad_payload)
    out = P.haiku_help_abuse_check("real student message")
    assert out["verdict"] == "legitimate_engagement"
    assert out["_error"] == "evidence_invalid"

# ─────────────────────────────────────────────────────────────────────────────
# haiku_deflection_check
# ─────────────────────────────────────────────────────────────────────────────

def test_deflection_check_pass_for_continuing(mock_haiku):
    mock_haiku.set_verdict(deflection="continuing")
    out = P.haiku_deflection_check("this is hard")
    assert out["verdict"] == "continuing"

def test_deflection_check_catches_explicit_end(mock_haiku):
    mock_haiku.set_verdict(deflection="deflection")
    out = P.haiku_deflection_check("let's stop here")
    assert out["verdict"] == "deflection"

def test_deflection_check_handles_empty():
    out = P.haiku_deflection_check("")
    assert out["verdict"] == "continuing"

# ─────────────────────────────────────────────────────────────────────────────
# run_preflight — orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _state(help_count=0, off_count=0):
    return {"help_abuse_count": help_count, "off_topic_count": off_count}

def test_preflight_all_pass_runs_dean(mock_haiku):
    """All 3 checks return clean → fired=False, Dean runs.

    M7 unified classifier surfaces
    the actual verdict (`on_topic_engaged`) instead of legacy `"none"`.
    Test asserts the new shape.
    """
    mock_haiku.set_verdict(
        help_abuse="legitimate_engagement",
        off_domain="in_domain",
        deflection="continuing",
    )
    out = P.run_preflight(_state(), "maybe the supraspinatus?", parallel=False)
    assert out.fired is False
    assert out.category == "on_topic_engaged"  # M7 surfaces the verdict
    assert out.new_help_abuse_count == 0  # reset on engagement
    assert out.suggested_mode == ""

def test_preflight_help_abuse_fires(mock_haiku):
    mock_haiku.set_verdict(help_abuse="help_abuse")
    out = P.run_preflight(_state(), "just tell me", parallel=False)
    assert out.fired is True
    assert out.category == "help_abuse"
    assert out.suggested_mode == "redirect"
    assert out.suggested_tone == "neutral"  # strike 1
    assert out.new_help_abuse_count == 1
    assert out.should_force_hint_advance is False

def test_preflight_help_abuse_strike_4_forces_hint_advance(mock_haiku):
    """L55: at strike 4, force hint advance.

    2026-05-05: counter now resets to 0 after firing so the student gets a
    fresh warning chain before the next hint-advance (was: counter kept
    climbing past threshold, surfacing 'Help-abuse: 5/4' in the UI and
    burning the 3-hint allotment in 3 consecutive strikes).
    """
    # 2026-05-05: post-D3 unified classifier doesn't honor the legacy
    # mock_haiku fixture (which targets per-classifier paths). Use a real
    # help-abuse phrase so the unified classifier itself returns help_abuse.
    mock_haiku.set_verdict(help_abuse="help_abuse")
    out = P.run_preflight(_state(help_count=3), "just give me the answer", parallel=False)
    assert out.new_help_abuse_count == 0  # was 4; resets after fire
    assert out.should_force_hint_advance is True

def test_preflight_off_domain_fires(mock_haiku):
    mock_haiku.set_verdict(off_domain="off_domain")
    out = P.run_preflight(_state(), "what's your favorite movie", parallel=False)
    assert out.fired is True
    assert out.category == "off_domain"
    assert out.suggested_mode == "nudge"
    assert out.new_off_topic_count == 1
    assert out.should_end_session is False

def test_preflight_off_domain_tone_escalates(mock_haiku):
    """neutral at strike 1, firm at strike 2-3, honest at strike 4."""
    mock_haiku.set_verdict(off_domain="off_domain")
    s1 = P.run_preflight(_state(off_count=0), "x", parallel=False)
    s2 = P.run_preflight(_state(off_count=1), "x", parallel=False)
    s3 = P.run_preflight(_state(off_count=2), "x", parallel=False)
    s4 = P.run_preflight(_state(off_count=3), "x", parallel=False)
    assert s1.suggested_tone == "neutral"
    assert s2.suggested_tone == "firm"
    assert s3.suggested_tone == "firm"
    assert s4.suggested_tone == "honest"

def test_preflight_off_domain_strike_4_ends_session(mock_haiku):
    """L56: at strike 4, graceful end + honest_close mode."""
    mock_haiku.set_verdict(off_domain="off_domain")
    out = P.run_preflight(_state(off_count=3), "movies", parallel=False)
    assert out.new_off_topic_count == 4
    assert out.should_end_session is True
    assert out.suggested_mode == "honest_close"
    assert out.suggested_tone == "honest"

def test_preflight_deflection_fires(mock_haiku):
    mock_haiku.set_verdict(deflection="deflection")
    out = P.run_preflight(_state(), "let's stop", parallel=False)
    assert out.fired is True
    assert out.category == "deflection"
    assert out.suggested_mode == "confirm_end"
    assert out.suggested_tone == "neutral"
    # No counter for deflection assert out.new_help_abuse_count == 0
    assert out.new_off_topic_count == 0

def test_preflight_deflection_priority_over_off_domain(mock_haiku):
    """When BOTH off_domain + deflection fire, deflection wins."""
    mock_haiku.set_verdict(off_domain="off_domain", deflection="deflection")
    out = P.run_preflight(_state(off_count=2), "let's stop", parallel=False)
    assert out.category == "deflection"
    # Off-domain counter NOT advanced
    assert out.new_off_topic_count == 2

@pytest.mark.skip(
    reason="M7 unified classifier "
    "picks ONE verdict — there's no longer a code-side priority gate "
    "between off_domain / help_abuse / deflection. Priority semantics "
    "(if any) live in the unified prompt itself; needs an integration "
    "test against the real LLM, not a mocked unit test."
)
def test_preflight_deflection_priority_over_help_abuse(mock_haiku):
    mock_haiku.set_verdict(help_abuse="help_abuse", deflection="deflection")
    out = P.run_preflight(_state(help_count=2), "ok stop", parallel=False)
    assert out.category == "deflection"
    assert out.new_help_abuse_count == 2  # not advanced

@pytest.mark.skip(reason="F8 — see test_preflight_deflection_priority_over_help_abuse")
def test_preflight_off_domain_priority_over_help_abuse(mock_haiku):
    """off_domain has session-end consequences → priority over help_abuse."""
    mock_haiku.set_verdict(help_abuse="help_abuse", off_domain="off_domain")
    out = P.run_preflight(_state(help_count=2), "movies", parallel=False)
    assert out.category == "off_domain"
    assert out.new_off_topic_count == 1
    # help_abuse counter reset on off_domain detection (avoid double-penalty)
    assert out.new_help_abuse_count == 2  # left unchanged (only no-fire path resets)

def test_preflight_engagement_resets_help_abuse_counter(mock_haiku):
    """L55: any non-help-abuse engagement resets the help_abuse counter."""
    mock_haiku.set_verdict(help_abuse="legitimate_engagement",
                            off_domain="in_domain", deflection="continuing")
    out = P.run_preflight(_state(help_count=3), "maybe the SA node?", parallel=False)
    assert out.fired is False
    assert out.new_help_abuse_count == 0  # reset

def test_preflight_off_domain_counter_decays_across_clean_turns(mock_haiku):
    """M7 strike decay — off_topic_count
    DECREMENTS by 1 on each engaged turn (preflight.py:523:
    `new_off = max(0, off_count - 1)`). Was previously preserved (L56).
    Decay prevents a single old misclassification from accumulating to
    strike 4 across many clean turns.
    """
    mock_haiku.set_verdict(help_abuse="legitimate_engagement",
                            off_domain="in_domain", deflection="continuing")
    out = P.run_preflight(_state(off_count=2), "x", parallel=False)
    assert out.new_off_topic_count == 1  # decayed by 1

def test_preflight_parallel_mode_works(mock_haiku):
    """Default parallel=True path uses ThreadPoolExecutor — same logic
    applies but executes concurrently. Sanity check that it doesn't
    crash + returns same shape."""
    mock_haiku.set_verdict(help_abuse="help_abuse")
    out = P.run_preflight(_state(), "idk", parallel=True)
    assert out.fired is True
    assert out.category == "help_abuse"
    assert out.elapsed_s >= 0

def test_preflight_checks_dict_carries_raw_results(mock_haiku):
    """Trace consumers can pull per-check raw results from .checks."""
    mock_haiku.set_verdict(help_abuse="help_abuse")
    out = P.run_preflight(_state(), "just tell me", parallel=False)
    assert "help_abuse" in out.checks
    assert "off_domain" in out.checks
    assert "deflection" in out.checks
    assert out.checks["help_abuse"]["verdict"] == "help_abuse"

# ─────────────────────────────────────────────────────────────────────────────
# Threshold constants
# ─────────────────────────────────────────────────────────────────────────────

def test_help_abuse_threshold_is_4_per_l55():
    assert P.HELP_ABUSE_HINT_ADVANCE_STRIKE == 4

def test_off_topic_threshold_is_4_per_l56():
    assert P.OFF_TOPIC_END_SESSION_STRIKE == 4
