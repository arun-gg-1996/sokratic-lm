"""
Interactive sim driver. Polls /tmp/sim_in.txt for commands, writes tutor
reply + diagnostics to /tmp/sim_out.txt. Graph + retriever stay warm.

Commands (one per /tmp/sim_in.txt write):
  NEW                  -> fresh session, runs rapport, returns tutor msg
  MSG <text>           -> append student msg, invoke graph, return tutor msg
  EXIT                 -> stop driver
"""
import json
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg  # noqa: E402
from conversation.graph import build_graph  # noqa: E402
from conversation.state import initial_state  # noqa: E402
from memory.memory_manager import MemoryManager  # noqa: E402
from retrieval.retriever import Retriever  # noqa: E402

IN = Path("/tmp/sim_in.txt")
OUT = Path("/tmp/sim_out.txt")
READY = Path("/tmp/sim_ready")

print("[driver] Loading retriever and graph...", flush=True)
retriever = Retriever()
memory = MemoryManager()
graph = build_graph(retriever, memory)
print("[driver] Ready.", flush=True)
READY.write_text("ready")

session = {"state": None, "thread_cfg": None}


def _last_tutor(state):
    for m in reversed(state.get("messages", [])):
        if m.get("role") == "tutor":
            return m.get("content", "")
    return ""


def _diag(state):
    return {
        "phase": state.get("phase"),
        "topic_confirmed": state.get("topic_confirmed"),
        "topic_options": state.get("topic_options", []),
        "locked_question": state.get("locked_question", ""),
        "locked_answer": state.get("locked_answer", ""),
        "student_state": state.get("student_state"),
        "hint_level": state.get("hint_level"),
        "turn_count": state.get("turn_count"),
        "assessment_turn": state.get("assessment_turn"),
        "clinical_opt_in": state.get("clinical_opt_in"),
        "student_reached_answer": state.get("student_reached_answer"),
        "exploration_used": state.get("exploration_used"),
        "locked_topic": state.get("locked_topic"),
    }


def _write_out(tutor_msg, state, extra=None):
    payload = {
        "tutor": tutor_msg,
        "diag": _diag(state) if state else {},
    }
    if extra:
        payload.update(extra)
    OUT.write_text(json.dumps(payload, indent=2, default=str))


def handle(cmd: str) -> bool:
    cmd = cmd.strip()
    if not cmd:
        return True
    if cmd == "EXIT":
        _write_out("bye", session["state"] or {})
        return False
    if cmd == "NEW":
        conv_id = str(uuid.uuid4())[:8]
        state = initial_state(f"sim_{conv_id}", cfg)
        thread_cfg = {"configurable": {"thread_id": conv_id}}
        state = graph.invoke(state, thread_cfg)
        session["state"] = state
        session["thread_cfg"] = thread_cfg
        _write_out(_last_tutor(state), state, {"conv_id": conv_id})
        return True
    if cmd.startswith("MSG "):
        text = cmd[4:].strip()
        state = session["state"]
        if state is None:
            _write_out("ERR: no session, send NEW first", {})
            return True
        prev_len = len(state.get("messages", []))
        state["messages"].append({"role": "student", "content": text})
        try:
            state = graph.invoke(state, session["thread_cfg"])
        except Exception as e:
            import traceback
            traceback.print_exc()
            _write_out(f"ERR: {e}", state)
            return True
        session["state"] = state
        # collect new tutor msgs since prev_len
        new_tutor = [
            m["content"] for m in state.get("messages", [])[prev_len:]
            if m.get("role") == "tutor"
        ]
        tutor_msg = "\n\n".join(new_tutor) or _last_tutor(state)
        _write_out(tutor_msg, state)
        return True
    _write_out(f"ERR: unknown command: {cmd}", session["state"] or {})
    return True


IN.write_text("")
OUT.write_text("")
print("[driver] Polling /tmp/sim_in.txt", flush=True)
while True:
    try:
        txt = IN.read_text().strip()
    except FileNotFoundError:
        txt = ""
    if txt:
        IN.write_text("")
        cont = handle(txt)
        if not cont:
            break
    time.sleep(0.3)

print("[driver] exit", flush=True)
