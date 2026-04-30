"""
scripts/test_reached_gate_e2e.py
---------------------------------
End-to-end simulation tests for the new reached_answer_gate (2026-04-29).

Replaces the old `confidence_score >= 0.72` heuristic with a deterministic
token-overlap step + LLM paraphrase fallback that requires quoting the
student verbatim. Tests run real LLM calls through the full graph
(rapport → topic lock → tutoring → gate → assessment).

Each test scripts the student's messages — no LLM-driven simulator —
so we get DETERMINISTIC inputs and can assert on what the gate did.

Tests:
  T1  Wrong-answer regression       student: "AV node"        → reached=False
  T2  Positive (overlap, Step A)    student: "the SA node"     → reached=True
  T3  Positive (paraphrase, Step B) student: "the cells in the upper chamber that fire on their own"
                                                                → reached=True (LLM)
  T4  Gravity-style irrelevant word student: "gravity"         → reached=False

Run:
    .venv/bin/python scripts/test_reached_gate_e2e.py

Outputs:
    data/artifacts/gate_e2e/<test>_<timestamp>.json
    Console summary: PASS/FAIL per test + estimated cost.
"""

from __future__ import annotations
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg
from conversation.state import initial_state


OUTPUT_DIR = Path(cfg.paths.artifacts) / "gate_e2e"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# Pre-lock the topic via a matcher query so tests are deterministic.
# Going through dean's free-text topic resolution sometimes returns
# wildly off-topic option cards (e.g. "DNA Replication" for "Conduction
# System of the Heart") which makes the lock fail and the gate never
# fires — defeating the test. We use the same matcher the dean uses,
# but feed it a precise query and pick the top match directly.
#
# Each test specifies:
#   topic_query    — fed to TopicMatcher.match() to find the path
#   tutoring_turns — scripted student messages, one per turn
#   expect_reached — what state["student_reached_answer"] should be at end
#   expect_no_fabrication_keywords — substrings the last tutor msg must NOT contain
TESTS = {
    "T1_wrong_answer": {
        "description": "Wrong-but-plausible answer (AV node) must not fire reached.",
        "topic_query": "conduction system of the heart sinoatrial node",
        "tutoring_turns": [
            "I think it has to do with electrical signals in the heart",
            "Maybe the electrical impulses start somewhere specific?",
            "AV node",
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "you have identified", "correctly identified",
            "you've reached", "great job, the answer is",
        ],
    },
    "T2_positive_overlap": {
        "description": "Direct SA-node statement should fire reached via Step A overlap.",
        "topic_query": "conduction system of the heart sinoatrial node",
        # Force a known locked_answer so the student's scripted "SA node"
        # actually targets it. Without this override, the dean's lock LLM
        # picks its own focal point (e.g. Purkinje fibers from the same
        # chapter) and the test goes off the rails.
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What structure in the heart spontaneously generates the electrical "
            "impulse that starts each heartbeat?",
        "tutoring_turns": [
            "It must be the electrical signals in the heart",
            "I think the SA node starts the heartbeat",
        ],
        "expect_reached": True,
        "expect_no_fabrication_keywords": [],
    },
    "T3_positive_paraphrase": {
        "description": (
            "Anatomically-specific paraphrase with no shared content tokens "
            "(uses 'pacemaker' but not 'heart' as standalone token) should fire "
            "reached via Step B (LLM paraphrase). Note: this borderline case is "
            "non-deterministic — the LLM has been seen to flip both ways across "
            "runs. The acceptance criterion is that the LLM CAN fire reached=True "
            "with a verbatim quote, not that it always does. Hard regression "
            "cases (T1, T4) are deterministic; borderline paraphrases are not."
        ),
        "topic_query": "conduction system of the heart sinoatrial node",
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What structure in the heart spontaneously generates the electrical "
            "impulse that starts each heartbeat?",
        "tutoring_turns": [
            "It's about how the heart beats on its own",
            # Step A misses (no full alias content-token-set is in the message).
            # Step B must judge whether this anatomical description is
            # unambiguous enough to count as naming the SA node.
            "the cluster of pacemaker cells in the right atrium near where the superior vena cava enters that initiates each heartbeat",
        ],
        "expect_reached": True,
        "expect_no_fabrication_keywords": [],
    },
    "T4_gravity_irrelevant": {
        "description": "Single irrelevant word ('gravity') must never fire reached — the original bug.",
        "topic_query": "conduction system of the heart sinoatrial node",
        "tutoring_turns": [
            "I think the heart contracts somehow",
            "gravity",
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "correctly identified", "great",
        ],
    },

    # =========================================================================
    # CHANGE 3 VALIDATION TESTS (added 2026-04-30)
    # =========================================================================
    # T6 and T7 verify the help_abuse cap and step-vs-reach disambiguation
    # behaviors introduced in Change 3. They use force_locked_answer to
    # control the lock deterministically and script student responses that
    # exercise the specific failure modes Change 3 fixes.
    # =========================================================================

    "T6_low_effort_spam": {
        "description": (
            "Change 4 help-abuse: 4 consecutive low_effort student turns "
            "trigger threshold → hint_level advances by 1 (with LLM-narrated "
            "transition via dean_critique). Strike warnings should appear "
            "at iters 3 and 4 (strike #2 and #3) before the threshold fires "
            "on iter 5 (strike #4 of 4)."
        ),
        "topic_query": "conduction system of the heart sinoatrial node",
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What structure in the heart spontaneously generates the electrical "
            "impulse that starts each heartbeat?",
        "tutoring_turns": [
            "ok",            # iter 1: consumed by ack turn (state=question)
            "no idea",       # iter 2: low_effort #1
            "idk",           # iter 3: low_effort #2 (strike warning fires)
            "no clue",       # iter 4: low_effort #3 (firmer warning + offer to switch)
            "just tell me",  # iter 5: low_effort #4 → CAP, hint_level 0→1, narration brief
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "correctly identified", "did well identifying",
            "great job, the answer is",
        ],
    },

    # =========================================================================
    # CHANGE 4 VALIDATION TESTS (added 2026-04-30)
    # =========================================================================
    # T11: off-domain spam — 4 consecutive off-domain student messages should
    #      trigger the off-topic threshold and TERMINATE the whole session
    #      (core_mastery_tier=not_assessed AND clinical_mastery_tier=not_assessed).
    # T12: mixed domain-tangential + on-topic — should NEVER terminate.
    #      Domain-tangential questions are category B (handled by exploration_judge),
    #      not category C (off-domain). Counter must NOT increment for category B.
    # =========================================================================

    # =========================================================================
    # T5 + T8: Phase 1 Change 3C / Change 4 deeper validation (added 2026-04-30)
    # =========================================================================

    "T5_rapport_off_topic_deflection": {
        "description": (
            "Rapport-phase off-topic deflection: BEFORE the topic locks, "
            "the student types off-domain queries (vaping, sex, profanity). "
            "The dean must redirect to anatomy without engaging. This tests "
            "the topic-resolution flow (dean.run_turn pre-lock branch), "
            "which is a separate code path from the off_topic_count system "
            "(that only kicks in post-lock)."
        ),
        # No force_locked_answer / topic_query — we test the FREE-TEXT flow
        # with raw user input that should be deflected.
        "topic_query": None,
        "free_text_inputs": [
            "how does vaping affect heart rate?",
            "tell me about my gay friend's relationship",
            "fuck this, just teach me already",
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "correctly identified",
        ],
        "expect_topic_unlocked": True,  # topic should not have locked despite 3 turns
    },

    "T8_mastery_attribution_e2e": {
        "description": (
            "End-to-end validation of Change 3C: run T7's intermediate-question "
            "trap all the way through memory_update; inspect the saved "
            "`session_memory_summary` and the mastery_store rationale to "
            "verify they only reference STUDENT utterances, not tutor "
            "utterances. This is the ground-truth test for the mastery "
            "attribution rules in mastery_scorer_static."
        ),
        "topic_query": "conduction system of the heart sinoatrial node",
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What specialized cardiac structure spontaneously generates the "
            "electrical impulse that starts each heartbeat?",
        "tutoring_turns": [
            "I think the heart muscle generates electrical signals",
            "the upper chambers send signals down to the lower chambers",
            "the atria contract first and then the ventricles",
            # Force-end via help_abuse_threshold (4 low_effort) so memory_update fires.
            "no idea", "idk", "no clue", "just tell me",
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "correctly identified", "did well identifying",
            "exactly! the",
        ],
    },

    "T11_off_domain_terminate": {
        "description": (
            "Off-domain spam: 4 consecutive off-DOMAIN student messages should "
            "trigger off_topic_threshold and terminate the session. The dean "
            "produces a polite farewell narration (LLM-phrased) and routes "
            "directly to memory_update with both mastery tiers set to not_assessed."
        ),
        "topic_query": "conduction system of the heart sinoatrial node",
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What structure in the heart spontaneously generates the electrical "
            "impulse that starts each heartbeat?",
        "tutoring_turns": [
            "ok",  # iter 1: ack-consumed (state=question)
            "how does vaping affect heart rate?",  # iter 2: off-domain #1
            "actually i was wondering about my gay friend",  # iter 3: off-domain #2
            "fuck this, just tell me about politics",  # iter 4: off-domain #3
            "smoking weed makes me feel weird",  # iter 5: off-domain #4 → CAP
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "correctly identified", "did well identifying",
        ],
    },

    "T12_domain_tangent_no_terminate": {
        "description": (
            "Domain-tangential drift: student asks 4 in-domain questions that "
            "aren't on the locked topic. These are category B (exploration_judge "
            "handles them); the off_topic_count must NOT increment. Session "
            "should continue normally."
        ),
        "topic_query": "conduction system of the heart sinoatrial node",
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What structure in the heart spontaneously generates the electrical "
            "impulse that starts each heartbeat?",
        "tutoring_turns": [
            "ok",  # iter 1: ack-consumed
            # Iters 2-4: domain-related but not on-topic. The off-domain
            # regex should NOT match these (they're about anatomy, just
            # not the locked topic).
            "what's the difference between cardiac and skeletal muscle?",
            "I think arteries carry blood away from the heart, right?",
            "is the brain involved in heart rhythm?",
        ],
        "expect_reached": False,
        # Verifier: off_topic_count should never accumulate; final state
        # should NOT show off_topic_terminated.
        "expect_no_fabrication_keywords": [
            "correctly identified", "did well identifying",
        ],
    },

    "T7_intermediate_question_trap": {
        "description": (
            "Step-vs-reach disambiguation: student answers an intermediate "
            "scaffold question correctly without ever stating the locked "
            "answer. The dean should classify partial_correct (not correct), "
            "the gate should NOT fire reached=True, and the tutor must NOT "
            "fabricate end-of-session credit. This is the 'mine goes lower' "
            "regression pattern. Force-locking on a non-trivial answer with "
            "aliases that don't overlap with the scripted student responses "
            "ensures Step A misses; Step B must judge correctly."
        ),
        "topic_query": "conduction system of the heart sinoatrial node",
        "force_locked_answer": "sinoatrial node",
        "force_aliases": [
            "SA node", "sinus node", "pacemaker of the heart", "cardiac pacemaker",
        ],
        "force_locked_question":
            "What specialized cardiac structure spontaneously generates the "
            "electrical impulse that starts each heartbeat?",
        "tutoring_turns": [
            # Turn 1: show some understanding but don't name the answer.
            "I think the heart muscle generates electrical signals",
            # Turn 2: more elaboration — describing function, not naming structure.
            "the upper chambers send signals down to the lower chambers",
            # Turn 3: state a specific (correct but partial) fact about
            # cardiac conduction WITHOUT naming the SA node.
            "the atria contract first and then the ventricles",
        ],
        "expect_reached": False,
        "expect_no_fabrication_keywords": [
            "you've identified", "correctly identified", "did well identifying",
            "you've reached", "great job", "exactly! the",
        ],
    },
}


