"""
scripts/cache_smoke_test.py
---------------------------
Verify whether Anthropic prompt caching is actually firing in the current
production pipeline.

Why this exists
---------------
The forward plan (2026-04-22) flagged "cache hit rate is 0% in
production due to a history-in-cached-block bug." Code inspection
(conversation/dean.py:111) confirms history is folded into the cached
prefix, so the prefix bytes change every turn. But telemetry on the
current pipeline was last captured 2026-04-20 (pre-rebuild) and tonight's
saved JSONs strip per-call cache fields. Need fresh evidence before
committing to a 1.5-hour cache-restructure fix.

What this does
--------------
Runs a single S2-Moderate conversation for ~3 turns end-to-end via the
real graph. Patches the Anthropic client to log `cache_read_input_tokens`
and `cache_creation_input_tokens` from EVERY messages.create() response
to stdout, tagged with the wrapper that made the call.

Output: per-turn / per-call cache_read and cache_write, plus a final
summary. If cache_read is 0 across all turns, the bug is confirmed.

Usage
-----
  cd /Users/arun-ghontale/UB/NLP/sokratic
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/cache_smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

# Patch the anthropic client BEFORE importing dean/teacher so the patch
# applies to their imports.
import anthropic  # noqa: E402

_CACHE_LOG: list[dict] = []
_CALL_INDEX = {"n": 0}


def _patched_messages_create(self, *args, **kwargs):
    # The dean/teacher pass cache_control via system blocks; any Anthropic
    # response includes usage.cache_read_input_tokens and
    # usage.cache_creation_input_tokens when caching is in play. We just
    # log them with the call index.
    resp = self._original_messages_create(*args, **kwargs)
    _CALL_INDEX["n"] += 1
    usage = getattr(resp, "usage", None)
    cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    _CACHE_LOG.append({
        "call_idx": _CALL_INDEX["n"],
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
        "input_tokens": inp,
        "output_tokens": out,
    })
    print(
        f"  [api call #{_CALL_INDEX['n']:>2}] "
        f"input={inp:>5}  cache_read={cr:>5}  cache_write={cw:>5}  output={out}",
        flush=True,
    )
    return resp


# Patch the Messages.create class methods directly. The Anthropic SDK
# attaches `messages` as a cached_property on the client, so we have to
# patch the underlying Messages class, not client.messages.
def _install_patch():
    from anthropic.resources.messages.messages import Messages
    if not hasattr(Messages, "_original_messages_create"):
        Messages._original_messages_create = Messages.create
        Messages.create = _patched_messages_create


_install_patch()

# NOW import the rest, post-patch.
from conversation.state import initial_state  # noqa: E402
from conversation.graph import build_graph  # noqa: E402
from memory.memory_manager import MemoryManager  # noqa: E402
from evaluation.simulation.profiles import PROFILES  # noqa: E402
from evaluation.simulation.student_simulator import StudentSimulator  # noqa: E402
from config import cfg  # noqa: E402


async def main():
    # Default to chunks-mode retriever (matches the production target).
    retriever_kind = os.environ.get("SOKRATIC_RETRIEVER", "chunks").strip().lower()
    if retriever_kind == "chunks":
        from retrieval.retriever import ChunkRetriever as Retriever
    else:
        from retrieval.retriever import Retriever

    print(f"\n=== Cache smoke test ({retriever_kind}-mode retriever) ===\n", flush=True)

    print("Building retriever + graph...", flush=True)
    try:
        retriever = Retriever()
    except Exception as e:
        print(f"Retriever load failed: {e}", flush=True)
        from retrieval.retriever import MockRetriever
        retriever = MockRetriever()

    memory_manager = MemoryManager()
    graph = build_graph(retriever, memory_manager)
    print("Graph built. Starting 3-turn conversation...\n", flush=True)

    # 3-turn S2 (Moderate) conversation. We pick a topic that is in-corpus
    # so the conversation stays on rails — what we're measuring is cache
    # behavior, not retrieval behavior.
    topic = "How does the body regulate blood pressure through neural mechanisms?"
    profile_id = "S2"
    conv_id = str(uuid.uuid4())[:8]
    student_id = f"{profile_id}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_config = {"configurable": {"thread_id": conv_id}}
    simulator = StudentSimulator(PROFILES[profile_id])

    # Per-turn: log the call indices so we can attribute calls to turns.
    turn_call_ranges: list[tuple[int, int, str]] = []  # (start, end, label)

    def _mark_turn_start(label):
        return _CALL_INDEX["n"] + 1

    def _mark_turn_end(start, label):
        end = _CALL_INDEX["n"]
        turn_call_ranges.append((start, end, label))

    # Turn 0: Rapport
    s = _mark_turn_start("rapport")
    print("\n--- Turn 0: rapport ---", flush=True)
    state["phase"] = "rapport"
    state = await graph.ainvoke(state, thread_config)
    _mark_turn_end(s, "rapport")

    # Turn 1: Topic input
    s = _mark_turn_start("topic_input")
    print("\n--- Turn 1: topic_input ---", flush=True)
    student_msg = topic
    state["messages"] = list(state.get("messages", [])) + [
        {"role": "student", "content": student_msg}
    ]
    state["phase"] = "topic_engagement"
    state = await graph.ainvoke(state, thread_config)
    _mark_turn_end(s, "topic_input")

    # Turn 2: Tutoring (student responds)
    s = _mark_turn_start("tutoring_1")
    print("\n--- Turn 2: tutoring_1 ---", flush=True)
    student_resp = simulator.respond(state)
    state["messages"] = list(state.get("messages", [])) + [
        {"role": "student", "content": student_resp}
    ]
    state = await graph.ainvoke(state, thread_config)
    _mark_turn_end(s, "tutoring_1")

    # Turn 3: Tutoring 2
    s = _mark_turn_start("tutoring_2")
    print("\n--- Turn 3: tutoring_2 ---", flush=True)
    student_resp = simulator.respond(state)
    state["messages"] = list(state.get("messages", [])) + [
        {"role": "student", "content": student_resp}
    ]
    state = await graph.ainvoke(state, thread_config)
    _mark_turn_end(s, "tutoring_2")

    # ----- Summary -----
    print("\n" + "=" * 70)
    print("CACHE SUMMARY")
    print("=" * 70)
    total_cr = sum(c["cache_read_input_tokens"] for c in _CACHE_LOG)
    total_cw = sum(c["cache_creation_input_tokens"] for c in _CACHE_LOG)
    total_in = sum(c["input_tokens"] for c in _CACHE_LOG)

    print(f"\nTotal API calls: {len(_CACHE_LOG)}")
    print(f"Total uncached input tokens: {total_in}")
    print(f"Total cache_read_input_tokens (savings):  {total_cr}")
    print(f"Total cache_creation_input_tokens (writes): {total_cw}")

    if turn_call_ranges:
        print(f"\nPer-turn breakdown:")
        for start, end, label in turn_call_ranges:
            calls = [c for c in _CACHE_LOG if start <= c["call_idx"] <= end]
            t_cr = sum(c["cache_read_input_tokens"] for c in calls)
            t_cw = sum(c["cache_creation_input_tokens"] for c in calls)
            t_in = sum(c["input_tokens"] for c in calls)
            print(f"  {label:<14} ({len(calls):>2} calls): "
                  f"input={t_in:>6} cache_read={t_cr:>6} cache_write={t_cw:>6}")

    print(f"\n{'='*70}")
    if total_cr == 0:
        print("⚠️  cache_read=0 across ALL calls — cache hit rate is 0%.")
        print("   The history-in-cached-block bug is observably real.")
    else:
        ratio = total_cr / max(total_cr + total_in, 1)
        print(f"✅ cache_read > 0 ({total_cr} tokens, {ratio*100:.1f}% of input).")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
