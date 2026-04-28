"""
ingestion/core/cost_tracker.py
------------------------------
Per-call cost accumulator for live "$X.XX so far" readouts during long
ingestion runs (B.6).

Why this exists:
  B.5's run_dual_task_batch emits usage dicts via a usage_callback. We need
  to (a) accumulate those across thousands of calls, (b) translate token
  counts into dollars at current model pricing, and (c) surface a running
  total + extrapolated remaining cost so the operator can abort early if
  costs trend higher than expected.

Design:
  - CostTracker is a plain object — no I/O, no global state.
  - One tracker per model (Sonnet for propositions, Haiku for summaries
    later, OpenAI embeddings for the dense index). They compose via
    MultiTracker for an aggregate report.
  - PRICING is a pure data table; updates are a one-line edit when
    Anthropic / OpenAI publish new rates.

Pricing rates (USD per 1M tokens) verified against published rate cards
on 2026-04-28. cache_creation is 1.25x base input; cache_read is 0.10x.
Update PRICING when models change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# ── Pricing tables ──────────────────────────────────────────────────────────

# Anthropic models. cache_creation = 1.25x input; cache_read = 0.10x input.
PRICING_ANTHROPIC: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {
        "input_tokens": 3.00,
        "output_tokens": 15.00,
        "cache_creation_input_tokens": 3.75,
        "cache_read_input_tokens": 0.30,
    },
    "claude-sonnet-4-6": {
        "input_tokens": 3.00,
        "output_tokens": 15.00,
        "cache_creation_input_tokens": 3.75,
        "cache_read_input_tokens": 0.30,
    },
    "claude-haiku-4-5": {
        "input_tokens": 1.00,
        "output_tokens": 5.00,
        "cache_creation_input_tokens": 1.25,
        "cache_read_input_tokens": 0.10,
    },
    "claude-opus-4-5": {
        "input_tokens": 15.00,
        "output_tokens": 75.00,
        "cache_creation_input_tokens": 18.75,
        "cache_read_input_tokens": 1.50,
    },
}

# OpenAI embedding models — single rate per token.
PRICING_OPENAI_EMBED: dict[str, float] = {
    "text-embedding-3-large": 0.13,
    "text-embedding-3-small": 0.02,
}


def get_pricing(model: str) -> dict[str, float]:
    """Look up pricing for a model. Raises KeyError if unknown.

    Caller should map model id to one of the keys in PRICING_ANTHROPIC.
    """
    if model not in PRICING_ANTHROPIC:
        raise KeyError(
            f"unknown model {model!r}; pricing table has: "
            f"{sorted(PRICING_ANTHROPIC)}"
        )
    return PRICING_ANTHROPIC[model]


# ── Anthropic message-call tracker ──────────────────────────────────────────

@dataclass
class CostTracker:
    """
    Accumulates token usage and dollar cost for one Anthropic model.

    Usage:
        tracker = CostTracker(model="claude-sonnet-4-5")
        await run_dual_task_batch(chunks, usage_callback=tracker.record)
        print(tracker.summary())
        print(f"projected total: ${tracker.estimated_total(2766):.2f}")
    """
    model: str = "claude-sonnet-4-5"
    pricing: dict[str, float] = field(default_factory=dict)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    call_count: int = 0

    def __post_init__(self):
        if not self.pricing:
            self.pricing = get_pricing(self.model)

    # ── Recording ───────────────────────────────────────────────────────────

    def record(self, usage: dict) -> float:
        """Add one call's usage. Returns the dollar cost of this single call.

        Robust to missing fields; defaults to 0. Callable directly as a
        usage_callback because the signature matches what extract_dual_task
        passes (a dict with the four token fields).
        """
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        cw_tok = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cr_tok = int(usage.get("cache_read_input_tokens", 0) or 0)

        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cache_creation_input_tokens += cw_tok
        self.cache_read_input_tokens += cr_tok
        self.call_count += 1

        return self._cost_of_call(in_tok, out_tok, cw_tok, cr_tok)

    def _cost_of_call(self, in_tok: int, out_tok: int, cw_tok: int, cr_tok: int) -> float:
        return (
            in_tok * self.pricing["input_tokens"]
            + out_tok * self.pricing["output_tokens"]
            + cw_tok * self.pricing["cache_creation_input_tokens"]
            + cr_tok * self.pricing["cache_read_input_tokens"]
        ) / 1_000_000

    # ── Reporting ───────────────────────────────────────────────────────────

    @property
    def total_cost(self) -> float:
        """USD spent so far across all recorded calls."""
        return self._cost_of_call(
            self.input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )

    @property
    def avg_cost_per_call(self) -> float:
        return self.total_cost / max(self.call_count, 1)

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of cacheable input that came from cache (0.0 - 1.0).

        cache_read / (cache_read + cache_creation). Returns 0.0 if neither
        field has any tokens (cache wasn't activated)."""
        denom = self.cache_read_input_tokens + self.cache_creation_input_tokens
        if denom == 0:
            return 0.0
        return self.cache_read_input_tokens / denom

    def estimated_total(self, total_calls: int) -> float:
        """Extrapolate total cost given the average cost per call so far.

        Useful for "we're 200/2766 chunks in; projected total cost: $X".
        Returns 0 if no calls recorded yet (can't extrapolate from zero).
        """
        if self.call_count == 0:
            return 0.0
        return self.avg_cost_per_call * total_calls

    def progress_line(self, done: int, total: int) -> str:
        """One-line live indicator. Safe to print every batch."""
        pct = (done / max(total, 1)) * 100
        cache_pct = self.cache_hit_rate * 100
        proj = self.estimated_total(total)
        return (
            f"[cost] {done}/{total} ({pct:.0f}%)  "
            f"spent ${self.total_cost:.3f}  "
            f"projected ${proj:.2f}  "
            f"cache {cache_pct:.0f}%"
        )

    def summary(self) -> str:
        """Multi-line summary suitable for end-of-run reporting."""
        lines = [
            f"Cost summary: {self.model}",
            f"  calls:                 {self.call_count}",
            f"  input tokens:          {self.input_tokens:>10,}",
            f"  output tokens:         {self.output_tokens:>10,}",
            f"  cache write tokens:    {self.cache_creation_input_tokens:>10,}",
            f"  cache read tokens:     {self.cache_read_input_tokens:>10,}",
            f"  cache hit rate:        {self.cache_hit_rate * 100:.1f}%",
            f"  total cost:            ${self.total_cost:.4f}",
            f"  avg cost / call:       ${self.avg_cost_per_call:.5f}",
        ]
        return "\n".join(lines)


