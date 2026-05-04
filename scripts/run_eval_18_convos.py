"""
scripts/run_eval_18_convos.py
-----------------------------
The curated 18-conversation eval batch (Phase 1, 2026-04-30).

Tests both:
  - Per-session quality (across S1-S6 profiles + diverse topics)
  - Cross-session memory cycle (mem0 read in rapport_node + mastery EWMA)

Structure:
  - 11 distinct students, 18 conversations total
  - 3 students × 2 sessions (memory pair tests, 6 convos)
  - 2 students × 3 sessions (EWMA convergence tests, 6 convos)
  - 6 students × 1 session (single-session quality, 6 convos)

Concurrency:
  - Across DIFFERENT students: 4 parallel chains
  - Within ONE student's chain: STRICTLY SEQUENTIAL (session N+1 must
    see session N's mem0 + mastery state on disk before it starts)

Each student's mem0 + mastery_store is cleared BEFORE their chain starts
so we test the memory cycle from a clean baseline.

Usage (from sokratic/ root):
    .venv/bin/python scripts/run_eval_18_convos.py
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
from simulation.profiles import PROFILES
from simulation.student_simulator import StudentSimulator

OUTPUT_DIR = Path(cfg.paths.artifacts) / "eval_run_18"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# 18-conversation plan
# =============================================================================

PLAN: list[dict] = [
    # === Multi-session pairs (memory test, 6 convos) ===
    {
        "student_id": "eval18_pair1_strong",
        "profile": "S1",
        "sessions": [
            {"topic": "How does the conduction system of the heart coordinate ventricular contraction?"},
            {"topic": "What role does the SA node play in heart rhythm regulation?"},
        ],
    },
    {
        "student_id": "eval18_pair2_moderate",
        "profile": "S2",
        "sessions": [
            {"topic": "How does an action potential propagate along a neuron?"},
            {"topic": "What is saltatory conduction and why does myelin matter for nerve impulse speed?"},
        ],
    },
    {
        "student_id": "eval18_pair3_disengaged",
        "profile": "S5",
        "sessions": [
            {"topic": "What are the phases of the cardiac cycle?"},
            {"topic": "How does heart rate affect cardiac output?"},
        ],
    },

    # === Multi-session triples (EWMA + memory accumulation, 6 convos) ===
    {
        "student_id": "eval18_triple1_progressing",
        "profile": "S3",
        "sessions": [
            {"topic": "What are the parts of a nephron?"},
            {"topic": "How does the nephron filter blood?"},
            {"topic": "What is tubular reabsorption and why does it matter for homeostasis?"},
        ],
    },
    {
        "student_id": "eval18_triple2_exploratory",
        "profile": "S6",
        "sessions": [
            {"topic": "Walk me through chemical digestion of carbohydrates"},
            {"topic": "What enzymes break down proteins in the stomach?"},
            {"topic": "How does the small intestine absorb nutrients?"},
        ],
    },

    # === Single-session quality (6 convos, S1-S6, distinct topics) ===
    {
        "student_id": "eval18_solo1_S1",
        "profile": "S1",
        "sessions": [
            {"topic": "How does the immune system distinguish self from non-self?"},
        ],
    },
    {
        "student_id": "eval18_solo2_S2",
        "profile": "S2",
        "sessions": [
            {"topic": "What controls breathing rate in the brainstem?"},
        ],
    },
    {
        "student_id": "eval18_solo3_S3",
        "profile": "S3",
        "sessions": [
            {"topic": "What is the structure of a long bone?"},
        ],
    },
    {
        "student_id": "eval18_solo4_S4",
        "profile": "S4",
        "sessions": [
            {"topic": "Explain the structure and motion of the elbow joint"},
        ],
    },
    {
        "student_id": "eval18_solo5_S5",
        "profile": "S5",
        "sessions": [
            {"topic": "What is the function of the spleen?"},
        ],
    },
    {
        "student_id": "eval18_solo6_S6",
        "profile": "S6",
        "sessions": [
            {"topic": "How does the thyroid regulate metabolism?"},
        ],
    },
]


# =============================================================================
# State dump (mirror of test_reached_gate_e2e.py — keeps scorer-compatible)
# =============================================================================

KEEP_TOP = (
    "student_id", "phase", "messages", "retrieved_chunks",
    "locked_question", "locked_answer", "locked_answer_aliases",
    "full_answer",  # Change 2026-04-30: two-tier anchor design
    "locked_topic", "topic_confirmed", "topic_selection",
    "topic_just_locked", "hint_level", "max_hints", "max_turns",
    "turn_count", "student_state", "student_reached_answer",
    "student_answer_confidence", "student_mastery_confidence",
    "confidence_samples", "help_abuse_count",
    "off_topic_count", "total_low_effort_turns", "total_off_topic_turns",
    "clinical_low_effort_count", "clinical_off_topic_count",
    "assessment_turn", "clinical_state", "clinical_confidence",
    "core_mastery_tier", "clinical_mastery_tier", "mastery_tier",
    "grading_rationale", "session_memory_summary",
    "weak_topics", "rejected_topic_paths",
    "exploration_max", "exploration_used",
)


def _dump_state_for_scorer(state: dict) -> dict:
    out = {}
    for k in KEEP_TOP:
        if k in state:
            out[k] = state[k]
    debug = state.get("debug") or {}
    safe = {}
    for k, v in debug.items():
        if k == "system_prompt":
            continue
        try:
            json.dumps(v, default=str)
            safe[k] = v
        except (TypeError, ValueError):
            safe[k] = repr(v)[:200]
    out["debug"] = safe
    return out


# =============================================================================
# Memory + mastery clear helper
# =============================================================================

def clear_student_state(student_id: str, memory_manager) -> None:
    """Reset mem0 + mastery_store for this student before their chain starts.
    Best-effort: failures (e.g. mem0 unavailable) shouldn't block the test."""
    try:
        memory_manager.forget(student_id)
    except Exception as e:
        print(f"  [{student_id}] memory_manager.forget failed: {type(e).__name__}: {e}")
    state_path = Path(getattr(cfg.paths, "data", "data")) / "student_state" / f"{student_id}.json"
    try:
        if state_path.exists():
            state_path.unlink()
            print(f"  [{student_id}] cleared mastery_store at {state_path}")
    except Exception as e:
        print(f"  [{student_id}] mastery_store clear failed: {e}")


