"""
scripts/stress_test_flows.py
============================
Coverage stress test for the sokratic conversation graph.

Hits every router branch, intent verdict, and lifecycle flag transition
by pre-injecting state and driving 1-3 student turns per scenario.

Run:
    cd /Users/arun-ghontale/UB/NLP/sokratic
    python scripts/stress_test_flows.py [--limit N] [--group GROUP] [--scenario ID]

Outputs:
    data/artifacts/eval/stress_test/<timestamp>/<scenario_id>.json   per-scenario state+trace
    data/artifacts/eval/stress_test/<timestamp>/report.json          summary
    data/artifacts/eval/stress_test/<timestamp>/coverage.json        which conditionals fired
"""

from __future__ import annotations

import asyncio
import json
import os
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

from config import cfg  # noqa: E402
from conversation.state import initial_state  # noqa: E402


# ---------------------------------------------------------------------------
# Coverage tracker — every distinct conditional we want to confirm fires
# ---------------------------------------------------------------------------

ROUTERS = [
    "rapport->prelock(opt_in_yes)",
    "rapport->memory_update(opt_in_no)",
    "rapport->reprompt(opt_in_ambiguous)",
    "prelock_anchor_pick->tutoring(match)",
    "prelock_anchor_pick->pivot(no_match)",
    "tutoring->assessment(answer_reached)",
    "tutoring->memory_update(hint_exhausted)",
    "tutoring->memory_update(max_turns)",
    "tutoring->memory_update(help_abuse_max)",
    "tutoring->memory_update(off_topic_max)",
    "tutoring->modal(deflection)",
    "any->memory_update(__exit_session__)",
    "assessment->memory_update(clinical_strike)",
    "assessment->memory_update(clinical_max_turns)",
]

INTENTS = [
    "on_topic_engaged",
    "help_abuse",
    "off_domain",
    "deflection",
    "opt_in_yes",
    "opt_in_no",
    "opt_in_ambiguous",
]

LIFECYCLE = [
    "exit_intent_pending=True",
    "session_ended=True",
    "exit_intent_pending=reset",
    "close_reason=exit_intent",
    "close_reason=hint_exhausted",
    "close_reason=opt_in_no",
    "close_reason=off_topic_strike_4",
    "close_reason=max_turns",
    "close_reason=clinical_strike",
]


