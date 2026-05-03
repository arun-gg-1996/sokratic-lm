"""
tests/test_vlm_extract.py
─────────────────────────
Coverage for vlm/extract.py — L77 Sonnet vision extraction.

Tests focus on:
  * Never-raises invariant (any failure path returns _empty_result dict)
  * File-size cap enforced
  * Unsupported extension rejected
  * Successful path coerces structures + image_type + confidence
  * JSON parse falls back to lenient regex
  * Coercion clamps confidence to [0, 1] and drops unnamed structures

All Anthropic client calls are mocked.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vlm import extract as V


def _png(tmp_path: Path, name: str = "img.png", size: int = 16) -> Path:
    """Tiny fake PNG (just bytes; Sonnet would parse but we mock the call)."""
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"\x00" * size))
    return p


def _client_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=80),
    )
    return client


def test_extract_missing_file_returns_empty(tmp_path: Path):
    out = V.extract_image_context(
        tmp_path / "nope.png",
        client=MagicMock(), model="m", domain_prompt="p",
    )
    assert out["confidence"] == 0.0
    assert out["identified_structures"] == []
    assert "file not found" in out.get("_error", "")


def test_extract_oversize_rejected(tmp_path: Path):
    p = _png(tmp_path, size=V.MAX_IMAGE_BYTES + 1024)
    out = V.extract_image_context(
        p, client=MagicMock(), model="m", domain_prompt="p",
    )
    assert "too large" in out.get("_error", "")


def test_extract_unsupported_extension(tmp_path: Path):
    p = tmp_path / "img.bmp"
    p.write_bytes(b"BMx")
    out = V.extract_image_context(
        p, client=MagicMock(), model="m", domain_prompt="p",
    )
    assert "unsupported extension" in out.get("_error", "")


def test_extract_happy_path(tmp_path: Path):
    p = _png(tmp_path)
    response = """{
  "identified_structures": [
    {"name": "humerus", "location": "central, vertical", "confidence": 0.95},
    {"name": "deltoid muscle", "location": "lateral, top", "confidence": 0.88}
  ],
  "image_type": "x-ray",
  "description": "AP X-ray of the right shoulder showing humerus + deltoid",
  "best_topic_guess": "shoulder anatomy",
  "confidence": 0.9
}"""
    client = _client_returning(response)
    out = V.extract_image_context(
        p, client=client, model="m", domain_prompt="Identify structures",
    )
    assert out["confidence"] == 0.9
    assert out["image_type"] == "x-ray"
    assert len(out["identified_structures"]) == 2
    assert out["identified_structures"][0]["name"] == "humerus"
    assert out["best_topic_guess"] == "shoulder anatomy"
    assert "_error" not in out


def test_extract_strips_markdown_fences(tmp_path: Path):
    p = _png(tmp_path)
    response = "```json\n{\"identified_structures\": [], \"image_type\": \"diagram\", \"description\": \"x\", \"best_topic_guess\": \"\", \"confidence\": 0.5}\n```"
    client = _client_returning(response)
    out = V.extract_image_context(
        p, client=client, model="m", domain_prompt="p",
    )
    assert out["confidence"] == 0.5
    assert out["image_type"] == "diagram"
    assert "_error" not in out


def test_extract_clamps_invalid_confidence(tmp_path: Path):
    p = _png(tmp_path)
    response = """{
  "identified_structures": [
    {"name": "valid", "confidence": 1.5},
    {"name": "negative", "confidence": -0.5},
    {"name": "", "confidence": 0.9},
    {"confidence": 0.7}
  ],
  "image_type": "diagram",
  "description": "test",
  "best_topic_guess": "",
  "confidence": 2.5
}"""
    client = _client_returning(response)
    out = V.extract_image_context(
        p, client=client, model="m", domain_prompt="p",
    )
    # Top-level confidence clamped to 1.0
    assert out["confidence"] == 1.0
    # Per-structure confidence clamped + unnamed entries dropped
    structs = out["identified_structures"]
    assert len(structs) == 2
    assert structs[0]["name"] == "valid"
    assert structs[0]["confidence"] == 1.0
    assert structs[1]["name"] == "negative"
    assert structs[1]["confidence"] == 0.0


def test_extract_unknown_image_type_falls_back_to_other(tmp_path: Path):
    p = _png(tmp_path)
    response = """{
  "identified_structures": [{"name": "x", "confidence": 0.5}],
  "image_type": "totally_fake_type",
  "description": "x",
  "best_topic_guess": "x",
  "confidence": 0.5
}"""
    client = _client_returning(response)
    out = V.extract_image_context(
        p, client=client, model="m", domain_prompt="p",
    )
    assert out["image_type"] == "other"


def test_extract_llm_exception_returns_empty(tmp_path: Path):
    p = _png(tmp_path)
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("Bedrock down")
    out = V.extract_image_context(
        p, client=client, model="m", domain_prompt="p",
    )
    assert out["confidence"] == 0.0
    assert "RuntimeError" in out.get("_error", "")


def test_extract_unparseable_response_returns_empty_with_raw(tmp_path: Path):
    p = _png(tmp_path)
    client = _client_returning("this is not JSON at all")
    out = V.extract_image_context(
        p, client=client, model="m", domain_prompt="p",
    )
    assert out["confidence"] == 0.0
    assert "json parse" in out.get("_error", "")
    assert "_raw" in out
