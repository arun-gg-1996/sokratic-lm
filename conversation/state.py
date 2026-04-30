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
    # ~5 alternate phrasings of locked_answer produced at lock time.
    # Used by reached_answer_gate's Step A token-overlap check so a
    # student saying "calf muscle pump" gets credit for "skeletal muscle
    # pump". Never shown to Teacher. Empty list means not yet locked
    # (or generation failed — gate falls back to LLM paraphrase check).
    locked_answer_aliases: list[str]
    # Two-tier anchor design (2026-04-30, post-18-convo eval review):
    # `locked_answer` is the SHORT concept anchor (1-5 words, used by
    # the reached_answer_gate token-overlap check). `full_answer` is the
    # COMPLETE textbook answer — may be a list, may be a sentence — used
    # by the mastery scorer + clinical assessment + end-of-session
    # summary. Decoupling them solves the "what are the parts of a
    # nephron?" problem where the gate needs a short anchor but the
    # genuine answer is a 4-component list. When `full_answer` is empty,
    # callers fall back to `locked_answer` for backward compatibility.
    full_answer: str

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
    # The TOC entry this session is grounded to (set at topic-lock time by the
    # TopicMatcher). None when the session is not grounded to a TOC node —
    # grading should treat such sessions as `ungraded`. Schema:
    # {path, chapter, section, subsection, difficulty, chunk_count, limited, score}
    locked_topic: Optional[dict]
    # True for exactly one dean_node call: the turn when locked_topic +
    # locked_question + locked_answer all become present for the first time.
    # Consumed by dean_node's "topic acknowledgement" branch which emits a
    # deterministic message announcing the topic and stating the locked
    # question verbatim, BEFORE any Socratic hints fire. Set to False at
    # turn-start in dean_node (or by the prelock helper). Without this
    # flag, students never saw the actual question — only paraphrased
    # hint #1 — which made the session feel directionless from turn one.
    topic_just_locked: bool
    # UI deterministic-choice helper for card/button flows.
    pending_user_choice: dict

    # --- Help abuse tracking ---
    # Counts consecutive low-effort student turns.
    # Resets to 0 when student makes a real attempt (any non-low_effort state).
    # When it reaches cfg.dean.help_abuse_threshold, hint_level advances and counter resets.
    # Gated entirely in Python (dean.py) — not LLM logic.
    help_abuse_count: int
    # --- Off-topic tracking (Change 4, 2026-04-30) ---
    # Counts consecutive off-DOMAIN turns (category C — outside the textbook
    # subject entirely; e.g. vaping, profanity, sexual content). Domain-
    # tangential questions (category B — in-domain but off the locked
    # topic) do NOT increment this counter; they're handled by
    # exploration_judge / exploration_retrieval.
    # Resets on ANY engaged turn. At cfg.dean.off_topic_threshold (default 4),
    # the dean terminates the WHOLE session (sets core/clinical mastery_tier
    # to "not_assessed", routes to memory_update with farewell narration).
    off_topic_count: int
    # --- Telemetry counters (non-resetting; read by mastery_scorer) ---
    # Track session-wide patterns even when consecutive strikes don't
    # accumulate (e.g. a student who interleaves stonewalls with genuine
    # attempts). Mastery scorer's rationale references these to penalize
    # session-wide stonewalling/drift even when no individual chain hit
    # threshold.
    total_low_effort_turns: int
    total_off_topic_turns: int
    # --- Clinical phase counters (Change 5.1, 2026-04-30) ---
    # Mirror help_abuse_count / off_topic_count but for the clinical
    # (assessment) phase. At cfg.dean.clinical_strike_threshold (default 2),
    # the clinical phase ENDS (clinical_mastery_tier=not_assessed) but the
    # session continues to memory_update — student keeps tutoring credit.
    clinical_low_effort_count: int
    clinical_off_topic_count: int

    # --- Topic-lock rejection tracking ---
    # TOC paths that failed the coverage gate in this session. Used by
    # sample_diverse to avoid re-suggesting topics we've already proven we
    # can't teach, which caused the card-loop bug in the 2026-04-22 session.
    rejected_topic_paths: list[str]

    # --- Exploration budget ---
    # Students occasionally ask about concepts outside the locked topic.
    # When Dean detects a tangential question and budget remains, we fire one
    # un-section-filtered retrieval and attach those chunks as exploration
    # context for Teacher's next draft. Capped per session to prevent drift.
    exploration_max: int
    exploration_used: int

    # --- Memory toggle (frontend-controlled) ---
    # When False, rapport_node skips reading mem0 and the session opens as
    # a fresh greeting regardless of any prior history. Default True. The
    # write side (memory_update_node) is NOT gated by this flag — sessions
    # always persist their summary so a future re-enable still has data.
    # Set per-session via the StartSessionRequest payload from the UI.
    memory_enabled: bool

    # --- Client-local hour (D.6b-5) ---
    # 0-23 from the frontend's `new Date().getHours()`, so the rapport
    # greeting picks morning/afternoon/evening from the user's clock
    # rather than the server's tz. None means the request didn't supply
    # it (legacy callers / curl tests) — server-time falls through.
    client_hour: Optional[int]

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
        locked_answer_aliases=[],
        full_answer="",
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
        locked_topic=None,
        topic_just_locked=False,
        pending_user_choice={},
        help_abuse_count=0,
        off_topic_count=0,
        total_low_effort_turns=0,
        total_off_topic_turns=0,
        clinical_low_effort_count=0,
        clinical_off_topic_count=0,
        rejected_topic_paths=[],
        exploration_max=int(getattr(getattr(cfg, "session", object()), "exploration_max", 3)),
        exploration_used=0,
        memory_enabled=True,
        client_hour=None,
        is_multimodal=False,
        image_structures=[],
        debug={
            "api_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "interventions": 0,
            "retrieval_calls": 0,
            "coverage_gap_events": 0,
            "grounded_turns": 0,
            "ungrounded_turns": 0,
            "invariant_violations": [],
            "current_node": "",
            "last_routing": "",
            "turn_trace": [],
            "all_turn_traces": [],
            "hint_progress": [],
            "hint_plan": [],
        },
    )