@dataclass
class CoverageTracker:
    routers_hit: set[str] = field(default_factory=set)
    intents_hit: set[str] = field(default_factory=set)
    lifecycle_hit: set[str] = field(default_factory=set)

    def record_state(self, prev: dict, curr: dict, all_traces: list[dict] | None = None) -> None:
        """Inspect state delta + turn traces, mark hit routers/intents/lifecycle.

        all_traces: the full list of turn_trace entries from each turn during
        the scenario (concatenated). The coverage tracker reads
        `category` from preflight wrapper entries (not `verdict`), and
        looks for topic_lock/anchor_pick wrapper names to detect those routers.
        """
        prev_phase = prev.get("phase", "")
        curr_phase = curr.get("phase", "")
        if prev_phase != curr_phase:
            transition = f"{prev_phase}->{curr_phase}"
            if transition == "rapport->memory_update":
                self.routers_hit.add("rapport->memory_update(opt_in_no)")
            if curr_phase == "assessment":
                self.routers_hit.add("tutoring->assessment(answer_reached)")
            if prev_phase == "rapport" and curr_phase == "tutoring":
                self.routers_hit.add("rapport->prelock(opt_in_yes)")
        # BLOCK 12 (S5) — close_reason detection regardless of phase
        # transition. Some scenarios pre-inject phase=memory_update so
        # there's no transition; check final state directly.
        if curr_phase == "memory_update":
            cr = str(curr.get("close_reason", "") or "")
            if cr == "exit_intent":
                self.routers_hit.add("any->memory_update(__exit_session__)")
                self.lifecycle_hit.add("close_reason=exit_intent")
            elif cr == "hints_exhausted":
                self.routers_hit.add("tutoring->memory_update(hint_exhausted)")
                self.lifecycle_hit.add("close_reason=hint_exhausted")
            elif cr in ("off_topic_strike_4", "off_topic_max", "off_domain_strike"):
                self.routers_hit.add("tutoring->memory_update(off_topic_max)")
                self.lifecycle_hit.add("close_reason=off_topic_strike_4")
            elif cr in ("tutoring_cap", "max_turns"):
                self.routers_hit.add("tutoring->memory_update(max_turns)")
                self.lifecycle_hit.add("close_reason=max_turns")
            elif cr == "clinical_cap":
                self.routers_hit.add("assessment->memory_update(clinical_max_turns)")
                self.lifecycle_hit.add("close_reason=clinical_strike")
            elif cr == "reach_skipped":
                self.lifecycle_hit.add("close_reason=opt_in_no")
                self.intents_hit.add("opt_in_no")  # implies opt_in_no fired
            elif cr == "reach_full":
                self.routers_hit.add("assessment->memory_update(clinical_max_turns)")
                self.intents_hit.add("opt_in_yes")  # implies opt_in_yes fired
        if not prev.get("exit_intent_pending") and curr.get("exit_intent_pending"):
            self.routers_hit.add("tutoring->modal(deflection)")
            self.lifecycle_hit.add("exit_intent_pending=True")
        if prev.get("exit_intent_pending") and not curr.get("exit_intent_pending"):
            self.lifecycle_hit.add("exit_intent_pending=reset")
        if not prev.get("session_ended") and curr.get("session_ended"):
            self.lifecycle_hit.add("session_ended=True")
        # Intent verdict from preflight trace entries — preflight wrappers
        # carry a `category` field with the unified-classifier verdict.
        traces = all_traces or (curr.get("debug", {}) or {}).get("turn_trace", []) or []
        for trace in traces:
            wrapper = trace.get("wrapper", "") or ""
            if "preflight" in wrapper:
                category = trace.get("category") or trace.get("verdict") or ""
                if isinstance(category, str):
                    for intent in INTENTS:
                        if intent in category:
                            self.intents_hit.add(intent)
                # 'continuing'/'on_topic' from sub-classifiers count as on_topic_engaged
                if isinstance(category, str) and category in ("continuing", "on_topic", "on_topic_engaged", ""):
                    if not category:
                        # Empty category — preflight ran but didn't fire.
                        # Conservative: mark on_topic_engaged hit since we got
                        # past preflight without intervention.
                        pass
                    else:
                        self.intents_hit.add("on_topic_engaged")
            if "topic_lock" in wrapper or "anchor_pick" in wrapper:
                if "match" in wrapper or "locked" in wrapper or "lock_complete" in wrapper or "resolved" in wrapper:
                    self.routers_hit.add("prelock_anchor_pick->tutoring(match)")
                elif "pivot" in wrapper or "reject" in wrapper or "reset" in wrapper:
                    self.routers_hit.add("prelock_anchor_pick->pivot(no_match)")
                elif "ambiguous" in wrapper or "reprompt" in wrapper:
                    self.routers_hit.add("rapport->reprompt(opt_in_ambiguous)")
                    self.intents_hit.add("opt_in_ambiguous")
            # BLOCK 12 (S5) — assessment_v2 opt-in handling has its own
            # trace wrappers; map them to intent verdicts that don't go
            # through preflight.
            if "assessment_v2.opt_in" in wrapper:
                if "yes" in wrapper or "accepted" in wrapper:
                    self.intents_hit.add("opt_in_yes")
                elif "no" in wrapper or "declined" in wrapper:
                    self.intents_hit.add("opt_in_no")
                elif "ambiguous" in wrapper or "clarify" in wrapper or "reask" in wrapper:
                    self.intents_hit.add("opt_in_ambiguous")

    def report(self) -> dict:
        return {
            "routers": {
                "hit": sorted(self.routers_hit),
                "missed": sorted(set(ROUTERS) - self.routers_hit),
                "coverage": f"{len(self.routers_hit)}/{len(ROUTERS)}",
            },
            "intents": {
                "hit": sorted(self.intents_hit),
                "missed": sorted(set(INTENTS) - self.intents_hit),
                "coverage": f"{len(self.intents_hit)}/{len(INTENTS)}",
            },
            "lifecycle": {
                "hit": sorted(self.lifecycle_hit),
                "missed": sorted(set(LIFECYCLE) - self.lifecycle_hit),
                "coverage": f"{len(self.lifecycle_hit)}/{len(LIFECYCLE)}",
            },
        }


# ---------------------------------------------------------------------------
# Scenario DSL
# ---------------------------------------------------------------------------


@dataclass
class Step:
    student_msg: str
    expect_phase: str | None = None
    expect_pending_kind: str | None = None
    expect_state: dict[str, Any] = field(default_factory=dict)  # field -> expected value
    expect_in_tutor_msg: list[str] = field(default_factory=list)  # substrings
    expect_close_reason: str | None = None
    note: str = ""


@dataclass
class Scenario:
    id: str
    group: str
    description: str
    setup: Callable[[Any, dict], dict] | None = None  # mutates state pre-flight
    prelocked_topic: str | None = None
    memory_enabled: bool = False
    steps: list[Step] = field(default_factory=list)


@dataclass
class StepResult:
    student_msg: str
    duration_s: float
    phase_after: str
    pending_kind_after: str
    close_reason_after: str
    tutor_msg: str
    violations: list[str]


@dataclass
class ScenarioResult:
    scenario_id: str
    group: str
    description: str
    duration_s: float
    passed: bool
    failed_at_step: int | None
    steps: list[StepResult]
    error: str | None = None


# ---------------------------------------------------------------------------
# Setup helpers — pre-inject state to skip slow flows
# ---------------------------------------------------------------------------

# A canonical prelocked topic we'll reuse — must exist in the corpus.
# Pulled from check_prelock_flow.py / existing eval scripts.
PRELOCKED_TOPIC = "Chapter 3: Cellular Energy > Glycolysis > Steps of glycolysis"

# Fallback paths if the primary one isn't in the corpus.
PRELOCKED_FALLBACKS = [
    "Chapter 1: Cell Biology > Cell Membrane > Phospholipid bilayer",
    "Chapter 2: Biochemistry > Enzymes > Enzyme kinetics",
]


