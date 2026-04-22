"""
scripts/validate_gates.py
--------------------------
Minimal 4-6 turn validation for Gate A/B/C/D.
Dumps state debug to data/artifacts/validate_gates/<ts>.json.
Run: .venv/bin/python scripts/validate_gates.py
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg
from conversation.state import initial_state


OUT = Path(cfg.paths.artifacts) / "validate_gates"
OUT.mkdir(parents=True, exist_ok=True)

TOPIC = "What nerve innervates the deltoid muscle?"
# Student replies: wrong → still wrong → correct
REPLIES = [
    "I think it's the median nerve.",
    "Maybe the musculocutaneous nerve?",
    "Oh right, the axillary nerve.",
]


async def main():
    from conversation.graph import build_graph
    from retrieval.retriever import Retriever
    from memory.memory_manager import MemoryManager

    retriever = Retriever()
    mem = MemoryManager()
    graph = build_graph(retriever, mem)

    conv_id = str(uuid.uuid4())[:8]
    student_id = f"GATE_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_cfg = {"configurable": {"thread_id": conv_id}}

    log = []

    def rollover():
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

    def snap(tag):
        rollover()
        log.append({
            "tag": tag,
            "phase": state.get("phase"),
            "topic_confirmed": state.get("topic_confirmed"),
            "topic_selection": state.get("topic_selection"),
            "locked_question": state.get("locked_question"),
            "locked_answer": state.get("locked_answer"),
            "hint_level": state.get("hint_level"),
            "student_state": state.get("student_state"),
            "student_reached_answer": state.get("student_reached_answer"),
            "retrieval_calls": state.get("debug", {}).get("retrieval_calls"),
            "api_calls": state.get("debug", {}).get("api_calls"),
            "cost_usd": state.get("debug", {}).get("cost_usd"),
        })

    # Rapport
    state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
    snap("after_rapport")

    # Topic question
    state["messages"].append({"role": "student", "content": TOPIC})
    state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
    snap("after_topic_input")

    # If the assistant offered topic cards, pick the first
    if not state.get("topic_confirmed", False):
        opts = state.get("topic_options", [])
        pick = opts[0] if opts else TOPIC
        state["messages"].append({"role": "student", "content": pick})
        state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
        snap("after_card_pick")

    # Now drive 3 tutoring turns
    for i, reply in enumerate(REPLIES):
        if state.get("phase") in ("memory_update",):
            break
        state["messages"].append({"role": "student", "content": reply})
        state = await asyncio.to_thread(graph.invoke, state, thread_cfg)
        snap(f"after_reply_{i+1}")
        if state.get("phase") in ("memory_update",):
            break

    # Save export
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    export = {
        "exported_at": stamp,
        "thread_id": conv_id,
        "student_id": student_id,
        "log_snapshots": log,
        "final_state": {
            "phase": state.get("phase"),
            "topic_confirmed": state.get("topic_confirmed"),
            "topic_selection": state.get("topic_selection"),
            "locked_question": state.get("locked_question"),
            "locked_answer": state.get("locked_answer"),
            "student_reached_answer": state.get("student_reached_answer"),
            "hint_level": state.get("hint_level"),
            "retrieved_chunks_n": len(state.get("retrieved_chunks", [])),
        },
        "debug": state.get("debug", {}),
        "messages": state.get("messages", []),
    }
    out_path = OUT / f"validate_{stamp}.json"
    with open(out_path, "w") as f:
        json.dump(export, f, indent=2, default=str)
    print(f"\nSAVED: {out_path}")
    print(f"api_calls={state.get('debug',{}).get('api_calls')} "
          f"cost=${state.get('debug',{}).get('cost_usd'):.4f} "
          f"retrieval_calls={state.get('debug',{}).get('retrieval_calls')}")
    return out_path


if __name__ == "__main__":
    asyncio.run(main())
