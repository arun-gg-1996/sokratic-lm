"""
memory/memory_manager.py
-------------------------
Orchestrates both memory tiers for the conversation graph.

Two operations:
  load(student_id)
    Called at session start (inside rapport_node).
    Fetches past memories from Tier 2 (mem0/Qdrant).
    Returns a list of weak_topics dicts to seed into TutorState.

  flush(student_id, state)
    Called at session end (inside memory_update_node).
    Writes the session summary to Tier 2.
    Tier 1 (LangGraph MemorySaver) is cleared automatically when the
    graph run ends.
"""

from memory.persistent_memory import PersistentMemory
from conversation.state import TutorState


class MemoryManager:
    def __init__(self):
        self.persistent = PersistentMemory()

    def load(self, student_id: str) -> list[dict]:
        """
        Load student's past weak topics and session history.

        Returns:
            List of weak topic dicts for seeding TutorState:
            [{topic: str, difficulty: str, failure_count: int}]
        """
        # TODO: call self.persistent.get(student_id)
        # TODO: parse memory strings into structured weak_topics list
        # TODO: sort by failure_count descending (most-failed first)
        raise NotImplementedError

    def flush(self, student_id: str, state: TutorState) -> None:
        """
        Write session outcomes to persistent memory.

        Builds a natural language summary from state:
          - topics covered this session
          - topics mastered (student reached answer)
          - topics failed (added to weak_topics)
        Then calls self.persistent.add(student_id, summary).
        """
        # TODO: build session summary string from state
        # TODO: call self.persistent.add(student_id, summary)
        raise NotImplementedError