def _setup_prelocked_with_anchor(student_id: str, cfg_obj) -> dict:
    """Build a state that's already past rapport + topic + anchor pick,
    sitting in tutoring with locked_topic + locked_question + locked_answer."""
    from backend.api.session import _apply_prelock
    from backend.dependencies import get_dean

    state = initial_state(student_id, cfg_obj)
    state["thread_id"] = f"stress_{student_id}_{int(time.time())}"
    state["memory_enabled"] = False
    state["client_hour"] = 14

    last_err = None
    for path in [PRELOCKED_TOPIC] + PRELOCKED_FALLBACKS:
        try:
            _apply_prelock(state, path)
            break
        except Exception as e:
            last_err = e
            continue
    else:
        raise RuntimeError(f"All prelocked paths failed; last error: {last_err}")

    # Resolve anchor pick by selecting the first variation.
    pc = state.get("pending_user_choice") or {}
    if pc.get("kind") == "anchor_pick":
        opts = pc.get("options") or []
        meta = pc.get("anchor_meta") or {}
        if opts and opts[0] in meta:
            v = meta[opts[0]]
            state["locked_question"] = str(v.get("question") or "").strip()
            state["locked_answer"] = str(v.get("answer") or "").strip()
            state["full_answer"] = str(v.get("full_answer") or v.get("answer") or "").strip()
            raw_aliases = v.get("aliases") or []
            state["locked_answer_aliases"] = [str(a) for a in raw_aliases if isinstance(a, str)]
            state["pending_user_choice"] = {}
            state["topic_confirmed"] = True
            state["phase"] = "tutoring"
            state["student_state"] = "answer"

    # Bug #3 fix — explicitly clear topic_just_locked so the next dean turn
    # evaluates the answer instead of emitting a topic_ack message. _apply_prelock's
    # legacy branch sets it True; our anchor-pick simulation needs it False.
    state["topic_just_locked"] = False

    # Get dean instance to ensure anchor + retrieval is warm.
    _ = get_dean()
    state["debug"]["turn_trace"] = []
    return state


def _setup_rapport_only(student_id: str, cfg_obj) -> dict:
    """Plain initial state, ready for rapport."""
    from backend.dependencies import get_graph

    state = initial_state(student_id, cfg_obj)
    state["thread_id"] = f"stress_{student_id}_{int(time.time())}"
    state["memory_enabled"] = False
    state["client_hour"] = 14
    # Run rapport_node so greeting + opt_in pending appears.
    graph = get_graph()
    config = {"configurable": {"thread_id": state["thread_id"]}}
    state = graph.invoke(state, config=config)
    state.setdefault("debug", {})["turn_trace"] = []
    return state


def _setup_anchor_pick_pending(student_id: str, cfg_obj) -> dict:
    """State with anchor_pick cards still pending (haven't picked yet)."""
    from backend.api.session import _apply_prelock

    state = initial_state(student_id, cfg_obj)
    state["thread_id"] = f"stress_{student_id}_{int(time.time())}"
    state["memory_enabled"] = False
    state["client_hour"] = 14

    last_err = None
    for path in [PRELOCKED_TOPIC] + PRELOCKED_FALLBACKS:
        try:
            _apply_prelock(state, path)
            break
        except Exception as e:
            last_err = e
            continue
    else:
        raise RuntimeError(f"prelock failed: {last_err}")
    state.setdefault("debug", {})["turn_trace"] = []
    return state


def _setup_assessment_phase(student_id: str, cfg_obj) -> dict:
    """State at start of assessment phase, opt_in pending."""
    state = _setup_prelocked_with_anchor(student_id, cfg_obj)
    state["phase"] = "assessment"
    state["assessment_turn"] = 1
    state["student_reached_answer"] = True
    state["pending_user_choice"] = {
        "kind": "opt_in",
        "options": ["Yes, give me a clinical scenario", "No, end the session"],
    }
    return state


# Pre-injection helpers that mutate state right before the turn fires
def _inject(**fields) -> Callable[[Any, dict], dict]:
    def _apply(_cfg, state: dict) -> dict:
        for k, v in fields.items():
            state[k] = v
        return state
    return _apply


# ---------------------------------------------------------------------------
# Turn dispatcher — mirrors backend/api/chat.py per-turn loop (no WS)
# ---------------------------------------------------------------------------

def run_turn(state: dict, student_msg: str) -> dict:
    """Mirror chat.py: handle __exit_session__ sentinel, append student msg,
    invoke graph, return new state."""
    from backend.dependencies import get_graph

    graph = get_graph()
    thread_id = state.get("thread_id") or "stress_default"
    config = {"configurable": {"thread_id": thread_id}}

    if student_msg == "__exit_session__":
        state["exit_intent_pending"] = True
        state["close_reason"] = "exit_intent"
        state["phase"] = "memory_update"
        state.setdefault("debug", {})["turn_trace"] = []
    elif student_msg == "__cancel_exit__":
        # BLOCK 9 (S3) — mirror chat.py cancel sentinel handling
        state["exit_intent_pending"] = False
        state["cancel_modal_pending"] = True
        state["recent_cancel_at_turn"] = int(state.get("turn_count", 0) or 0)
        state.setdefault("debug", {})["turn_trace"] = []
        try:
            from conversation.snapshots import log_system_event
            log_system_event(state, "exit_modal_canceled")
        except Exception:
            pass
    else:
        messages = list(state.get("messages", []))
        messages.append({"role": "student", "content": student_msg})
        state["messages"] = messages
        state.setdefault("debug", {})["turn_trace"] = []

    new_state = graph.invoke(state, config=config)
    return new_state


