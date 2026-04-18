"""
conversation/summarizer.py
---------------------------
Summarizes old conversation turns when the session gets too long.

Triggered inside dean_node when:
    turn_count >= max_turns - summarizer_keep_recent

Strategy:
  - Always keep the last cfg.session.summarizer_keep_recent (10) turns intact.
  - Summarize everything before that into one paragraph using Claude.
  - The paragraph replaces the old turns in state['messages'].
  - Rolling: if session continues past max_turns again, next oldest batch gets summarized.

The summary deliberately does NOT include the answer to any open question.
Uses cfg.prompts.summarizer_system prompt from config.yaml.
Uses cfg.models.summarizer (claude-sonnet-4-5).
"""

import anthropic
from config import cfg


def maybe_summarize(messages: list[dict]) -> list[dict]:
    """
    Check if the message list needs compression. If so, summarize the oldest turns.

    Args:
        messages: Current full message list from state["messages"].

    Returns:
        Possibly-shortened message list. If long enough to trigger:
          [{"role": "system", "content": "[Session summary]: <summary>"}]
          + last summarizer_keep_recent messages
        Otherwise returns messages unchanged.
    """
    keep = cfg.session.summarizer_keep_recent
    if len(messages) <= keep:
        return messages

    old_turns = messages[:-keep]
    recent_turns = messages[-keep:]

    # Format old turns as readable conversation
    formatted_parts = []
    for msg in old_turns:
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        formatted_parts.append(f"{role}: {content}")
    formatted_old_turns = "\n".join(formatted_parts)

    client = anthropic.Anthropic()
    system = cfg.prompts.summarizer_system.format(old_turns=formatted_old_turns)

    resp = client.messages.create(
        model=cfg.models.summarizer,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": "Summarize the conversation above."}],
    )
    summary = resp.content[0].text

    return [{"role": "system", "content": f"[Session summary]: {summary}"}] + list(recent_turns)
