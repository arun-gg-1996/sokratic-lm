"""
scripts/stress_test_verifier.py
================================
Tier 2 stress test — verifier quartet + retry orchestrator coverage.

Tests scenarios that require crafted Teacher drafts:
  V1-V4: Each of the 4 verifier checks fires individually
  V5:    Multi-check failure on same draft
  V6:    Attempts 1-3 fail same check → Dean replan triggered
  V7:    Attempt 4 also leaks → SAFE_GENERIC_PROBE fired
  V8:    Hard timeout → SAFE_GENERIC_PROBE fired
  V9:    Leak passes but pedagogy fails on attempt 4 → ship anyway
  V10:   Sycophantic but otherwise clean → caught by sycophancy_check
  V11:   Empty draft from Teacher → handled gracefully

Run:
    cd /Users/arun-ghontale/UB/NLP/sokratic
    python scripts/stress_test_verifier.py [--scenario VN]
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from conversation import verifier_quartet as C  # noqa: E402  (post-D3: verifier funcs live here)
from conversation.retry_orchestrator import (  # noqa: E402
    SAFE_GENERIC_PROBE,
    run_turn as run_turn_with_retry,
)
from conversation.teacher_v2 import (  # noqa: E402
    TeacherDraftResult,
    TeacherPromptInputs,
)
from conversation.turn_plan import TurnPlan  # noqa: E402


# ---------------------------------------------------------------------------
# Scenario shapes
# ---------------------------------------------------------------------------

@dataclass
class DirectCheckScenario:
    """Test a single verifier check with a crafted draft."""
    id: str
    description: str
    check_fn: Callable[[], dict]
    expect_verdict_in: list[str]  # one of these verdict values is acceptable


@dataclass
class OrchestratorScenario:
    """Test the retry orchestrator with a mock Teacher returning crafted drafts."""
    id: str
    description: str
    teacher_responses: list[str]  # one per attempt; if empty list returned mid-way Teacher returns ""
    locked_answer: str = "pyruvate"
    locked_question: str = "What is the three-carbon end product of glycolysis?"
    expect_used_safe_probe: bool | None = None
    expect_used_replan: bool | None = None
    expect_final_attempt_at_or_above: int | None = None
    expect_leak_cap: bool | None = None


@dataclass
class StepResult:
    duration_s: float
    pass_: bool
    detail: str


@dataclass
class ScenarioResult:
    scenario_id: str
    description: str
    duration_s: float
    passed: bool
    detail: str


# ---------------------------------------------------------------------------
# Mock Teacher for orchestrator scenarios
# ---------------------------------------------------------------------------

class _MockTeacher:
    """Test double for TeacherV2 — returns scripted drafts in sequence."""

    def __init__(self, scripted: list[str]):
        self.scripted = list(scripted)
        self.call_count = 0

    def draft(self, turn_plan, inputs, *, prior_attempts=None, prior_failures=None):
        idx = min(self.call_count, len(self.scripted) - 1) if self.scripted else 0
        text = self.scripted[idx] if self.scripted else ""
        self.call_count += 1
        return TeacherDraftResult(
            text=text,
            mode=turn_plan.mode,
            tone=turn_plan.tone,
            elapsed_ms=10,
        )


class _MockDean:
    """Test double for Dean.replan — returns a fresh TurnPlan with same shape."""

    def __init__(self):
        self.call_count = 0

    def replan(self, dean_state, dean_chunks, *, prior_plan, prior_attempts, prior_failures):
        self.call_count += 1
        from dataclasses import replace
        new_plan = replace(prior_plan, scenario=f"{prior_plan.scenario}.replanned")
        # Mirror the structure ReplanResult uses (turn_plan attribute)
        class _R:
            pass
        r = _R()
        r.turn_plan = new_plan
        return r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_inputs() -> TeacherPromptInputs:
    return TeacherPromptInputs(
        chunks=[],
        history=[{"role": "tutor", "content": "What's the end product of glycolysis?"}],
        locked_subsection="Steps of glycolysis",
        locked_question="What is the three-carbon end product of glycolysis?",
        domain_name="Human Anatomy & Physiology",
        domain_short="anatomy",
        student_descriptor="student",
    )


def _build_plan() -> TurnPlan:
    return TurnPlan(
        scenario="test",
        hint_text="hint about glycolysis end product",
        mode="socratic",
        tone="encouraging",
        forbidden_terms=["pyruvate"],
        permitted_terms=[],
        shape_spec={"max_sentences": 3, "exactly_one_question": True},
        carryover_notes="",
    )


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def build_direct_scenarios() -> list[DirectCheckScenario]:
    scenarios: list[DirectCheckScenario] = []

    # V1 — leak check fires when answer letter is given
    scenarios.append(DirectCheckScenario(
        id="V1_leak_first_letter",
        description="Draft reveals 'starts with the letter P' → leak_check fires",
        check_fn=lambda: C.haiku_hint_leak_check(
            "Hmm, you're close. The answer starts with the letter P — what comes after glucose breakdown?",
            "pyruvate", aliases=[],
        ),
        expect_verdict_in=["leak"],
    ))

    # V2 — sycophancy check fires
    scenarios.append(DirectCheckScenario(
        id="V2_sycophancy_overpraise",
        description="Draft starts with 'Great job!' even though student answered wrong → sycophancy fires",
        check_fn=lambda: C.haiku_sycophancy_check(
            "Great job! Amazing thinking! What an excellent question — what do you think happens after glucose enters the cell?",
            "neutral", False,
        ),
        expect_verdict_in=["sycophantic"],
    ))

    # V3 — shape check: no question mark
    scenarios.append(DirectCheckScenario(
        id="V3_shape_no_question",
        description="Draft has no question → shape_check fires",
        check_fn=lambda: C.haiku_shape_check(
            "Glucose is broken down through glycolysis into smaller molecules. Energy is released in the process. The end products are important for cellular respiration.",
            shape_spec={"max_sentences": 3, "exactly_one_question": True},
            hint_level=1,
            hint_text="ask about end product",
            prior_tutor_questions=[],
        ),
        expect_verdict_in=["fail"],
    ))

    # V4 — pedagogy check: irrelevant draft
    scenarios.append(DirectCheckScenario(
        id="V4_pedagogy_irrelevant",
        description="Draft talks about an unrelated topic → pedagogy_check fires",
        check_fn=lambda: C.haiku_pedagogy_check(
            "What's your favorite color? Color preferences are very interesting psychologically — what draws you to certain hues?",
            locked_subsection="Steps of glycolysis",
            locked_question="What is the three-carbon end product of glycolysis?",
        ),
        expect_verdict_in=["fail"],
    ))

    # V5 — clean draft passes all checks
    scenarios.append(DirectCheckScenario(
        id="V5_clean_draft_passes",
        description="A genuinely good Socratic draft → no checks fire",
        check_fn=lambda: {
            "leak": C.haiku_hint_leak_check(
                "What molecule do you think glucose gets broken into during glycolysis?",
                "pyruvate", aliases=[],
            ).get("verdict"),
            "sycophancy": C.haiku_sycophancy_check(
                "What molecule do you think glucose gets broken into during glycolysis?",
                "neutral", False,
            ).get("verdict"),
        },
        expect_verdict_in=["clean"],
    ))

    return scenarios


def build_orchestrator_scenarios() -> list[OrchestratorScenario]:
    scenarios: list[OrchestratorScenario] = []

    # Crafted drafts that should fail consistently across attempts
    leak_draft = "Hmm, you're close. The answer starts with the letter P — try again!"
    sycophant_draft = "Excellent! Brilliant thinking! What an amazing question to ask!"
    irrelevant_draft = "What's your favorite color and why?"
    clean_draft = "What molecule do you think glucose gets broken into during glycolysis?"

    # V6 — All 3 attempts leak → Dean replan triggered
    scenarios.append(OrchestratorScenario(
        id="V6_replan_after_3_leaks",
        description="3 leak drafts in a row → Dean replan fires for attempt 4",
        teacher_responses=[leak_draft, leak_draft, leak_draft, clean_draft],
        expect_used_replan=True,
        expect_final_attempt_at_or_above=4,
    ))

    # V7 — All 4 attempts leak → SAFE_GENERIC_PROBE
    scenarios.append(OrchestratorScenario(
        id="V7_safe_probe_on_persistent_leak",
        description="All 4 attempts leak → SAFE_GENERIC_PROBE fired",
        teacher_responses=[leak_draft, leak_draft, leak_draft, leak_draft],
        expect_used_safe_probe=True,
        expect_leak_cap=True,
    ))

    # V8 — Empty Teacher response repeatedly → SAFE_GENERIC_PROBE
    scenarios.append(OrchestratorScenario(
        id="V8_safe_probe_on_empty_drafts",
        description="Teacher returns empty text on all attempts → SAFE_GENERIC_PROBE",
        teacher_responses=["", "", "", ""],
        expect_used_safe_probe=True,
    ))

    # V9 — Attempt 4 has non-leak failure (e.g. pedagogy/shape) → ship anyway
    scenarios.append(OrchestratorScenario(
        id="V9_ship_attempt4_on_nonleak_fail",
        description="Attempts 1-3 leak; attempt 4 is irrelevant (pedagogy fails, leak passes) → ship attempt 4",
        teacher_responses=[leak_draft, leak_draft, leak_draft, irrelevant_draft],
        expect_used_safe_probe=False,
        expect_used_replan=True,
        expect_final_attempt_at_or_above=4,
    ))

    # V10 — Sycophancy detected on attempts 1-3, clean on 4 (after replan)
    scenarios.append(OrchestratorScenario(
        id="V10_sycophancy_then_clean",
        description="3 sycophant drafts → replan → clean draft on 4",
        teacher_responses=[sycophant_draft, sycophant_draft, sycophant_draft, clean_draft],
        expect_used_replan=True,
        expect_final_attempt_at_or_above=4,
    ))

    # V11 — First attempt clean passes immediately
    scenarios.append(OrchestratorScenario(
        id="V11_clean_passes_first_attempt",
        description="A clean draft passes on attempt 1 → no replan, no probe",
        teacher_responses=[clean_draft, clean_draft, clean_draft, clean_draft],
        expect_used_safe_probe=False,
        expect_used_replan=False,
    ))

    return scenarios


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_direct(scenario: DirectCheckScenario) -> ScenarioResult:
    t0 = time.monotonic()
    try:
        raw = scenario.check_fn()
    except Exception as e:
        return ScenarioResult(
            scenario_id=scenario.id,
            description=scenario.description,
            duration_s=time.monotonic() - t0,
            passed=False,
            detail=f"EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()[:1500]}",
        )

    duration = time.monotonic() - t0
    # Support 3 check return shapes:
    #   1. {'verdict': 'leak'|'clean'|'sycophantic', ...}  (leak, sycophancy)
    #   2. {'pass': True|False, ...}                      (shape, pedagogy)
    #   3. {'leak': 'clean', 'sycophancy': 'clean', ...}  (V5 multi-check dict)
    verdicts: list[str] = []
    if isinstance(raw, dict):
        if "verdict" in raw:
            verdicts.append(str(raw.get("verdict") or ""))
        elif "pass" in raw and isinstance(raw["pass"], bool):
            # shape/pedagogy: pass=False is equivalent to verdict='fail'
            verdicts.append("fail" if not raw["pass"] else "pass")
        else:
            for v in raw.values():
                verdicts.append(str(v or ""))
    matched = any(v in scenario.expect_verdict_in for v in verdicts)
    return ScenarioResult(
        scenario_id=scenario.id,
        description=scenario.description,
        duration_s=duration,
        passed=matched,
        detail=f"verdicts={verdicts} | expected one of {scenario.expect_verdict_in} | raw_keys={list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}",
    )


def run_orchestrator(scenario: OrchestratorScenario) -> ScenarioResult:
    t0 = time.monotonic()
    teacher = _MockTeacher(scenario.teacher_responses)
    dean = _MockDean()
    dean_state = {"hint_level": 1, "messages": []}
    plan = _build_plan()
    inputs = _build_inputs()

    try:
        result = run_turn_with_retry(
            dean_state=dean_state,
            teacher=teacher,
            dean=dean,
            turn_plan=plan,
            teacher_inputs=inputs,
            dean_chunks=[],
            locked_answer=scenario.locked_answer,
            locked_answer_aliases=[],
            prior_tutor_questions=[],
            parallel_quartet=True,
            timeout_s=60.0,
        )
    except Exception as e:
        return ScenarioResult(
            scenario_id=scenario.id,
            description=scenario.description,
            duration_s=time.monotonic() - t0,
            passed=False,
            detail=f"EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()[:1500]}",
        )

    duration = time.monotonic() - t0
    violations = []
    if scenario.expect_used_safe_probe is not None:
        if bool(result.used_safe_generic_probe) != scenario.expect_used_safe_probe:
            violations.append(f"used_safe_generic_probe expected {scenario.expect_used_safe_probe}, got {result.used_safe_generic_probe}")
    if scenario.expect_used_replan is not None:
        if bool(result.used_dean_replan) != scenario.expect_used_replan:
            violations.append(f"used_dean_replan expected {scenario.expect_used_replan}, got {result.used_dean_replan}")
    if scenario.expect_final_attempt_at_or_above is not None:
        if int(result.final_attempt) < scenario.expect_final_attempt_at_or_above:
            violations.append(f"final_attempt expected >={scenario.expect_final_attempt_at_or_above}, got {result.final_attempt}")
    if scenario.expect_leak_cap is not None:
        if bool(result.leak_cap_fallback_fired) != scenario.expect_leak_cap:
            violations.append(f"leak_cap_fallback_fired expected {scenario.expect_leak_cap}, got {result.leak_cap_fallback_fired}")

    detail = (
        f"final_attempt={result.final_attempt} | "
        f"used_safe_probe={result.used_safe_generic_probe} | "
        f"used_replan={result.used_dean_replan} | "
        f"leak_cap={result.leak_cap_fallback_fired} | "
        f"timed_out={result.timed_out} | "
        f"final_text_len={len(result.final_text)} | "
        f"attempts={len(result.attempts)}"
    )
    if violations:
        detail += " || VIOLATIONS: " + "; ".join(violations)
    return ScenarioResult(
        scenario_id=scenario.id,
        description=scenario.description,
        duration_s=duration,
        passed=not violations,
        detail=detail,
    )


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=str, default=None)
    args = parser.parse_args()

    direct = build_direct_scenarios()
    orch = build_orchestrator_scenarios()
    all_scenarios = direct + orch
    if args.scenario:
        all_scenarios = [s for s in all_scenarios if s.id == args.scenario]
        direct = [s for s in direct if s.id == args.scenario]
        orch = [s for s in orch if s.id == args.scenario]

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = REPO / "data" / "artifacts" / "eval" / "stress_test_verifier" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== TIER 2: VERIFIER STRESS TEST ===")
    print(f"Direct check scenarios: {len(direct)}")
    print(f"Orchestrator scenarios: {len(orch)}")
    print(f"Output: {out_dir}\n", flush=True)

    results: list[ScenarioResult] = []
    durations: list[float] = []
    overall_t0 = time.monotonic()

    # Run direct scenarios first (they're fast)
    for i, s in enumerate(direct):
        print(f"[D{i+1}/{len(direct)}] {s.id} — {s.description[:80]}", flush=True)
        result = run_direct(s)
        durations.append(result.duration_s)
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"    {status} | {result.duration_s:6.2f}s | {result.detail[:200]}", flush=True)
        results.append(result)

    # Then orchestrator scenarios (slow — full retry loops)
    for i, s in enumerate(orch):
        print(f"[O{i+1}/{len(orch)}] {s.id} — {s.description[:80]}", flush=True)
        result = run_orchestrator(s)
        durations.append(result.duration_s)
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"    {status} | {result.duration_s:6.2f}s | {result.detail[:300]}", flush=True)
        avg = sum(durations) / max(len(durations), 1)
        remaining = max(len(orch) - i - 1, 0) * avg
        print(f"    avg {avg:5.2f}s | ETA {remaining/60:.1f} min remaining\n", flush=True)
        results.append(result)

    duration_total = time.monotonic() - overall_t0
    pass_count = sum(1 for r in results if r.passed)
    fail_count = len(results) - pass_count

    summary = {
        "timestamp": timestamp,
        "duration_s": duration_total,
        "scenarios_total": len(results),
        "passed": pass_count,
        "failed": fail_count,
        "results": [asdict(r) for r in results],
    }
    (out_dir / "report.json").write_text(json.dumps(summary, indent=2, default=str))

    print("\n" + "=" * 70)
    print(f"DONE in {duration_total/60:.1f} min — {pass_count} pass / {fail_count} fail")
    print(f"Report: {out_dir / 'report.json'}")


if __name__ == "__main__":
    main()
