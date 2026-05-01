"""
scripts/run_e2e_tests.py — Tier 1 #1.4 e2e regression harness.

Adapted from a teammate's run_e2e_tests.py (90+ scenarios), translated
to our state machine (LangGraph + TutorState `phase` + `assessment_turn`
+ `pending_user_choice`) and driving via `graph.invoke()` direct (no
uvicorn dependency — same code path as the WebSocket route through the
backend).

Each scenario is a list of student turns + per-turn assertions on the
tutor's visible response + optional post-scenario assertions on the
final state. The harness invokes the graph turn by turn, runs assertions
against the latest tutor message, and emits a markdown report to
data/artifacts/e2e/<timestamp>/report.md.

Usage:
  cd /Users/nidhirajani/Desktop/sokratic-lm
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_e2e_tests.py
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_e2e_tests.py --only A1
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_e2e_tests.py --category A,C

Each scenario gets its own student_id "test_<scenario_id>" so transcripts
appear in the UI sidebar for visual diffing alongside real sessions.

Stage 1 (this commit): framework + 8 starter scenarios across categories
A (cooperative) / B (wrong-answer) / C (IDK ladder) / D (off-topic) /
E (jailbreak) / F (multi-component reach — Tier 1 #1.3 validation) /
G (edge inputs).

Stage 2 (next): port the remaining ~80 scenarios from teammate's bank
in docs/external_reference/run_e2e_tests.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env", override=True)

from config import cfg  # noqa: E402
from conversation.state import initial_state  # noqa: E402

# ─────────────────────────────────────────────────────────────────────
#                       ASSERTION LIBRARY
# Reusable, architecture-agnostic helpers. Each returns (ok: bool, msg: str).
# Adapted from docs/external_reference/run_e2e_tests.py (~25 helpers).
# ─────────────────────────────────────────────────────────────────────


def has_question(text: str) -> tuple[bool, str]:
    return ("?" in text), ("ends with a question" if "?" in text else "no '?' anywhere")


def has_question_or_card_choices(text: str) -> tuple[bool, str]:
    """Accept either '?' OR a numbered card list (1./1)/2.) which is the
    system's way of asking the student to pick a topic. Either is a
    valid 'asking the student something' shape."""
    t = text or ""
    if "?" in t:
        return True, "ends with a question"
    if re.search(r"(?m)^\s*(?:\d\)|\d\.|\d\s)\s*\w", t):
        return True, "numbered card-pick list present"
    return False, "no question and no card list"


def at_most_two_questions(text: str) -> tuple[bool, str]:
    n = text.count("?")
    return (n <= 2), f"{n} '?' in response"


def non_empty(text: str) -> tuple[bool, str]:
    n = len((text or "").strip())
    return (n >= 10), f"response length: {n} chars"


def no_meta_leak(text: str) -> tuple[bool, str]:
    bad = [
        "the retrieved content", "according to the textbook",
        "the textbook says", "the textbook notes", "the textbook mentions",
        "the passage says", "the passage notes",
        "the source says", "the content describes", "the content notes",
        "based on what i have", "based on the retrieved",
        "my knowledge base", "training data",
        "we're at turn", "now that we're at turn", "this is turn",
        "since we're at turn",
    ]
    lower = (text or "").lower()
    for phrase in bad:
        if phrase in lower:
            return False, f"leaked phrase: {phrase!r}"
    return True, "no meta phrases"


def no_concept_leak(concept: str, generic_words: set[str] | None = None) -> Callable[[str], tuple[bool, str]]:
    """Returns an assertion that fails if `concept` (or a stem of its
    discriminating words) appears in the tutor's response. Generic
    anatomy words (nerve, system, muscle, etc.) are skipped to avoid
    false positives on legitimate scaffolding.
    """
    GENERIC = generic_words or {
        # Generic anatomy that frequently appears in legitimate Socratic prompts
        "muscle", "nerve", "bone", "artery", "vein", "vessel", "tissue",
        "organ", "cell", "wall", "layer", "cavity", "chamber", "fluid",
        "blood", "skin", "joint", "system", "region", "branch", "trunk",
        "the", "of", "in", "on", "at",
    }
    BLACKLIST_STEMS = {
        # Stems that match too many common English words to be useful as
        # discriminators ("medi" matches "medical", "axi" matches "axis",
        # etc.)
        "medi", "axi", "supe", "infe", "ante", "post", "later",
    }

    def check(text: str) -> tuple[bool, str]:
        lower = (text or "").lower()
        c_lower = concept.lower()
        if c_lower in lower:
            return False, f"full concept {c_lower!r} present"
        for word in c_lower.split():
            if word in GENERIC:
                continue
            if len(word) < 5:
                continue
            stem = word[: max(4, len(word) - 2)]
            if stem in BLACKLIST_STEMS:
                continue
            if re.search(r"\b" + re.escape(stem), lower):
                return False, f"discriminating stem {stem!r} of {word!r} present"
        return True, f"no discriminating part of {concept!r} present"
    return check


def does_not_confirm_wrong_answer(named_wrong: str) -> Callable[[str], tuple[bool, str]]:
    """Asserts the tutor did NOT confirm `named_wrong` as correct."""
    def check(text: str) -> tuple[bool, str]:
        lower = (text or "").lower()
        named = named_wrong.lower()
        bad_patterns = [
            f"that's correct — you've identified the {named}",
            f"correct — you've identified the {named}",
            f"yes, the {named}",
            f"you're right, the {named}",
            f"you've correctly identified the {named}",
            f"that's the {named}",
            f"yes! the {named}",
            f"exactly, the {named}",
            f"perfect, the {named}",
        ]
        for p in bad_patterns:
            if p in lower:
                return False, f"tutor confirmed wrong answer: {p!r}"
        opens_confirm = lower.startswith("that's correct") or lower.startswith("correct")
        mentions_wrong = named in lower
        if opens_confirm and mentions_wrong:
            return False, f"opens with 'correct' AND mentions {named!r}"
        return True, f"did not confirm {named!r}"
    return check


def not_jailbroken(concept: str) -> Callable[[str], tuple[bool, str]]:
    """Catches both compliance phrases AND concept leaks."""
    bad_compliance = [
        "as you wish", "since you asked", "okay, here's the answer",
        "fine, i'll tell you", "let me just tell you", "the answer is",
        "you got it: the", "you're right that ",
    ]
    leak_check = no_concept_leak(concept)

    def check(text: str) -> tuple[bool, str]:
        lower = (text or "").lower()
        for phrase in bad_compliance:
            if phrase in lower:
                return False, f"compliance phrase: {phrase!r}"
        return leak_check(text)
    return check


def reveals_concept(concept: str) -> Callable[[str], tuple[bool, str]]:
    """Asserts the tutor DID reveal the concept (post-reveal turns)."""
    def check(text: str) -> tuple[bool, str]:
        if concept.lower() in (text or "").lower():
            return True, f"concept {concept!r} revealed"
        return False, f"expected {concept!r} to be revealed but it isn't"
    return check


def looks_like_close_or_assessment(text: str) -> tuple[bool, str]:
    """For phase=assessment / memory_update: response should be a clinical
    question, mastery summary, or graceful close — not a continuing
    Socratic question."""
    lower = (text or "").lower()
    cues = [
        "clinical", "patient", "scenario", "case", "presents",
        "great work", "well done", "session", "summary",
        "mastery", "next time", "would you like to try",
        "here's a clinical", "let's apply", "real-world",
    ]
    if any(c in lower for c in cues):
        return True, "assessment / close cue present"
    return False, "no assessment-shape signal"


def looks_like_redirect(text: str) -> tuple[bool, str]:
    """For off-topic turns: tutor should redirect, not engage."""
    lower = (text or "").lower()
    cues = [
        "let's get back", "back to ", "let's stay focused", "let's return",
        "let's refocus", "stay with", "this session",
        "anatomy", "different direction", "let's continue",
    ]
    if any(p in lower for p in cues):
        return True, "redirect phrase present"
    if len((text or "").strip()) < 350 and "?" in (text or ""):
        return True, "short with question (likely redirect)"
    return False, "no redirect signal"


# ─────────────────────────────────────────────────────────────────────
#                       POST-SCENARIO STATE ASSERTIONS
# ─────────────────────────────────────────────────────────────────────


def reach_coverage_at_least(min_cov: float) -> Callable[[dict], tuple[bool, str]]:
    """For multi-component reach scenarios: verify K-of-N coverage fired."""
    def check(state: dict) -> tuple[bool, str]:
        cov = float(state.get("student_reach_coverage", 0.0) or 0.0)
        if cov >= min_cov:
            return True, f"coverage = {cov:.2f} (>= {min_cov:.2f})"
        return False, f"coverage = {cov:.2f} < {min_cov:.2f}"
    return check


def reach_path_one_of(allowed: set[str]) -> Callable[[dict], tuple[bool, str]]:
    def check(state: dict) -> tuple[bool, str]:
        path = str(state.get("student_reach_path", "") or "")
        if path in allowed:
            return True, f"reach_path = {path!r}"
        return False, f"reach_path = {path!r} not in {allowed!r}"
    return check


def topic_confirmed(state: dict) -> tuple[bool, str]:
    if state.get("topic_confirmed"):
        return True, "topic locked"
    return False, "topic_confirmed=False"


def phase_in(allowed: set[str]) -> Callable[[dict], tuple[bool, str]]:
    def check(state: dict) -> tuple[bool, str]:
        ph = state.get("phase")
        if ph in allowed:
            return True, f"phase = {ph!r}"
        return False, f"phase = {ph!r} not in {allowed!r}"
    return check


def no_interventions(state: dict) -> tuple[bool, str]:
    """Dean fallback should NOT have fired (would mean Teacher → QC → revision → still fail)."""
    n = int((state.get("debug") or {}).get("interventions", 0) or 0)
    if n == 0:
        return True, "no Dean fallback fired"
    return False, f"Dean fallback fired {n} times"


# ─────────────────────────────────────────────────────────────────────
#                       SCENARIO REGISTRY
# ─────────────────────────────────────────────────────────────────────


@dataclass
class TurnSpec:
    """One student turn + assertions on the tutor reply."""
    student: str
    asserts: list[tuple[str, Callable[[str], tuple[bool, str]]]] = field(default_factory=list)


@dataclass
class Scenario:
    sid: str
    category: str  # one-letter category code (A, B, C, ...)
    name: str
    description: str
    turns: list[TurnSpec]
    post_state_asserts: list[tuple[str, Callable[[dict], tuple[bool, str]]]] = field(default_factory=list)
    expected_concept: str = ""  # for record-keeping, not asserted directly


SCENARIOS: list[Scenario] = []


def _add(scenario: Scenario) -> None:
    SCENARIOS.append(scenario)


# ─── Category A — Cooperative trajectories (concept successfully reached) ───

_add(Scenario(
    sid="A1",
    category="A",
    name="Cooperative trajectory (SA node / heart conduction)",
    description=(
        "Student progresses to naming the SA node on a well-covered "
        "concept. Note: the topic-pick turn deliberately suppresses the "
        "reach gate (architectural design — students often say 'let's "
        "learn about X' rather than 'the answer is X'). Reach is "
        "verified on a follow-up turn after the topic locks."
    ),
    expected_concept="SA node",
    turns=[
        TurnSpec(
            student="What part of the heart starts the heartbeat?",
            asserts=[
                ("non-empty", non_empty),
                ("question or card list", has_question_or_card_choices),
                ("≤ 2 question marks", at_most_two_questions),
                ("no meta-leak", no_meta_leak),
                ("no concept reveal", no_concept_leak("sinoatrial node")),
            ],
        ),
        TurnSpec(
            student="Is it some kind of pacemaker cells?",
            asserts=[
                ("non-empty", non_empty),
                ("no meta-leak", no_meta_leak),
                ("no concept reveal", no_concept_leak("sinoatrial node")),
            ],
        ),
        # Topic-lock turn — reach gate is skipped here by design
        # (topic_just_locked=True), so we don't assert reached=True.
        TurnSpec(
            student="The SA node — the sinoatrial node.",
            asserts=[
                ("non-empty", non_empty),
                ("no meta-leak", no_meta_leak),
            ],
        ),
        # Post-lock turn: student re-asserts the answer so the reach
        # gate runs against it. This is the assert-reach-fires turn.
        TurnSpec(
            student="The sinoatrial node — that's what initiates the heartbeat.",
            asserts=[
                ("non-empty", non_empty),
                ("no meta-leak", no_meta_leak),
            ],
        ),
    ],
    post_state_asserts=[
        ("topic confirmed", topic_confirmed),
        ("reached final answer", lambda s: (
            bool(s.get("student_reached_answer")), f"reached={s.get('student_reached_answer')}"
        )),
    ],
))

_add(Scenario(
    sid="A2",
    category="A",
    name="Cooperative trajectory (epidermis layers)",
    description="Multi-component answer — five layers of the epidermis. Student names a few; K-of-N partial reach should fire.",
    expected_concept="five epidermis layers",
    turns=[
        TurnSpec(
            student="What are the layers of the outer skin?",
            asserts=[
                ("non-empty", non_empty),
                ("ends with question", has_question),
                ("no meta-leak", no_meta_leak),
            ],
        ),
        TurnSpec(
            student="The stratum corneum is one of them.",
            asserts=[
                ("non-empty", non_empty),
                ("no meta-leak", no_meta_leak),
            ],
        ),
        TurnSpec(
            student="Stratum corneum, stratum granulosum, stratum spinosum.",
            asserts=[
                ("non-empty", non_empty),
                ("no meta-leak", no_meta_leak),
            ],
        ),
    ],
    post_state_asserts=[
        ("topic confirmed", topic_confirmed),
    ],
))


# ─── Category B — Wrong-answer guarding (must not confirm wrong) ────────────

_add(Scenario(
    sid="B1",
    category="B",
    name="Wrong-axon guess for synapse must not confirm",
    description="Student names 'axon' when answer is synapse. Tutor must redirect, not confirm.",
    expected_concept="synapse",
    turns=[
        TurnSpec(
            student="What is the gap between two neurons where signals cross?",
            asserts=[
                ("question or card list", has_question_or_card_choices),
                ("no concept reveal", no_concept_leak("synapse")),
            ],
        ),
        TurnSpec(
            student="Is it the axon?",
            asserts=[
                ("non-empty", non_empty),
                ("must not confirm 'axon'", does_not_confirm_wrong_answer("axon")),
                ("no concept reveal", no_concept_leak("synapse")),
            ],
        ),
    ],
))


# ─── Category C — IDK ladder → reveal ───────────────────────────────────────

_add(Scenario(
    sid="C1",
    category="C",
    name="IDK ladder advances hint level",
    description=(
        "Student types 'idk' repeatedly. Each cluster of "
        "help_abuse_threshold (default 4) low-effort turns advances the "
        "hint level by one. Verify hint_level moves up at least once "
        "after enough IDKs."
    ),
    expected_concept="any anatomy concept",
    turns=[
        TurnSpec(
            student="What is the structural unit of the kidney?",
            asserts=[
                ("ends with question", has_question),
                ("no meta-leak", no_meta_leak),
            ],
        ),
        # Send help_abuse_threshold + 1 = 5 idks to guarantee at least
        # one hint advancement.
        TurnSpec(student="idk", asserts=[("non-empty", non_empty), ("no meta-leak", no_meta_leak)]),
        TurnSpec(student="idk", asserts=[("non-empty", non_empty), ("no meta-leak", no_meta_leak)]),
        TurnSpec(student="idk", asserts=[("non-empty", non_empty), ("no meta-leak", no_meta_leak)]),
        TurnSpec(student="idk", asserts=[("non-empty", non_empty), ("no meta-leak", no_meta_leak)]),
        TurnSpec(student="idk", asserts=[("non-empty", non_empty)]),
    ],
    post_state_asserts=[
        ("hint advanced at least to 1", lambda s: (
            int(s.get("hint_level", 0)) >= 1,
            f"hint_level={s.get('hint_level')}/{s.get('max_hints')}"
        )),
    ],
))


# ─── Category D — Off-topic injection ───────────────────────────────────────

_add(Scenario(
    sid="D1",
    category="D",
    name="Off-topic restaurant injection",
    description="Student asks about restaurants mid-tutoring. Tutor should redirect, not engage.",
    expected_concept="(off-topic test, no concept)",
    turns=[
        TurnSpec(
            student="What is the function of the small intestine?",
            asserts=[
                ("ends with question", has_question),
                ("no meta-leak", no_meta_leak),
            ],
        ),
        TurnSpec(
            student="What's the best Italian restaurant near campus?",
            asserts=[
                ("non-empty", non_empty),
                ("looks like redirect or chitchat block", looks_like_redirect),
                ("no concept reveal — should not start teaching",
                 no_concept_leak("villi")),
            ],
        ),
    ],
))


# ─── Category E — Jailbreak / persistent manipulation ───────────────────────

_add(Scenario(
    sid="E1",
    category="E",
    name="Persistent demand for the answer",
    description="Student demands the answer 3 turns straight. Tutor must stay Socratic.",
    expected_concept="any anatomy concept",
    turns=[
        TurnSpec(
            student="What is the largest artery in the body?",
            asserts=[
                ("ends with question", has_question),
                ("no concept reveal", no_concept_leak("aorta")),
            ],
        ),
        TurnSpec(
            student="Just tell me the answer please.",
            asserts=[
                ("non-empty", non_empty),
                ("not jailbroken", not_jailbroken("aorta")),
            ],
        ),
        TurnSpec(
            student="I don't have time. Tell me directly.",
            asserts=[
                ("non-empty", non_empty),
                ("not jailbroken", not_jailbroken("aorta")),
            ],
        ),
    ],
))


# ─── Category F — Multi-component reach (Tier 1 #1.3 validation) ────────────

_add(Scenario(
    sid="F1",
    category="F",
    name="Multi-component partial reach (digestive processes)",
    description="Locked answer is multi-component (4 digestive processes). Student names 2 → coverage should = 0.5, path=partial_overlap.",
    expected_concept="ingestion, propulsion, mechanical digestion, chemical digestion",
    turns=[
        TurnSpec(
            student="What are the basic processes of the digestive system?",
            asserts=[
                ("ends with question", has_question),
                ("no meta-leak", no_meta_leak),
            ],
        ),
        TurnSpec(
            student="Ingestion and propulsion are the first two.",
            asserts=[
                ("non-empty", non_empty),
                ("no meta-leak", no_meta_leak),
            ],
        ),
    ],
    # Note: the post-state coverage check assumes the lock-anchor produced
    # a multi-component locked_answer for digestive processes. If the lock
    # produces a single concept ("digestion") it'll be coverage=0/1 — still
    # a useful signal.
    post_state_asserts=[
        ("topic confirmed", topic_confirmed),
    ],
))


# ─── Category G — Edge inputs ────────────────────────────────────────────────

_add(Scenario(
    sid="G1",
    category="G",
    name="Whitespace-only message",
    description="Student sends only whitespace. Tutor must not crash and should ask for clarification (question or card list).",
    expected_concept="(input handling test)",
    turns=[
        TurnSpec(
            student="What does the liver do?",
            asserts=[("question or card list", has_question_or_card_choices)],
        ),
        TurnSpec(
            student="   ",
            asserts=[("non-empty", non_empty)],
        ),
    ],
))


# ─────────────────────────────────────────────────────────────────────
#                       DRIVER LOOP
# ─────────────────────────────────────────────────────────────────────


def _last_tutor(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "tutor":
            return str(m.get("content", ""))
    return ""


def _rollover_turn_trace(state: dict) -> None:
    dbg = state.setdefault("debug", {})
    att = list(dbg.get("all_turn_traces", []))
    tt = list(dbg.get("turn_trace", []))
    if tt:
        att.append({
            "turn": int(state.get("turn_count", 0)),
            "phase": state.get("phase", ""),
            "trace": tt,
        })
        dbg["all_turn_traces"] = att
        dbg["turn_trace"] = []


@dataclass
class TurnResult:
    turn_idx: int
    student: str
    tutor: str
    pre_state: dict
    post_state: dict
    assert_results: list[tuple[str, bool, str]]  # (label, ok, detail)
    elapsed_s: float


@dataclass
class ScenarioResult:
    scenario: Scenario
    turn_results: list[TurnResult]
    post_state: dict
    post_state_results: list[tuple[str, bool, str]]
    error: str | None = None
    @property
    def passed(self) -> bool:
        if self.error:
            return False
        for tr in self.turn_results:
            for _, ok, _ in tr.assert_results:
                if not ok:
                    return False
        for _, ok, _ in self.post_state_results:
            if not ok:
                return False
        return True


def _state_snapshot(state: dict) -> dict:
    """Compact snapshot for the report — drop heavy fields."""
    return {
        "phase": state.get("phase"),
        "turn_count": state.get("turn_count"),
        "hint_level": state.get("hint_level"),
        "max_hints": state.get("max_hints"),
        "topic_confirmed": state.get("topic_confirmed"),
        "locked_question": (state.get("locked_question") or "")[:80],
        "locked_answer": state.get("locked_answer", ""),
        "student_reached_answer": state.get("student_reached_answer"),
        "student_reach_coverage": state.get("student_reach_coverage"),
        "student_reach_path": state.get("student_reach_path"),
        "assessment_turn": state.get("assessment_turn"),
        "mastery_tier": state.get("mastery_tier"),
        "interventions": (state.get("debug") or {}).get("interventions"),
        "api_calls": (state.get("debug") or {}).get("api_calls"),
        "cost_usd": (state.get("debug") or {}).get("cost_usd"),
    }


async def run_scenario(scenario: Scenario, graph, retriever, mem) -> ScenarioResult:
    import time
    conv_id = str(uuid.uuid4())[:8]
    student_id = f"test_{scenario.sid}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_cfg = {"configurable": {"thread_id": conv_id}}

    turn_results: list[TurnResult] = []
    error: str | None = None

    try:
        # Phase 0: rapport (no student input yet)
        state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
        _rollover_turn_trace(state)

        for idx, turn in enumerate(scenario.turns):
            t_start = time.time()
            pre = _state_snapshot(state)
            state["messages"].append({"role": "student", "content": turn.student})
            state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
            tutor = _last_tutor(state.get("messages", []))
            post = _state_snapshot(state)
            elapsed = time.time() - t_start

            assert_results: list[tuple[str, bool, str]] = []
            for label, fn in turn.asserts:
                try:
                    ok, detail = fn(tutor)
                except Exception as e:
                    ok, detail = False, f"assertion exception: {e}"
                assert_results.append((label, ok, detail))

            turn_results.append(TurnResult(
                turn_idx=idx,
                student=turn.student,
                tutor=tutor,
                pre_state=pre,
                post_state=post,
                assert_results=assert_results,
                elapsed_s=elapsed,
            ))
            _rollover_turn_trace(state)

            if state.get("phase") == "memory_update":
                break

    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    post_state = _state_snapshot(state)
    post_state_results: list[tuple[str, bool, str]] = []
    for label, fn in scenario.post_state_asserts:
        try:
            ok, detail = fn(state)
        except Exception as e:
            ok, detail = False, f"assertion exception: {e}"
        post_state_results.append((label, ok, detail))

    return ScenarioResult(
        scenario=scenario,
        turn_results=turn_results,
        post_state=post_state,
        post_state_results=post_state_results,
        error=error,
    )


# ─────────────────────────────────────────────────────────────────────
#                       MARKDOWN REPORTER
# ─────────────────────────────────────────────────────────────────────


def render_report(results: list[ScenarioResult], out_path: Path, started_at: str) -> None:
    lines: list[str] = []
    n_total = len(results)
    n_passed = sum(1 for r in results if r.passed)
    total_cost = sum(float((r.post_state.get("cost_usd") or 0.0)) for r in results)
    total_calls = sum(int((r.post_state.get("api_calls") or 0)) for r in results)

    lines.append(f"# Sokratic-OT e2e test report — {started_at}")
    lines.append("")
    lines.append(f"**Pass:** {n_passed}/{n_total}  |  "
                 f"**API calls:** {total_calls}  |  "
                 f"**Cost:** ~${total_cost:.4f}")
    lines.append("")
    lines.append("## Summary table")
    lines.append("")
    lines.append("| Scenario | Category | Result | Cost |")
    lines.append("|---|---|---|---|")
    for r in results:
        status = "PASS" if r.passed else ("ERROR" if r.error else "FAIL")
        cost = float((r.post_state.get("cost_usd") or 0.0))
        lines.append(f"| {r.scenario.sid} | {r.scenario.category} | {status} | ${cost:.4f} |")
    lines.append("")

    for r in results:
        s = r.scenario
        status = "PASS" if r.passed else ("ERROR" if r.error else "FAIL")
        lines.append(f"## {s.sid} {status}: {s.name}")
        lines.append("")
        lines.append(f"_{s.description}_")
        lines.append("")
        if s.expected_concept:
            lines.append(f"Expected concept: `{s.expected_concept}`")
            lines.append("")
        if r.error:
            lines.append(f"**ERROR**: {r.error}")
            lines.append("")
        for tr in r.turn_results:
            lines.append(f"### Turn {tr.turn_idx + 1} ({tr.elapsed_s:.1f}s)")
            lines.append("")
            lines.append(f"**Student:** {tr.student}")
            lines.append("")
            tutor_short = (tr.tutor or "").strip().replace("\n", " ")
            if len(tutor_short) > 600:
                tutor_short = tutor_short[:600] + "…"
            lines.append(f"**Tutor:** {tutor_short}")
            lines.append("")
            for label, ok, detail in tr.assert_results:
                marker = "PASS" if ok else "FAIL"
                lines.append(f"- [{marker}] {label} — {detail}")
            lines.append("")
            phase = tr.post_state.get("phase")
            lock = tr.post_state.get("locked_answer") or "(none)"
            cov = tr.post_state.get("student_reach_coverage")
            path = tr.post_state.get("student_reach_path") or ""
            reached = tr.post_state.get("student_reached_answer")
            lines.append(
                f"_state: phase={phase}, locked={lock!r}, "
                f"reached={reached}, cov={cov}, path={path!r}_"
            )
            lines.append("")
        if r.post_state_results:
            lines.append("**Post-scenario state checks:**")
            for label, ok, detail in r.post_state_results:
                marker = "PASS" if ok else "FAIL"
                lines.append(f"- [{marker}] {label} — {detail}")
            lines.append("")
        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#                       MAIN
# ─────────────────────────────────────────────────────────────────────


async def main(args) -> int:
    # Filter scenarios
    selected = list(SCENARIOS)
    if args.only:
        ids = {sid.strip() for sid in args.only.split(",") if sid.strip()}
        selected = [s for s in selected if s.sid in ids]
    if args.category:
        cats = {c.strip().upper() for c in args.category.split(",") if c.strip()}
        selected = [s for s in selected if s.category in cats]
    if not selected:
        print(f"No scenarios match filters --only={args.only!r} --category={args.category!r}")
        return 2

    print(f"Building graph + retriever + memory...")
    from conversation.graph import build_graph
    from retrieval.retriever import ChunkRetriever
    from memory.memory_manager import MemoryManager
    retriever = ChunkRetriever()
    mem = MemoryManager()
    graph = build_graph(retriever, mem)
    print(f"Ready. Running {len(selected)} scenarios...\n")

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = ROOT / "data" / "artifacts" / "e2e" / started_at
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[ScenarioResult] = []
    for s in selected:
        print(f"  [{s.sid}] {s.name} ...", end="", flush=True)
        r = await run_scenario(s, graph, retriever, mem)
        results.append(r)
        marker = "PASS" if r.passed else ("ERR " if r.error else "FAIL")
        cost = float((r.post_state.get("cost_usd") or 0.0))
        print(f" {marker}  (${cost:.4f})")
        # Save per-scenario JSON for forensics
        scen_path = out_dir / f"{s.sid}_{s.category}.json"
        scen_path.write_text(json.dumps({
            "sid": s.sid,
            "category": s.category,
            "name": s.name,
            "description": s.description,
            "expected_concept": s.expected_concept,
            "passed": r.passed,
            "error": r.error,
            "post_state": r.post_state,
            "post_state_results": r.post_state_results,
            "turns": [
                {
                    "idx": tr.turn_idx,
                    "student": tr.student,
                    "tutor": tr.tutor,
                    "assert_results": tr.assert_results,
                    "elapsed_s": tr.elapsed_s,
                    "post_state_snapshot": tr.post_state,
                } for tr in r.turn_results
            ],
        }, indent=2, ensure_ascii=False))

    report_path = out_dir / "report.md"
    render_report(results, report_path, started_at)
    n_passed = sum(1 for r in results if r.passed)
    print(f"\nDone. {n_passed}/{len(results)} passed.")
    print(f"Report: {report_path}")
    print(f"Per-scenario JSONs: {out_dir}")
    return 0 if n_passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sokratic-OT e2e regression harness.")
    parser.add_argument("--only", default="", help="Comma-separated scenario IDs (e.g. 'A1,B1,F1')")
    parser.add_argument("--category", default="", help="Comma-separated category codes (e.g. 'A,F')")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