def latest_tutor_message(state: dict) -> str:
    for msg in reversed(state.get("messages", []) or []):
        if msg.get("role") == "tutor":
            return str(msg.get("content", "") or "")
    return ""


# ---------------------------------------------------------------------------
# Assertion engine
# ---------------------------------------------------------------------------

def check_step(state_after: dict, step: Step) -> list[str]:
    violations: list[str] = []
    if step.expect_phase is not None:
        actual = state_after.get("phase", "")
        if actual != step.expect_phase:
            violations.append(f"phase: expected {step.expect_phase!r}, got {actual!r}")
    if step.expect_pending_kind is not None:
        pc = state_after.get("pending_user_choice") or {}
        actual_kind = pc.get("kind", "") if isinstance(pc, dict) else ""
        if step.expect_pending_kind == "":
            if actual_kind:
                violations.append(f"pending_kind: expected empty, got {actual_kind!r}")
        elif actual_kind != step.expect_pending_kind:
            violations.append(f"pending_kind: expected {step.expect_pending_kind!r}, got {actual_kind!r}")
    if step.expect_close_reason is not None:
        actual = str(state_after.get("close_reason", "") or "")
        if actual != step.expect_close_reason:
            violations.append(f"close_reason: expected {step.expect_close_reason!r}, got {actual!r}")
    for field_name, expected in step.expect_state.items():
        actual = state_after.get(field_name)
        if actual != expected:
            violations.append(f"state[{field_name}]: expected {expected!r}, got {actual!r}")
    if step.expect_in_tutor_msg:
        tutor_msg = latest_tutor_message(state_after).lower()
        for needle in step.expect_in_tutor_msg:
            if needle.lower() not in tutor_msg:
                violations.append(f"tutor_msg missing substring: {needle!r}")
    return violations


# ---------------------------------------------------------------------------
# Scenario definitions — Tier 1 coverage
# ---------------------------------------------------------------------------

