"""
conversation/edges.py
----------------------
Conditional routing logic for the LangGraph graph.

Each function takes the current TutorState and returns the name of the
next node to visit.

How the loop works:
  The graph does NOT loop internally. Each graph.invoke() call handles
  exactly ONE student turn and ends at END. Streamlit calls invoke()
  again with the next student message. The LangGraph checkpointer
  (MemorySaver) preserves state between calls via thread_id.

Routing rules:
  - After rapport_node      → always "dean_node"
  - After dean_node         → "assessment_node"  if hint_level > max_hints
                                                OR student_reached_answer
                            → END               otherwise (response delivered, wait for next input)
  - After assessment_node   → "memory_update_node"
  - After memory_update_node → END
"""

from langgraph.graph import END
from conversation.state import TutorState


def after_rapport(state: TutorState) -> str:
    """Always move to tutoring after rapport."""
    return "dean_node"


def after_dean(state: TutorState) -> str:
    """
    After Dean delivers a response:
    - Move to assessment if student answered or hints exhausted.
    - Otherwise END — Streamlit will call invoke() again on next student message.
    """
    if state["student_reached_answer"]:
        return "assessment_node"
    if state["hint_level"] > state["max_hints"]:
        return "assessment_node"
    return END


def after_assessment(state: TutorState) -> str:
    """Always move to memory update after assessment."""
    return "memory_update_node"
