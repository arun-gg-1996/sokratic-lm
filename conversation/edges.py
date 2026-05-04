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
                                                 OR turn_count >= max_turns
                             → END               otherwise (response delivered, wait for next input)
                             Note: hint_level > max_hints does NOT route to
                             assessment — students get the full max_turns
                             budget; hint exhaustion just halts formal hints.
  - After assessment_node    → END               if assessment_turn in (1, 2)
                                                  (waiting for student reply: opt-in choice or clinical answer)
                             → "memory_update_node" if assessment_turn == 3 (done)
  - After memory_update_node → END
"""

from langgraph.graph import END
from conversation.state import TutorState
from config import cfg


def after_rapport(state: TutorState) -> str:
    """Route after rapport_node (which skips if phase != 'rapport').
    - If phase already memory_update (M1 explicit-exit fired between turns) →
      memory_update_node so close fires immediately, no Dean call.
    - If in assessment phase waiting for student input (opt-in or clinical) → assessment_node
    - Otherwise → dean_node
    """
    if state.get("phase") == "memory_update":
        return "memory_update_node"
    if state.get("phase") == "assessment" and state.get("assessment_turn") in (1, 2):
        return "assessment_node"
    return "dean_node"


def after_dean(state: TutorState) -> str:
    """
    After Dean delivers a response:
    - Move to assessment if student answered, hints exhausted, or turn limit hit.
    - When assessment_style == "none", skip assessment entirely and go
      straight to memory update.
    - Otherwise END — Streamlit will call invoke() again on next student message.

    IMPORTANT (revised 2026-05-01): hint_level > max_hints DOES route to
    assessment. The earlier attempt to let tutoring continue past hint 3
    failed because the Dean's early-exit at hint-exhaustion (dean.py:~1792)
    skips Teacher draft entirely — so the session would loop with no new
    tutor message. The architecture assumes hint exhaustion = session-end
    trigger; honour that.
    """
    assessment_style = getattr(cfg.session, "assessment_style", "clinical")

    if state.get("phase") == "memory_update":
        return "memory_update_node"
    if state.get("student_reached_answer"):
        if assessment_style == "none":
            return "memory_update_node"
        return "assessment_node"
    # M1 — hint-exhausted goes STRAIGHT to memory_update with honest_close
    # tone. Asking opt_in for a clinical bonus when the student didn't even
    # reach the core answer is bad UX (and produced wrong reach_close text).
    if int(state.get("hint_level", 0) or 0) > int(state.get("max_hints", 0) or 0):
        return "memory_update_node"
    if int(state.get("turn_count", 0) or 0) >= int(state.get("max_turns", 0) or 0):
        return "assessment_node"
    return END


def after_assessment(state: TutorState) -> str:
    """
    After assessment_node runs:
    - assessment_turn in (1, 2): waiting for student's answer (END).
    - assessment_turn == 3: assessment complete — move to memory update.
    """
    if int(state.get("assessment_turn", 0) or 0) == 3:
        return "memory_update_node"
    return END
