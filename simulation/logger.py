"""
simulation/logger.py
---------------------
Saves simulation conversations to data/simulations/ as JSONL.

Each conversation is one JSON line:
{
  "conv_id": "uuid",
  "student_profile": "S3",
  "topic": "Chapter 11 > 11.2 > Deltoid Innervation",
  "topic_difficulty": "moderate",
  "turns": [
    {"role": "tutor",   "content": "...", "phase": "tutoring", "turn": 1},
    {"role": "student", "content": "...", "turn": 2}
  ],
  "outcome": {
    "reached_answer": false,
    "turns_taken": 9,
    "hints_used": 3,
    "weak_topics_added": ["Chapter 11 > Deltoid Innervation"]
  },
  "dean_interventions": 2
}

Resume support: log_conversation() is a no-op if conv_id already exists in output.
Output path: cfg.paths.simulations / "conversations.jsonl"
"""

import json
from pathlib import Path
from config import cfg


def log_conversation(result: dict) -> None:
    """
    Append a completed conversation to the simulation output JSONL file.
    No-op if conv_id already exists (resume support).

    Args:
        result: Conversation dict matching the schema above.
                Must contain "conv_id" field.
    """
    out_path = Path(cfg.paths.simulations) / "conversations.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conv_id = result.get("conv_id", "")

    # Check for duplicate conv_id (resume support)
    if out_path.exists():
        existing_ids = set()
        for line in out_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    existing_ids.add(json.loads(line).get("conv_id", ""))
                except json.JSONDecodeError:
                    continue
        if conv_id in existing_ids:
            return  # already logged, skip

    with open(out_path, "a") as f:
        f.write(json.dumps(result) + "\n")


def load_conversations(path: str = None) -> list[dict]:
    """
    Load all logged conversations from JSONL for analysis or EULER scoring.

    Args:
        path: Override default path. Defaults to cfg.paths.simulations/conversations.jsonl.

    Returns:
        List of conversation dicts. Empty list if file doesn't exist.
    """
    load_path = Path(path) if path else Path(cfg.paths.simulations) / "conversations.jsonl"
    if not load_path.exists():
        return []
    return [
        json.loads(line)
        for line in load_path.read_text().splitlines()
        if line.strip()
    ]
