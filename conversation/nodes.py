"""
conversation/nodes.py
----------------------
One function per LangGraph node. Each function:
  - Takes the full TutorState
  - Does one focused thing
  - Returns a partial state update dict (LangGraph merges it)

Nodes:
  rapport_node        — greet student, load memory, pick topic
  dean_node           — main Dean-Teacher loop for one student turn
  assessment_node     — clinical question or answer reveal
  memory_update_node  — flush session to mem0, clear state

The graph in graph.py wires these together with edges from edges.py.
"""

from conversation.state import TutorState


def rapport_node(state: TutorState, dean, memory_manager) -> dict:
    """
    Phase 1 — Rapport.

    - Load student memory via Dean's get_student_memory tool.
    - Seed weak_topics in state.
    - Suggest the most-failed weak topic OR prompt topic selection.
    - Return greeting message and updated state fields.
    """
    # TODO: implement
    raise NotImplementedError


def dean_node(state: TutorState, dean, teacher) -> dict:
    """
    Phase 2 — Socratic Tutoring (one full Dean-Teacher loop turn).

    - Dean decides whether to search.
    - Dean locks the answer if not yet set.
    - Dean checks student answer.
    - Dean gets Teacher draft.
    - Dean evaluates draft (leak, question, sycophancy).
    - Returns approved response or Dean's own response on repeated failure.
    - Runs summarizer if turn_count > max_turns.
    """
    # TODO: implement
    raise NotImplementedError


def assessment_node(state: TutorState, dean, teacher) -> dict:
    """
    Phase 3 — Assessment.

    If student_reached_answer = True:
        1. Teacher (supervised by Dean) asks a clinical application question.
        2. Student answers.
        3. Dean compares student's reasoning against textbook and generates a
           MASTERY SUMMARY — a short paragraph with:
             - confirmation of correct answer
             - exact textbook quote as ground truth
             - evaluation of clinical reasoning
             - "Topic marked as mastered."
           Mastery summary shown to student and logged to data/artifacts/conversations/.

    If student_reached_answer = False (hint_level exceeded max_hints):
        1. Dean reveals the answer with the exact textbook passage.
           e.g. "The answer is the axillary nerve. The textbook states: '...'
                 This topic has been added to your weak spots."
        2. Topic added to weak_topics with incremented failure_count.
        3. No mastery summary.
    """
    # TODO: implement
    raise NotImplementedError


def memory_update_node(state: TutorState, dean, memory_manager) -> dict:
    """
    Phase 4 — Memory Update.

    - Dean calls update_student_memory with a session summary.
    - Increment concepts_covered_count and failure counts.
    - Return cleared state fields ready for next session.
    """
    # TODO: implement
    raise NotImplementedError
