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

# Haiku 4-5 chosen for v1 ingestion (revised 2026-04-28 after smoke testing).
#
# History of this decision:
#   - First picked Sonnet 4-5 because empirically Sonnet 4-6 silently ignores
#     cache_control in our environment (verified with serial back-to-back calls).
#   - Then verified via Anthropic docs that Sonnet 4-6 has a 2048-token cache
#     minimum, and Haiku 4-5 has a 4096-token cache minimum. Our original
#     1659-token system prompt was below both thresholds for those models, but
#     above Sonnet 4-5's 1024-token threshold — that's why only Sonnet 4-5
#     appeared to "honor" caching.
#   - Expanded the system prompt to 4488 tokens with 6 additional few-shot
#     examples covering edge cases. Now Haiku 4-5 (4096 threshold) caches.
#   - 10-chunk side-by-side smoke test (same chunks, both models): Haiku 4-5
#     matches or beats Sonnet 4-5 on 5/10 chunks (better pronoun resolution,
#     stricter atomicity, preserved chemical equation notation), ties on 4,
#     loses slightly on 1. JSON parse rate 100% on both. Same cleaning fidelity.
#   - Cost on 7570 chunks: Haiku ~$29 vs Sonnet ~$86. Haiku wins on cost AND
#     on adherence to the prompt's atomicity / faithfulness rules.
DEFAULT_MODEL = "claude-haiku-4-5"
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

CRITICAL CONSTRAINTS — read before producing output for every chunk:
  - cleaned_text and propositions must be CONSISTENT: every proposition's
    content must trace back to a sentence (or fragment) that you preserved
    in cleaned_text. If you removed something as noise, do NOT generate
    propositions from it.
  - propositions must STAY in the source's terminology: do not substitute
    synonyms ("augment" must not become "increase", "Other larger structures"
    must not become "Some biological structures"). Lossless paraphrase only —
    no semantic drift.
  - Numeric data, dose ranges, and unit symbols (mg/dL, mmHg, mL/min, °C) are
    PROTECTED CONTENT: copy them verbatim. Never round, summarize, or
    convert units across systems.
  - Acronym handling: when an acronym is introduced via "<Full Term>
    (<ACRONYM>)" syntax, propositions may use either form, but the EXPANDED
    form should appear in at least one proposition that introduces the term.
  - Lists: when the source enumerates items ("A, B, and C"), produce one
    proposition that names the full list AND, if needed, separate
    propositions for each individual item's distinct attributes. Do NOT
    collapse a 5-item list into one compound proposition that loses the
    individual items.
  - Empty results are valid output. Do NOT fabricate propositions to fill
    an apparent gap when the chunk contains no asserted content.

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

═══════════════════════════════════════════════════════════════════════
EXAMPLE 4 — clean prose with no noise; pass-through of the source text

Input chunk:
ATP is the primary energy currency of the cell. ATP is produced
through cellular respiration, which occurs in three main stages:
glycolysis, the citric acid cycle, and the electron transport chain.
Glycolysis takes place in the cytoplasm and breaks glucose into two
pyruvate molecules. The citric acid cycle and the electron transport
chain occur in the mitochondria. Together, these stages produce a
net total of approximately 30 to 32 ATP molecules per glucose
molecule under aerobic conditions.

Expected output:
{"cleaned_text": "ATP is the primary energy currency of the cell. ATP is produced through cellular respiration, which occurs in three main stages: glycolysis, the citric acid cycle, and the electron transport chain. Glycolysis takes place in the cytoplasm and breaks glucose into two pyruvate molecules. The citric acid cycle and the electron transport chain occur in the mitochondria. Together, these stages produce a net total of approximately 30 to 32 ATP molecules per glucose molecule under aerobic conditions.", "propositions": ["ATP is the primary energy currency of the cell.", "ATP is produced through cellular respiration.", "Cellular respiration occurs in three main stages.", "The three stages of cellular respiration are glycolysis, the citric acid cycle, and the electron transport chain.", "Glycolysis takes place in the cytoplasm.", "Glycolysis breaks glucose into two pyruvate molecules.", "The citric acid cycle occurs in the mitochondria.", "The electron transport chain occurs in the mitochondria.", "Cellular respiration produces a net total of approximately 30 to 32 ATP molecules per glucose molecule under aerobic conditions."]}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 5 — chunk that is ONLY noise; return empty cleaned_text + empty propositions

This pattern arises in OpenStax-style textbooks when a section break is
filled by a single sidebar or media reference that contains no asserted
facts about the body — just an instruction to consume external media.
Do NOT invent propositions from the implied content the video might
teach. The textbook itself did not assert them.

