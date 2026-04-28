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

# Sonnet 4-5 chosen over 4-6 (despite teammate's legacy choice of 4-6)
# because empirically only 4-5 honors the cache_control: ephemeral marker
# in our environment. Verified 2026-04-28 with serial back-to-back calls:
#   4-5: call 1 cache_create=1813, call 2 cache_read=1813
#   4-6: call 1 input=1826, call 2 input=1826  (no caching, even with beta header)
# Same per-token pricing for both models, so caching is the deciding factor —
# saves ~$20-25 on the full-corpus run by reading the system prompt at $0.30/M
# instead of $3.00/M for ~99% of the 2766 calls.
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
      section/chapter you will...", "Goals:", "Outcomes:", and similar
      bulleted lists of learning goals at the top of a section).
  (b) URLs, video/animation links, and navigation markers ("Watch this
      video", "INTERACTIVE LINK", "Click here", "►", "View this animation",
      "Visit this website", "Use this online resource").
  (c) Decorative parenthetical figure/table references that don't carry
      information by themselves: "(Figure 5.2)", "(see Table 3.1)",
      "(Fig 2-3)". KEEP descriptions that contain real content even if
      they reference a figure: "Table 5.3 lists three causes: A, B, C"
      → keep, because the content follows.
  (d) Page numbers, copyright footers, chapter-header repeats, ISBNs,
      attribution lines ("Adapted from...").

CRITICAL — when noise WRAPS factual content, keep the facts.
A sentence like "Watch this video to understand how the kidney filters
blood through the glomerulus and processes urea" contains a real fact
about the kidney. Strip only the "Watch this video to understand" frame
and KEEP "the kidney filters blood through the glomerulus and processes
urea" as part of the cleaned text. The video reference is removable; the
biology it describes is not.

DO NOT remove or paraphrase anything else. Specifically PRESERVE:
  - Every anatomical, physiological, biochemical, or clinical fact.
  - Numerical data, measurements, drug names, doses, units.
  - Cause-and-effect relationships, comparisons, mechanisms.
  - Disease names, conditions, symptoms, examples, edge cases.
Use the source's exact terminology. Do not rewrite for style. Do not
abbreviate or expand acronyms. Do not soften technical language.

If the chunk contains ONLY a learning-objectives list, ONLY a video
reference with no embedded biology, ONLY navigation markers, or ONLY a
discussion question with no asserted content, return cleaned_text as the
empty string and propositions as an empty list. Do not invent content.

TASK 2 — DECOMPOSE the cleaned text into propositions.
Each proposition must be:
  - Atomic (one fact per statement; no compound clauses connected by
    "and" / "but" / "because" unless the connection IS the fact).
  - Self-contained (no pronouns referring to other propositions; expand
    "it" / "they" / "this" using the antecedent).
  - Faithful to the source (same terminology, no added concepts, no
    inferred causation that isn't in the source).
  - At most 20 propositions per chunk.
Only emit propositions for content that appears in the cleaned_text.
Do not generate propositions from material that was removed as noise.

Return ONLY valid JSON in this exact shape, with NO surrounding prose,
NO markdown fences, and NO commentary:
{"cleaned_text": "...", "propositions": ["...", "..."]}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 1 — learning-objective preamble + URL + interactive link

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

═══════════════════════════════════════════════════════════════════════
EXAMPLE 2 — fact-bearing sentence wrapped in a video reference

Input chunk:
Watch this video (http://openstax.org/l/osteoporosis) to get a better
understanding of how thoracic vertebrae may become weakened and may
fracture due to osteoporosis, especially in postmenopausal women.
Vertebral osteoporosis can lead to compression fractures of the
vertebral bodies, which contribute to kyphosis of the thoracic spine.

Expected output:
{"cleaned_text": "Thoracic vertebrae may become weakened and may fracture due to osteoporosis, especially in postmenopausal women. Vertebral osteoporosis can lead to compression fractures of the vertebral bodies, which contribute to kyphosis of the thoracic spine.", "propositions": ["Thoracic vertebrae may become weakened due to osteoporosis.", "Thoracic vertebrae may fracture due to osteoporosis.", "Postmenopausal women are especially susceptible to thoracic vertebrae weakening from osteoporosis.", "Vertebral osteoporosis can lead to compression fractures of the vertebral bodies.", "Compression fractures of the vertebral bodies contribute to kyphosis of the thoracic spine."]}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 3 — table reference WITH embedded content; preserve the content

Input chunk:
The endocrine glands secrete a variety of hormones, each with distinct
target tissues and effects. Table 17.4 summarizes the major hormones
of the anterior pituitary: growth hormone (GH) targets bone and muscle
to promote growth, prolactin (PRL) targets the mammary glands to
stimulate milk production, and thyroid-stimulating hormone (TSH)
targets the thyroid gland to stimulate the release of thyroxine.
(Figure 17.7)

Expected output:
{"cleaned_text": "The endocrine glands secrete a variety of hormones, each with distinct target tissues and effects. Table 17.4 summarizes the major hormones of the anterior pituitary: growth hormone (GH) targets bone and muscle to promote growth, prolactin (PRL) targets the mammary glands to stimulate milk production, and thyroid-stimulating hormone (TSH) targets the thyroid gland to stimulate the release of thyroxine.", "propositions": ["The endocrine glands secrete a variety of hormones.", "Each hormone has distinct target tissues and effects.", "Growth hormone (GH) targets bone and muscle.", "Growth hormone promotes growth.", "Prolactin (PRL) targets the mammary glands.", "Prolactin stimulates milk production.", "Thyroid-stimulating hormone (TSH) targets the thyroid gland.", "Thyroid-stimulating hormone stimulates the release of thyroxine."]}
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
