"""
tests/test_chunker_boundaries.py
--------------------------------
Unit tests for the sentence-boundary guarantees in core/chunker.py (B.3).

Pre-B.3 audit (2026-04-28) found:
  - 9.4% of unique paragraph chunks ended mid-sentence
  - 64% of overlap chunks ended mid-sentence (token-cap, not sentence-cap)

These tests pin down the new contract:
  - chunker output never ends mid-sentence (modulo the rare drop-too-long-clause path)
  - overlap chunks always end at sentence terminators
  - the back-off helper handles edge cases without slicing mid-token
"""
from __future__ import annotations

import re

import pytest

from ingestion.core.chunker import (
    _back_off_to_sentence_end,
    _split_to_token_budget,
    _trim_overlap_to_budget,
    _get_token_encoder,
    _token_len,
)


@pytest.fixture(scope="module")
def encoder():
    enc = _get_token_encoder()
    if enc is None:
        pytest.skip("tiktoken not available; chunker fallback path not tested here")
    return enc


def _ends_at_sentence_terminator(text: str) -> bool:
    """True if text's last non-whitespace char is `.`, `!`, or `?`."""
    if not text:
        return True
    return text.rstrip().endswith((".", "!", "?", '."', '!"', '?"'))


# ── _back_off_to_sentence_end ───────────────────────────────────────────────

class TestBackOffToSentenceEnd:
    def test_short_text_within_budget_already_sentence_aligned(self, encoder):
        text = "The heart pumps blood. The lungs oxygenate it."
        result = _back_off_to_sentence_end(text, encoder, max_tokens=50)
        assert result == text
        assert _ends_at_sentence_terminator(result)

    def test_short_text_within_budget_partial_trailing_sentence(self, encoder):
        text = "The heart pumps blood. The lungs oxygenate"
        result = _back_off_to_sentence_end(text, encoder, max_tokens=50)
        # Partial "The lungs oxygenate" must be dropped
        assert result == "The heart pumps blood."
        assert _ends_at_sentence_terminator(result)

    def test_long_text_backs_off_to_last_complete_sentence(self, encoder):
        text = (
            "Sentence one is short. Sentence two is also short. "
            "Sentence three contains many specific anatomical terms about "
            "the cardiovascular system to take up tokens. Sentence four is here."
        )
        # Pick a budget that fits 2-3 sentences
        result = _back_off_to_sentence_end(text, encoder, max_tokens=15)
        assert _ends_at_sentence_terminator(result)
        # Must be a true prefix of the source text
        assert text.startswith(result)

    def test_first_sentence_exceeds_budget_returns_empty(self, encoder):
        text = (
            "This first sentence is intentionally constructed with many specific "
            "anatomical and physiological terms about the cardiovascular system "
            "to ensure it cannot fit inside a tiny token budget for testing. "
            "Then a short follow-up."
        )
        result = _back_off_to_sentence_end(text, encoder, max_tokens=10)
        # First sentence alone exceeds budget => caller decides; we return ""
        assert result == ""

    def test_empty_input_returns_empty(self, encoder):
        assert _back_off_to_sentence_end("", encoder, max_tokens=100) == ""


# ── _trim_overlap_to_budget ─────────────────────────────────────────────────

