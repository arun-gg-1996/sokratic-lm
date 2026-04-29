from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from threading import RLock
from typing import Any

from conversation.dean import DeanAgent
from conversation.graph import build_graph
from memory.memory_manager import MemoryManager
from retrieval.retriever import Retriever


@dataclass
class SessionRuntime:
    """In-memory session state cache keyed by thread_id."""

    _states: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def get(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._states.get(thread_id)
            return dict(value) if isinstance(value, dict) else None

    def set(self, thread_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._states[thread_id] = dict(state)


@lru_cache(maxsize=1)
def get_memory_manager() -> MemoryManager:
    return MemoryManager()


@lru_cache(maxsize=1)
def get_retriever():
    """
    Use the single production retriever path only.
    """
    return Retriever()


@lru_cache(maxsize=1)
def get_graph():
    retriever = get_retriever()
    memory_manager = get_memory_manager()
    return build_graph(retriever, memory_manager)


@lru_cache(maxsize=1)
def get_dean() -> DeanAgent:
    """Direct DeanAgent reference. The graph holds one internally but
    doesn't expose it; some endpoints (notably the Revisit pre-lock in
    session.py) need to call dean.* methods imperatively before the
    graph runs. Reuses the same retriever + memory_client singletons
    so dean state stays consistent across the two construction sites."""
    retriever = get_retriever()
    memory_client = get_memory_manager().persistent
    return DeanAgent(retriever, memory_client)


@lru_cache(maxsize=1)
def get_runtime_store() -> SessionRuntime:
    return SessionRuntime()
