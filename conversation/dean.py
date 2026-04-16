"""
conversation/dean.py
---------------------
Dean agent — the supervisor of the tutoring session.

Dean's responsibilities:
  1. At session start: fetch student memory via get_student_memory tool.
  2. Each turn: decide whether to call search_textbook (only if no relevant context yet).
  3. Lock the correct answer on the FIRST specific student question (immutable after that).
     Do NOT lock at topic selection — only when student asks a specific question.
  4. Call check_student_answer on every student message.
  5. Pass retrieved_chunks + hint_level to Teacher (NEVER the locked_answer).
  6. Evaluate Teacher's draft using 3-level LeakGuard + quality checks:

     LeakGuard (runs in order, short-circuits on first hit):
       Level 1 — Exact match:   locked_answer.lower() in response.lower()           < 1ms
       Level 2 — Semantic match: cosine(embed(response), embed(locked_answer)) > 0.85  ~50ms
       Level 3 — Entailment:    "Could a student reading only this immediately state
                                  the locked answer?" — embedded in Dean's GPT-4o call,
                                  no extra API cost

     Quality checks:
       - No question in response?
       - Validates a wrong student claim (sycophancy)?

     Hard turn rule (absolute, hardcoded, not configurable):
       - if turn_count < 3: ALWAYS FAIL — no answer or definition in first 2 turns
         (course requirement: bot forbidden from direct answer in first two turns)

     → PASS: send Teacher's response to student
     → FAIL (retry < max_teacher_retries): send critique back to Teacher
     → FAIL (retry >= max_teacher_retries): Dean writes the student response directly

     Help abuse gating (all code — not prompt logic):
       - Dean LLM classifies student_state each turn (one of 5 labels)
       - If student_state == "low_effort": help_abuse_count += 1, else reset to 0
       - If low_effort AND help_abuse_count < help_abuse_threshold: block hint_level advance,
         pass student_state="low_effort" to Teacher (triggers meta-question behavior)
       - If help_abuse_count >= help_abuse_threshold: advance hint_level anyway, reset counter

  7. At session end: call update_student_memory with session summary.

Dean uses GPT-4o with ALL_TOOLS available (from tools/mcp_tools.py).
The full assembled prompt (static + dynamic sections) is logged to data/artifacts/session_prompts/.
"""

from conversation.state import TutorState
from config import cfg


class DeanAgent:
    def __init__(self, retriever, memory_client, embed_fn):
        """
        Args:
            retriever:      Retriever instance (retrieval/retriever.py)
            memory_client:  mem0 client (memory/persistent_memory.py)
            embed_fn:       Function that embeds a string -> np.ndarray
        """
        # TODO: initialize OpenAI client
        # TODO: store retriever, memory_client, embed_fn
        # TODO: call tools.mcp_tools.save_tool_definitions() on first init
        raise NotImplementedError

    def run_turn(self, state: TutorState) -> dict:
        """
        Main Dean logic for a single student turn.

        Called by the LangGraph 'dean_node'.
        Returns a partial state update dict.

        Flow:
          - search if needed
          - lock answer if not set
          - check student answer
          - get Teacher draft (via teacher.py)
          - evaluate draft
          - return response or critique
        """
        # TODO: implement full Dean turn logic
        raise NotImplementedError

    def get_full_prompt(self, state: TutorState) -> str:
        """
        Return the full assembled Dean prompt with delimiters for human inspection.
        Format:
            === STATIC ===
            <system prompt + retrieved chunks>
            === DYNAMIC ===
            <conversation history>
        """
        # TODO: assemble and return the full prompt string
        raise NotImplementedError

    def _log_prompt(self, conv_id: str, turn: int, prompt: str) -> None:
        """Save the full prompt to data/artifacts/session_prompts/{conv_id}_turn_{n}.txt"""
        # TODO: write to artifacts directory
        raise NotImplementedError

    def _log_intervention(self, conv_id: str, turn: int, critique: str, final_response: str) -> None:
        """Log Dean interventions to data/artifacts/dean_interventions/."""
        # TODO: append to {conv_id}_interventions.json
        raise NotImplementedError
