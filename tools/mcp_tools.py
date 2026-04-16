"""
tools/mcp_tools.py
------------------
MCP tool definitions available to the Dean agent.
Teacher has NO tools — Teacher only receives what Dean passes in the prompt.

Tools:
  - search_textbook(query)
  - check_student_answer(student_claim, locked_answer)
  - flag_answer_leak(response_text, locked_answer)
  - get_student_memory(student_id)
  - update_student_memory(student_id, memory_text)

Each tool is defined as a dict matching the OpenAI tool-calling schema.
The full schema is also saved to data/artifacts/tool_definitions.json
for human inspection.

Dean receives all tools in the `tools` array when initialized.
GPT-4o decides when to call each tool based on the description and the
instructions in the Dean system prompt.
"""

import json
from pathlib import Path
from config import cfg


# --- Tool schemas (OpenAI function-calling format) ---

SEARCH_TEXTBOOK = {
    "type": "function",
    "function": {
        "name": "search_textbook",
        "description": (
            "Search the textbook knowledge base and return the top relevant passages. "
            "Only call this if you do not already have relevant context for the current student message."
        ),
        "parameters": {
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
}

CHECK_STUDENT_ANSWER = {
    "type": "function",
    "function": {
        "name": "check_student_answer",
        "description": (
            "Compare the student's claim to the locked correct answer using semantic similarity. "
            "Returns whether the student is correct and a similarity score."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "student_claim": {
                    "type": "string",
                    "description": "What the student said or claimed."
                },
                "locked_answer": {
                    "type": "string",
                    "description": "The correct answer that was locked at the start of this question."
                }
            },
            "required": ["student_claim", "locked_answer"]
        }
    }
}

FLAG_ANSWER_LEAK = {
    "type": "function",
    "function": {
        "name": "flag_answer_leak",
        "description": (
            "Check whether the Teacher's drafted response contains the correct answer "
            "or a clear synonym of it. Returns leaked=True if found."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "response_text": {
                    "type": "string",
                    "description": "The Teacher's drafted response to check."
                },
                "locked_answer": {
                    "type": "string",
                    "description": "The correct answer term to look for."
                }
            },
            "required": ["response_text", "locked_answer"]
        }
    }
}

GET_STUDENT_MEMORY = {
    "type": "function",
    "function": {
        "name": "get_student_memory",
        "description": (
            "Fetch this student's past session history from memory: "
            "mastered topics, weak topics with failure counts, and session summaries."
        ),
        "parameters": {
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
}

UPDATE_STUDENT_MEMORY = {
    "type": "function",
    "function": {
        "name": "update_student_memory",
        "description": (
            "Write the outcome of this session to the student's memory. "
            "Call this once at the end of the session. "
            "Include what was covered, what was mastered, and what the student struggled with."
        ),
        "parameters": {
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
}


ALL_TOOLS = [
    SEARCH_TEXTBOOK,
    CHECK_STUDENT_ANSWER,
    # FLAG_ANSWER_LEAK is intentionally excluded.
    # LeakGuard levels 1 (exact match) and 2 (semantic similarity) run automatically
    # in Python in dean.py before Dean's LLM call. Level 3 (entailment) is embedded
    # in Dean's GPT-4o evaluation prompt. Dean never calls this as a tool.
    GET_STUDENT_MEMORY,
    UPDATE_STUDENT_MEMORY,
]


def save_tool_definitions():
    """Save all tool schemas to data/artifacts/tool_definitions.json for human review."""
    out = Path(cfg.paths.artifacts) / "tool_definitions.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(ALL_TOOLS, f, indent=2)
    print(f"Tool definitions saved to {out}")


# --- Tool implementations (called by the graph when Dean makes a tool call) ---

def search_textbook(query: str, retriever) -> list[dict]:
    """Run hybrid retrieval and return top chunks."""
    # TODO: call retriever.retrieve(query)
    raise NotImplementedError


def check_student_answer(student_claim: str, locked_answer: str, embed_fn) -> dict:
    """
    Compare student_claim to locked_answer using embedding cosine similarity.
    Returns {"correct": bool, "similarity": float}.
    Threshold: cfg.thresholds.student_answer_correct
    """
    # TODO: embed both strings, compute cosine similarity
    # TODO: return {"correct": similarity >= cfg.thresholds.student_answer_correct, "similarity": similarity}
    raise NotImplementedError


def flag_answer_leak(response_text: str, locked_answer: str, embed_fn) -> dict:
    """
    LeakGuard levels 1 and 2 — called directly by dean.py, NOT via LLM tool call.

    Level 1 — exact match (< 1ms):
        locked_answer.lower() in response_text.lower()
    Level 2 — semantic match (~50ms):
        cosine(embed(response_text), embed(locked_answer)) > cfg.thresholds.answer_leak_semantic

    Returns {"leaked": bool, "level": 1 | 2 | None}
    """
    # TODO: level 1 — exact string match
    # TODO: level 2 — embedding cosine similarity >= cfg.thresholds.answer_leak_semantic
    raise NotImplementedError


def get_student_memory(student_id: str, memory_client) -> list[dict]:
    """Fetch relevant past memories for this student from mem0."""
    # TODO: call memory_client.search(student_id)
    raise NotImplementedError


def update_student_memory(student_id: str, memory_text: str, memory_client) -> None:
    """Write session outcome to mem0."""
    # TODO: call memory_client.add(memory_text, user_id=student_id)
    raise NotImplementedError
