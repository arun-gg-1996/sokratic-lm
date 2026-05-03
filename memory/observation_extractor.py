"""
memory/observation_extractor.py
───────────────────────────────
L4 implementation — single Haiku call at session end extracts misconception
+ learning_style observations as concrete self-contained sentences. Replaces
the previous 5-method heuristic stack (_build_misconceptions counted trace
markers; _build_learning_style counted hedge words / word counts).

Per docs/AUDIT_2026-05-02.md L4:

  Drop the existing 5 _build_* methods in memory/memory_manager.py.
  Replace with one Haiku call at session end that extracts misconception(s)
  + learning_style observations as concrete sentences, then writes each as
  a separate mem0 entry with full metadata.

  Write style: one concrete self-contained sentence per claim. Topic name
  baked into every claim so mem0's atomization can't strip context.

Categories produced (mem0 only carries these two per L1):
  * misconception   — observed factual errors / confusions during the session
  * learning_style  — observable interaction patterns (hedging, terseness,
                      hint-reliance, exploration tendency, etc.)

What's NOT produced here (now in SQL per L1):
  * session_summary, open_thread, topics_covered

The function returns a list of (text, category) tuples. The caller wraps
each with metadata (subsection_path / section_path / session_at / thread_id)
and writes via safe_mem0_write.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# Categories per L1 (after the SQL split).
ALLOWED_CATEGORIES = {"misconception", "learning_style"}


@dataclass
class Observation:
    text: str          # the concrete self-contained sentence to write
    category: str      # one of ALLOWED_CATEGORIES
    evidence: str = "" # optional excerpt from the transcript (debug only)


# Single-shot Haiku prompt. The Haiku model is cheap, so we pay per session
# rather than per turn. The output is strict JSON; defensive parsing handles
# markdown fences + key-typo coercion.
EXTRACTION_PROMPT = """\
You are reviewing a student-tutor session and extracting durable
observations to persist into the student's long-term memory.

You will produce TWO categories of observations:

  1. "misconception" — concrete factual errors or persistent confusions
     the student exhibited during this session. Each misconception is one
     standalone sentence including the topic name (so it makes sense out
     of context). Skip if the student got everything right.

  2. "learning_style" — observable interaction patterns useful for adapting
     future sessions. Examples: terseness, hedging, hint-reliance, fast
     vs slow uptake, preference for analogies, concrete vs abstract
     reasoning, exploration tendency. Each cue is one standalone sentence.
     Skip if the session is too short to characterize.

CRITICAL rules:
  * Each "text" field must be a SINGLE concrete sentence — not a bullet
    list, not a paragraph.
  * Bake the topic name into every misconception claim so mem0's
    atomization can't strip context.
  * Do NOT invent observations. If you didn't see clear evidence, return
    an empty list for that category.
  * Output strict JSON only — no prose, no markdown fences.

Output schema:
{{
  "misconceptions": [
    {{"text": "...", "evidence": "..."}}
  ],
  "learning_style": [
    {{"text": "...", "evidence": "..."}}
  ]
}}

SESSION CONTEXT
Topic: {topic}
Locked question: {locked_question}
Target answer: {locked_answer}
Reached answer: {reached}
Total turns: {turn_count}
Hint level reached: {hint_level}

TRANSCRIPT (last {max_turns} student-tutor pairs)
{transcript}
"""


def _format_transcript(messages: list[dict], max_turns: int = 12) -> str:
    """Render the last `max_turns` student / tutor exchanges as plain text."""
    if not messages:
        return "(no messages)"
    # Keep last 2*max_turns messages so we get roughly max_turns student+tutor pairs
    tail = messages[-(2 * max_turns):]
    lines: list[str] = []
    for m in tail:
        role = m.get("role") or "?"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        prefix = {"student": "STUDENT", "tutor": "TUTOR"}.get(role, role.upper())
        lines.append(f"{prefix}: {content}")
    return "\n\n".join(lines)


def _parse_extraction_json(text: str) -> dict:
    """Strip markdown fences and json.loads. Tolerates noisy preamble."""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def extract_observations(
    state: dict,
    *,
    client: Any,
    model: str,
    max_tokens: int = 800,
    temperature: float = 0.0,
    max_turns: int = 12,
) -> list[Observation]:
    """Single Haiku call → list of Observation per L4.

    Returns [] on any LLM error (caller logs the failure via
    safe_mem0_write trace, but the session continues normally).

    Caller is responsible for adding required metadata to each
    observation before persisting via safe_mem0_write.
    """
    locked = state.get("locked_topic") or {}
    if not locked:
        locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}

    topic = (
        locked.get("subsection")
        or locked.get("section")
        or state.get("topic_selection")
        or "(unspecified)"
    )
    locked_question = state.get("locked_question") or ""
    locked_answer = state.get("locked_answer") or ""
    reached = bool(state.get("student_reached_answer"))
    turn_count = sum(1 for m in (state.get("messages") or []) if m.get("role") == "student")
    hint_level = int(state.get("hint_level") or 0)

    transcript = _format_transcript(state.get("messages") or [], max_turns=max_turns)

    prompt = EXTRACTION_PROMPT.format(
        topic=topic,
        locked_question=locked_question or "(none)",
        locked_answer=locked_answer or "(none)",
        reached="yes" if reached else "no",
        turn_count=turn_count,
        hint_level=hint_level,
        max_turns=max_turns,
        transcript=transcript,
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return []

    raw = resp.content[0].text if resp.content else ""
    try:
        parsed = _parse_extraction_json(raw)
    except (ValueError, json.JSONDecodeError):
        return []

    out: list[Observation] = []
    for item in (parsed.get("misconceptions") or []):
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        out.append(Observation(
            text=text,
            category="misconception",
            evidence=(item.get("evidence") or "").strip(),
        ))
    for item in (parsed.get("learning_style") or []):
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        out.append(Observation(
            text=text,
            category="learning_style",
            evidence=(item.get("evidence") or "").strip(),
        ))
    return out
