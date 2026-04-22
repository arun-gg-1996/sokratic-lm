"""
conversation/state.py
---------------------
Defines TutorState — the single shared object that flows through every
node in the LangGraph graph.

Every field is described inline. Nodes read from state and return
partial updates (LangGraph merges them automatically).

One topic per session. A new session (new thread_id) handles the next topic.
Memory continuity across sessions is handled by mem0 (persistent_memory.py).

Use initial_state() to create a properly initialized state at session start.
"""

from typing import TypedDict, Literal, Optional


class TutorState(TypedDict):
    # --- Identity ---
    student_id: str

    # --- Phase ---
    phase: Literal["rapport", "tutoring", "assessment", "memory_update"]

    # --- Conversation history ---
    # Full list of messages. Oldest turns are replaced by a summary paragraph
    # once turn_count >= max_turns - summarizer_keep_recent.
    messages: list[dict]            # {"role": "tutor"|"student", "content": str}

    # --- Retrieval ---
    # Top-5 chunks returned by the last retriever call.
    # Each dict: {text, chapter_title, section_title, subsection_title, page, score}
    retrieved_chunks: list[dict]

    # --- Question/answer anchors ---
    # Both are set once by Dean immediately after topic lock.
    # Immutable for the remainder of the session.
    # locked_question is shown to Teacher to keep Socratic guidance on target.
    # locked_answer is not shown to Teacher during tutoring.
    locked_question: str
    # Extracted by Dean from retrieved propositions/chunks after topic lock.
    # Never shown to Teacher (except in draft_clinical during assessment phase).
    # Empty string means not yet locked.
    locked_answer: str

    # --- Hint tracking ---
    hint_level: int                 # current level, starts at 0 (pre-lock)
    max_hints: int                  # from cfg.session.max_hints

    # --- Turn tracking ---
    turn_count: int
    max_turns: int                  # from cfg.session.max_turns

    # --- Progress ---
    student_reached_answer: bool
    # Confidence attached to the latest student answer classification (0.0 - 1.0).
    student_answer_confidence: float
    # Running mean confidence over session turns (0.0 - 1.0).
    student_mastery_confidence: float
    # Number of turns that contributed to running confidence.
    confidence_samples: int

    # --- Assessment flow ---
    # Controls assessment sequence:
    #   0 = not started
    #   1 = asked whether student wants optional clinical question; waiting for yes/no
    #   2 = clinical question sent; waiting for student response
    #   3 = done (mastery summary or reveal sent; ready for memory_update)
    # after_assessment checks this to decide whether to wait (END) or route to memory_update_node.
    assessment_turn: int
    clinical_opt_in: Optional[bool]
    clinical_turn_count: int
    clinical_max_turns: int
    clinical_completed: bool
    clinical_state: Optional[Literal["correct", "partial_correct", "incorrect"]]
    clinical_confidence: float
    clinical_history: list[dict]   # [{turn, state, confidence, pass}]

    # --- Mastery grading ---
    # Tiered outcome based on both core tutoring and clinical application.
    # Valid values:
    #   strong | proficient | developing | needs_review | not_assessed
    core_mastery_tier: str
    clinical_mastery_tier: str
    mastery_tier: str
    grading_rationale: str
    session_memory_summary: str

    # --- Memory ---
    # Seeded from mem0 at session start. Used during rapport to suggest topic.
    weak_topics: list[dict]         # [{topic: str, difficulty: str, failure_count: int}]

    # --- Dean retry counter ---
    # Resets to 0 on each new student turn.
    # If it reaches cfg.dean.max_teacher_retries, Dean responds directly.
    dean_retry_count: int

    # --- Dean's critique for Teacher retry ---
    # Set by Dean when it rejects a Teacher response. Empty string on first attempt.
    dean_critique: str

    # --- Student state ---
    # Dean classifies the student's last message each turn using submit_turn_evaluation.
    # Passed to Teacher to determine response behavior.
    # None until first student message is classified.
    student_state: Optional[Literal["correct", "partial_correct", "incorrect", "question", "irrelevant", "low_effort"]]

    # --- Topic selection ---
    # False until the student has narrowed their broad topic to a specific concept.
    # Dean first presents 3-4 scoped options and requires an explicit selection
    # (option number or option text) before Socratic tutoring begins.
    topic_confirmed: bool
    # Current scoped options presented to the student (3-4 options).
    topic_options: list[str]
    # Last scoping question shown to the student.
    topic_question: str
    # Final selected scoped topic used for retrieval/tutoring.
    topic_selection: str
    # UI deterministic-choice helper for card/button flows.
    pending_user_choice: dict

    # --- Help abuse tracking ---
    # Counts consecutive low-effort student turns.
    # Resets to 0 when student makes a real attempt (any non-low_effort state).
    # When it reaches cfg.dean.help_abuse_threshold, hint_level advances anyway and counter resets.
    # Gated entirely in Python (dean.py) — not LLM logic.
    help_abuse_count: int

    # --- Multimodal ---
    is_multimodal: bool
    image_structures: list[str]     # structure names from Vision model

    # --- Debug tracking (per-session, shown in Streamlit debug panel) ---
    # Updated after every Anthropic API call and every tool call.
    # turn_trace resets to [] at the start of each new student turn in dean_node.
    debug: dict
    # Schema:
    # {
    #   "api_calls": int,           # total Anthropic API calls this session
    #   "input_tokens": int,        # cumulative input tokens
    #   "output_tokens": int,       # cumulative output tokens
    #   "cost_usd": float,          # cumulative estimated API cost (incl. cache pricing)
    #   "interventions": int,       # times Dean fallback was used instead of Teacher
    #   "retrieval_calls": int,     # retrieval fire count (must be 1 per session after topic lock)
    #   "current_node": str,        # last LangGraph node that ran
    #   "last_routing": str,        # what after_dean returned + reason
    #   "turn_trace": list[dict],   # current student turn list of wrappers called + outcomes
    #   "all_turn_traces": list[dict]  # full session history of per-turn traces
    # }
    # turn_trace entry format:
    # {"wrapper": "dean._setup_call", "tool_called": "search_textbook", "result": "5 chunks returned"}
    # {"wrapper": "teacher.draft_socratic", "tool_called": None, "result": "drafted"}
    # {"wrapper": "dean._quality_check_call", "tool_called": None, "result": "PASS"}


