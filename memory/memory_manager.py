"""
memory/memory_manager.py
-------------------------
Orchestrates cross-session memory for the conversation graph.

What gets persisted (one mem0 string per category, per session):
  1. session_summary    — overview: topic, anchor question, target answer,
                          turns, reached/not, brief outcome
  2. misconceptions     — observed factual errors corrected during assessment
                          (zero or more per session)
  3. open_thread        — only if reached_answer=False; "resume from here"
                          marker for next session's rapport
  4. topics_covered     — chapter / section / subsection + status (mastered/
                          partial); used by topic-pivot to avoid re-teaching
  5. learning_style_cue — observable patterns from THIS session (terse vs
                          verbose, hedging frequency, hint usage); future
                          sessions can synthesize a consolidated style note

Each is stored as its own mem0 entry so semantic search at read time can
target one category. mem0 namespaces by (cfg.domain.mem0_namespace, student_id).

If mem0 / Qdrant is unavailable, all operations silently no-op (the
underlying PersistentMemory class is wrapped in try/except and degrades
to client=None on init failure) — the conversation graph never crashes
on a memory failure.
"""
from __future__ import annotations

from datetime import datetime

from conversation.state import TutorState


# Markers we expect to find in turn_trace entries when the dean / assessment
# detects student errors. Defensive — these may not be present in every
# session; absence just means no misconception strings get written.
_CORRECTION_TRACE_MARKERS = (
    "dean.assessment_diagnostic_correction",
    "dean.assessment_correction",
    "dean.factual_correction",
)

# Hedging markers — used to characterize learning style. Substring match,
# case-insensitive.
_HEDGE_MARKERS = (
    "i'm not sure", "im not sure", "not really sure",
    "i think", "i guess", "honestly", "maybe",
    "i'm not totally sure", "kind of", "sort of",
)


