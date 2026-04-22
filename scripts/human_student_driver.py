"""
scripts/human_student_driver.py
--------------------------------
Driver where a pre-authored list of student messages is fed to the graph turn by
turn — no LLM student simulator. This lets a real reviewer (or the operator
acting as the student) replay a deterministic conversation.

Usage:
    .venv/bin/python scripts/human_student_driver.py \
        --profile S2 \
        --script data/artifacts/human_scripts/S2_deltoid_v1.json \
        --out data/artifacts/human_convos/
"""
import argparse
import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg
from conversation.state import initial_state


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


def _last_tutor(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "tutor":
            return str(m.get("content", ""))
    return ""


async def run_conversation(script: list, profile_id: str, topic_hint: str, out_dir: Path) -> Path:
    from conversation.graph import build_graph
    from retrieval.retriever import Retriever
    from memory.memory_manager import MemoryManager

    retriever = Retriever()
    mem = MemoryManager()
    graph = build_graph(retriever, mem)

    conv_id = str(uuid.uuid4())[:8]
    student_id = f"HUMAN_{profile_id}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_cfg = {"configurable": {"thread_id": conv_id}}

    transcript = []

    # Rapport turn — no student input yet
    state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
    transcript.append({
        "phase": "rapport",
        "role": "tutor",
        "content": _last_tutor(state.get("messages", [])),
        "snapshot": _snap(state),
    })
    _rollover_turn_trace(state)

    for idx, student_msg in enumerate(script):
        # student speaks
        state["messages"].append({"role": "student", "content": student_msg})
        transcript.append({
            "phase": state.get("phase"),
            "role": "student",
            "content": student_msg,
        })
        prev_len = len(state["messages"])
        state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
        # capture new tutor messages (there may be >1 if e.g. assessment opt-in follows ack)
        new = state.get("messages", [])[prev_len:]
        for m in new:
            if m.get("role") == "tutor":
                transcript.append({
                    "phase": state.get("phase"),
                    "role": "tutor",
                    "content": m.get("content", ""),
                    "snapshot": _snap(state),
                })
        _rollover_turn_trace(state)
        print(f"[{profile_id} turn {idx+1}] student={student_msg[:50]!r}")
        print(f"  tutor last={_last_tutor(state.get('messages',[]))[:80]!r}")
        print(f"  phase={state.get('phase')} hint={state.get('hint_level')} topic_conf={state.get('topic_confirmed')} reached={state.get('student_reached_answer')}")
        if state.get("phase") == "memory_update":
            break

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = out_dir / f"human_{profile_id}_{conv_id}_{stamp}.json"
    export = {
        "profile_id": profile_id,
        "topic_hint": topic_hint,
        "conv_id": conv_id,
        "exported_at": stamp,
        "transcript": transcript,
        "final_state": {
            "phase": state.get("phase"),
            "topic_confirmed": state.get("topic_confirmed"),
            "topic_selection": state.get("topic_selection"),
            "locked_question": state.get("locked_question"),
            "locked_answer": state.get("locked_answer"),
            "student_reached_answer": state.get("student_reached_answer"),
            "hint_level": state.get("hint_level"),
            "student_state": state.get("student_state"),
            "retrieval_calls": state.get("debug", {}).get("retrieval_calls"),
            "api_calls": state.get("debug", {}).get("api_calls"),
            "cost_usd": state.get("debug", {}).get("cost_usd"),
        },
        "messages": state.get("messages", []),
        "debug": state.get("debug", {}),
    }
    with open(out_path, "w") as f:
        json.dump(export, f, indent=2, default=str)
    print(f"\nSAVED: {out_path}")
    print(f"Summary: api_calls={export['final_state']['api_calls']} "
          f"cost=${export['final_state']['cost_usd']:.4f} "
          f"reached={export['final_state']['student_reached_answer']}")
    return out_path


def _snap(state: dict) -> dict:
    return {
        "phase": state.get("phase"),
        "topic_confirmed": state.get("topic_confirmed"),
        "topic_selection": state.get("topic_selection"),
        "locked_question": state.get("locked_question"),
        "locked_answer": state.get("locked_answer"),
        "hint_level": state.get("hint_level"),
        "student_state": state.get("student_state"),
        "student_reached_answer": state.get("student_reached_answer"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--script", required=True, help="Path to JSON with a list of student messages")
    ap.add_argument("--topic-hint", default="", help="Free-text label for what the script is about")
    ap.add_argument("--out", default="data/artifacts/human_convos/")
    args = ap.parse_args()

    with open(args.script) as f:
        script_data = json.load(f)
    script = script_data["messages"] if isinstance(script_data, dict) else script_data

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_conversation(script, args.profile, args.topic_hint, out_dir))


if __name__ == "__main__":
    main()
