"""
memory/persistent_memory.py
-----------------------------
Cross-session student memory using mem0 backed by Qdrant.

What mem0 does:
  You write a natural language memory string → mem0 embeds it and stores
  it in the 'sokratic_memory' Qdrant collection.
  You search with a query string → mem0 returns semantically relevant
  past memories for that student.

This module wraps the mem0 client and exposes two simple methods:
  - get(student_id, query)   → list of relevant memory strings
  - add(student_id, text)    → store a new memory

The mem0 client is configured to point at local Qdrant
(host/port from cfg.memory).
"""

from config import cfg


class PersistentMemory:
    def __init__(self):
        """
        Initialize mem0 client connected to local Qdrant.
        Collection: cfg.memory.memory_collection
        """
        # TODO: import mem0 and initialize:
        #   from mem0 import Memory
        #   self.client = Memory.from_config({
        #       "vector_store": {
        #           "provider": "qdrant",
        #           "config": {
        #               "host": cfg.memory.qdrant_host,
        #               "port": cfg.memory.qdrant_port,
        #               "collection_name": cfg.memory.memory_collection,
        #           }
        #       }
        #   })
        raise NotImplementedError

    def get(self, student_id: str, query: str = "") -> list[dict]:
        """
        Fetch relevant past memories for a student.

        Args:
            student_id: Unique student identifier.
            query:      Optional semantic search query (e.g. topic being studied).
                        If empty, returns all memories for this student.

        Returns:
            List of memory dicts: [{text: str, metadata: dict}]
        """
        # TODO: call self.client.search(query, user_id=student_id)
        # TODO: if query is empty, call self.client.get_all(user_id=student_id)
        raise NotImplementedError

    def add(self, student_id: str, memory_text: str) -> None:
        """
        Store a new memory for a student.

        Args:
            student_id:   Unique student identifier.
            memory_text:  Natural language description of what happened.
        """
        # TODO: call self.client.add(memory_text, user_id=student_id)
        raise NotImplementedError
