"""
Shared Haiku infrastructure used by the verifier quartet and the
preflight classifier.

Exposes the Anthropic client factory, the strict-JSON extractor, the
evidence-quote validator, the single-shot Haiku call wrapper, and the
cached-system-block helper. The classifiers themselves live in
`conversation/verifier_quartet.py` (post-draft safety) and
`conversation/preflight_classifier.py` (pre-plan intent).

Each classifier returns a dict with `verdict`, `evidence`, and
`rationale`; the evidence is a verbatim substring of the input which
is validated post-call. If validation fails the verdict snaps back
to the safe default.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from conversation.llm_client import beta_headers, make_anthropic_client, resolve_model

# ─────────────────────────────────────────────────────────────────────
# CLIENT BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_HAIKU_TEMPERATURE = 0.0
_HAIKU_MAX_TOKENS = 200

# One client across all classifiers. Lazy init so import time stays cheap.
_CLIENT = None

def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = make_anthropic_client()
    return _CLIENT

# ─────────────────────────────────────────────────────────────────────
# SHARED HELPERS
# ─────────────────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)

def _extract_json(text: str) -> dict | None:
    """Pull the first plausible JSON object out of LLM text. Tolerant of
 fenced markdown, leading/trailing prose, smart quotes."""
    if not text:
        return None
    s = text.strip()
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(s)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None

def _validate_evidence(evidence: str, source_text: str) -> bool:
    """True if `evidence` (a quoted substring claim) actually appears in
 `source_text`. Case-insensitive, whitespace-tolerant.

 The classifiers must cite a verbatim substring — this enforces it.
 Catches LLM hallucinations where it claims to see something that
 isn't there.
"""
    if not evidence:
        return True  # empty evidence == "no leak found", legitimate
    src_normal = re.sub(r"\s+", " ", (source_text or "").strip().lower())
    ev_normal = re.sub(r"\s+", " ", evidence.strip().lower())
    if not ev_normal:
        return True
    return ev_normal in src_normal

def _haiku_call(system_blocks: list, user_text: str) -> str:
    """Single-shot Haiku classifier call. Returns raw response text."""
    resp = _client().messages.create(
        model=resolve_model(_HAIKU_MODEL),
        temperature=_HAIKU_TEMPERATURE,
        max_tokens=_HAIKU_MAX_TOKENS,
        system=system_blocks,
        messages=[{"role": "user", "content": user_text}],
        extra_headers=beta_headers(),
    )
    if not resp.content:
        return ""
    return resp.content[0].text or ""

def _cached_system_block(text: str) -> list:
    """Wrap a static prompt in the Anthropic cache_control format.
 Haiku 4.5 caches blocks ≥4096 actual tokens; rough approx = chars/4."""
    return [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]

# ─────────────────────────────────────────────────────────────────────
# CLASSIFIER 1 — HINT-3 LEAK
# ─────────────────────────────────────────────────────────────────────
