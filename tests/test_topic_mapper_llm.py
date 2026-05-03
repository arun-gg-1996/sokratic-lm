"""
tests/test_topic_mapper_llm.py
──────────────────────────────
Tests for retrieval/topic_mapper_llm.py (L9 — single-Haiku-call topic mapper).

Coverage:
  * Routing thresholds (.route_decision()) match the L9 spec table.
  * TOC block builder joins topic_index + raptor summaries correctly.
  * Abbreviations block formatting + missing-file fallback.
  * map_topic() happy path with mocked Anthropic-style client.
  * Resilience: bad JSON, no JSON, LLM exception, missing top_matches.
  * Caching of TOC + abbreviation blocks across calls.
  * Per-domain config resolution refuses to silently fall back.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from retrieval.topic_mapper_llm import (
    BORDERLINE_HIGH_MIN,
    BORDERLINE_LOW_MIN,
    STRONG_MIN_CONFIDENCE,
    TopicMapperResult,
    TopicMatchCandidate,
    build_abbreviations_block,
    build_prompt,
    build_toc_block,
    clear_caches,
    map_topic,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_caches():
    clear_caches()
    yield
    clear_caches()


@pytest.fixture
def topic_index_path(tmp_path: Path) -> Path:
    p = tmp_path / "topic_index.json"
    p.write_text(json.dumps([
        {"chapter": "Muscle Tissue", "chapter_num": 10,
         "section": "Skeletal Muscle", "subsection": "Sarcomere Structure",
         "display_label": "Sarcomere Structure"},
        {"chapter": "Muscle Tissue", "chapter_num": 10,
         "section": "Skeletal Muscle", "subsection": "Sliding Filament Theory",
         "display_label": "Sliding Filaments"},
        {"chapter": "The Cardiovascular System: The Heart", "chapter_num": 19,
         "section": "Cardiac Muscle and Electrical Activity", "subsection": "SA Node",
         "display_label": "SA Node Pacemaker"},
    ]))
    return p


@pytest.fixture
def raptor_summaries_path(tmp_path: Path) -> Path:
    p = tmp_path / "raptor.jsonl"
    rows = [
        {"chapter": "Muscle Tissue", "section": "Skeletal Muscle",
         "subsection": "Sarcomere Structure",
         "summary": "Sarcomeres are the contractile units of muscle fibers."},
        {"chapter": "The Cardiovascular System: The Heart",
         "section": "Cardiac Muscle and Electrical Activity",
         "subsection": "SA Node",
         "summary": "SA node initiates the heartbeat as the primary pacemaker."},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


@pytest.fixture
def abbrevs_path(tmp_path: Path) -> Path:
    p = tmp_path / "curated_abbrevs.json"
    p.write_text(json.dumps({
        "abbreviations": [
            {"short": "SA node", "expansion": "sinoatrial node", "context": "cardiac"},
            {"short": "ATP", "expansion": "adenosine triphosphate", "context": "metabolism"},
        ]
    }))
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Mock Anthropic client (matches the .messages.create API surface)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _MockContent:
    text: str


@dataclass
class _MockUsage:
    input_tokens: int = 1000
    output_tokens: int = 50
    cache_read_input_tokens: int = 0


@dataclass
class _MockResponse:
    content: list
    usage: _MockUsage


class MockAnthropicClient:
    def __init__(self, response_text: str = "", raise_exc: Exception | None = None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.last_messages = None
        self.last_model = None

    @property
    def messages(self):
        return self

    def create(self, model, max_tokens, temperature, messages):
        self.last_model = model
        self.last_messages = messages
        if self.raise_exc:
            raise self.raise_exc
        return _MockResponse(
            content=[_MockContent(text=self.response_text)],
            usage=_MockUsage(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Routing thresholds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict,confidence,expected", [
    ("strong",     0.95, "lock_immediately"),
    ("strong",     0.85, "lock_immediately"),
    ("strong",     0.84, "refuse_with_starter_cards"),  # below STRONG_MIN
    ("borderline", 0.85, "confirm_and_lock"),
    ("borderline", 0.70, "confirm_and_lock"),
    ("borderline", 0.69, "show_top_matches"),
    ("borderline", 0.50, "show_top_matches"),
    ("borderline", 0.49, "refuse_with_starter_cards"),
    ("none",       0.10, "refuse_with_starter_cards"),
    ("none",       0.99, "refuse_with_starter_cards"),  # verdict=none always refuses
])
def test_route_decision(verdict, confidence, expected):
    r = TopicMapperResult(
        query="x",
        verdict=verdict,
        confidence=confidence,
        student_intent="topic_request",
        deferred_question=None,
    )
    assert r.route_decision() == expected


def test_threshold_constants_match_spec():
    assert STRONG_MIN_CONFIDENCE == 0.85
    assert BORDERLINE_HIGH_MIN == 0.70
    assert BORDERLINE_LOW_MIN == 0.50


# ─────────────────────────────────────────────────────────────────────────────
# TOC + abbreviations block builders
# ─────────────────────────────────────────────────────────────────────────────

def test_toc_block_includes_all_three_levels(topic_index_path, raptor_summaries_path):
    block = build_toc_block(topic_index_path, raptor_summaries_path, use_cache=False)
    assert "Muscle Tissue > Skeletal Muscle > Sarcomere Structure" in block
    assert "display_label: Sarcomere Structure" in block
    assert "Sarcomeres are the contractile units" in block
    # Subsection without summary still appears (summary line is empty)
    assert "Sliding Filament Theory" in block


def test_toc_block_truncates_long_summaries(tmp_path):
    ti = tmp_path / "ti.json"
    ti.write_text(json.dumps([
        {"chapter": "X", "section": "Y", "subsection": "Z", "display_label": "Z"}
    ]))
    rs = tmp_path / "rs.jsonl"
    rs.write_text(json.dumps({"chapter": "X", "section": "Y", "subsection": "Z",
                              "summary": "A" * 500}))
    block = build_toc_block(ti, rs, use_cache=False)
    # 220-char cap → 217 chars + "..."
    assert "AAAAAAA..." in block
    assert "A" * 250 not in block


def test_abbreviations_block_format(abbrevs_path):
    block = build_abbreviations_block(abbrevs_path, use_cache=False)
    assert "SA node = sinoatrial node [cardiac]" in block
    assert "ATP = adenosine triphosphate [metabolism]" in block


def test_abbreviations_block_missing_file_returns_empty(tmp_path):
    block = build_abbreviations_block(tmp_path / "nope.json", use_cache=False)
    assert block == ""


def test_toc_block_caches_by_path(topic_index_path, raptor_summaries_path):
    # First call fills cache
    a = build_toc_block(topic_index_path, raptor_summaries_path)
    # Mutate underlying file
    topic_index_path.write_text(json.dumps([]))
    # Cached call still returns original content
    b = build_toc_block(topic_index_path, raptor_summaries_path)
    assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# map_topic() happy paths
# ─────────────────────────────────────────────────────────────────────────────

STRONG_RESPONSE = json.dumps({
    "verdict": "strong",
    "confidence": 0.92,
    "student_intent": "topic_request",
    "deferred_question": None,
    "top_matches": [
        {"path": "The Cardiovascular System: The Heart > Cardiac Muscle and Electrical Activity > SA Node",
         "confidence": 0.92,
         "rationale": "User asked about SA node directly."},
    ],
})


def test_map_topic_strong_match(topic_index_path, raptor_summaries_path, abbrevs_path):
    client = MockAnthropicClient(response_text=STRONG_RESPONSE)
    result = map_topic(
        "tell me about the SA node",
        client=client,
        model="haiku",
        topic_index_path=topic_index_path,
        raptor_summaries_path=raptor_summaries_path,
        curated_abbrevs_path=abbrevs_path,
        domain_name="human anatomy",
        domain_short="anatomy",
    )
    assert result.verdict == "strong"
    assert result.confidence == 0.92
    assert result.student_intent == "topic_request"
    assert result.deferred_question is None
    assert len(result.top_matches) == 1
    assert result.top_matches[0].path.endswith("> SA Node")
    assert result.route_decision() == "lock_immediately"
    assert result.input_tokens == 1000


def test_map_topic_borderline_high(topic_index_path, raptor_summaries_path, abbrevs_path):
    client = MockAnthropicClient(response_text=json.dumps({
        "verdict": "borderline", "confidence": 0.78,
        "top_matches": [
            {"path": "Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
             "confidence": 0.78, "rationale": "Sarcomere is plausibly what they meant."},
        ],
    }))
    result = map_topic("how do muscles contract",
                       client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.route_decision() == "confirm_and_lock"


def test_map_topic_borderline_low_returns_top_matches(topic_index_path,
                                                      raptor_summaries_path,
                                                      abbrevs_path):
    client = MockAnthropicClient(response_text=json.dumps({
        "verdict": "borderline", "confidence": 0.55,
        "top_matches": [
            {"path": "Muscle Tissue > Skeletal Muscle > Sliding Filament Theory",
             "confidence": 0.55, "rationale": "Maybe muscle physiology"},
            {"path": "Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
             "confidence": 0.45, "rationale": "Less likely"},
        ],
    }))
    result = map_topic("muscle stuff",
                       client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.route_decision() == "show_top_matches"
    assert len(result.top_matches) == 2


def test_map_topic_none(topic_index_path, raptor_summaries_path, abbrevs_path):
    client = MockAnthropicClient(response_text=json.dumps({
        "verdict": "none", "confidence": 0.1, "top_matches": []
    }))
    result = map_topic("what's the weather",
                       client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.verdict == "none"
    assert result.route_decision() == "refuse_with_starter_cards"


# ─────────────────────────────────────────────────────────────────────────────
# Resilience
# ─────────────────────────────────────────────────────────────────────────────

def test_map_topic_handles_llm_exception(topic_index_path, raptor_summaries_path, abbrevs_path):
    client = MockAnthropicClient(raise_exc=ConnectionError("AWS down"))
    result = map_topic("anything",
                       client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.verdict == "none"
    assert result.confidence == 0.0
    assert "llm_error" in result.raw_response
    assert result.route_decision() == "refuse_with_starter_cards"


def test_map_topic_handles_garbage_response(topic_index_path, raptor_summaries_path, abbrevs_path):
    client = MockAnthropicClient(response_text="this is not JSON at all")
    result = map_topic("anything",
                       client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.verdict == "none"
    assert "json_parse_error" in result.raw_response


def test_map_topic_handles_markdown_fenced_json(topic_index_path, raptor_summaries_path, abbrevs_path):
    fenced = "```json\n" + STRONG_RESPONSE + "\n```"
    client = MockAnthropicClient(response_text=fenced)
    result = map_topic("SA node",
                       client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.verdict == "strong"


def test_map_topic_drops_invalid_paths(topic_index_path, raptor_summaries_path, abbrevs_path):
    """Paths missing the canonical ' > ' separator must be filtered out."""
    client = MockAnthropicClient(response_text=json.dumps({
        "verdict": "borderline", "confidence": 0.6,
        "top_matches": [
            {"path": "garbage no separator", "confidence": 0.6, "rationale": "x"},
            {"path": "Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
             "confidence": 0.6, "rationale": "ok"},
        ],
    }))
    result = map_topic("test", client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert len(result.top_matches) == 1
    assert "Sarcomere" in result.top_matches[0].path


def test_map_topic_clamps_confidence_to_unit_interval(topic_index_path,
                                                       raptor_summaries_path,
                                                       abbrevs_path):
    client = MockAnthropicClient(response_text=json.dumps({
        "verdict": "strong", "confidence": 1.7,  # over 1.0
        "top_matches": [
            {"path": "X > Y > Z", "confidence": -0.5, "rationale": "x"},  # negative
        ],
    }))
    result = map_topic("test", client=client, model="haiku",
                       topic_index_path=topic_index_path,
                       raptor_summaries_path=raptor_summaries_path,
                       curated_abbrevs_path=abbrevs_path,
                       domain_name="x", domain_short="x")
    assert result.confidence == 1.0
    # path missing " > " separator? no, "X > Y > Z" has it → clamped to 0.0
    assert result.top_matches[0].confidence == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder smoke
# ─────────────────────────────────────────────────────────────────────────────

def test_build_prompt_includes_query_and_toc():
    prompt = build_prompt(
        "what is the SA node",
        domain_name="human anatomy",
        domain_short="anatomy",
        toc_block="- Ch 1 > Sec > Sub\n  display_label: x\n  summary: y",
        abbrevs_block="  - SA node = sinoatrial node",
    )
    assert "human anatomy" in prompt
    assert "what is the SA node" in prompt
    assert "Ch 1 > Sec > Sub" in prompt
    assert "SA node = sinoatrial node" in prompt
    assert "Output JSON only:" in prompt


def test_build_prompt_omits_abbrevs_section_when_empty():
    prompt = build_prompt(
        "x", domain_name="X", domain_short="x",
        toc_block="...", abbrevs_block="",
    )
    assert "COMMON ABBREVIATIONS" not in prompt
