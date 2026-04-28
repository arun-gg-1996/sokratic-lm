"""
tests/test_cost_tracker.py
--------------------------
Unit tests for the per-call cost accumulator (B.6).

Pricing math is the only thing this module does, so the tests exhaustively
verify the rate computations against the current Sonnet 4.5 / Haiku 4.5 /
text-embedding-3-large rate cards.
"""
from __future__ import annotations

import pytest

from ingestion.core.cost_tracker import (
    PRICING_ANTHROPIC,
    PRICING_OPENAI_EMBED,
    CostTracker,
    EmbeddingCostTracker,
    MultiTracker,
    get_pricing,
    make_progress_printer,
)


# ── Pricing tables ──────────────────────────────────────────────────────────

class TestPricingTables:
    def test_sonnet_45_pricing(self):
        p = get_pricing("claude-sonnet-4-5")
        assert p["input_tokens"] == 3.00
        assert p["output_tokens"] == 15.00
        assert p["cache_creation_input_tokens"] == 3.75   # 1.25x input
        assert p["cache_read_input_tokens"] == 0.30        # 0.10x input

    def test_haiku_45_pricing(self):
        p = get_pricing("claude-haiku-4-5")
        assert p["input_tokens"] == 1.00
        assert p["output_tokens"] == 5.00
        assert p["cache_creation_input_tokens"] == 1.25
        assert p["cache_read_input_tokens"] == 0.10

    def test_unknown_model_raises(self):
        with pytest.raises(KeyError, match="unknown model"):
            get_pricing("claude-mystery-9-9")

    def test_cache_rates_are_consistent_factors(self):
        """Cache write should be 1.25x input; cache read 0.10x input
        for every Anthropic model we list."""
        for model, pricing in PRICING_ANTHROPIC.items():
            base = pricing["input_tokens"]
            assert abs(pricing["cache_creation_input_tokens"] - base * 1.25) < 1e-9, (
                f"{model}: cache_creation should be 1.25x input"
            )
            assert abs(pricing["cache_read_input_tokens"] - base * 0.10) < 1e-9, (
                f"{model}: cache_read should be 0.10x input"
            )


# ── CostTracker.record ──────────────────────────────────────────────────────

class TestCostTrackerRecord:
    def test_single_call_cost(self):
        t = CostTracker(model="claude-sonnet-4-5")
        cost = t.record({
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })
        # 1000 * 3 / 1e6 + 500 * 15 / 1e6 = 0.003 + 0.0075 = 0.0105
        assert cost == pytest.approx(0.0105, abs=1e-6)
        assert t.call_count == 1
        assert t.total_cost == pytest.approx(0.0105, abs=1e-6)

    def test_cache_read_pays_one_tenth(self):
        t = CostTracker(model="claude-sonnet-4-5")
        cost = t.record({"cache_read_input_tokens": 1000})
        # 1000 * 0.30 / 1e6 = 0.0003
        assert cost == pytest.approx(0.0003, abs=1e-9)

    def test_cache_creation_pays_premium(self):
        t = CostTracker(model="claude-sonnet-4-5")
        cost = t.record({"cache_creation_input_tokens": 1000})
        # 1000 * 3.75 / 1e6 = 0.00375
        assert cost == pytest.approx(0.00375, abs=1e-9)

    def test_multiple_calls_accumulate(self):
        t = CostTracker(model="claude-sonnet-4-5")
        for _ in range(5):
            t.record({"input_tokens": 100, "output_tokens": 50})
        assert t.call_count == 5
        assert t.input_tokens == 500
        assert t.output_tokens == 250
        # Cost: 500 * 3 / 1e6 + 250 * 15 / 1e6 = 0.0015 + 0.00375 = 0.00525
        assert t.total_cost == pytest.approx(0.00525, abs=1e-7)

    def test_missing_fields_default_to_zero(self):
        t = CostTracker(model="claude-sonnet-4-5")
        # Only output_tokens supplied
        cost = t.record({"output_tokens": 500})
        assert t.input_tokens == 0
        assert t.cache_creation_input_tokens == 0
        assert t.cache_read_input_tokens == 0
        assert cost == pytest.approx(500 * 15 / 1_000_000, abs=1e-9)

    def test_haiku_pricing_is_lower(self):
        t_sonnet = CostTracker(model="claude-sonnet-4-5")
        t_haiku = CostTracker(model="claude-haiku-4-5")
        usage = {"input_tokens": 1000, "output_tokens": 500}
        t_sonnet.record(usage)
        t_haiku.record(usage)
        # Haiku is exactly 1/3 of Sonnet for both rates
        assert t_haiku.total_cost == pytest.approx(t_sonnet.total_cost / 3, abs=1e-9)


