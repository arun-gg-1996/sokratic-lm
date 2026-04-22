"""
tools/mcp_tools.py
------------------
Tool definitions and implementations available to the Dean agent.
Teacher has NO tools — Teacher only receives what Dean passes in the prompt.

Tools:
  - search_textbook(query, retriever)
  - get_student_memory(student_id, memory_client)
  - update_student_memory(student_id, memory_text, memory_client)
  - submit_turn_evaluation  (schema only — Dean calls this via Anthropic tool_use
                             to return structured JSON each turn)

Removed from earlier design:
  - check_student_answer: Dean LLM now reasons about correctness directly
  - flag_answer_leak:     LeakGuard Level 3 (entailment) is embedded in
                          dean._quality_check_call prompt — no separate tool needed

Each schema is defined in Anthropic tool-calling format (not OpenAI format).
The full schema is saved to data/artifacts/tool_definitions.json for human inspection.

Dean receives DEAN_TOOLS in the `tools` array during _setup_call.
Claude decides when to call each tool based on the description and the
instructions in the Dean system prompt.
"""

import json
from pathlib import Path
from config import cfg


# --- Tool schemas (Anthropic tool-calling format) ---

SEARCH_TEXTBOOK = {
    "name": "search_textbook",
    "description": (
        "Search the textbook knowledge base and return the top relevant passages. "
        "Only call this if you do not already have relevant context for the current student message."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The student's question or the topic to search for."
            }
        },
        "required": ["query"]
    }
}

GET_STUDENT_MEMORY = {
    "name": "get_student_memory",
    "description": (
        "Fetch this student's past session history from memory: "
        "mastered topics, weak topics with failure counts, and session summaries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "student_id": {
                "type": "string",
                "description": "The unique identifier for the student."
            }
        },
        "required": ["student_id"]
    }
}

UPDATE_STUDENT_MEMORY = {
    "name": "update_student_memory",
    "description": (
        "Write the outcome of this session to the student's memory. "
        "Call this once at the end of the session. "
        "Include what was covered, what was mastered, and what the student struggled with."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "student_id": {
                "type": "string",
                "description": "The unique identifier for the student."
            },
            "memory_text": {
                "type": "string",
                "description": (
                    "Natural language summary of the session outcome. "
                    "Example: 'Session 3. Covered deltoid and rotator cuff. "
                    "Mastered: deltoid origin/insertion. "
                    "Struggled with: axillary nerve innervation (failed 3 times).'"
                )
            }
        },
        "required": ["student_id", "memory_text"]
    }
}

SUBMIT_TURN_EVALUATION = {
    "name": "submit_turn_evaluation",
    "description": (
        "Submit your structured evaluation of this tutoring turn. "
        "Call this tool after reviewing the student's message and retrieving any needed context. "
        "This is the required way to return your assessment — do not write it as free text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "student_state": {
                "type": "string",
                "enum": ["correct", "partial_correct", "incorrect", "question", "irrelevant", "low_effort"],
                "description": "Classification of the student's last message."
            },
            "student_reached_answer": {
                "type": "boolean",
                "description": "True only when student_state is 'correct' and the student identified the locked answer."
            },
            "hint_level": {
                "type": "integer",
                "description": "The hint level that should apply for Teacher's next response (1-3)."
            },
            "locked_answer": {
                "type": "string",
                "description": (
                    "The correct answer for this question. Set this on first extraction and "
                    "return the same value every subsequent turn. Never change it once set."
                )
            },
            "search_needed": {
                "type": "boolean",
                "description": "Whether you called search_textbook this turn."
            },
            "critique": {
                "type": "string",
                "description": (
                    "Empty string on most turns. "
                    "If this evaluation is in response to a Teacher draft that failed quality check, "
                    "provide a short critique so Teacher can improve the retry."
                )
            }
        },
        "required": [
            "student_state",
            "student_reached_answer",
            "hint_level",
            "locked_answer",
            "search_needed",
            "critique"
        ]
    }
}


# Tools given to Dean during _setup_call.
# Note: get_student_memory is handled in rapport_node via memory_manager.load(),
# so we avoid exposing it in every tutoring turn to keep setup fast.
DEAN_TOOLS = [SEARCH_TEXTBOOK, SUBMIT_TURN_EVALUATION]

# All tools (for artifact saving)
ALL_TOOLS = [SEARCH_TEXTBOOK, GET_STUDENT_MEMORY, UPDATE_STUDENT_MEMORY, SUBMIT_TURN_EVALUATION]


def save_tool_definitions():
    """Save all tool schemas to data/artifacts/tool_definitions.json for human review."""
    out = Path(cfg.paths.artifacts) / "tool_definitions.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(ALL_TOOLS, f, indent=2)
    print(f"Tool definitions saved to {out}")


# --- Tool implementations (called by dean.py when Claude makes a tool call) ---

def search_textbook(query: str, retriever, top_k: int | None = None) -> list[dict]:
    """
    Run hybrid retrieval and return top chunks.

    Args:
        query:     Student's question or topic string.
        retriever: Retriever or MockRetriever instance.
        top_k:     Optional override of the default top_chunks_final.
                   Lock-anchors and hint-plan benefit from wider recall
                   (~12); per-turn Teacher drafts can stick with default.

    Returns:
        List of chunk dicts with text, metadata, and score.
    """
    if top_k is not None:
        try:
            return retriever.retrieve(query, top_k=int(top_k))
        except TypeError:
            # MockRetriever or older signature without top_k — fallback.
            pass
    return retriever.retrieve(query)


def get_student_memory(student_id: str, memory_client) -> list[dict]:
    """
    Fetch relevant past memories for this student from mem0.

    Args:
        student_id:    Unique student identifier.
        memory_client: PersistentMemory instance.

    Returns:
        List of memory dicts (may be empty if no history or Qdrant unavailable).
    """
    return memory_client.get(student_id)


def update_student_memory(student_id: str, memory_text: str, memory_client) -> None:
    """
    Write session outcome to mem0.

    Args:
        student_id:    Unique student identifier.
        memory_text:   Natural language session summary.
        memory_client: PersistentMemory instance.
    """
    memory_client.add(student_id, memory_text)
