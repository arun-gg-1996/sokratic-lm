"""
conversation/rendering.py
-------------------------
Deterministic renderers for prompt history blocks.
"""


def render_history(messages: list[dict]) -> str:
    """
    Deterministic, byte-stable rendering of full conversation.

    Reads only `role` and `content` fields from each message dict.
    Any additional metadata fields are intentionally ignored.

    Append-only contract:
      render_history(messages[:n+1]).startswith(render_history(messages[:n]))
    """
    lines: list[str] = []
    for msg in messages or []:
        role = str(msg.get("role", "unknown")).capitalize()
        content = str(msg.get("content", ""))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
