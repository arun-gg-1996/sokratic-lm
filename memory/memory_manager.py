"""
memory/memory_manager.py
-------------------------
Orchestrates memory tiers for the conversation graph.

Memory is stubbed out — load() always returns [] and flush() is a no-op.
Re-enable mem0 integration here when ready.
"""

from conversation.state import TutorState


class MemoryManager:
    def __init__(self):
        self.last_flush_status = "stubbed"

    def load(self, student_id: str) -> list[dict]:
        """
        Load student's past weak topics from persistent memory.
        Stubbed out — returns empty list always.
        """
        return []

    def flush(self, student_id: str, state: TutorState, summary_text: str = "") -> bool:
        """
        Write session summary to persistent memory.
        Stubbed out — no-op.
        """
        self.last_flush_status = "stubbed_no_op"
        return False