# =============================================================================
# Single-session driver (LLM-driven student via simulator)
# =============================================================================

async def run_one_session(
    student_id: str,
    profile_id: str,
    topic: str,
    session_index: int,
    graph,
) -> dict:
    """Drive ONE session through the full graph. LLM-driven student via
    simulator. Returns a dict with the full state + transcript + bugs."""
    conv_id = str(uuid.uuid4())[:8]
    thread_id = f"{student_id}_s{session_index}_{conv_id}"
    # M2 Bug A — production calls SQLiteStore.ensure_student in start_session;
    # the eval harness bypasses that path, so insert here to satisfy the
    # students→sessions FK constraint at session-end time.
    try:
        from memory.sqlite_store import SQLiteStore
        SQLiteStore().ensure_student(student_id)
    except Exception:
        pass
    state = initial_state(student_id, cfg)
    state["thread_id"] = thread_id
    thread_config = {"configurable": {"thread_id": thread_id}}
    simulator = StudentSimulator(PROFILES[profile_id])

    turns_log: list[dict] = []
    bugs_noted: list[dict] = []

    # ---- Rapport ----
    try:
        state = await asyncio.to_thread(graph.invoke, state, thread_config)
        for msg in state.get("messages", []):
            if msg.get("role") == "tutor":
                turns_log.append({"turn": 0, "phase": "rapport", "role": "tutor", "content": msg["content"]})
    except Exception as e:
        bugs_noted.append({"phase": "rapport", "error": f"{type(e).__name__}: {e}"})
        print(f"  [{student_id} s{session_index}] rapport ERROR: {e}")
        return _build_result(student_id, conv_id, profile_id, topic, session_index, turns_log, state, bugs_noted)

    # ---- Topic input ----
    state["messages"].append({"role": "student", "content": topic})
    turns_log.append({"turn": 0, "phase": "topic_input", "role": "student", "content": topic})
    try:
        state = await asyncio.to_thread(graph.invoke, state, thread_config)
        last_tutor = next((m for m in reversed(state.get("messages", [])) if m.get("role") == "tutor"), None)
        if last_tutor:
            turns_log.append({"turn": 0, "phase": "topic_engagement", "role": "tutor", "content": last_tutor["content"]})
    except Exception as e:
        bugs_noted.append({"phase": "topic_engagement", "error": f"{type(e).__name__}: {e}"})
        print(f"  [{student_id} s{session_index}] topic_input ERROR: {e}")
        return _build_result(student_id, conv_id, profile_id, topic, session_index, turns_log, state, bugs_noted)

    # ---- Pre-lock loop (handles legacy topic-cards AND v2 confirm_topic UX) ----
    # B1 fix (2026-05-03): the legacy harness only checked `state.topic_options`
    # and hard-picked opts[0]. v2's L10 confirm_and_lock sets topic_options=[]
    # and puts options under `pending_user_choice.options` (kind="confirm_topic").
    # Hard-picking opts[0] then fell back to `topic` (re-typing the original
    # question), which v2 read as a fresh topic query, kicking off another
    # confirm_and_lock — infinite loop until the harness 16-turn cap.
    #
    # New approach: defer to simulator.respond() which already handles every
    # pending_user_choice kind correctly (mimics UI button clicks per the
    # f78f8e1 simulator fix). Loop a few times because the v2 flow may need
    # multiple turns to lock (confirm_and_lock → yes → coverage gate → cards
    # → pick).
    prelock_loops = 0
    MAX_PRELOCK_LOOPS = 5
    while not state.get("topic_confirmed", False) and prelock_loops < MAX_PRELOCK_LOOPS:
        prelock_loops += 1
        # If v2 surfaced a pending choice, simulator.respond() returns the
        # appropriate button click. Otherwise fall back to first card option
        # / typed topic — same as the legacy behavior.
        pending = state.get("pending_user_choice") or {}
        opts = state.get("topic_options") or []
        if pending and pending.get("options"):
            try:
                chosen = await asyncio.to_thread(simulator.respond, state)
            except Exception:
                chosen = pending["options"][0]
        elif opts:
            chosen = opts[0]
        else:
            chosen = topic
        state["messages"].append({"role": "student", "content": chosen})
        turns_log.append({
            "turn": 0,
            "phase": f"prelock_{prelock_loops}",
            "role": "student",
            "content": chosen,
            "pending_kind": pending.get("kind", "") if pending else "",
        })
        try:
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            last_tutor = next((m for m in reversed(state.get("messages", [])) if m.get("role") == "tutor"), None)
            if last_tutor:
                turns_log.append({"turn": 0, "phase": f"prelock_{prelock_loops}_tutor", "role": "tutor", "content": last_tutor["content"]})
        except Exception as e:
            bugs_noted.append({"phase": "prelock", "error": f"{type(e).__name__}: {e}"})
            return _build_result(student_id, conv_id, profile_id, topic, session_index, turns_log, state, bugs_noted)
    if not state.get("topic_confirmed", False):
        bugs_noted.append({
            "phase": "prelock",
            "error": f"failed to lock topic after {MAX_PRELOCK_LOOPS} pre-lock loops",
        })

    # ---- Tutoring + assessment loop ----
    max_loop = 14
    loop_count = 0
    while loop_count < max_loop:
        loop_count += 1
        phase = state.get("phase", "tutoring")
        turn_count = state.get("turn_count", 0)

        if phase == "memory_update":
            break
        if state.get("assessment_turn", 0) >= 3:
            break

        try:
            student_resp = await asyncio.to_thread(simulator.respond, state)
        except Exception as e:
            bugs_noted.append({"phase": "student_simulator", "turn": turn_count, "error": f"{type(e).__name__}: {e}"})
            student_resp = "I'm not sure."

        state["messages"].append({"role": "student", "content": student_resp})
        turns_log.append({"turn": turn_count + 1, "phase": phase, "role": "student", "content": student_resp})

        try:
            prev_msgs_len = len(state.get("messages", []))
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            new_msgs = state.get("messages", [])[prev_msgs_len:]
            logged = {t.get("content") for t in turns_log}
            for msg in new_msgs:
                if msg.get("role") == "tutor" and msg.get("content") not in logged:
                    turns_log.append({
                        "turn": state.get("turn_count", turn_count + 1),
                        "phase": state.get("phase"),
                        "role": "tutor",
                        "content": msg["content"],
                        "student_state": state.get("student_state"),
                        "hint_level": state.get("hint_level"),
                        "student_reached_answer": state.get("student_reached_answer"),
                    })
        except Exception as e:
            bugs_noted.append({"phase": "graph_invoke", "turn": turn_count, "error": f"{type(e).__name__}: {e}"})
            import traceback; traceback.print_exc()
            break

        if state.get("phase") == "memory_update":
            break

    return _build_result(student_id, conv_id, profile_id, topic, session_index, turns_log, state, bugs_noted)