class MemoryManager:
    """Real implementation backed by `PersistentMemory` (mem0 + Qdrant).

    Falls back to no-op behavior if mem0 or Qdrant is unavailable, so the
    conversation graph never crashes on a memory failure — it just runs
    without persistence (matches the previous stubbed contract for callers).
    """

    def __init__(self):
        # Lazy import so test rigs that don't need persistence don't pay
        # the mem0 init cost.
        from memory.persistent_memory import PersistentMemory
        self.persistent = PersistentMemory()
        self.last_flush_status = "ready" if self.persistent.available else "stub_unavailable"

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------
    def load(self, student_id: str, query: str = "") -> list[dict]:
        """
        Fetch relevant past memories for a student.

        Args:
            student_id: unique student identifier.
            query:      optional semantic search query (e.g. topic being
                        studied, or "weak concepts"). Empty returns all.

        Returns:
            List of mem0 dicts (may be empty). Never raises.
        """
        if not self.persistent.available:
            return []
        return self.persistent.get(student_id, query)

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------
    def flush(
        self,
        student_id: str,
        state: TutorState,
        summary_text: str = "",
    ) -> bool:
        """Write 5 memory strings (session summary, misconceptions, open
        thread, topics covered, learning style cue) to mem0.

        Returns True if at least one write succeeded.
        """
        if not self.persistent.available:
            self.last_flush_status = "stub_unavailable"
            return False

        attempts = 0
        successes = 0

        for memory_text in self._build_memories(state, summary_text):
            attempts += 1
            if self.persistent.add(student_id, memory_text):
                successes += 1

        self.last_flush_status = f"wrote_{successes}_of_{attempts}"
        return successes > 0

    # ------------------------------------------------------------------
    # CLEAR (for clean test runs)
    # ------------------------------------------------------------------
    def clear_namespace(self) -> int:
        """Wipe ALL memories in the current namespace.

        Useful before a measurement run so prior memories don't pollute
        the baseline. Returns 1 if the underlying Qdrant collection was
        dropped, 0 if mem0 unavailable.

        Implementation note: drops the underlying Qdrant collection
        rather than enumerating mem0 entries — much faster, and mem0
        will recreate the collection on next add().
        """
        if not self.persistent.available or self.persistent.client is None:
            return 0
        try:
            from qdrant_client import QdrantClient
            from config import cfg
            mem_collection = getattr(
                getattr(cfg, "domain", object()), "memory_collection",
                cfg.memory.memory_collection,
            )
            qdrant = QdrantClient(
                host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port,
            )
            if qdrant.collection_exists(mem_collection):
                qdrant.delete_collection(collection_name=mem_collection)
            # Re-init PersistentMemory so its local mem0 client doesn't
            # reference the dropped collection on subsequent add() calls.
            from memory.persistent_memory import PersistentMemory
            self.persistent = PersistentMemory()
            return 1
        except Exception:
            return 0

    def forget(self, student_id: str) -> int:
        """Delete all mem0 entries for a single student.

        Wraps PersistentMemory.delete_user. Use this for the frontend
        "forget me" / privacy reset action — it does NOT touch other
        students' memories.

        Returns:
            Count of memories deleted, or -1 on failure / unavailability.
        """
        if not self.persistent.available:
            return -1
        return self.persistent.delete_user(student_id)

    # ------------------------------------------------------------------
    # Internal: memory string builders
    # ------------------------------------------------------------------
    def _build_memories(self, state: TutorState, summary_text: str) -> list[str]:
        """Return the list of memory strings to write for this session.
        Empty / None values are filtered out so we never write meaningless
        entries."""
        out: list[str] = []
        ts = datetime.now().strftime("%Y-%m-%d")

        sess = self._build_session_summary(state, summary_text, ts)
        if sess:
            out.append(sess)

        out.extend(self._build_misconceptions(state, ts))

        thread = self._build_open_thread(state, ts)
        if thread:
            out.append(thread)

        topics = self._build_topics_covered(state, ts)
        if topics:
            out.append(topics)

        style = self._build_learning_style(state, ts)
        if style:
            out.append(style)

        return out

    @staticmethod
    def _build_session_summary(state: TutorState, prior: str, ts: str) -> str | None:
        topic = state.get("topic_selection", "") or "(unknown topic)"
        locked_q = state.get("locked_question", "") or ""
        locked_a = state.get("locked_answer", "") or ""
        reached = bool(state.get("student_reached_answer"))
        turns = int(state.get("turn_count", 0) or 0)
        hint_final = int(state.get("hint_level", 0) or 0)
        outcome = "reached the target answer" if reached else "did not reach the target"
        body = (
            f"[Session summary {ts}] [{turns} turns, hint_level_final={hint_final}]\n"
            f"Topic: {topic}\n"
            f"Anchor question: {locked_q}\n"
            f"Target answer: {locked_a}\n"
            f"Outcome: student {outcome}."
        )
        if prior:
            body += f"\nNarrative: {prior.strip()}"
        return body

    @staticmethod
    def _build_misconceptions(state: TutorState, ts: str) -> list[str]:
        """Pull misconception entries from debug.turn_trace and
        debug.all_turn_traces. Format one mem0 string per detected
        correction event. Returns [] if none observed."""
        out: list[str] = []
        debug = state.get("debug") or {}

        def _scan_trace(trace_list):
            for entry in trace_list or []:
                if not isinstance(entry, dict):
                    continue
                wrapper = str(entry.get("wrapper", "") or "")
                if wrapper in _CORRECTION_TRACE_MARKERS:
                    correction = (
                        entry.get("correction")
                        or entry.get("explanation")
                        or entry.get("result")
                        or ""
                    )
                    if correction:
                        out.append(
                            f"[Misconception {ts}] {str(correction)[:300]}"
                        )

        _scan_trace(debug.get("turn_trace") or [])
        for archived in debug.get("all_turn_traces") or []:
            if isinstance(archived, dict):
                _scan_trace(archived.get("trace") or [])

        return out

    @staticmethod
    def _build_open_thread(state: TutorState, ts: str) -> str | None:
        if state.get("student_reached_answer"):
            return None
        topic = state.get("topic_selection", "") or ""
        locked_q = state.get("locked_question", "") or ""
        if not topic:
            return None
        turns = int(state.get("turn_count", 0) or 0)
        # Don't bother writing if the session never made progress.
        if turns == 0:
            return None
        return (
            f"[Open thread from session {ts}]\n"
            f"Student was working on: {topic}\n"
            f"Anchor question being explored: {locked_q}\n"
            f"Status: did not reach final answer in {turns} turns. "
            f"Resume this topic in next session."
        )

    @staticmethod
    def _build_topics_covered(state: TutorState, ts: str) -> str | None:
        topic_sel = state.get("topic_selection", "") or ""
        locked = state.get("locked_topic") or {}
        chapter = str(locked.get("chapter", "") or "")
        section = str(locked.get("section", "") or "")
        subsection = str(locked.get("subsection", "") or "")
        if not (topic_sel or chapter or section):
            return None
        reached = bool(state.get("student_reached_answer"))
        status = "mastered" if reached else "partial"
        return (
            f"[Topics covered {ts}]\n"
            f"Chapter: {chapter}\n"
            f"Section: {section}\n"
            f"Subsection: {subsection}\n"
            f"Status: {status} (use this to avoid re-teaching the same "
            f"subsection in future sessions; partial means resume rather "
            f"than skip)."
        )

    @staticmethod
    def _build_learning_style(state: TutorState, ts: str) -> str | None:
        msgs = state.get("messages") or []
        student_msgs = [m for m in msgs if m.get("role") == "student"]
        if not student_msgs:
            return None

        cues: list[str] = []

        # Hint reliance
        hint_final = int(state.get("hint_level", 0) or 0)
        reached = bool(state.get("student_reached_answer"))
        if reached and hint_final == 0:
            cues.append("reached the answer with no hints — strong independent reasoning")
        elif hint_final >= 3:
            cues.append("needed multiple hints to converge — benefits from progressive scaffolding")

        # Hedging frequency
        hedge_count = 0
        for m in student_msgs:
            content = (m.get("content", "") or "").lower()
            if any(h in content for h in _HEDGE_MARKERS):
                hedge_count += 1
        hedge_ratio = hedge_count / max(len(student_msgs), 1)
        if hedge_ratio >= 0.5:
            cues.append(
                "hedges responses frequently with 'I think... maybe...' patterns; "
                "benefits from validation before pushing forward"
            )

        # Verbosity
        word_counts = [len((m.get("content", "") or "").split()) for m in student_msgs]
        avg_words = sum(word_counts) / max(len(word_counts), 1)
        if avg_words <= 5:
            cues.append(
                "very terse responses (1-5 words typical) — likely benefits from "
                "embodied or analogy-based prompting to elicit longer reasoning"
            )
        elif avg_words >= 30:
            cues.append("verbose detailed reasoning — strong engagement")

        if not cues:
            return None
        bullets = "\n".join(f"- {c}" for c in cues)
        return f"[Learning style cue {ts}]\n{bullets}"
