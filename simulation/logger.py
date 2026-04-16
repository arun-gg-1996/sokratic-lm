"""
simulation/logger.py
---------------------
Saves simulation conversations to data/simulations/ as JSONL.

Each conversation is one line in the output file:
{
  "conv_id": "uuid",
  "student_profile": "S3",
  "topic": "Chapter 11 > 11.2 > Deltoid Innervation",
  "topic_difficulty": "moderate",
  "turns": [
    {"role": "tutor", "content": "...", "phase": "tutoring", "turn": 1},
    {"role": "student", "content": "...", "turn": 2}
  ],
  "outcome": {
    "reached_answer": false,
    "turns_taken": 9,
    "hints_used": 3,
    "weak_topics_added": ["Chapter 11 > Deltoid Innervation"],
    "euler_scores": {"question": 0.9, "relevance": 0.85, "helpful": 0.7, "no_reveal": 1.0}
  },
  "dean_interventions": 2
}

Resume support: log_conversation() is a no-op if conv_id already exists in output.
"""

import json
import uuid
from pathlib import Path
from config import cfg


def log_conversation(result: dict) -> None:
    """
    Append a completed conversation to the simulation output JSONL file.
    Skips if conv_id already logged (resume support).

    Args:
        result: Full conversation dict as described above.
    """
    out_path = Path(cfg.paths.simulations) / "conversations.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # TODO: check if conv_id already in file (resume support)
    # TODO: append result as JSON line
    raise NotImplementedError


def load_conversations(path: str = None) -> list[dict]:
    """Load all logged conversations from JSONL for analysis."""
    # TODO: read and parse JSONL
    raise NotImplementedError