def _build_result(student_id, conv_id, profile_id, topic, session_index, turns_log, state, bugs_noted):
    return {
        "student_id": student_id,
        "conv_id": conv_id,
        "profile_id": profile_id,
        "topic": topic,
        "session_index": session_index,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "turns_log": turns_log,
        "outcome": {
            "phase_final": state.get("phase"),
            "reached_answer": state.get("student_reached_answer", False),
            "locked_answer": state.get("locked_answer", ""),
            "hint_level_final": state.get("hint_level", 0),
            "turn_count": state.get("turn_count") or len([t for t in turns_log if t.get("role") == "student"]),
            "assessment_turn": state.get("assessment_turn", 0),
            "student_state_final": state.get("student_state"),
            "core_mastery_tier": state.get("core_mastery_tier"),
            "clinical_mastery_tier": state.get("clinical_mastery_tier"),
            "mastery_tier": state.get("mastery_tier"),
            "total_low_effort_turns": state.get("total_low_effort_turns"),
            "total_off_topic_turns": state.get("total_off_topic_turns"),
        },
        "debug_summary": {
            "api_calls": state.get("debug", {}).get("api_calls", 0),
            "input_tokens": state.get("debug", {}).get("input_tokens", 0),
            "output_tokens": state.get("debug", {}).get("output_tokens", 0),
            "cost_usd": state.get("debug", {}).get("cost_usd", 0.0),
            "interventions": state.get("debug", {}).get("interventions", 0),
        },
        "bugs_noted": bugs_noted,
        # Critical for the eval scorer:
        "final_state": _dump_state_for_scorer(state),
    }