Input chunk:
INTERACTIVE LINK Watch this animation (http://openstax.org/l/spinalcord)
to see how the spinal cord transmits signals between the brain and the
peripheral nerves. ► Click play to begin. (Figure 13.4) Visit our
website at http://openstax.org for additional review materials and
practice questions on this topic.

Expected output:
{"cleaned_text": "", "propositions": []}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 6 — numerical and measurement data must be preserved verbatim

Input chunk:
Normal fasting blood glucose levels in adults range from 70 to 99 mg/dL.
Fasting levels between 100 and 125 mg/dL indicate prediabetes. Fasting
levels of 126 mg/dL or higher on two separate tests confirm a diagnosis
of diabetes mellitus. Postprandial blood glucose levels typically peak
within 1 to 2 hours after a meal and should remain below 140 mg/dL in
healthy adults. (See Table 24.6 for a summary of these ranges.)

Expected output:
{"cleaned_text": "Normal fasting blood glucose levels in adults range from 70 to 99 mg/dL. Fasting levels between 100 and 125 mg/dL indicate prediabetes. Fasting levels of 126 mg/dL or higher on two separate tests confirm a diagnosis of diabetes mellitus. Postprandial blood glucose levels typically peak within 1 to 2 hours after a meal and should remain below 140 mg/dL in healthy adults.", "propositions": ["Normal fasting blood glucose levels in adults range from 70 to 99 mg/dL.", "Fasting blood glucose levels between 100 and 125 mg/dL indicate prediabetes.", "Fasting blood glucose levels of 126 mg/dL or higher on two separate tests confirm a diagnosis of diabetes mellitus.", "Postprandial blood glucose levels typically peak within 1 to 2 hours after a meal.", "Postprandial blood glucose levels should remain below 140 mg/dL in healthy adults."]}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 7 — multi-step mechanism; decompose ordered process into atomic steps

Input chunk:
When a skeletal muscle fiber is stimulated by a motor neuron, an action
potential travels along the sarcolemma and propagates into the
T-tubules. The action potential triggers the sarcoplasmic reticulum to
release calcium ions into the sarcoplasm. Calcium ions then bind to
troponin on the actin filaments, which causes tropomyosin to shift and
expose the myosin-binding sites on actin. Myosin heads bind to actin
and undergo a power stroke, pulling the actin filaments toward the
center of the sarcomere and shortening the muscle fiber.

Expected output:
{"cleaned_text": "When a skeletal muscle fiber is stimulated by a motor neuron, an action potential travels along the sarcolemma and propagates into the T-tubules. The action potential triggers the sarcoplasmic reticulum to release calcium ions into the sarcoplasm. Calcium ions then bind to troponin on the actin filaments, which causes tropomyosin to shift and expose the myosin-binding sites on actin. Myosin heads bind to actin and undergo a power stroke, pulling the actin filaments toward the center of the sarcomere and shortening the muscle fiber.", "propositions": ["A skeletal muscle fiber is stimulated by a motor neuron.", "Stimulation of a skeletal muscle fiber generates an action potential along the sarcolemma.", "The action potential propagates from the sarcolemma into the T-tubules.", "The action potential triggers the sarcoplasmic reticulum to release calcium ions into the sarcoplasm.", "Calcium ions bind to troponin on the actin filaments.", "Calcium binding to troponin causes tropomyosin to shift.", "The shift of tropomyosin exposes the myosin-binding sites on actin.", "Myosin heads bind to actin.", "Myosin heads undergo a power stroke.", "The power stroke pulls actin filaments toward the center of the sarcomere.", "Pulling actin filaments toward the center of the sarcomere shortens the muscle fiber."]}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 8 — L2 subsection with technical terminology; preserve exact terms

Input chunk:
(Figure 14.5) The brachial plexus is formed by the ventral rami of
spinal nerves C5 through T1. The plexus gives rise to five major
peripheral nerves of the upper limb: the musculocutaneous nerve, the
axillary nerve, the radial nerve, the median nerve, and the ulnar
nerve. Each of these nerves innervates a specific group of muscles
and a defined region of skin. Damage to any one of them produces a
characteristic pattern of motor and sensory deficits.

Expected output:
{"cleaned_text": "The brachial plexus is formed by the ventral rami of spinal nerves C5 through T1. The plexus gives rise to five major peripheral nerves of the upper limb: the musculocutaneous nerve, the axillary nerve, the radial nerve, the median nerve, and the ulnar nerve. Each of these nerves innervates a specific group of muscles and a defined region of skin. Damage to any one of them produces a characteristic pattern of motor and sensory deficits.", "propositions": ["The brachial plexus is formed by the ventral rami of spinal nerves C5 through T1.", "The brachial plexus gives rise to five major peripheral nerves of the upper limb.", "The five major peripheral nerves of the upper limb are the musculocutaneous, axillary, radial, median, and ulnar nerves.", "Each of the five major peripheral nerves of the upper limb innervates a specific group of muscles.", "Each of the five major peripheral nerves of the upper limb innervates a defined region of skin.", "Damage to any one of the five major peripheral nerves of the upper limb produces a characteristic pattern of motor and sensory deficits."]}

═══════════════════════════════════════════════════════════════════════
EXAMPLE 9 — clinical/disease comparison; preserve cause-effect and category distinctions

Input chunk:
DISORDERS OF THE Endocrine System Diabetes mellitus is a chronic
metabolic disorder characterized by hyperglycemia. There are two main
forms of the disease. Type 1 diabetes is an autoimmune condition in
which the immune system destroys the insulin-producing beta cells of
the pancreas, resulting in an absolute deficiency of insulin. It
typically begins in childhood or adolescence and requires lifelong
insulin replacement therapy. Type 2 diabetes, in contrast, results
from a combination of insulin resistance in peripheral tissues and a
relative deficiency of insulin secretion. It is most often associated
with obesity, physical inactivity, and a family history of the
disease, and is usually managed initially with lifestyle modification
and oral hypoglycemic agents. (Figure 17.18) See the interactive
animation at http://openstax.org/l/diabetes for additional review.

Expected output:
{"cleaned_text": "Diabetes mellitus is a chronic metabolic disorder characterized by hyperglycemia. There are two main forms of the disease. Type 1 diabetes is an autoimmune condition in which the immune system destroys the insulin-producing beta cells of the pancreas, resulting in an absolute deficiency of insulin. It typically begins in childhood or adolescence and requires lifelong insulin replacement therapy. Type 2 diabetes, in contrast, results from a combination of insulin resistance in peripheral tissues and a relative deficiency of insulin secretion. It is most often associated with obesity, physical inactivity, and a family history of the disease, and is usually managed initially with lifestyle modification and oral hypoglycemic agents.", "propositions": ["Diabetes mellitus is a chronic metabolic disorder.", "Diabetes mellitus is characterized by hyperglycemia.", "There are two main forms of diabetes mellitus.", "Type 1 diabetes is an autoimmune condition.", "In type 1 diabetes, the immune system destroys the insulin-producing beta cells of the pancreas.", "Type 1 diabetes results in an absolute deficiency of insulin.", "Type 1 diabetes typically begins in childhood or adolescence.", "Type 1 diabetes requires lifelong insulin replacement therapy.", "Type 2 diabetes results from a combination of insulin resistance in peripheral tissues and a relative deficiency of insulin secretion.", "Type 2 diabetes is most often associated with obesity.", "Type 2 diabetes is most often associated with physical inactivity.", "Type 2 diabetes is most often associated with a family history of the disease.", "Type 2 diabetes is usually managed initially with lifestyle modification and oral hypoglycemic agents."]}
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
    abort_event: asyncio.Event | None = None,
) -> DualTaskResult:
    """Run dual-task extraction on one chunk. Never raises; returns a
    DualTaskResult whose `error` field is non-None on failure.

    abort_event: optional asyncio.Event. If set when this coroutine runs,
    the API call is skipped and an error result is returned. Used by the
    pipeline orchestrator to short-circuit remaining calls once the cost
    cap is reached."""
    chunk_id = chunk.get("chunk_id", "unknown")
    chunk_text = (chunk.get("text") or "").strip()
    if not chunk_text:
        return DualTaskResult(
            chunk_id=chunk_id, cleaned_text="", error="empty chunk text",
        )

    if abort_event is not None and abort_event.is_set():
        return DualTaskResult(
            chunk_id=chunk_id, cleaned_text=chunk_text,
            error="aborted by cost cap before API call",
        )

    async with semaphore:
        # Re-check after acquiring the semaphore — cost may have crossed the
        # cap while we were queued behind earlier calls.
        if abort_event is not None and abort_event.is_set():
            return DualTaskResult(
                chunk_id=chunk_id, cleaned_text=chunk_text,
                error="aborted by cost cap before API call",
            )
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
    abort_event: asyncio.Event | None = None,
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
            abort_event=abort_event,
        )
        done_counter += 1
        if progress_callback is not None:
            try:
                progress_callback(done_counter, total)
            except Exception:
                pass
        return result

    return await asyncio.gather(*[_wrapped(c) for c in chunks])