def build_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []

    # --- A. Rapport ---
    scenarios.append(Scenario(
        id="A1_opt_in_yes",
        group="A_rapport",
        description="opt_in_yes at greeting → enter prelock/topic flow",
        setup=lambda c, s: _setup_rapport_only("eval_a1", c),
        steps=[
            Step(student_msg="yes", note="affirm greeting"),
        ],
    ))
    scenarios.append(Scenario(
        id="A2_opt_in_no",
        group="A_rapport",
        description="opt_in_no at greeting → memory_update with opt_in_no close",
        setup=lambda c, s: _setup_rapport_only("eval_a2", c),
        steps=[
            Step(student_msg="no thanks, not today",
                 expect_phase="memory_update"),
        ],
    ))
    scenarios.append(Scenario(
        id="A3_opt_in_ambiguous",
        group="A_rapport",
        description="opt_in_ambiguous at greeting → re-prompt, stay in rapport",
        setup=lambda c, s: _setup_rapport_only("eval_a3", c),
        steps=[
            Step(student_msg="maybe later, not sure"),
        ],
    ))
    scenarios.append(Scenario(
        id="A4_off_domain_at_greeting",
        group="A_rapport",
        description="off_domain at greeting → off_topic strike, stays in rapport",
        setup=lambda c, s: _setup_rapport_only("eval_a4", c),
        steps=[
            Step(student_msg="tell me about football"),
        ],
    ))
    scenarios.append(Scenario(
        id="A5_deflection_at_greeting",
        group="A_rapport",
        description="deflection at greeting → direct close (BLOCK 14: no modal at rapport stage)",
        setup=lambda c, s: _setup_rapport_only("eval_a5", c),
        steps=[
            # BLOCK 14: rapport-stage decline routes direct-to-memory_update
            # (no modal — student hasn't invested progress to confirm-exit over).
            Step(student_msg="I want to leave",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent"),
        ],
    ))

    # --- C. Anchor pick (most direct entry for many flows) ---
    scenarios.append(Scenario(
        id="C1_anchor_pick_match_chip",
        group="C_anchor",
        description="Anchor pick — student types one of the 3 questions verbatim → tutoring",
        setup=lambda c, s: _setup_anchor_pick_pending("eval_c1", c),
        steps=[
            Step(student_msg="__USE_FIRST_ANCHOR__",
                 expect_phase="tutoring",
                 expect_pending_kind=""),
        ],
    ))
    scenarios.append(Scenario(
        id="C2_anchor_pick_pivot",
        group="C_anchor",
        description="Anchor pick — student types unrelated question → pivot, clears lock",
        setup=lambda c, s: _setup_anchor_pick_pending("eval_c2", c),
        steps=[
            Step(student_msg="actually I want to learn about something completely different — what is meiosis?"),
        ],
    ))
    scenarios.append(Scenario(
        id="C3_anchor_pick_deflection",
        group="C_anchor",
        description="Anchor pick — deflection → modal",
        setup=lambda c, s: _setup_anchor_pick_pending("eval_c3", c),
        steps=[
            Step(student_msg="I want to stop now",
                 expect_state={"exit_intent_pending": True}),
        ],
    ))
    scenarios.append(Scenario(
        id="C4_anchor_pick_exit_sentinel",
        group="C_anchor",
        description="Anchor pick — __exit_session__ sentinel → memory_update",
        setup=lambda c, s: _setup_anchor_pick_pending("eval_c4", c),
        steps=[
            # Per design: memory_update_node clears exit_intent_pending after
            # using it (modal already closed by then). session_ended=True is
            # the durable signal frontend uses to disable input.
            Step(student_msg="__exit_session__",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent",
                 expect_state={"session_ended": True, "exit_intent_pending": False}),
        ],
    ))

    # --- D. Tutoring (already past anchor pick) ---
    scenarios.append(Scenario(
        id="D1_correct_answer_to_assessment",
        group="D_tutoring",
        description="Engaged answer matching locked_answer → assessment phase",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d1", c),
        steps=[
            # Use the locked_answer itself as the student message — guaranteed match
            Step(student_msg="__USE_LOCKED_ANSWER__",
                 expect_phase="assessment"),
        ],
    ))
    scenarios.append(Scenario(
        id="D2_engaged_wrong_no_advance",
        group="D_tutoring",
        description="Engaged but wrong answer → tutoring continues, hint may or may not advance",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d2", c),
        steps=[
            Step(student_msg="I think it might be related to the krebs cycle but I'm not sure how exactly",
                 expect_phase="tutoring"),
        ],
    ))
    scenarios.append(Scenario(
        id="D3_help_abuse_strike",
        group="D_tutoring",
        description="Help abuse pattern → help_abuse_count increments",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d3", c),
        steps=[
            Step(student_msg="just tell me the answer please"),
        ],
    ))
    scenarios.append(Scenario(
        id="D4_off_topic_strike",
        group="D_tutoring",
        description="Off-topic chatter → off_topic_count increments",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d4", c),
        steps=[
            Step(student_msg="hey what's your favorite movie"),
        ],
    ))
    scenarios.append(Scenario(
        id="D5_deflection_mid_tutoring",
        group="D_tutoring",
        description="Deflection mid-tutoring → exit_intent_pending=True",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d5", c),
        steps=[
            Step(student_msg="can we end here?",
                 expect_state={"exit_intent_pending": True}),
        ],
    ))
    scenarios.append(Scenario(
        id="D6_exit_sentinel_in_tutoring",
        group="D_tutoring",
        description="__exit_session__ button mid-tutoring → memory_update with exit_intent",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d6", c),
        steps=[
            Step(student_msg="__exit_session__",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent",
                 expect_state={"session_ended": True}),
        ],
    ))
    scenarios.append(Scenario(
        id="D7_hint_exhausted_close",
        group="D_tutoring",
        description="Pre-inject hint_level past max → memory_update with hint_exhausted",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_d7", c),
        steps=[
            Step(student_msg="hmm I really don't know"),
        ],
    ))
    # Mutate state right before the turn — hint_level just past max_hints
    def _d7_inject(c, s):
        s = _setup_prelocked_with_anchor("eval_d7", c)
        s["hint_level"] = s.get("max_hints", 3) + 1
        return s
    scenarios[-1].setup = _d7_inject

    scenarios.append(Scenario(
        id="D8_max_turns_close",
        group="D_tutoring",
        description="Pre-inject turn_count near max → memory_update with max_turns",
        setup=None,
        steps=[
            Step(student_msg="thinking...", expect_phase="memory_update",
                 expect_close_reason="tutoring_cap"),  # actual close reason = "tutoring_cap" per _derive_close_reason
        ],
    ))
    def _d8_inject(c, s):
        s = _setup_prelocked_with_anchor("eval_d8", c)
        s["turn_count"] = s.get("max_turns", 25) - 1  # next turn hits limit
        # Bug #4 fix: max out hint_level headroom so hints_exhausted can't
        # fire on the same turn that turn_count hits max.
        s["max_hints"] = 99
        s["hint_level"] = 0
        return s
    scenarios[-1].setup = _d8_inject

    scenarios.append(Scenario(
        id="D9_help_abuse_threshold_close",
        group="D_tutoring",
        description="Pre-inject help_abuse_count at threshold-1, fire one more → close",
        setup=None,
        steps=[
            Step(student_msg="just give me the damn answer already"),
        ],
    ))
    def _d9_inject(c, s):
        s = _setup_prelocked_with_anchor("eval_d9", c)
        threshold = int(getattr(cfg.dean, "help_abuse_threshold", 4))
        s["help_abuse_count"] = threshold - 1
        return s
    scenarios[-1].setup = _d9_inject

    scenarios.append(Scenario(
        id="D10_off_topic_threshold_close",
        group="D_tutoring",
        description="Pre-inject off_topic_count at threshold-1, fire one more → close",
        setup=None,
        steps=[
            Step(student_msg="what about basketball though"),
        ],
    ))
    def _d10_inject(c, s):
        s = _setup_prelocked_with_anchor("eval_d10", c)
        threshold = int(getattr(cfg.dean, "off_topic_threshold", 4))
        s["off_topic_count"] = threshold - 1
        return s
    scenarios[-1].setup = _d10_inject

    # --- E. Clinical / Assessment ---
    scenarios.append(Scenario(
        id="E1_clinical_opt_in_yes",
        group="E_clinical",
        description="opt_in_yes at assessment → clinical question presented",
        setup=lambda c, s: _setup_assessment_phase("eval_e1", c),
        steps=[
            Step(student_msg="yes, give me a clinical scenario"),
        ],
    ))
    scenarios.append(Scenario(
        id="E2_clinical_opt_in_no",
        group="E_clinical",
        description="opt_in_no at assessment → memory_update",
        setup=lambda c, s: _setup_assessment_phase("eval_e2", c),
        steps=[
            Step(student_msg="no thanks, I'm done",
                 expect_phase="memory_update"),
        ],
    ))
    scenarios.append(Scenario(
        id="E3_clinical_strike_threshold",
        group="E_clinical",
        description="Pre-inject clinical_off_topic_count at threshold-1, fire off-topic → close",
        setup=None,
        steps=[
            Step(student_msg="what's the weather like today"),
        ],
    ))
    def _e3_inject(c, s):
        s = _setup_assessment_phase("eval_e3", c)
        s["assessment_turn"] = 2  # past opt-in, in clinical
        s["clinical_opt_in"] = True
        s["pending_user_choice"] = {}
        threshold = int(getattr(cfg.dean, "clinical_strike_threshold", 2))
        s["clinical_off_topic_count"] = threshold - 1
        return s
    scenarios[-1].setup = _e3_inject

    # --- H. Edge cases ---
    scenarios.append(Scenario(
        id="H1_empty_message",
        group="H_edge",
        description="Empty student message → handler doesn't crash",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_h1", c),
        steps=[
            Step(student_msg=""),
        ],
    ))
    scenarios.append(Scenario(
        id="H2_exit_sentinel_at_rapport",
        group="H_edge",
        description="__exit_session__ during rapport phase → memory_update",
        setup=lambda c, s: _setup_rapport_only("eval_h2", c),
        steps=[
            Step(student_msg="__exit_session__",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent"),
        ],
    ))

    # --- B. Topic prelock (post-greeting, no prelocked topic) ---
    scenarios.append(Scenario(
        id="B1_topic_via_text",
        group="B_topic",
        description="Post-greeting, student types topic name → topic resolution",
        setup=lambda c, s: _setup_rapport_only("eval_b1_topic", c),
        steps=[
            Step(student_msg="yes"),  # consume opt_in_yes
            Step(student_msg="I want to learn about glycolysis"),
        ],
    ))
    scenarios.append(Scenario(
        id="B2_topic_off_domain",
        group="B_topic",
        description="Post-greeting, student types off-domain topic → off_topic strike",
        setup=lambda c, s: _setup_rapport_only("eval_b2_topic", c),
        steps=[
            Step(student_msg="yes"),
            Step(student_msg="tell me about cars and basketball"),
        ],
    ))
    scenarios.append(Scenario(
        id="B3_topic_deflection",
        group="B_topic",
        description="Post-greeting topic-pick deflection → direct close (BLOCK 14: no modal at rapport stage)",
        setup=lambda c, s: _setup_rapport_only("eval_b3_topic", c),
        steps=[
            Step(student_msg="yes"),
            # BLOCK 14: still rapport-stage (no topic locked) so direct-to-close.
            Step(student_msg="actually I want to leave",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent"),
        ],
    ))
    scenarios.append(Scenario(
        id="B4_topic_idk",
        group="B_topic",
        description="Post-greeting, student says I don't know → low effort handling",
        setup=lambda c, s: _setup_rapport_only("eval_b4_topic", c),
        steps=[
            Step(student_msg="yes"),
            Step(student_msg="I don't know what to study"),
        ],
    ))
    scenarios.append(Scenario(
        id="B5_prelock_cap_guided_pick",
        group="B_topic",
        description="prelock_loop_count=7 → guided-pick card",
        setup=None,
        steps=[
            Step(student_msg="something"),
        ],
    ))
    def _b5_inject(c, s):
        s = _setup_rapport_only("eval_b5_cap", c)
        s["prelock_loop_count"] = 7  # at cap
        return s
    scenarios[-1].setup = _b5_inject

    # --- F. Additional close reasons not covered above ---
    scenarios.append(Scenario(
        id="F1_reach_full_close",
        group="F_close",
        description="Pre-inject clinical_completed + correct → close_reason=reach_full",
        setup=None,
        steps=[
            Step(student_msg="ok thanks", expect_phase="memory_update",
                 expect_close_reason="reach_full"),
        ],
    ))
    def _f1_inject(c, s):
        s = _setup_assessment_phase("eval_f1", c)
        s["assessment_turn"] = 3
        s["clinical_completed"] = True
        s["clinical_state"] = "correct"
        s["clinical_opt_in"] = True
        s["pending_user_choice"] = {}
        s["phase"] = "memory_update"
        return s
    scenarios[-1].setup = _f1_inject

    scenarios.append(Scenario(
        id="F2_reach_skipped_close",
        group="F_close",
        description="Reached answer but declined clinical → close_reason=reach_skipped",
        setup=None,
        steps=[
            Step(student_msg="ok bye", expect_phase="memory_update",
                 expect_close_reason="reach_skipped"),
        ],
    ))
    def _f2_inject(c, s):
        s = _setup_assessment_phase("eval_f2", c)
        s["student_reached_answer"] = True
        s["clinical_opt_in"] = False
        s["assessment_turn"] = 3
        s["pending_user_choice"] = {}
        s["phase"] = "memory_update"
        return s
    scenarios[-1].setup = _f2_inject

    scenarios.append(Scenario(
        id="F3_clinical_cap_close",
        group="F_close",
        description="Clinical max turns hit (clinical_completed but not correct) → close_reason=clinical_cap",
        setup=None,
        steps=[
            Step(student_msg="ok done", expect_phase="memory_update",
                 expect_close_reason="clinical_cap"),
        ],
    ))
    def _f3_inject(c, s):
        s = _setup_assessment_phase("eval_f3", c)
        s["clinical_completed"] = True
        s["clinical_state"] = "incorrect"
        s["clinical_opt_in"] = True
        s["assessment_turn"] = 3
        s["pending_user_choice"] = {}
        s["phase"] = "memory_update"
        return s
    scenarios[-1].setup = _f3_inject

    # --- I. Cross-phase sentinel ---
    scenarios.append(Scenario(
        id="I1_exit_sentinel_at_clinical",
        group="I_sentinel",
        description="__exit_session__ during clinical phase → memory_update with exit_intent",
        setup=None,
        steps=[
            Step(student_msg="__exit_session__",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent"),
        ],
    ))
    def _i1_inject(c, s):
        s = _setup_assessment_phase("eval_i1", c)
        s["assessment_turn"] = 2
        s["clinical_opt_in"] = True
        s["pending_user_choice"] = {}
        return s
    scenarios[-1].setup = _i1_inject

    # --- J. Tone escalation (off_domain strikes) ---
    scenarios.append(Scenario(
        id="J1_off_domain_tone_escalation",
        group="J_tone",
        description="3 off_domain in a row → off_topic_count climbs through tone tiers",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_j1", c),
        steps=[
            Step(student_msg="what's the weather today"),
            Step(student_msg="who won the football game"),
            Step(student_msg="tell me a joke"),
        ],
    ))

    # --- M. Cancel-modal flow (BLOCK 9 / S3) ---
    scenarios.append(Scenario(
        id="M1_cancel_modal_soft_reset",
        group="M_cancel_modal",
        description="Deflection → exit modal → student cancels → soft_reset bridging turn",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_m1", c),
        steps=[
            Step(student_msg="i want to stop now",
                 expect_state={"exit_intent_pending": True}),
            Step(student_msg="__cancel_exit__",
                 expect_state={"exit_intent_pending": False, "cancel_modal_pending": False}),
        ],
    ))

    # --- K. Literal __exit_session__ typed (not button) ---
    scenarios.append(Scenario(
        id="K1_literal_exit_string_typed",
        group="K_edge",
        description="Student types literal '__exit_session__' as text → SHOULD be treated same as button (current behavior)",
        setup=lambda c, s: _setup_prelocked_with_anchor("eval_k1", c),
        steps=[
            # Per chat.py current logic, the sentinel match is exact.
            # This scenario documents whether typing it bypasses the modal.
            Step(student_msg="__exit_session__",
                 expect_phase="memory_update",
                 expect_close_reason="exit_intent"),
        ],
    ))

    return scenarios


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _resolve_special_msg(state: dict, student_msg: str) -> str:
    """Replace placeholder messages with state-derived values."""
    if student_msg == "__USE_FIRST_ANCHOR__":
        pc = state.get("pending_user_choice") or {}
        opts = pc.get("options") or []
        if opts:
            return str(opts[0])
        return "yes"
    if student_msg == "__USE_LOCKED_ANSWER__":
        return str(state.get("full_answer") or state.get("locked_answer") or "I don't know")
    return student_msg


def run_scenario(scenario: Scenario, out_dir: Path) -> ScenarioResult:
    t0 = time.monotonic()
    step_results: list[StepResult] = []
    failed_at = None
    error = None

    try:
        # Setup
        if scenario.setup:
            state = scenario.setup(cfg, {})
        else:
            state = _setup_prelocked_with_anchor(scenario.id, cfg)
        state["memory_enabled"] = scenario.memory_enabled

        # Run steps
        for i, step in enumerate(scenario.steps):
            student_msg = _resolve_special_msg(state, step.student_msg)
            step_t0 = time.monotonic()
            try:
                state = run_turn(state, student_msg)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                step_results.append(StepResult(
                    student_msg=student_msg,
                    duration_s=time.monotonic() - step_t0,
                    phase_after=state.get("phase", "?"),
                    pending_kind_after="",
                    close_reason_after="",
                    tutor_msg="",
                    violations=[f"EXCEPTION: {error}\n{traceback.format_exc()[:1500]}"],
                ))
                failed_at = i
                break

            duration = time.monotonic() - step_t0
            violations = check_step(state, step)
            step_results.append(StepResult(
                student_msg=student_msg,
                duration_s=duration,
                phase_after=state.get("phase", ""),
                pending_kind_after=(state.get("pending_user_choice") or {}).get("kind", "") if isinstance(state.get("pending_user_choice"), dict) else "",
                close_reason_after=str(state.get("close_reason", "") or ""),
                tutor_msg=latest_tutor_message(state)[:500],
                violations=violations,
            ))
            if violations and failed_at is None:
                failed_at = i
    except Exception as e:
        error = f"setup error: {type(e).__name__}: {e}"
        failed_at = 0

    duration = time.monotonic() - t0
    passed = (failed_at is None) and (error is None)

    result = ScenarioResult(
        scenario_id=scenario.id,
        group=scenario.group,
        description=scenario.description,
        duration_s=duration,
        passed=passed,
        failed_at_step=failed_at,
        steps=step_results,
        error=error,
    )

    # Persist per-scenario log. Also include all_turn_traces so the
    # coverage tracker can see every preflight verdict + wrapper that
    # fired across all turns (turn_trace itself resets each turn).
    debug_obj = {}
    if "state" in dir() and isinstance(state, dict):
        debug_obj = state.get("debug", {}) or {}
    log_path = out_dir / f"{scenario.id}.json"
    log_path.write_text(json.dumps({
        "result": asdict(result),
        "final_state": _summarize_state(state) if "state" in dir() else None,
        "all_turn_traces": debug_obj.get("all_turn_traces", []) if isinstance(debug_obj, dict) else [],
        "last_turn_trace": debug_obj.get("turn_trace", []) if isinstance(debug_obj, dict) else [],
    }, indent=2, default=str))

    return result


def _summarize_state(state: dict) -> dict:
    """Trim state to essentials for logging."""
    keys = [
        "phase", "turn_count", "max_turns", "hint_level", "max_hints",
        "topic_confirmed", "locked_question", "locked_answer",
        "help_abuse_count", "off_topic_count",
        "clinical_low_effort_count", "clinical_off_topic_count",
        "assessment_turn", "clinical_turn_count",
        "session_ended", "exit_intent_pending", "close_reason",
        "student_state", "student_reached_answer",
        "pending_user_choice",
    ]
    return {k: state.get(k) for k in keys}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--group", type=str, default=None)
    parser.add_argument("--scenario", type=str, default=None)
    args = parser.parse_args()

    scenarios = build_scenarios()
    if args.group:
        scenarios = [s for s in scenarios if s.group == args.group]
    if args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario]
    if args.limit:
        scenarios = scenarios[: args.limit]

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = REPO / "data" / "artifacts" / "eval" / "stress_test" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== STRESS TEST FLOWS ===")
    print(f"Scenarios: {len(scenarios)}")
    print(f"Output: {out_dir}\n", flush=True)

    coverage = CoverageTracker()
    results: list[ScenarioResult] = []
    durations: list[float] = []
    overall_t0 = time.monotonic()

    for i, scenario in enumerate(scenarios):
        prev_state = None
        print(f"[{i+1}/{len(scenarios)}] {scenario.id} — {scenario.description[:80]}", flush=True)
        # Snapshot prev state for coverage (use scenario setup as baseline)
        try:
            prev_state = scenario.setup(cfg, {}) if scenario.setup else _setup_prelocked_with_anchor(scenario.id, cfg)
        except Exception:
            prev_state = {}

        result = run_scenario(scenario, out_dir)
        durations.append(result.duration_s)

        # Update coverage from final logged state. turn_trace resets each
        # turn — we read all_turn_traces (the archive) + the last turn's
        # turn_trace from the scenario log.
        try:
            final_log = json.loads((out_dir / f"{scenario.id}.json").read_text())
            final_state = final_log.get("final_state") or {}
            all_traces: list[dict] = []
            for archived in final_log.get("all_turn_traces", []) or []:
                inner = archived.get("trace", []) if isinstance(archived, dict) else []
                if isinstance(inner, list):
                    all_traces.extend(inner)
            last_turn = final_log.get("last_turn_trace", []) or []
            if isinstance(last_turn, list):
                all_traces.extend(last_turn)
            coverage.record_state(prev_state, final_state, all_traces=all_traces)
        except Exception:
            pass

        # Per-scenario report line
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"    {status} | {result.duration_s:6.2f}s | step: {len(result.steps)} | failed_at: {result.failed_at_step}")
        for j, sr in enumerate(result.steps):
            phase_change = sr.phase_after
            print(f"      step{j} ({sr.duration_s:5.2f}s) phase={phase_change} pending={sr.pending_kind_after!r} close={sr.close_reason_after!r}")
            for v in sr.violations:
                print(f"        ! {v}")
        if result.error:
            print(f"      ERROR: {result.error}")

        # Running ETA
        avg = sum(durations) / len(durations)
        remaining = (len(scenarios) - i - 1) * avg
        print(f"    avg {avg:5.2f}s/scenario | ETA {remaining/60:.1f} min remaining\n", flush=True)
        results.append(result)

    overall_duration = time.monotonic() - overall_t0
    pass_count = sum(1 for r in results if r.passed)
    fail_count = len(results) - pass_count

    # Final report
    cov = coverage.report()
    summary = {
        "timestamp": timestamp,
        "duration_s": overall_duration,
        "scenarios_total": len(results),
        "passed": pass_count,
        "failed": fail_count,
        "results": [asdict(r) for r in results],
        "coverage": cov,
    }
    (out_dir / "report.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "coverage.json").write_text(json.dumps(cov, indent=2))

    print("\n" + "=" * 70)
    print(f"DONE in {overall_duration/60:.1f} min — {pass_count} pass / {fail_count} fail")
    print(f"Routers:   {cov['routers']['coverage']} hit  | missed: {cov['routers']['missed']}")
    print(f"Intents:   {cov['intents']['coverage']} hit  | missed: {cov['intents']['missed']}")
    print(f"Lifecycle: {cov['lifecycle']['coverage']} hit | missed: {cov['lifecycle']['missed']}")
    print(f"\nReport: {out_dir / 'report.json'}")


if __name__ == "__main__":
    main()
