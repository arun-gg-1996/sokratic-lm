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

All errors are non-fatal — if Qdrant is down, load() returns [] and
flush() silently skips. The session continues normally in both cases.
"""

import re
from memory.persistent_memory import PersistentMemory
from conversation.state import TutorState


class MemoryManager:
    def __init__(self):
        self.persistent = PersistentMemory()
        self.last_flush_status = "not_run"

    def load(self, student_id: str) -> list[dict]:
        """
        Load student's past weak topics and session history from mem0.

        Parses memory strings looking for patterns like:
          "Struggled with: X (failed N times)"
        Returns structured weak_topics sorted by failure_count descending.

        Returns:
            List of weak topic dicts:
            [{topic: str, difficulty: str, failure_count: int}]
            Returns [] on any error or if no history exists.
        """
        try:
            memories = self.persistent.get(student_id)
            if isinstance(memories, dict):
                memories = memories.get("results", [])
            if not memories:
                return []

            weak_topics: dict[str, dict] = {}

            for mem in memories:
                if not isinstance(mem, dict):
                    continue
                # mem0 returns dicts with a "memory" or "text" key
                text = mem.get("memory") or mem.get("text") or ""
                if not text:
                    continue

                # Parse the "Struggled with:" segment, allowing one or many topics.
                struggle_match = re.search(
                    r"Struggled with:\s*(.*?)(?:\.|$)",
                    text,
                    re.IGNORECASE | re.DOTALL,
                )
                if not struggle_match:
                    continue

                struggled_segment = struggle_match.group(1).strip()
                if not struggled_segment or struggled_segment.lower() == "none":
                    continue

                # Matches examples like:
                #   "Deltoid innervation (failed 3 times)"
                #   "Deltoid innervation (3 times)"
                matches = re.findall(
                    r"([^;,.]+?)\s*\((?:failed\s*)?(\d+)\s*times?\)",
                    struggled_segment,
                    re.IGNORECASE,
                )
                for topic_name, count_str in matches:
                    topic_name = topic_name.strip()
                    if not topic_name:
                        continue
                    count = int(count_str)
                    if topic_name not in weak_topics or weak_topics[topic_name]["failure_count"] < count:
                        weak_topics[topic_name] = {
                            "topic": topic_name,
                            "difficulty": "moderate",  # default; overridden if stored
                            "failure_count": count,
                        }

            return sorted(weak_topics.values(), key=lambda x: x["failure_count"], reverse=True)

        except Exception:
            return []

    def flush(self, student_id: str, state: TutorState, summary_text: str = "") -> bool:
        """
        Write session outcomes to persistent memory.

        Builds a natural language summary from state fields and writes
        it to mem0. mem0 embeds the summary for future semantic retrieval.

        Non-fatal: if Qdrant is unavailable, this is silently skipped.
        """
        if not getattr(self.persistent, "available", False):
            self.last_flush_status = "skipped_qdrant_unavailable"
            return False

        try:
            if summary_text and summary_text.strip():
                summary = summary_text.strip()
            else:
                weak_topics = state.get("weak_topics", [])
                reached = state.get("student_reached_answer", False)
                topic_path = ""

                # Try to extract topic from messages (first student message is usually the topic)
                for msg in state.get("messages", []):
                    if msg.get("role") == "student":
                        topic_path = msg.get("content", "")[:80]
                        break

                mastered_str = topic_path if reached else "none"
                struggled_parts = []
                for wt in weak_topics:
                    topic = wt.get("topic", "")
                    count = wt.get("failure_count", 1)
                    struggled_parts.append(f"{topic} (failed {count} times)")

                struggled_str = "; ".join(struggled_parts) if struggled_parts else "none"

                summary = (
                    f"Session summary for student {student_id}. "
                    f"Topic covered: {topic_path or 'unknown'}. "
                    f"Mastered: {mastered_str}. "
                    f"Struggled with: {struggled_str}."
                )

            persisted = bool(self.persistent.add(student_id, summary))
            self.last_flush_status = "persisted_to_mem0_qdrant" if persisted else "failed_mem0_add"
            return persisted

        except Exception:
            self.last_flush_status = "failed_exception_during_flush"
            return False
