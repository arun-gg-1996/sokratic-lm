"""
conversation/graph.py
----------------------
Assembles the full LangGraph StateGraph and compiles it into a runnable.

Graph shape:
  START → rapport_node → dean_node ⟷ (loops on student input)
                                    ↓ (when answer reached or hints exhausted)
                             assessment_node → memory_update_node → END

Usage from Streamlit (ui/app.py):
    from conversation.graph import build_graph
    graph = build_graph(retriever, memory_manager)

    # On each student message:
    state = graph.invoke(state)          # blocking
    # or
    for chunk in graph.stream(state):    # streaming tokens
        ...
"""

from langgraph.graph import StateGraph, END
from conversation.state import TutorState
from conversation.nodes import rapport_node, dean_node, assessment_node, memory_update_node
from conversation.edges import after_rapport, after_dean, after_assessment


def build_graph(retriever, memory_manager):
    """
    Build and compile the LangGraph StateGraph.

    Args:
        retriever:       Retriever instance
        memory_manager:  MemoryManager instance (loads/flushes mem0)

    Returns:
        Compiled LangGraph runnable.
    """
    # TODO: import DeanAgent and TeacherAgent, initialize with retriever + memory_manager
    # TODO: wrap node functions with functools.partial to inject agent dependencies
    # TODO: build StateGraph(TutorState)
    # TODO: add_node for each of the 4 nodes
    # TODO: add_edge START -> rapport_node
    # TODO: add_conditional_edges for after_rapport, after_dean, after_assessment
    # TODO: add_edge memory_update_node -> END
    # TODO: return graph.compile(checkpointer=MemorySaver())
    raise NotImplementedError