class TestTrimOverlapToBudget:
    def test_overlap_fits_no_trim(self, encoder):
        prefix = "The atria contract first. Then the ventricles contract."
        body = "The right ventricle pumps blood to the lungs."
        out = _trim_overlap_to_budget(prefix, body, encoder, max_tokens=200)
        assert "atria contract first" in out
        assert "right ventricle pumps blood to the lungs" in out
        assert _ends_at_sentence_terminator(out)

    def test_overlap_body_trimmed_to_sentence_boundary(self, encoder):
        prefix = "The atria contract first. Then the ventricles contract."
        body = (
            "The right atrium receives blood from the venae cavae. "
            "The right ventricle pumps blood to the lungs through the pulmonary "
            "artery, which then divides into smaller branches. "
            "The pulmonary capillaries oxygenate the blood. "
            "The oxygenated blood returns via the pulmonary veins."
        )
        # Budget tight enough to force body trimming
        out = _trim_overlap_to_budget(prefix, body, encoder, max_tokens=50)
        assert _ends_at_sentence_terminator(out), (
            f"overlap chunk ended mid-sentence: {out[-80:]!r}"
        )
        # Body must NOT contain a mid-sentence cut
        assert not out.endswith(",")
        assert not out.endswith("the")

    def test_overlap_prefix_only_when_body_too_long(self, encoder):
        prefix = "The heart pumps blood. The lungs oxygenate it."
        # Body's first sentence alone exceeds remaining budget
        body = (
            "This very long sentence about the entire cardiovascular system and "
            "all of its constituent vessels including arteries, arterioles, "
            "capillaries, venules, and veins continues without any sentence "
            "terminator inside the budget."
        )
        out = _trim_overlap_to_budget(prefix, body, encoder, max_tokens=30)
        # We should fall back to prefix-only, never mid-sentence cut
        assert _ends_at_sentence_terminator(out), (
            f"prefix-only fallback ended mid-sentence: {out[-80:]!r}"
        )
        assert "Context from previous chunk" in out

    def test_no_overlap_chunks_end_mid_sentence_random_inputs(self, encoder):
        """Property test: across many synthetic prefixes/bodies/budgets, no
        overlap chunk should ever end mid-sentence."""
        prefixes = [
            "Sentence A. Sentence B.",
            "Quick prefix.",
        ]
        bodies = [
            "First. Second. Third. Fourth. Fifth.",
            "One sentence here. Then another sentence with more anatomical terminology. And a third about the digestive system. Plus a fourth for good measure.",
            "A. B. C. D.",  # very short sentences
        ]
        budgets = [10, 25, 50, 80, 200]
        for p in prefixes:
            for b in bodies:
                for budget in budgets:
                    out = _trim_overlap_to_budget(p, b, encoder, max_tokens=budget)
                    assert _ends_at_sentence_terminator(out), (
                        f"FAIL prefix={p!r} body={b!r} budget={budget} "
                        f"out={out[-80:]!r}"
                    )


# ── _split_to_token_budget ──────────────────────────────────────────────────

class TestSplitToTokenBudget:
    def test_within_budget_passes_through(self, encoder):
        text = "Short paragraph that fits. Just two sentences total."
        parts = _split_to_token_budget(text, encoder, max_tokens=200)
        assert parts == [text]

    def test_split_at_sentence_boundary(self, encoder):
        # Use realistic anatomy-length sentences so MIN_CHUNK_TOKENS=15 filter
        # doesn't drop short fragments; we want to actually exercise the
        # multi-chunk path here.
        text = (
            "The heart is a four-chambered muscular organ located in the "
            "mediastinum between the lungs. "
            "The right atrium receives deoxygenated blood from the venae cavae "
            "and pumps it forward into the right ventricle. "
            "The right ventricle then propels this blood through the pulmonary "
            "artery toward the lungs for oxygenation. "
            "The left atrium receives oxygenated blood returning from the lungs "
            "via the four pulmonary veins. "
            "The left ventricle generates the highest pressure of any chamber "
            "and ejects oxygenated blood into the systemic circulation."
        )
        # Budget tight enough to force multiple chunks
        parts = _split_to_token_budget(text, encoder, max_tokens=30)
        assert len(parts) >= 2, f"expected multiple chunks, got {len(parts)}: {parts}"
        for p in parts:
            assert _ends_at_sentence_terminator(p), (
                f"chunk ended mid-sentence: {p[-80:]!r}"
            )
            assert _token_len(p, encoder) <= 30

    def test_long_sentence_clause_split(self, encoder):
        # Single sentence > max_tokens. Should clause-split, not mid-token cut.
        text = (
            "Hormones regulate many physiological processes; they bind to "
            "specific receptors on target cells; they are produced by endocrine "
            "glands such as the pituitary, the thyroid, and the adrenals; they "
            "exert effects via second messenger systems."
        )
        parts = _split_to_token_budget(text, encoder, max_tokens=15)
        # All resulting fragments should be reasonable; most importantly,
        # the final fragment should not have been sliced mid-token.
        assert len(parts) >= 1
        # No fragment should end mid-word (heuristic: ends with letter and no
        # punctuation indicates mid-clause).
        for p in parts:
            last = p.rstrip()[-1] if p.rstrip() else ""
            mid_token = last.isalpha() and last.islower() and not p.rstrip().endswith(("the", "and", "or"))
            # Allow partial words only if it's a known short connector at end
            assert not (last.isalpha() and not last.isupper() and not _ends_at_sentence_terminator(p) and last not in ";:,"), (
                f"chunk appears to end mid-word: {p[-50:]!r}"
            )