# ── CostTracker derived metrics ─────────────────────────────────────────────

class TestCostTrackerMetrics:
    def test_avg_cost_per_call(self):
        t = CostTracker(model="claude-sonnet-4-5")
        for _ in range(4):
            t.record({"input_tokens": 100, "output_tokens": 50})
        # total = 4 * 0.00105 = 0.0042; avg = 0.00105
        assert t.avg_cost_per_call == pytest.approx(0.00105, abs=1e-7)

    def test_avg_cost_per_call_no_calls_safe(self):
        t = CostTracker(model="claude-sonnet-4-5")
        # Should not divide by zero
        assert t.avg_cost_per_call == 0.0

    def test_cache_hit_rate_full_hit(self):
        t = CostTracker(model="claude-sonnet-4-5")
        t.record({"cache_read_input_tokens": 1000})
        assert t.cache_hit_rate == 1.0

    def test_cache_hit_rate_full_miss(self):
        t = CostTracker(model="claude-sonnet-4-5")
        t.record({"cache_creation_input_tokens": 1000})
        assert t.cache_hit_rate == 0.0

    def test_cache_hit_rate_mixed(self):
        t = CostTracker(model="claude-sonnet-4-5")
        t.record({"cache_creation_input_tokens": 1000, "cache_read_input_tokens": 4000})
        # 4000 / (1000 + 4000) = 0.8
        assert t.cache_hit_rate == 0.8

    def test_cache_hit_rate_no_cache_activity(self):
        t = CostTracker(model="claude-sonnet-4-5")
        t.record({"input_tokens": 100, "output_tokens": 50})
        assert t.cache_hit_rate == 0.0

    def test_estimated_total_extrapolation(self):
        t = CostTracker(model="claude-sonnet-4-5")
        for _ in range(10):
            t.record({"input_tokens": 100, "output_tokens": 50})
        # 10 calls cost 0.0105; avg = 0.00105; project for 1000 calls
        assert t.estimated_total(1000) == pytest.approx(1.05, abs=1e-5)

    def test_estimated_total_no_calls_returns_zero(self):
        t = CostTracker(model="claude-sonnet-4-5")
        assert t.estimated_total(1000) == 0.0

    def test_progress_line_format(self):
        t = CostTracker(model="claude-sonnet-4-5")
        for _ in range(5):
            t.record({"input_tokens": 100, "output_tokens": 50})
        line = t.progress_line(done=5, total=100)
        assert "5/100" in line
        assert "5%" in line
        assert "spent $" in line
        assert "projected $" in line
        assert "cache" in line.lower()

    def test_summary_contains_all_fields(self):
        t = CostTracker(model="claude-sonnet-4-5")
        t.record({"input_tokens": 100, "output_tokens": 50,
                  "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50})
        summary = t.summary()
        assert "claude-sonnet-4-5" in summary
        assert "calls:" in summary
        assert "input tokens:" in summary
        assert "output tokens:" in summary
        assert "cache write tokens:" in summary
        assert "cache read tokens:" in summary
        assert "total cost:" in summary


# ── EmbeddingCostTracker ────────────────────────────────────────────────────

class TestEmbeddingCostTracker:
    def test_text_embedding_3_large_rate(self):
        t = EmbeddingCostTracker(model="text-embedding-3-large")
        cost = t.record(1_000_000)
        assert cost == pytest.approx(0.13, abs=1e-9)
        assert t.total_cost == pytest.approx(0.13, abs=1e-9)
        assert t.call_count == 1

    def test_text_embedding_3_small_rate(self):
        t = EmbeddingCostTracker(model="text-embedding-3-small")
        cost = t.record(1_000_000)
        assert cost == pytest.approx(0.02, abs=1e-9)

    def test_accumulates_across_calls(self):
        t = EmbeddingCostTracker(model="text-embedding-3-large")
        t.record(500_000)
        t.record(500_000)
        assert t.call_count == 2
        assert t.total_tokens == 1_000_000
        assert t.total_cost == pytest.approx(0.13, abs=1e-9)

    def test_estimated_total(self):
        t = EmbeddingCostTracker(model="text-embedding-3-large")
        # No need to record anything to extrapolate — flat rate
        assert t.estimated_total(1_000_000) == pytest.approx(0.13, abs=1e-9)

    def test_summary_contains_model_and_total(self):
        t = EmbeddingCostTracker(model="text-embedding-3-large")
        t.record(500_000)
        s = t.summary()
        assert "text-embedding-3-large" in s
        assert "total tokens" in s
        assert "total cost" in s


# ── MultiTracker ────────────────────────────────────────────────────────────

class TestMultiTracker:
    def test_aggregates_across_trackers(self):
        sonnet = CostTracker(model="claude-sonnet-4-5")
        embed = EmbeddingCostTracker(model="text-embedding-3-large")

        sonnet.record({"input_tokens": 1000, "output_tokens": 500})  # $0.0105
        embed.record(1_000_000)                                       # $0.13

        mt = MultiTracker()
        mt.add(sonnet)
        mt.add(embed)

        assert mt.total_cost == pytest.approx(0.0105 + 0.13, abs=1e-6)

    def test_summary_concatenates(self):
        sonnet = CostTracker(model="claude-sonnet-4-5")
        sonnet.record({"input_tokens": 100})
        mt = MultiTracker()
        mt.add(sonnet)
        s = mt.summary()
        assert "Cost summary: claude-sonnet-4-5" in s
        assert "GRAND TOTAL:" in s


# ── make_progress_printer ───────────────────────────────────────────────────

class TestMakeProgressPrinter:
    def test_prints_every_n_calls(self, capsys):
        t = CostTracker(model="claude-sonnet-4-5")
        cb = make_progress_printer(t, print_every=5)
        # Pre-populate with one usage record so progress_line has content
        t.record({"input_tokens": 100, "output_tokens": 50})
        # Simulate progress callbacks
        for done in range(1, 11):
            cb(done, 10)
        captured = capsys.readouterr()
        lines = [l for l in captured.out.splitlines() if l.startswith("[cost]")]
        # Should print at done=5 and done=10 (final). 2 prints.
        assert len(lines) == 2

    def test_always_prints_final(self, capsys):
        t = CostTracker(model="claude-sonnet-4-5")
        cb = make_progress_printer(t, print_every=100)  # never hits modulus
        t.record({"input_tokens": 1, "output_tokens": 1})
        cb(7, 7)  # done == total
        captured = capsys.readouterr()
        assert "7/7" in captured.out


# ── End-to-end: integrate with mocked dual-task batch ───────────────────────

class TestUsageCallbackIntegration:
    """The key contract: CostTracker.record can be passed directly as the
    usage_callback to run_dual_task_batch. This test pins that down."""

    def test_record_signature_matches_callback_contract(self):
        t = CostTracker(model="claude-sonnet-4-5")
        # The callback contract from extract_dual_task: callback(usage_dict)
        # where usage_dict has the four token fields.
        usage = {
            "input_tokens": 120,
            "output_tokens": 80,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        t.record(usage)  # must not raise
        assert t.call_count == 1
