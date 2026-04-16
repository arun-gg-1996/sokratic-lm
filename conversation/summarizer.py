"""
conversation/summarizer.py
---------------------------
Summarizes old conversation turns when the session gets too long.

Triggered when state['turn_count'] > cfg.session.max_turns.

Strategy:
  - Always keep the last cfg.session.summarizer_keep_recent turns intact.
  - Summarize everything before that into one paragraph using GPT-4o.
  - The paragraph replaces the old turns in state['messages'].
  - This rolls forward: if the session keeps going and hits the limit again,
    the next oldest batch gets summarized the same way.

The summary deliberately does NOT include the answer to any open question.
Uses the 'summarizer_system' prompt from config.yaml.
"""

from config import cfg


def maybe_summarize(messages: list[dict]) -> list[dict]:
    """
    Check if the message list is too long. If so, summarize the oldest turns.

    Args:
        messages: Current full message list from state.

    Returns:
        Possibly-shortened message list with a summary block prepended.
    """
    keep = cfg.session.summarizer_keep_recent
    if len(messages) <= keep:
        return messages

    # TODO: split messages into old_turns and recent_turns
    # TODO: call GPT-4o with cfg.prompts.summarizer_system to summarize old_turns
    # TODO: return [{"role": "system", "content": f"[Session summary]: {summary}"}] + recent_turns
    raise NotImplementedError