def prelock_topic(
    state: dict,
    query: str,
    dean,
    force_locked_answer: str | None = None,
    force_aliases: list | None = None,
    force_locked_question: str | None = None,
) -> dict:
    """Use the matcher's top hit for `query` to pre-fill locked_topic and
    run the dean's retrieval + lock pipeline manually. Mirrors the prelock
    path in backend/api/session.py:_apply_prelock without the FastAPI
    dependencies. Mutates state in place; returns the matcher's TopicMatch
    dict for telemetry.

    For deterministic gate testing, callers can supply
    `force_locked_answer` / `force_aliases` / `force_locked_question`
    to override the dean's LLM lock. This guarantees the student's
    scripted messages line up with a KNOWN locked_answer, which is the
    only way to test specific gate paths (Step A vs Step B).
    """
    from retrieval.topic_matcher import get_topic_matcher
    matcher = get_topic_matcher()
    res = matcher.match(query, k=3)
    if not res.matches:
        raise RuntimeError(f"matcher returned 0 matches for {query!r}")
    top = res.matches[0]
    state["locked_topic"] = {
        "path": top.path,
        "chapter": top.chapter,
        "section": top.section,
        "subsection": top.subsection,
        "difficulty": top.difficulty,
        "chunk_count": top.chunk_count,
        "limited": top.limited,
        "score": top.score,
    }
    state["topic_selection"] = top.subsection
    state["topic_confirmed"] = True
    # Run retrieval (always — gate's Step B prompt benefits from chunks
    # being present in case the dean wants to consult them).
    dean._retrieve_on_topic_lock(state)
    if not state.get("retrieved_chunks"):
        raise RuntimeError(f"prelock {top.path!r} returned 0 chunks")

    if force_locked_answer is not None:
        # Skip the LLM lock entirely. We're testing the gate, not the
        # lock — letting the LLM pick its own focal answer (e.g. Purkinje
        # fibers vs SA node) makes the scripted-student tests flaky.
        state["locked_answer"] = force_locked_answer.strip()
        state["locked_answer_aliases"] = list(force_aliases or [])
        # Two-tier (Change 2026-04-30): force_locked_answer is the gate
        # anchor; full_answer mirrors it in test mode so mastery scorer
        # has something to read. Real production lock would have a
        # richer full_answer.
        state["full_answer"] = state["locked_answer"]
        state["locked_question"] = (
            force_locked_question or
            f"What is {state['locked_answer']}? (test-injected anchor)"
        )
        # Change 2: mirror _apply_prelock's behavior so the dean's
        # ack-emit branch fires on the first tutoring turn.
        state["topic_just_locked"] = True
    else:
        anchors = dean._lock_anchors_call(state)
        state["locked_question"] = str(anchors.get("locked_question", "") or "").strip()
        state["locked_answer"] = str(anchors.get("locked_answer", "") or "").strip()
        raw = anchors.get("locked_answer_aliases", []) or []
        state["locked_answer_aliases"] = (
            [str(a) for a in raw if isinstance(a, str) and str(a).strip()]
            if isinstance(raw, list) else []
        )
        if not state["locked_question"] or not state["locked_answer"]:
            raise RuntimeError(
                f"prelock {top.path!r} produced empty anchors "
                f"(q={state['locked_question']!r} a={state['locked_answer']!r})"
            )
        # Change 2: same as the force-lock branch — make the ack flow
        # fire on the first dean_node turn. Mirrors what _apply_prelock
        # does in backend/api/session.py (production path).
        state["topic_just_locked"] = True
    return {
        "matcher_path": top.path,
        "matcher_score": top.score,
        "chunk_count": top.chunk_count,
        "locked_question": state["locked_question"],
        "locked_answer": state["locked_answer"],
        "locked_answer_aliases": state["locked_answer_aliases"],
        "lock_source": "forced" if force_locked_answer is not None else "llm",
    }


