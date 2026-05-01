"""
Anthropic client factory + model-ID resolver.

Returns either:
  - anthropic.Anthropic()        — direct API (default)
  - anthropic.AnthropicBedrock() — when SOKRATIC_USE_BEDROCK=1

For Bedrock, model IDs need a cross-region inference profile prefix
("us.anthropic..."). `resolve_model()` rewrites short Anthropic model
names to the appropriate ID for the active client.

Use:
    from conversation.llm_client import make_anthropic_client, resolve_model

    client = make_anthropic_client()
    resp = client.messages.create(
        model=resolve_model("claude-sonnet-4-6"),
        ...
    )

Set SOKRATIC_USE_BEDROCK=1 in .env to flip the whole runtime to Bedrock.
"""
from __future__ import annotations

import os
from typing import Any


def _use_bedrock() -> bool:
    return os.environ.get("SOKRATIC_USE_BEDROCK", "0").strip() == "1"


def make_anthropic_client() -> Any:
    """Construct an Anthropic client. Honors SOKRATIC_USE_BEDROCK env flag."""
    import anthropic
    if _use_bedrock():
        # AnthropicBedrock auto-reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
        # / AWS_REGION via boto3's default chain.
        return anthropic.AnthropicBedrock()
    return anthropic.Anthropic()


def make_async_anthropic_client() -> Any:
    """Async variant — used by ingestion/scripts batch paths."""
    import anthropic
    if _use_bedrock():
        return anthropic.AsyncAnthropicBedrock()
    return anthropic.AsyncAnthropic()


# Map "short" Anthropic model names → Bedrock cross-region inference IDs.
# Verified 2026-05-01 in us-east-1 with boto3 list_foundation_models +
# direct invocations: all three IDs successfully respond to messages.create().
_BEDROCK_MODEL_MAP = {
    "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-sonnet-4-5-20250929": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-opus-4-5-20251101": "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-7": "us.anthropic.claude-opus-4-7",
}


def beta_headers() -> dict:
    """Return Anthropic beta headers appropriate for the active client.

    Anthropic Direct accepts `anthropic-beta: prompt-caching-2024-07-31`
    to enable explicit prompt caching. Bedrock's API rejects this header
    with `400 invalid beta flag` — Bedrock has its own caching mechanism
    that doesn't need a header.

    Use:
        resp = client.messages.create(
            extra_headers=beta_headers(),
            ...
        )
    """
    if _use_bedrock():
        return {}  # Bedrock rejects anthropic-beta headers
    return {"anthropic-beta": "prompt-caching-2024-07-31"}


def resolve_model(name: str) -> str:
    """Convert a short Anthropic model name to the right ID for the
    active client.

    Anthropic Direct: returns name as-is (the SDK accepts short IDs).
    Bedrock: returns the cross-region inference profile ID. If the
    name is already in Bedrock format (starts with "us.anthropic."),
    pass through unchanged.
    """
    if not _use_bedrock():
        return name
    if name.startswith("us.anthropic.") or name.startswith("anthropic."):
        return name
    return _BEDROCK_MODEL_MAP.get(name, name)
