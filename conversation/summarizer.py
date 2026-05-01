"""
conversation/summarizer.py
---------------------------
Summarizes old conversation turns when the session gets too long.

NOTE ON CACHE BEHAVIOR:
This summarizer replaces the first N messages in state["messages"] with a single
system-role summary. Because render_history reads state["messages"] directly,
this mutation changes rendered history bytes and invalidates the cached history
block from that turn forward.

This is acceptable because summarization is intended near the session cap.
The post-summary history becomes a new stable prefix and can cache again on
subsequent turns.

Do NOT move summarizer execution earlier/mid-session without reconsidering
cache impact and conversation continuity tradeoffs.

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

    from conversation.llm_client import make_anthropic_client
    client = make_anthropic_client()
    # D.6b-2: split the prompt into a stable instruction prefix +
    # per-call old_turns block so the prefix is cache-eligible. Even
    # though our current summarizer prompt is short (~50 tokens, below
    # Anthropic's 1024-token cache floor), wiring it as a structured
    # cached/uncached pair is the right shape: if the prompt grows
    # (e.g., we add few-shot exemplars or extended pedagogical rules)
    # caching kicks in automatically. Below-threshold prompts are
    # transparently passed through uncached by the API.
    template = cfg.prompts.summarizer_system
    if "{old_turns}" in template:
        prefix, suffix = template.split("{old_turns}", 1)
    else:
        # Defensive: very old configs may not have the placeholder.
        # Treat the whole template as static and ignore the formatted
        # turns (degraded but doesn't crash).
        prefix, suffix = template, ""

    system_blocks = [
        {
            "type": "text",
            "text": prefix,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    # The actual conversation gets its own block (per-call) plus any
    # trailing template text (typically empty).
    conversation_block_text = formatted_old_turns + (suffix or "")
    system_blocks.append({"type": "text", "text": conversation_block_text})

    from conversation.llm_client import resolve_model
    resp = client.messages.create(
        model=resolve_model(cfg.models.summarizer),
        max_tokens=300,
        system=system_blocks,
        messages=[{"role": "user", "content": "Summarize the conversation above."}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    summary = resp.content[0].text

    return [{"role": "system", "content": f"[Session summary]: {summary}"}] + list(recent_turns)