def _last_tutor_message(state) -> str:
    for m in reversed(state.get("messages", [])):
        if m.get("role") == "tutor":
            return str(m.get("content", "") or "")
    return ""


def _dump_state_for_scorer(state) -> dict:
    """Return a JSON-safe snapshot of `state` for the quality scorer.

    Includes everything the scorer needs:
      - messages, locked_topic, locked_question, locked_answer, aliases
      - retrieved_chunks (full payload)
      - debug.* (turn_trace, all_turn_traces, counters)
      - student_reached_answer, hint_level, etc.

    Skips noisy fields the scorer doesn't use (e.g. internal callbacks).
    """
    keep_top = (
        "student_id", "phase", "messages", "retrieved_chunks",
        "locked_question", "locked_answer", "locked_answer_aliases",
        "full_answer",  # Change 2026-04-30: two-tier anchor design
        "locked_topic", "topic_confirmed", "topic_selection",
        "topic_just_locked", "hint_level", "max_hints", "max_turns",
        "turn_count", "student_state", "student_reached_answer",
        "student_answer_confidence", "student_mastery_confidence",
        "confidence_samples", "help_abuse_count",
        # Change 4 / 5.1 counter fields
        "off_topic_count", "total_low_effort_turns", "total_off_topic_turns",
        "clinical_low_effort_count", "clinical_off_topic_count",
        "assessment_turn", "clinical_state", "clinical_confidence",
        "core_mastery_tier", "clinical_mastery_tier", "mastery_tier",
        "grading_rationale", "session_memory_summary",
        "weak_topics", "rejected_topic_paths",
        "exploration_max", "exploration_used",
    )
    out = {}
    for k in keep_top:
        if k in state:
            out[k] = state[k]
    # Debug block — keep most fields; redact large or non-JSON-safe ones.
    debug = state.get("debug") or {}
    safe_debug = {}
    for k, v in debug.items():
        # Skip prompt strings (they bloat the file; we have wrapper signals
        # without them).
        if k == "system_prompt":
            continue
        try:
            import json as _json
            _json.dumps(v, default=str)
            safe_debug[k] = v
        except (TypeError, ValueError):
            safe_debug[k] = repr(v)[:200]
    out["debug"] = safe_debug
    return out


