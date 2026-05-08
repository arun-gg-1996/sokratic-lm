"""
Builds the LangGraph StateGraph for one tutoring turn.

Graph shape:

    START → rapport_node → dean_node → assessment_node → memory_update_node → END

Each node owns one phase: rapport greets the student and loads
cross-session memory; dean_node runs preflight + Dean planner +
Teacher draft + verifier quartet; assessment_node handles the opt-in
and clinical loop after the answer is reached; memory_update_node
flushes the session to mem0 + SQLite and writes the closing message.

Each `graph.invoke(state, config)` handles exactly ONE student turn
and ends at END. The frontend calls invoke again on each new student
message; LangGraph's MemorySaver checkpointer preserves state between
calls via thread_id.

The bootstrap path (topic-lock anchor calls, starter cards, prelock
refusal handlers) still uses the legacy DeanAgent / TeacherAgent for
its LLM calls, so both are instantiated here and threaded into the
relevant nodes.

    from conversation.graph import build_graph
    graph = build_graph(retriever, memory_manager)
    state = graph.invoke(state, config={"configurable": {"thread_id": tid}})
"""

from functools import partial
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from conversation.state import TutorState
from conversation.lifecycle_v2 import (
    rapport_node,
    memory_update_node,
    after_rapport,
    after_dean,
    after_assessment,
)
from conversation.nodes_v2 import dean_node_v2, assessment_node_v2

# V1 agents — retained until the D1-bootstrap migration ports the 4
# legacy dean methods used by topic_lock_v2 (and the teacher callback
# registry; the latter has already moved to conversation/streaming.py).
# These instances are passed via partial into the V2 nodes so the
# bootstrap path's LLM-driven refuse/anchor/ack calls still work.
from conversation.dean import DeanAgent
from conversation.teacher import TeacherAgent

def build_graph(retriever, memory_manager):
    """Build and compile the V2 LangGraph StateGraph.

 Args:
 retriever: Retriever or MockRetriever instance
 memory_manager: MemoryManager instance (loads/flushes mem0)

 Returns:
 Compiled LangGraph runnable (with MemorySaver checkpointer).
"""
    # Compatibility: real MemoryManager exposes `.persistent`, while the
    # current stubbed manager may not. Dean accepts either and currently
    # does not depend on persistence-specific methods.
    memory_client = getattr(memory_manager, "persistent", memory_manager)
    dean = DeanAgent(retriever, memory_client)
    teacher = TeacherAgent()

    graph = StateGraph(TutorState)

    # rapport_node: V2 (TeacherV2 mode="rapport"). `teacher` partial
    # kwarg is currently unused by the V2 path but retained as the
    # signature still accepts it.
    graph.add_node(
        "rapport_node",
        partial(rapport_node, teacher=teacher, memory_manager=memory_manager),
    )

    # dean_node: V2 per-turn loop — preflight → dean_v2.plan → retry
    # orchestrator → verifier quartet. `dean` is V1 DeanAgent — required
    # by topic_lock_v2's bootstrap helpers (_lock_anchors_call
    # _retrieve_on_topic_lock, _build_topic_ack_message
    # _prelock_refuse_call, _prelock_anchor_fail_call). Without a real
    # DeanAgent instance, those calls silently fail and the bootstrap
    # path emits templated fallbacks instead of LLM-crafted replies.
    graph.add_node(
        "dean_node",
        partial(dean_node_v2, dean=dean, teacher=teacher, retriever=retriever),
    )

    # assessment_node: V2 — opt-in + clinical phase via DeanV2 + TeacherV2.
    graph.add_node(
        "assessment_node",
        partial(assessment_node_v2, dean=dean, teacher=teacher, retriever=retriever),
    )

    # memory_update_node: V2 — close-LLM (TeacherV2 mode="close") +
    # mem0 flush + SQLite session-end + mastery upsert.
    graph.add_node(
        "memory_update_node",
        partial(memory_update_node, dean=dean, memory_manager=memory_manager),
    )

    graph.add_edge(START, "rapport_node")
    graph.add_conditional_edges("rapport_node", after_rapport)
    graph.add_conditional_edges("dean_node", after_dean)
    graph.add_conditional_edges("assessment_node", after_assessment)
    graph.add_edge("memory_update_node", END)

    return graph.compile(checkpointer=MemorySaver())
