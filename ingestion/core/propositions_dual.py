"""
ingestion/core/propositions_dual.py
-----------------------------------
Dual-task proposition extraction (B.5).

One Sonnet 4.5 call per chunk does TWO things and returns JSON:
  (a) cleans the chunk text — strips URLs, INTERACTIVE LINK markers,
      LEARNING OBJECTIVES preambles, decorative figure/table refs, page
      numbers, attribution lines — preserves all factual content using
      source terminology.
  (b) decomposes the cleaned text into atomic propositions (≤ 20 per chunk),
      faithful to the source.

Why dual-task in one prompt:
  - Single API round-trip per chunk — cheaper and cuts wall time roughly in half
    versus two sequential calls.
  - Sonnet sees both the source text AND the cleaned version it just produced
    when extracting propositions, so the propositions stay aligned with what
    actually survives cleaning.
  - The system prompt body is identical across all chunks, so prompt caching
    pays for itself after the first call (~80% input-token cost reduction
    once cached).

Architecture notes:
  - The system prompt body is source-agnostic; describes noise patterns
    generically so the same module works for the second textbook in Phase C.
  - Optional source-specific suffix (from sources/X/prompt_overrides.py) is
    appended to the cached system block.
  - Async parallel via AsyncAnthropic + asyncio.gather, capped by a semaphore.
  - Per-call cost tracking: each call emits a usage dict; B.6 cost_tracker
    aggregates these for live "$X.XX so far" readouts during long runs.
  - JSON parse failures are isolated — a single malformed response doesn't
    crash the batch; the affected chunk gets an empty proposition list and
    `error` populated for B.8 pilot validation.

The legacy synchronous core/propositions.py (single-task, anthropic.Anthropic)
is left untouched for backwards compat with the existing build_indexes path
until B.7 (pipeline orchestrator) routes ingestion through this module.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable

from anthropic import AsyncAnthropic


# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_PROMPT_VERSION = "v1"
DEFAULT_CONCURRENCY = 20
DEFAULT_MAX_OUTPUT_TOKENS = 2048
PROPOSITION_CAP = 20  # max propositions per chunk


# Source-agnostic prompt body. The single example demonstrates the noise
# patterns we expect (LEARNING OBJECTIVES preamble, INTERACTIVE LINK marker,
# inline URL, decorative parenthetical figure ref) and the desired output
# (clean prose + atomic propositions in source terminology).
SYSTEM_PROMPT_BODY = """\
You are processing chunks from a textbook. For each chunk you receive,
do TWO things and return the result as JSON.