# =============================================================================
# Per-student chain (sessions sequential, mem0 carries forward)
# =============================================================================

async def run_student_chain(student_plan: dict, graph, memory_manager) -> list[dict]:
    student_id = student_plan["student_id"]
    profile_id = student_plan["profile"]
    sessions = student_plan["sessions"]

    print(f"\n[{student_id}] starting chain ({len(sessions)} session{'s' if len(sessions) != 1 else ''})")
    clear_student_state(student_id, memory_manager)

    results = []
    for i, sess in enumerate(sessions, start=1):
        topic = sess["topic"]
        print(f"  [{student_id} s{i}] topic: {topic[:80]}")
        try:
            result = await run_one_session(student_id, profile_id, topic, i, graph)
        except Exception as e:
            print(f"  [{student_id} s{i}] CHAIN ERROR: {type(e).__name__}: {e}")
            result = {
                "student_id": student_id,
                "session_index": i,
                "topic": topic,
                "error": f"{type(e).__name__}: {e}",
                "outcome": {},
                "final_state": {},
            }
        out_path = OUTPUT_DIR / f"{student_id}_session{i}.json"
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        outcome = result.get("outcome") or {}
        print(f"  [{student_id} s{i}] DONE — reached={outcome.get('reached_answer')} "
              f"phase={outcome.get('phase_final')} turns={outcome.get('turn_count')} "
              f"cost=${(result.get('debug_summary') or {}).get('cost_usd', 0):.3f}")
        results.append(result)
    return results


# =============================================================================
# Orchestrator: 4 parallel students max
# =============================================================================

CONCURRENCY = 4


async def main():
    from conversation.graph import build_graph
    from retrieval.retriever import Retriever
    from memory.memory_manager import MemoryManager

    print("Building graph...")
    retriever = Retriever()
    memory_manager = MemoryManager()
    graph = build_graph(retriever, memory_manager)
    print(f"Graph built. Running {sum(len(p['sessions']) for p in PLAN)} conversations "
          f"across {len(PLAN)} students at concurrency={CONCURRENCY}.\n")

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def gated(plan):
        async with semaphore:
            return await run_student_chain(plan, graph, memory_manager)

    started = datetime.now()
    all_results = await asyncio.gather(*(gated(p) for p in PLAN), return_exceptions=True)
    elapsed = (datetime.now() - started).total_seconds()

    flat = []
    for chain in all_results:
        if isinstance(chain, Exception):
            print(f"!! Chain exception: {chain}")
            continue
        flat.extend(chain)

    total_cost = sum((r.get("debug_summary") or {}).get("cost_usd", 0.0) for r in flat)

    print(f"\n{'=' * 72}")
    print(f"RUN COMPLETE: {len(flat)}/{sum(len(p['sessions']) for p in PLAN)} conversations")
    print(f"Wall time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"Total API cost: ${total_cost:.3f}")
    print(f"Saved to: {OUTPUT_DIR}")
    print(f"{'=' * 72}")

    # Manifest for downstream analysis + scoring
    manifest_path = OUTPUT_DIR / "_manifest.json"
    manifest = {
        "started_at": started.isoformat(timespec="seconds"),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": elapsed,
        "total_conversations": len(flat),
        "expected_conversations": sum(len(p["sessions"]) for p in PLAN),
        "total_cost_usd": round(total_cost, 4),
        "concurrency": CONCURRENCY,
        "saved_files": sorted(p.name for p in OUTPUT_DIR.glob("*.json") if p.name != "_manifest.json"),
        "plan_summary": [
            {"student_id": p["student_id"], "profile": p["profile"], "n_sessions": len(p["sessions"])}
            for p in PLAN
        ],
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    print(f"Manifest: {manifest_path}\n")


if __name__ == "__main__":
    asyncio.run(main())