# ── OpenAI embedding tracker ────────────────────────────────────────────────

@dataclass
class EmbeddingCostTracker:
    """
    Accumulates token usage and cost for an OpenAI embedding model.
    Simpler than CostTracker because embedding has just one rate.
    """
    model: str = "text-embedding-3-large"
    rate_per_million: float = 0.0
    total_tokens: int = 0
    call_count: int = 0

    def __post_init__(self):
        if not self.rate_per_million:
            self.rate_per_million = PRICING_OPENAI_EMBED.get(self.model, 0.0)

    def record(self, tokens: int) -> float:
        """Add one embedding call's tokens. Returns USD cost of this call."""
        tokens = int(tokens or 0)
        self.total_tokens += tokens
        self.call_count += 1
        return tokens * self.rate_per_million / 1_000_000

    @property
    def total_cost(self) -> float:
        return self.total_tokens * self.rate_per_million / 1_000_000

    def estimated_total(self, total_tokens_projected: int) -> float:
        return total_tokens_projected * self.rate_per_million / 1_000_000

    def summary(self) -> str:
        return (
            f"Embedding cost: {self.model}\n"
            f"  calls:        {self.call_count}\n"
            f"  total tokens: {self.total_tokens:>10,}\n"
            f"  total cost:   ${self.total_cost:.4f}"
        )


# ── Aggregator across multiple trackers ─────────────────────────────────────

@dataclass
class MultiTracker:
    """Sum costs across several trackers (Sonnet propositions + maybe Haiku
    summaries + OpenAI embeddings) for a single end-of-run total."""
    trackers: list = field(default_factory=list)

    def add(self, tracker) -> None:
        self.trackers.append(tracker)

    @property
    def total_cost(self) -> float:
        return sum(getattr(t, "total_cost", 0.0) for t in self.trackers)

    def summary(self) -> str:
        sections = [t.summary() for t in self.trackers]
        sections.append(f"\nGRAND TOTAL: ${self.total_cost:.4f}")
        return "\n\n".join(sections)


# ── Convenience helpers ─────────────────────────────────────────────────────

def make_progress_printer(
    tracker: CostTracker,
    print_every: int = 10,
):
    """Return a (done, total) callback that prints `tracker.progress_line`
    every `print_every` calls (and always at the very end).

    Wire as: progress_callback=make_progress_printer(tracker, print_every=20)
    on run_dual_task_batch.
    """

    def _cb(done: int, total: int) -> None:
        if done == total or done % print_every == 0:
            print(tracker.progress_line(done, total))

    return _cb