TASK 1 — CLEAN the chunk text.
Remove ONLY the following kinds of noise. Do NOT remove anything else.
  (a) Learning-objective preambles ("Objectives:", "By the end of this
      section/chapter you will...", "Goals:", "Outcomes:", and similar).
  (b) URLs, video/animation links, and navigation markers ("Watch this
      video", "INTERACTIVE LINK", "Click here", "►", "View this animation").
  (c) Decorative parenthetical figure/table references that don't carry
      information by themselves: "(Figure 5.2)", "(see Table 3.1)",
      "(Fig 2-3)". KEEP descriptions that contain real content even if
      they reference a figure: "Table 5.3 lists three causes: A, B, C"
      → keep, because the content follows.
  (d) Page numbers, copyright footers, chapter-header repeats, ISBNs,
      attribution lines ("Adapted from...").

DO NOT remove or paraphrase anything else. Specifically PRESERVE:
  - Every anatomical, physiological, biochemical, or clinical fact.
  - Numerical data, measurements, drug names, doses, units.
  - Cause-and-effect relationships, comparisons, mechanisms.
  - Disease names, conditions, symptoms, examples, edge cases.
Use the source's exact terminology. Do not rewrite for style.

TASK 2 — DECOMPOSE the cleaned text into propositions.
Each proposition must be:
  - Atomic (one fact per statement).
  - Self-contained (no pronouns referring to other propositions).
  - Faithful to the source (same terminology, no added concepts).
  - Cap at 20 propositions per chunk.

Return ONLY valid JSON in this exact shape, with NO surrounding prose:
{"cleaned_text": "...", "propositions": ["...", "..."]}

EXAMPLE

Input chunk:
LEARNING OBJECTIVES By the end of this section, you will be able to:
• Describe the structure of the heart.
• Explain blood flow through the chambers.

The heart is a four-chambered muscular organ located in the mediastinum
between the lungs (Figure 19.2). The right atrium receives deoxygenated
blood from the venae cavae. INTERACTIVE LINK View this animation
(http://example.com/heart) for more detail. The right ventricle pumps
blood to the lungs via the pulmonary artery.

Expected output:
{"cleaned_text": "The heart is a four-chambered muscular organ located in the mediastinum between the lungs. The right atrium receives deoxygenated blood from the venae cavae. The right ventricle pumps blood to the lungs via the pulmonary artery.", "propositions": ["The heart is a four-chambered muscular organ.", "The heart is located in the mediastinum.", "The mediastinum is between the lungs.", "The right atrium receives deoxygenated blood from the venae cavae.", "The right ventricle pumps blood to the lungs.", "The right ventricle pumps blood via the pulmonary artery."]}
"""


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class DualTaskResult:
    """Outcome of one dual-task call. Always populated; check `error`."""
    chunk_id: str
    cleaned_text: str
    propositions: list[dict] = field(default_factory=list)
    usage: dict | None = None         # raw token usage if API responded
    error: str | None = None          # None on success


# ── Prompt assembly ──────────────────────────────────────────────────────────

def build_cached_system(extra_suffix: str = "") -> list[dict]:
    """Build the `system=` blocks for messages.create with the body cached.

    The single text block carries the entire instruction body + few-shot
    example + optional source-specific suffix. The cache_control: ephemeral
    marker makes Anthropic's prompt cache treat this prefix as cacheable;
    after the first request in a 5-minute window, subsequent calls hit the
    cache for ~10× faster processing on the cached portion and ~10% input-
    token cost.
    """
    body = SYSTEM_PROMPT_BODY
    if extra_suffix and extra_suffix.strip():
        body = (
            f"{SYSTEM_PROMPT_BODY}\n\n"
            f"ADDITIONAL DOMAIN-SPECIFIC INSTRUCTIONS:\n{extra_suffix.strip()}"
        )
    return [{"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}]


# ── Response parsing ─────────────────────────────────────────────────────────

def parse_response(text: str) -> tuple[str | None, list[str], str | None]:
    """Parse the LLM's JSON response.

    Returns (cleaned_text, propositions, error). `error` is None on success.

    Robust to:
      - markdown code fences (```json ... ```)
      - extra prose before the JSON object
      - propositions over the cap (truncated)
      - non-string entries in the propositions list (filtered out)
    """
    if not text:
        return None, [], "empty response"

    candidate = text.strip()

    # Strip a markdown fence if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1)
    else:
        # Otherwise, try to slice from the first '{' to the last '}'.
        first_brace = candidate.find("{")
        last_brace = candidate.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            candidate = candidate[first_brace : last_brace + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, [], f"json parse error: {exc}"

    if not isinstance(parsed, dict):
        return None, [], "response is not a JSON object"

    cleaned = parsed.get("cleaned_text")
    props = parsed.get("propositions")

    if not isinstance(cleaned, str):
        return None, [], "cleaned_text missing or not a string"
    if not isinstance(props, list):
        return None, [], "propositions missing or not a list"

    cleaned = cleaned.strip()
    props = [p.strip() for p in props if isinstance(p, str) and p.strip()]
    if len(props) > PROPOSITION_CAP:
        props = props[:PROPOSITION_CAP]

    return cleaned, props, None


# ── Single-chunk extraction ──────────────────────────────────────────────────

async def extract_dual_task(
    client: AsyncAnthropic,
    chunk: dict,
    semaphore: asyncio.Semaphore,
    *,
    model: str = DEFAULT_MODEL,
    cached_system: list[dict],
    usage_callback: Callable[[dict], None] | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> DualTaskResult:
    """Run dual-task extraction on one chunk. Never raises; returns a
    DualTaskResult whose `error` field is non-None on failure."""
    chunk_id = chunk.get("chunk_id", "unknown")
    chunk_text = (chunk.get("text") or "").strip()
    if not chunk_text:
        return DualTaskResult(
            chunk_id=chunk_id, cleaned_text="", error="empty chunk text",
        )

    async with semaphore:
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                temperature=0,
                system=cached_system,
                messages=[{"role": "user", "content": chunk_text}],
            )
        except Exception as exc:
            return DualTaskResult(
                chunk_id=chunk_id,
                cleaned_text=chunk_text,
                error=f"api error: {type(exc).__name__}: {exc}",
            )

    # Capture usage even on parse failure — billable regardless.
    usage = {
        "input_tokens": getattr(resp.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(resp.usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }
    if usage_callback is not None:
        try:
            usage_callback(usage)
        except Exception:
            pass  # never let a callback bug crash the batch

    text = ""
    if resp.content:
        # Sonnet returns a list of content blocks; concatenate text blocks.
        text = "".join(getattr(b, "text", "") for b in resp.content)

    cleaned, prop_strs, parse_err = parse_response(text)
    if parse_err:
        return DualTaskResult(
            chunk_id=chunk_id,
            cleaned_text=chunk_text,
            usage=usage,
            error=parse_err,
        )

    proposition_records = [
        {
            "proposition_id": str(uuid.uuid4()),
            "text": p_text,
            "parent_chunk_id": chunk_id,
            "parent_chunk_text": cleaned,
        }
        for p_text in prop_strs
    ]
    return DualTaskResult(
        chunk_id=chunk_id,
        cleaned_text=cleaned,
        propositions=proposition_records,
        usage=usage,
        error=None,
    )


# ── Batch orchestration ──────────────────────────────────────────────────────

async def run_dual_task_batch(
    chunks: list[dict],
    *,
    client: AsyncAnthropic | None = None,
    model: str = DEFAULT_MODEL,
    extra_system_suffix: str = "",
    concurrency: int = DEFAULT_CONCURRENCY,
    usage_callback: Callable[[dict], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> list[DualTaskResult]:
    """
    Run dual-task extraction across a list of chunks in parallel.

    Args:
        chunks               input list (chunk_id, text required; rest ignored).
        client               optional AsyncAnthropic to inject for tests; if
                             None, constructs a fresh client.
        model                model identifier; defaults to Sonnet 4.5.
        extra_system_suffix  optional source-specific instructions appended to
                             the cached system body (from
                             sources/X/prompt_overrides.py).
        concurrency          max in-flight requests; defaults to 20.
        usage_callback       called once per response with the raw usage dict;
                             B.6 cost_tracker uses this to maintain a live
                             running cost estimate.
        progress_callback    called as (done, total) after each chunk
                             completes. Order is non-deterministic (parallel),
                             so this is for "X of Y done" UI only.

    Returns:
        list of DualTaskResult preserving the input chunk order.
    """
    if not chunks:
        return []
    if client is None:
        client = AsyncAnthropic()

    sem = asyncio.Semaphore(concurrency)
    cached_system = build_cached_system(extra_system_suffix)

    total = len(chunks)
    done_counter = 0

    async def _wrapped(chunk):
        nonlocal done_counter
        result = await extract_dual_task(
            client, chunk, sem,
            model=model,
            cached_system=cached_system,
            usage_callback=usage_callback,
            max_output_tokens=max_output_tokens,
        )
        done_counter += 1
        if progress_callback is not None:
            try:
                progress_callback(done_counter, total)
            except Exception:
                pass
        return result

    return await asyncio.gather(*[_wrapped(c) for c in chunks])
