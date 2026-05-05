"""
conversation/classifiers.py — shared Haiku infrastructure (post-D3 split).

After the D3 split this module owns ONLY the helpers shared by both
verifier_quartet and preflight_classifier — no public classifier
functions live here anymore. Specifically:

  * Haiku model constants (_HAIKU_MODEL, _HAIKU_TEMPERATURE, _HAIKU_MAX_TOKENS)
  * Anthropic client factory (_client)
  * Strict-JSON extractor (_extract_json)
  * Quote-validation helper (_validate_evidence)
  * Single-shot Haiku call wrapper (_haiku_call)
  * Cached-system-block helper (_cached_system_block)

Public classifiers live elsewhere by lifecycle:
  * conversation/verifier_quartet.py     — POST-DRAFT safety checks
  * conversation/preflight_classifier.py — PRE-PLAN intent classification

Original design notes (pre-split):

Replace regex pre-filters in dean.py's deterministic checks with small
Haiku LLM calls so all behavioral judgments share one architectural
pattern (LLM-only). Built for Tier 1 #1.4 follow-up after the user's
"i dont want regex... LLM calls only" directive (2026-05-01).

Design principles
-----------------
- **Sonnet stays for high-stakes pedagogy** (Dean QC, Teacher draft,
  mastery scoring). Haiku is for cheap classifiers where regex used to live.
- **Each classifier is one function** that returns a dict with
  `verdict`, `evidence`, `rationale`, plus classifier-specific fields.
- **Strict JSON output** + post-call evidence-quote validation
  (the LLM must cite a verbatim substring; if the substring isn't in
  the input, force `verdict` to the safe default).
- **Asymmetric stakes** stated in every prompt so Haiku biases the
  right way when ambiguous.
- **Cached system block** — Haiku's empirical cache floor is ~4100
  tokens, so the system blocks are padded with examples to cross
  that floor. Each classifier's system block ≥4500 tokens.
- **Parallel-friendly** — pure functions with no shared state, safe
  for `asyncio.gather`.

Calls Bedrock or Anthropic Direct via `make_anthropic_client()` and
`resolve_model("claude-haiku-4-5-20251001")`.

Public API
----------
    haiku_hint_leak_check(draft, locked_answer, aliases) -> dict
    haiku_sycophancy_check(draft, student_state, reach_fired) -> dict
    haiku_off_domain_check(student_msg) -> dict

Each returns a dict with at minimum {"verdict": str, "evidence": str,
"rationale": str}. See per-function docstrings for full schema.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from conversation.llm_client import beta_headers, make_anthropic_client, resolve_model


# ─────────────────────────────────────────────────────────────────────
#                         CLIENT BOOTSTRAP
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
#                         SHARED HELPERS
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
#                     CLASSIFIER 1 — HINT-3 LEAK
# ─────────────────────────────────────────────────────────────────────
