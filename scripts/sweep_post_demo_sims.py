"""
scripts/sweep_post_demo_sims.py
One-shot multi-sim driver for the post-demo browser-sim sweep.
Boots the graph + retriever ONCE, runs the demo-critical sims
sequentially (per feedback_no_parallel_eval), captures tutor
messages + per-turn state diagnostics, dumps a structured JSON
artifact for quality scoring.

Sims covered (subset of ):
 1. Happy path -> rapport -> tutoring -> clinical -> close
 5. Help-abuse counter -> F5c leak audit + counter
 6. Off-topic counter -> telemetry + topic stays locked

Run:
 source .venv/bin/activate
 python scripts/sweep_post_demo_sims.py

Output:
 data/artifacts/sweep_<timestamp>/sweep.json (turn-by-turn state)
 data/artifacts/sweep_<timestamp>/sweep.md (human-readable transcript)
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from config import cfg  # noqa: E402
from conversation.graph import build_graph  # noqa: E402
from conversation.state import initial_state  # noqa: E402
from memory.memory_manager import MemoryManager  # noqa: E402
# Use ChunkRetriever (chunks-only architecture) -- the base Retriever
# expects a proposition payload that the live qdrant collection no
# longer provides, so it returns 0 hits. ChunkRetriever overrides
# _qdrant_search / _bm25_search to use chunk_id as the parent_id.
# Same class production uses via backend/dependencies.py:41.
from retrieval.retriever import ChunkRetriever as Retriever  # noqa: E402

# Subset of state fields we capture per turn for the quality rubric.
DIAG_FIELDS = [
    "phase",
    "topic_confirmed",
    "locked_question",
    "locked_answer",
    "student_state",
    "hint_level",
    "turn_count",
    "assessment_turn",
    "clinical_opt_in",
    "student_reached_answer",
    "exploration_used",
    "locked_topic",
    "help_abuse_count",
    "off_topic_count",
    "consecutive_low_effort_count",
    "total_help_abuse_turns",
    "total_off_topic_turns",
    "total_low_effort_turns",
    "clinical_help_abuse_count",
    "clinical_off_topic_count",
    "clinical_low_effort_count",
    "total_clinical_help_abuse_turns",
    "total_clinical_off_topic_turns",
    "total_clinical_low_effort_turns",
    "clinical_mastery_tier",
    "close_reason",
    "session_ended",
    "session_ended_reason",
    "exit_intent_pending",
]

def _diag(state: dict) -> dict:
    return {k: state.get(k) for k in DIAG_FIELDS}

def _last_tutor(state: dict) -> str:
    for m in reversed(state.get("messages", []) or []):
        if m.get("role") == "tutor":
            return m.get("content", "") or ""
    return ""

def _new_tutor_messages(state: dict, prev_len: int) -> list[str]:
    return [
        m.get("content", "") or ""
        for m in (state.get("messages", []) or [])[prev_len:]
        if m.get("role") == "tutor"
    ]

def run_sim(graph, sim_name: str, student_turns: list[str]) -> dict:
    """Run one sim. Returns {sim_name, turns: [{student, tutor, diag}, ...]}."""
    print(f"\n[{sim_name}] starting...", flush=True)
    conv_id = f"sweep_{sim_name}_{uuid.uuid4().hex[:8]}"
    state = initial_state(conv_id, cfg)
    thread_cfg = {"configurable": {"thread_id": conv_id}}

    # First invoke: rapport / greeter turn (no student msg yet).
    t0 = time.time()
    state = graph.invoke(state, thread_cfg)
    rapport_tutor = _last_tutor(state)
    rapport_diag = _diag(state)
    rapport_dt = time.time() - t0
    print(
        f"[{sim_name}] rapport opened in {rapport_dt:.1f}s | phase={rapport_diag['phase']}",
        flush=True,
    )

    turns = [
        {
            "turn": 0,
            "student": "<rapport open>",
            "tutor": rapport_tutor,
            "diag": rapport_diag,
            "latency_s": round(rapport_dt, 2),
        }
    ]

    for i, msg in enumerate(student_turns, start=1):
        prev_len = len(state.get("messages", []) or [])
        state["messages"].append({"role": "student", "content": msg})
        t0 = time.time()
        try:
            state = graph.invoke(state, thread_cfg)
        except Exception as e:
            import traceback

            traceback.print_exc()
            turns.append(
                {
                    "turn": i,
                    "student": msg,
                    "tutor": f"<ERROR: {e}>",
                    "diag": _diag(state),
                    "latency_s": round(time.time() - t0, 2),
                    "error": str(e),
                }
            )
            break
        dt = time.time() - t0
        new_tutor = _new_tutor_messages(state, prev_len)
        diag = _diag(state)
        turns.append(
            {
                "turn": i,
                "student": msg,
                "tutor": "\n\n".join(new_tutor) or _last_tutor(state),
                "diag": diag,
                "latency_s": round(dt, 2),
            }
        )
        print(
            f"[{sim_name}] T{i} {dt:.1f}s | phase={diag['phase']} "
            f"hint={diag.get('hint_level')} "
            f"help_abuse={diag.get('help_abuse_count')}/"
            f"{diag.get('total_help_abuse_turns')} "
            f"off_topic={diag.get('off_topic_count')}/"
            f"{diag.get('total_off_topic_turns')} "
            f"low_effort={diag.get('consecutive_low_effort_count')}/"
            f"{diag.get('total_low_effort_turns')} "
            f"close={diag.get('close_reason')}",
            flush=True,
        )
        if diag.get("session_ended"):
            print(
                f"[{sim_name}] session ended: {diag.get('session_ended_reason')}",
                flush=True,
            )
            break

    return {"sim": sim_name, "turns": turns}

SIM_HAPPY_PATH = [
    "compact and spongy bone",
    # one partial attempt then a more complete answer to drive
    # student_reached_answer.
    "compact bone is dense and spongy bone has spaces filled with marrow",
    "Compact bone forms the dense outer cortex providing strength and "
    "protection, organized in osteons with concentric lamellae around a "
    "central canal. Spongy (cancellous) bone has a lattice of trabeculae "
    "with marrow-filled spaces, found at the bone's interior and ends, "
    "absorbs shock and houses red marrow for hematopoiesis.",
    "yes",                                          # opt-in to clinical
    "An elderly patient with osteoporosis-related fracture risk would have "
    "thinner trabeculae in spongy bone and a thinner compact cortex; "
    "weight-bearing exercise could stimulate osteoblast activity to "
    "reinforce trabecular architecture.",
]

SIM_HELP_ABUSE = [
    "tissue membranes",
    "tell me the answer",
    "just give me the answer please",
    "answer please",
    "what's the answer",
    "stop with hints, just tell me",
]

SIM_OFF_TOPIC = [
    # Reliable corpus topic: "Directional Terms in Anatomy" (Chapter 1).
    "directional terms",
    "what's the weather like today?",
    "have you seen the new Marvel movie?",
    "who won the game last night?",
    "let's talk about something else",
]

def main() -> int:
    print("[sweep] booting retriever + memory + graph...", flush=True)
    t0 = time.time()
    retriever = Retriever()
    memory = MemoryManager()
    graph = build_graph(retriever, memory)
    print(f"[sweep] ready in {time.time() - t0:.1f}s", flush=True)

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = ROOT / "data" / "artifacts" / f"sweep_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    sims = [
        ("happy_path", SIM_HAPPY_PATH),
        ("help_abuse", SIM_HELP_ABUSE),
        ("off_topic", SIM_OFF_TOPIC),
    ]

    runs = []
    overall_t0 = time.time()
    for name, script in sims:
        try:
            runs.append(run_sim(graph, name, script))
        except Exception as e:
            import traceback

            traceback.print_exc()
            runs.append({"sim": name, "error": str(e), "turns": []})
    overall_dt = time.time() - overall_t0

    out_json = out_dir / "sweep.json"
    out_json.write_text(json.dumps({"runs": runs, "elapsed_s": overall_dt}, indent=2))
    print(f"\n[sweep] wrote {out_json} ({len(runs)} sims, {overall_dt:.1f}s total)", flush=True)
    print(f"[sweep] artifact dir: {out_dir}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