def _all_tutor_messages(state) -> list[str]:
    return [str(m.get("content", "") or "") for m in state.get("messages", []) if m.get("role") == "tutor"]


def _gate_decisions_in_trace(state) -> list[dict]:
    """Pull every dean.confidence_score / reached_answer_gate trace entry.

    Defensive: trace structures contain mixed types — lists-of-dicts
    nested in lists, sometimes plain strings. Skip anything that isn't
    a dict so the harness never blows up while inspecting traces.
    """
    out = []
    debug = state.get("debug", {}) or {}
    # all_turn_traces is a list of per-turn trace lists.
    for tr in debug.get("all_turn_traces", []) or []:
        if not isinstance(tr, list):
            continue
        for entry in tr:
            if not isinstance(entry, dict):
                continue
            wrap = entry.get("wrapper", "")
            if wrap in {"dean.confidence_score", "dean.reached_answer_gate"}:
                out.append(entry)
    # turn_trace is a flat list of dicts for the most recent turn.
    for entry in debug.get("turn_trace", []) or []:
        if not isinstance(entry, dict):
            continue
        wrap = entry.get("wrapper", "")
        if wrap in {"dean.confidence_score", "dean.reached_answer_gate"}:
            out.append(entry)
    return out


async def run_test(name: str, spec: dict, graph, dean) -> dict:
    """Drive the graph through one scripted conversation and capture state.

    We pre-lock the topic deterministically via the matcher's top hit so
    the gate is ALWAYS exercised against a real locked_answer + aliases.
    Free-text resolution sometimes returns off-topic option cards which
    masks gate behavior — see the 2026-04-29 refactor notes.
    """
    conv_id = str(uuid.uuid4())[:8]
    student_id = f"gate_test_{name}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_config = {"configurable": {"thread_id": conv_id}}

    log: list[dict] = []
    notes: list[str] = []

    # ---- Step 1: pre-lock topic via matcher (no graph call yet) ----
    try:
        prelock_info = prelock_topic(
            state,
            spec["topic_query"],
            dean,
            force_locked_answer=spec.get("force_locked_answer"),
            force_aliases=spec.get("force_aliases"),
            force_locked_question=spec.get("force_locked_question"),
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        notes.append(f"prelock failed: {e}")
        print(f"  [{name}] PRELOCK ERROR: {e}")
        return {
            "name": name, "conv_id": conv_id, "description": spec["description"],
            "expectations": {
                "reached": spec["expect_reached"],
                "no_fabrication_keywords": spec.get("expect_no_fabrication_keywords", []),
            },
            "outcomes": {
                "prelock_error": str(e),
                "final_reached": None,
                "fabrication_hits_in_last_tutor": [],
                "last_tutor_message": "",
                "phase_final": None,
                "locked_answer": None,
                "locked_answer_aliases": None,
                "locked_question": None,
                "turn_count": 0,
                "hint_level_final": 0,
                "student_state_final": None,
            },
            "gate_traces": [],
            "result": {"overall_pass": False, "reached_pass": False, "fabrication_pass": False},
            "notes": notes, "log": log,
            "debug_summary": {"cost_usd": 0.0, "api_calls": 0, "input_tokens": 0, "output_tokens": 0},
        }
    log.append({"step": "prelock", **prelock_info})
    print(f"  [{name}] PRELOCKED path={prelock_info['matcher_path']}")
    print(f"  [{name}] LOCKED answer={prelock_info['locked_answer']!r} "
          f"aliases={prelock_info['locked_answer_aliases']}")

    # ---- Step 2: drive rapport (greeting, no topic dance) ----
    # rapport_node still runs but topic is already locked — dean_node
    # skips the lock path on its first call.
    state = await asyncio.to_thread(graph.invoke, state, thread_config)
    rapport_msg = _last_tutor_message(state)
    log.append({"step": "rapport_greeting", "tutor": rapport_msg[:300]})
    print(f"  [{name}] rapport: {rapport_msg[:80]}...")

    # ---- Step 3: scripted tutoring turns ----
    for i, msg in enumerate(spec["tutoring_turns"], start=1):
        # Stop early if assessment phase already entered (shouldn't for T1/T4)
        if state.get("phase") in {"assessment", "memory_update"}:
            log.append({
                "step": f"early_stop_before_turn_{i}",
                "phase": state.get("phase"),
                "reason": "graph entered post-tutoring phase",
            })
            break
        state["messages"].append({"role": "student", "content": msg})
        log.append({
            "step": f"tutoring_turn_{i}_student",
            "student": msg,
        })
        try:
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
        except Exception as e:
            notes.append(f"graph_invoke turn {i} error: {e}")
            print(f"  [{name}] turn {i} ERROR: {e}")
            break
        log.append({
            "step": f"tutoring_turn_{i}_after_invoke",
            "tutor": _last_tutor_message(state)[:400],
            "student_state": state.get("student_state"),
            "student_reached_answer": state.get("student_reached_answer"),
            "student_answer_confidence": state.get("student_answer_confidence"),
            "phase": state.get("phase"),
            "hint_level": state.get("hint_level"),
        })
        print(f"  [{name}] turn {i}: state={state.get('student_state')} "
              f"reached={state.get('student_reached_answer')} "
              f"phase={state.get('phase')}")

    # ---- Step 5: collect gate decisions + assertions ----
    gate_traces = _gate_decisions_in_trace(state)
    final_reached = bool(state.get("student_reached_answer", False))
    last_tutor = _last_tutor_message(state)

    expect_reached = spec["expect_reached"]
    reached_pass = (final_reached == expect_reached)

    fab_keywords = spec.get("expect_no_fabrication_keywords") or []
    fabrication_hits = [k for k in fab_keywords if k.lower() in last_tutor.lower()]
    fabrication_pass = (len(fabrication_hits) == 0)

    overall_pass = reached_pass and fabrication_pass

    # Phase 1a-6 (2026-04-30): also dump the full final state so the
    # quality scorer (evaluation/quality/runner.py) can read all
    # turn_trace entries, retrieved_chunks, hint_progress, etc. The
    # `final_state` block is what the scorer's _load_from_test_harness
    # path consumes when present. Filter out truly unbounded fields
    # (full retrieved_chunks contents are kept since the scorer needs
    # them; if they bloat too much in a future test we can trim).
    final_state_dump = _dump_state_for_scorer(state)

    return {
        "name": name,
        "conv_id": conv_id,
        "description": spec["description"],
        "expectations": {
            "reached": expect_reached,
            "no_fabrication_keywords": fab_keywords,
        },
        "outcomes": {
            "final_reached": final_reached,
            "fabrication_hits_in_last_tutor": fabrication_hits,
            "last_tutor_message": last_tutor,
            "phase_final": state.get("phase"),
            "locked_answer": state.get("locked_answer"),
            "locked_answer_aliases": state.get("locked_answer_aliases"),
            "locked_question": state.get("locked_question"),
            "turn_count": state.get("turn_count"),
            "hint_level_final": state.get("hint_level"),
            "student_state_final": state.get("student_state"),
        },
        "gate_traces": gate_traces,
        "final_state": final_state_dump,
        "result": {
            "reached_pass": reached_pass,
            "fabrication_pass": fabrication_pass,
            "overall_pass": overall_pass,
        },
        "notes": notes,
        "log": log,
        "debug_summary": {
            "api_calls": state.get("debug", {}).get("api_calls", 0),
            "input_tokens": state.get("debug", {}).get("input_tokens", 0),
            "output_tokens": state.get("debug", {}).get("output_tokens", 0),
            "cost_usd": state.get("debug", {}).get("cost_usd", 0.0),
        },
    }


def save_result(result: dict) -> Path:
    fname = f"{result['name']}_{result['conv_id']}_{datetime.now().strftime('%H%M%S')}.json"
    p = OUTPUT_DIR / fname
    with open(p, "w") as f:
        json.dump(result, f, indent=2, default=str)
    return p


async def main():
    only = sys.argv[1:] or None  # let user run a single test by name

    print("Building graph (real retriever + memory_manager)...")
    from conversation.graph import build_graph
    from backend.dependencies import get_dean
    from retrieval.retriever import Retriever
    from memory.memory_manager import MemoryManager
    retriever = Retriever()
    memory_manager = MemoryManager()
    graph = build_graph(retriever, memory_manager)
    dean = get_dean()
    print("Graph built.\n")

    summaries = []
    total_cost = 0.0
    for name, spec in TESTS.items():
        if only and name not in only:
            continue
        print(f"\n{'='*70}\nRUNNING {name}\n  {spec['description']}\n{'='*70}")
        result = await run_test(name, spec, graph, dean)
        path = save_result(result)
        cost = float(result["debug_summary"].get("cost_usd", 0.0) or 0.0)
        total_cost += cost
        verdict = "PASS" if result["result"]["overall_pass"] else "FAIL"
        summaries.append({
            "name": name,
            "verdict": verdict,
            "expected_reached": result["expectations"]["reached"],
            "got_reached": result["outcomes"]["final_reached"],
            "fab_hits": result["outcomes"]["fabrication_hits_in_last_tutor"],
            "locked_answer": result["outcomes"]["locked_answer"],
            "aliases": result["outcomes"]["locked_answer_aliases"],
            "cost_usd": cost,
            "path": str(path),
        })
        print(f"  → {verdict} | cost=${cost:.4f}")
        print(f"  → saved {path}")

    print(f"\n{'='*70}\nSUMMARY (total cost ~${total_cost:.4f})\n{'='*70}")
    for s in summaries:
        print(f"  {s['verdict']:4}  {s['name']:30}  expected_reached={s['expected_reached']}  got={s['got_reached']}")
        print(f"        locked={s['locked_answer']!r}  aliases={s['aliases']}")
        if s["fab_hits"]:
            print(f"        FABRICATION HITS: {s['fab_hits']}")
    fails = [s for s in summaries if s["verdict"] == "FAIL"]
    print()
    if fails:
        print(f"  {len(fails)}/{len(summaries)} FAILED.")
        sys.exit(1)
    else:
        print(f"  ALL {len(summaries)} PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
