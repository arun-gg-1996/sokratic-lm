"""
scripts/run_final_convos.py
----------------------------
Run a small set of test conversations across 4 profiles using real RAG retriever,
save each conversation JSON to data/artifacts/final_convo/ for manual review.

Usage (from sokratic/ root):
    .venv/bin/python scripts/run_final_convos.py

Profiles tested:
  S1 — Strong        (happy path, gets answer early)
  S2 — Moderate      (needs 1-2 hints)
  S4 — Overconfident (sycophancy guard test)
  S5 — Disengaged    (help abuse counter test)

Topics: one specific anatomy topic per profile to keep conversations focused.
"""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from pathlib import Path as _Path

load_dotenv(_Path(__file__).parent.parent / ".env", override=True)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg
from conversation.state import initial_state
from simulation.profiles import PROFILES
from simulation.student_simulator import StudentSimulator

OUTPUT_DIR = Path(cfg.paths.artifacts) / "final_convo"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Curated topics — must be TEXTBOOK-ANSWERABLE in OpenStax A&P 2e.
# 2026-04-29: previous topics (deltoid innervation, wrist drop / humeral fracture)
# turned out to be NOT in the corpus (verified: 0 chunks mention 'axillary' +
# 'deltoid' together; 'wrist drop' appears 0 times). The OLD pipeline's
# "successes" on these were LLM parametric-knowledge hallucinations. Switching
# to topics with strong, demonstrated coverage in the corpus so the e2e tests
# the system on questions it can honestly answer.
PROFILE_TOPICS = {
    "S1a": ("S1", "What are the structural and functional differences between T helper cells and cytotoxic T cells?"),  # Ch21 T Cell-Mediated Immune Responses (24 chunks)
    "S2a": ("S2", "How does the body regulate blood pressure through neural mechanisms?"),  # Ch20 Neural Regulation (24 chunks)
    "S3a": ("S3", "what are the parts of a nephron"),  # Ch25 Nephrons (24 chunks)
    "S4a": ("S4", "Reflexes are basically just instant nerve reactions, right?"),  # Ch15 Structure of Reflexes (16 chunks)
    "S5a": ("S5", "elbow joint"),  # Ch9 Elbow Joint (8 chunks)
    "S6a": ("S6", "I'm not sure I really understand chemical digestion — could we go through it?"),  # Ch23 Chemical Digestion (58 chunks)
}


async def run_one(profile_id: str, topic: str, graph) -> dict:
    conv_id = str(uuid.uuid4())[:8]
    student_id = f"{profile_id}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_config = {"configurable": {"thread_id": conv_id}}
    simulator = StudentSimulator(PROFILES[profile_id])

    turns_log = []
    bugs_noted = []

    # --- Rapport turn ---
    try:
        state = await asyncio.to_thread(graph.invoke, state, thread_config)
        for msg in state.get("messages", []):
            if msg.get("role") == "tutor":
                turns_log.append({"turn": 0, "phase": "rapport", "role": "tutor", "content": msg["content"]})
        print(f"[{profile_id}] Rapport done.")
    except Exception as e:
        bugs_noted.append({"phase": "rapport", "error": str(e)})
        print(f"[{profile_id}] Rapport ERROR: {e}")
        return _build_result(profile_id, conv_id, topic, turns_log, state, bugs_noted)

    # --- Topic engagement (turn 0) ---
    state["messages"].append({"role": "student", "content": topic})
    turns_log.append({"turn": 0, "phase": "topic_input", "role": "student", "content": topic})
    try:
        state = await asyncio.to_thread(graph.invoke, state, thread_config)
        last_tutor = next((m for m in reversed(state.get("messages", [])) if m.get("role") == "tutor"), None)
        if last_tutor:
            turns_log.append({"turn": 0, "phase": "topic_engagement", "role": "tutor", "content": last_tutor["content"]})
        print(f"[{profile_id}] Topic engagement done. topic_confirmed={state.get('topic_confirmed')}")
    except Exception as e:
        bugs_noted.append({"phase": "topic_engagement", "error": str(e)})
        print(f"[{profile_id}] Topic engagement ERROR: {e}")
        return _build_result(profile_id, conv_id, topic, turns_log, state, bugs_noted)

    # --- If topic_confirmed is False, need to pick one of the options ---
    # Simulate student selecting option 1
    if not state.get("topic_confirmed", False):
        topic_options = state.get("topic_options", [])
        if topic_options:
            selection = topic_options[0]
        else:
            selection = topic  # fallback
        state["messages"].append({"role": "student", "content": selection})
        turns_log.append({"turn": 0, "phase": "option_selection", "role": "student", "content": selection})
        try:
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            last_tutor = next((m for m in reversed(state.get("messages", [])) if m.get("role") == "tutor"), None)
            if last_tutor:
                turns_log.append({"turn": 1, "phase": "tutoring", "role": "tutor", "content": last_tutor["content"]})
            print(f"[{profile_id}] First tutoring turn done. locked_answer={state.get('locked_answer')}")
        except Exception as e:
            bugs_noted.append({"phase": "first_tutoring", "error": str(e)})
            print(f"[{profile_id}] First tutoring ERROR: {e}")
            return _build_result(profile_id, conv_id, topic, turns_log, state, bugs_noted)

    # --- Main tutoring loop ---
    max_loop = 12  # safety cap
    loop_count = 0
    while loop_count < max_loop:
        loop_count += 1
        phase = state.get("phase", "tutoring")
        turn_count = state.get("turn_count", 0)

        # Exit if done
        if phase in ("memory_update",):
            break
        if state.get("assessment_turn", 0) >= 3:
            break

        # Generate student response
        try:
            student_resp = await asyncio.to_thread(simulator.respond, state)
        except Exception as e:
            bugs_noted.append({"phase": "student_simulator", "turn": turn_count, "error": str(e)})
            student_resp = "I'm not sure."

        state["messages"].append({"role": "student", "content": student_resp})
        turns_log.append({"turn": turn_count + 1, "phase": phase, "role": "student", "content": student_resp})
        print(f"[{profile_id}] Turn {turn_count+1} student: {student_resp[:60]}...")

        # Invoke graph
        try:
            prev_messages_len = len(state.get("messages", []))
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            # Find new tutor messages (deduplicated by content)
            new_msgs = state.get("messages", [])[prev_messages_len:]
            logged_contents = {t.get("content") for t in turns_log}
            for msg in new_msgs:
                if msg.get("role") == "tutor" and msg.get("content") not in logged_contents:
                    turns_log.append({
                        "turn": state.get("turn_count", turn_count + 1),
                        "phase": state.get("phase"),
                        "role": "tutor",
                        "content": msg["content"],
                        "student_state": state.get("student_state"),
                        "hint_level": state.get("hint_level"),
                        "locked_answer": state.get("locked_answer"),
                        "student_reached_answer": state.get("student_reached_answer"),
                    })
            print(f"[{profile_id}] Turn {state.get('turn_count')} | state={state.get('student_state')} | hint={state.get('hint_level')} | reached={state.get('student_reached_answer')} | phase={state.get('phase')}")
        except Exception as e:
            bugs_noted.append({"phase": "graph_invoke", "turn": turn_count, "error": str(e)})
            print(f"[{profile_id}] Graph invoke ERROR at turn {turn_count}: {e}")
            import traceback; traceback.print_exc()
            break

        # Check for assessment completion
        if state.get("phase") == "assessment" and state.get("assessment_turn", 0) == 3:
            break
        if state.get("phase") == "memory_update":
            break

    return _build_result(profile_id, conv_id, topic, turns_log, state, bugs_noted)


