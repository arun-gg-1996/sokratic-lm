"""
conversation/graph.py
----------------------
Assembles the full LangGraph StateGraph and compiles it into a runnable.

Graph shape:
  START → rapport_node → dean_node ⟷ (loops on student input via Streamlit)
                                    ↓ (when answer reached, hints exhausted, or turn limit)
                             assessment_node → memory_update_node → END

Note: The graph does NOT loop internally. Each graph.invoke() handles exactly
one student turn and ends at END (or after assessment when assessment_turn < 2).
Streamlit calls invoke() again with the updated state on each student message.
The LangGraph MemorySaver checkpointer preserves state between calls via thread_id.

Usage from Streamlit (ui/app.py):
    from conversation.graph import build_graph
    graph = build_graph(retriever, memory_manager)
    config = {"configurable": {"thread_id": thread_id}}

    # On each student message:
    state = graph.invoke(state, config=config)
"""

from functools import partial
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from conversation.state import TutorState
from conversation.nodes import rapport_node, dean_node, assessment_node, memory_update_node
from conversation.edges import after_rapport, after_dean, after_assessment
from conversation.teacher import TeacherAgent
from conversation.dean import DeanAgent


def build_graph(retriever, memory_manager):
    """
    Build and compile the LangGraph StateGraph.

    Routes the dean_node slot to either the LEGACY implementation
    (conversation.nodes.dean_node) or the NEW v2 stack
    (conversation.nodes_v2.dean_node_v2) based on the SOKRATIC_USE_V2_FLOW
    env var (Track 4.7c). Default OFF — production stays on legacy until
    flag is flipped.

    The v2 dean_node_v2 itself falls back to the legacy dean_node for
    UNLOCKED-topic turns (per the Track 4.7b scope), so the flag only
    materially affects locked-topic per-turn tutoring behavior.

    Args:
        retriever:       Retriever or MockRetriever instance
        memory_manager:  MemoryManager instance (loads/flushes mem0)

    Returns:
        Compiled LangGraph runnable (with MemorySaver checkpointer).
    """
    from conversation.nodes_v2 import dean_node_v2, use_v2_flow

    # Compatibility: real MemoryManager exposes `.persistent`, while the current
    # stubbed manager may not. Dean accepts either and currently does not depend
    # on persistence-specific methods.
    memory_client = getattr(memory_manager, "persistent", memory_manager)
    dean = DeanAgent(retriever, memory_client)
    teacher = TeacherAgent()

    graph = StateGraph(TutorState)

    graph.add_node("rapport_node", partial(rapport_node, teacher=teacher, memory_manager=memory_manager))

    # Per Track 4.7c: feature-flagged dispatch on the dean_node slot.
    # When SOKRATIC_USE_V2_FLOW=1, dean_node_v2 owns the locked-topic
    # path; it still defers to legacy dean_node for unlocked-topic turns.
    if use_v2_flow():
        graph.add_node(
            "dean_node",
            partial(dean_node_v2, dean=dean, teacher=teacher, retriever=retriever),
        )
    else:
        graph.add_node("dean_node", partial(dean_node, dean=dean, teacher=teacher))

    graph.add_node("assessment_node", partial(assessment_node, dean=dean, teacher=teacher))
    graph.add_node("memory_update_node", partial(memory_update_node, dean=dean, memory_manager=memory_manager))

    graph.add_edge(START, "rapport_node")
    graph.add_conditional_edges("rapport_node", after_rapport)
    graph.add_conditional_edges("dean_node", after_dean)
    graph.add_conditional_edges("assessment_node", after_assessment)
    graph.add_edge("memory_update_node", END)

    return graph.compile(checkpointer=MemorySaver())
