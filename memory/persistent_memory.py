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
  - get(student_id, query)   → list of relevant memory dicts
  - add(student_id, text)    → store a new memory

All methods are wrapped in try/except — if Qdrant is not running, the session
continues normally with empty memory rather than crashing.
"""

from config import cfg


class PersistentMemory:
    def __init__(self):
        """
        Initialize mem0 client connected to local Qdrant.
        Collection: cfg.memory.memory_collection

        If Qdrant is unavailable, self.client is set to None and all
        operations silently return empty results.
        """
        try:
            from mem0 import Memory
            vector_size = int(getattr(getattr(cfg, "qdrant", object()), "vector_size", 3072))
            self.namespace = getattr(getattr(cfg, "domain", object()), "mem0_namespace", "default")
            mem_collection = getattr(getattr(cfg, "domain", object()), "memory_collection", cfg.memory.memory_collection)
            # mem0 defaults to text-embedding-3-small (1536 dim). We use
            # text-embedding-3-large (3072 dim) everywhere else and the
            # sokratic_memory Qdrant collection is sized at 3072. Configure
            # mem0's embedder explicitly so the dimensions match (otherwise
            # Qdrant rejects every add() with: "Vector dimension error:
            # expected dim: 3072, got 1536").
            config = {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": cfg.memory.qdrant_host,
                        "port": cfg.memory.qdrant_port,
                        "collection_name": mem_collection,
                        "embedding_model_dims": vector_size,
                    }
                },
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": cfg.models.embeddings,  # text-embedding-3-large
                        "embedding_dims": vector_size,
                    },
                },
            }
            self.client = Memory.from_config(config)
            self.available = True
            self.unavailable_reason = ""
        except Exception:
            self.client = None
            self.available = False
            self.unavailable_reason = "qdrant_or_mem0_unavailable"
            self.namespace = "default"

    def _namespaced_user_id(self, student_id: str) -> str:
        return f"{self.namespace}:{student_id}"

    def get(self, student_id: str, query: str = "") -> list[dict]:
        """
        Fetch relevant past memories for a student.

        Args:
            student_id: Unique student identifier.
            query:      Optional semantic search query (e.g. topic being studied).
                        If empty, returns all memories for this student.

        Returns:
            List of memory dicts from mem0 (may be empty).
            On any error (Qdrant down, no history) returns [].
        """
        if self.client is None:
            return []
        try:
            user_id = self._namespaced_user_id(student_id)
            if query:
                resp = self.client.search(query, user_id=user_id)
            else:
                resp = self.client.get_all(user_id=user_id)
            # mem0's response shape varies: sometimes a list of dicts,
            # sometimes {'results': [list of dicts]} on newer versions.
            # Normalize to always return a flat list of memory dicts so
            # callers don't need to introspect.
            if isinstance(resp, dict) and "results" in resp:
                return list(resp.get("results") or [])
            if isinstance(resp, list):
                return resp
            return []
        except Exception:
            return []

    def add(self, student_id: str, memory_text: str) -> bool:
        """
        Store a new memory for a student.

        Args:
            student_id:   Unique student identifier.
            memory_text:  Natural language description of what happened.

        Non-fatal: if Qdrant is down, this is silently skipped.
        Session has already ended by this point — no user impact.
        """
        if self.client is None:
            return False
        try:
            self.client.add(memory_text, user_id=self._namespaced_user_id(student_id))
            return True
        except Exception:
            return False

    def delete_user(self, student_id: str) -> int:
        """
        Delete all memories for a single student.

        Args:
            student_id: Unique student identifier.

        Returns:
            Number of memories deleted, or -1 if mem0/Qdrant is unavailable
            or the operation failed. Returns 0 if the student had no memories.

        Why per-user delete (not clear_namespace)
        ----------------------------------------
        clear_namespace() drops the entire 'sokratic_memory' Qdrant
        collection — wiping every student's data. That's correct for
        --clear-memory in eval scripts but catastrophic to expose to
        end users via a UI.

        This method only deletes the calling user's mem0 entries:
          - Privacy / forget-me operations from the frontend
          - Per-student reset for demos without affecting other users
        """
        if self.client is None:
            return -1
        try:
            user_id = self._namespaced_user_id(student_id)
            # Snapshot count before delete so we can report something useful
            existing = self.get(student_id)
            n_before = len(existing) if existing else 0
            # mem0 exposes delete_all(user_id=...) which removes every memory
            # filed under that namespaced user_id. Verified in mem0 0.1.x.
            self.client.delete_all(user_id=user_id)
            return n_before
        except Exception:
            return -1