def initial_state(student_id: str, cfg) -> TutorState:
    """
    Return a fully initialized TutorState for a new session.
    Call this at the start of every session before invoking the graph.

    Args:
        student_id: Unique identifier for the student.
        cfg:        Loaded config object (from config.py).
    """
    return TutorState(
        student_id=student_id,
        phase="rapport",
        messages=[],
        retrieved_chunks=[],
        locked_question="",
        locked_answer="",
        hint_level=0,
        max_hints=cfg.session.max_hints,
        turn_count=0,
        max_turns=cfg.session.max_turns,
        student_reached_answer=False,
        student_answer_confidence=0.0,
        student_mastery_confidence=0.0,
        confidence_samples=0,
        assessment_turn=0,
        clinical_opt_in=None,
        clinical_turn_count=0,
        clinical_max_turns=3,
        clinical_completed=False,
        clinical_state=None,
        clinical_confidence=0.0,
        clinical_history=[],
        core_mastery_tier="not_assessed",
        clinical_mastery_tier="not_assessed",
        mastery_tier="not_assessed",
        grading_rationale="",
        session_memory_summary="",
        weak_topics=[],
        dean_retry_count=0,
        dean_critique="",
        student_state=None,
        topic_confirmed=False,
        topic_options=[],
        topic_question="",
        topic_selection="",
        pending_user_choice={},
        help_abuse_count=0,
        is_multimodal=False,
        image_structures=[],
        debug={
            "api_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "interventions": 0,
            "retrieval_calls": 0,
            "current_node": "",
            "last_routing": "",
            "turn_trace": [],
            "all_turn_traces": [],
            "hint_progress": [],
            "hint_plan": [],
        },
    )
