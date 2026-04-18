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
  - After rapport_node       → always "dean_node"
  - After dean_node          → "assessment_node"  if student_reached_answer
                                                 OR hint_level > max_hints
                                                 OR turn_count >= max_turns
                             → END               otherwise (response delivered, wait for next input)
  - After assessment_node    → END               if assessment_turn in (1, 2)
                                                  (waiting for student reply: opt-in choice or clinical answer)
                             → "memory_update_node" if assessment_turn == 3 (done)
  - After memory_update_node → END
"""

from langgraph.graph import END
from conversation.state import TutorState


def after_rapport(state: TutorState) -> str:
    """Route after rapport_node (which skips if phase != 'rapport').
    - If in assessment phase waiting for student input (opt-in or clinical) → assessment_node
    - Otherwise → dean_node
    """
    if state.get("phase") == "assessment" and state.get("assessment_turn") in (1, 2):
        return "assessment_node"
    return "dean_node"


def after_dean(state: TutorState) -> str:
    """
    After Dean delivers a response:
    - Move to assessment if student answered, hints exhausted, or turn limit hit.
    - Otherwise END — Streamlit will call invoke() again on next student message.
    """
    if state["student_reached_answer"]:
        return "assessment_node"
    if state["hint_level"] > state["max_hints"]:
        return "assessment_node"
    if state["turn_count"] >= state["max_turns"]:
        return "assessment_node"
    return END


def after_assessment(state: TutorState) -> str:
    """
    After assessment_node runs:
    - assessment_turn in (1, 2): waiting for student's answer (END).
    - assessment_turn == 3: assessment complete — move to memory update.
    """
    if state["assessment_turn"] == 3:
        return "memory_update_node"
    return END