def _build_result(profile_id, conv_id, topic, turns_log, state, bugs_noted):
    return {
        "conv_id": conv_id,
        "profile_id": profile_id,
        "topic": topic,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "turns": turns_log,
        "outcome": {
            "phase_final": state.get("phase"),
            "reached_answer": state.get("student_reached_answer", False),
            "locked_answer": state.get("locked_answer", ""),
            "hint_level_final": state.get("hint_level", 1),
            "turn_count": state.get("turn_count") or len([t for t in turns_log if t.get("role") == "student"]),
            "assessment_turn": state.get("assessment_turn", 0),
            "student_state_final": state.get("student_state"),
        },
        "debug_summary": {
            "api_calls": state.get("debug", {}).get("api_calls", 0),
            "total_input_tokens": state.get("debug", {}).get("input_tokens", 0),
            "total_output_tokens": state.get("debug", {}).get("output_tokens", 0),
            "cost_usd": state.get("debug", {}).get("cost_usd", 0.0),
            "interventions": state.get("debug", {}).get("interventions", 0),
        },
        "bugs_noted": bugs_noted,
    }


def save_result(result: dict):
    fname = f"{result['profile_id']}_{result['conv_id']}_{datetime.now().strftime('%H%M%S')}.json"
    out_path = OUTPUT_DIR / fname
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"  → Saved: {out_path}")
    return out_path


async def main():
    from conversation.graph import build_graph
    # Allow swapping retriever implementations via env var so we can A/B
    # the chunk-level vs proposition-level architectures end-to-end.
    #   SOKRATIC_RETRIEVER=chunks  → ChunkRetriever (chunk-level index)
    #   anything else (default)    → Retriever (propositions)
    import os as _os
    _retriever_kind = _os.environ.get("SOKRATIC_RETRIEVER", "propositions").strip().lower()
    if _retriever_kind == "chunks":
        from retrieval.retriever import ChunkRetriever as Retriever  # type: ignore
    else:
        from retrieval.retriever import Retriever
    from memory.memory_manager import MemoryManager

    print("Building graph with real RAG retriever...")
    try:
        retriever = Retriever()
        print("  Real Retriever loaded.")
    except Exception as e:
        print(f"  Real Retriever failed ({e}), falling back to MockRetriever.")
        from retrieval.retriever import MockRetriever
        retriever = MockRetriever()

    memory_manager = MemoryManager()
    graph = build_graph(retriever, memory_manager)
    print("Graph built.\n")

    saved_paths = []
    # PROFILE_TOPICS maps a run-id (e.g. "S6a", "S6b") to a (profile_id, topic) tuple.
    # The run-id is just a per-conversation tag — the real profile identifier
    # to look up in PROFILES is the tuple's first element. Earlier versions of
    # this loop assumed the key was the profile_id, which broke once we added
    # multiple runs per profile (S6a/S6b for two topics on the same profile).
    for run_id, (profile_id, topic) in PROFILE_TOPICS.items():
        print(f"\n{'='*60}")
        print(f"Running {run_id} (profile {profile_id}) — {PROFILES[profile_id].name}")
        print(f"Topic: {topic}")
        print('='*60)
        result = await run_one(profile_id, topic, graph)
        # Tag the run_id into the saved record so multiple runs per profile
        # don't collide on filename / be hard to tell apart on review.
        result["run_id"] = run_id
        path = save_result(result)
        saved_paths.append(path)
        print(f"  Done: {len(result['turns'])} turns, reached_answer={result['outcome']['reached_answer']}, bugs={len(result['bugs_noted'])}")

    print(f"\n{'='*60}")
    print(f"All conversations saved to {OUTPUT_DIR}:")
    for p in saved_paths:
        print(f"  {p.name}")
    print("\nReview these files for flow bugs before evaluation.")


if __name__ == "__main__":
    asyncio.run(main())
