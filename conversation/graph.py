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

    Args:
        retriever:       Retriever or MockRetriever instance
        memory_manager:  MemoryManager instance (loads/flushes mem0)

    Returns:
        Compiled LangGraph runnable (with MemorySaver checkpointer).
    """
    dean = DeanAgent(retriever, memory_manager.persistent)
    teacher = TeacherAgent()

    graph = StateGraph(TutorState)

    graph.add_node("rapport_node", partial(rapport_node, teacher=teacher, memory_manager=memory_manager))
    graph.add_node("dean_node", partial(dean_node, dean=dean, teacher=teacher))
    graph.add_node("assessment_node", partial(assessment_node, dean=dean, teacher=teacher))
    graph.add_node("memory_update_node", partial(memory_update_node, dean=dean, memory_manager=memory_manager))

    graph.add_edge(START, "rapport_node")
    graph.add_conditional_edges("rapport_node", after_rapport)
    graph.add_conditional_edges("dean_node", after_dean)
    graph.add_conditional_edges("assessment_node", after_assessment)
    graph.add_edge("memory_update_node", END)

    return graph.compile(checkpointer=MemorySaver())
