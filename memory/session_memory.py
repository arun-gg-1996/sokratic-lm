"""
memory/session_memory.py
-------------------------
Thin wrapper around LangGraph MemorySaver.

Session memory is in-process and lives only for the current conversation.
It stores the full TutorState as LangGraph checkpoint state.

The MemorySaver is passed as the `checkpointer` argument when compiling
the graph in conversation/graph.py. LangGraph handles persistence
automatically — this module just provides the configured instance.

At session end, memory_manager.py handles flushing important fields
(weak_topics, mastered topics) to Tier 2 (persistent_memory.py).
"""

from langgraph.checkpoint.memory import MemorySaver


def get_session_checkpointer() -> MemorySaver:
    """Return a configured MemorySaver instance for use as LangGraph checkpointer."""
    return MemorySaver()
