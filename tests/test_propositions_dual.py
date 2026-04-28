"""
tests/test_propositions_dual.py
-------------------------------
Unit tests for the dual-task proposition extraction module (B.5).

These tests do NOT make real API calls. The Anthropic client is mocked.
A separate pilot run (B.8) on 50 real chunks validates end-to-end behavior
against the live API.

Coverage:
  - prompt assembly (cache_control marker, optional source suffix)
  - response parsing (clean JSON, fenced JSON, malformed, missing fields,
    cap enforcement, non-string filtering)
  - extract_dual_task happy path with mocked client
  - extract_dual_task with API error (no crash, returns error string)
  - extract_dual_task with malformed JSON (no crash, returns error string)
  - usage_callback fires per response
  - run_dual_task_batch preserves chunk order across async gather
  - run_dual_task_batch progress_callback fires for every chunk
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ingestion.core.propositions_dual import (
    DEFAULT_MODEL,
    PROPOSITION_CAP,
    SYSTEM_PROMPT_BODY,
    DualTaskResult,
    build_cached_system,
    extract_dual_task,
    parse_response,
    run_dual_task_batch,
)


# ── build_cached_system ─────────────────────────────────────────────────────

class TestBuildCachedSystem:
    def test_default_block_is_cached(self):
        blocks = build_cached_system()
        assert isinstance(blocks, list) and len(blocks) == 1
        b = blocks[0]
        assert b["type"] == "text"
        assert b["cache_control"] == {"type": "ephemeral"}
        assert "TASK 1" in b["text"]
        assert "TASK 2" in b["text"]
        assert "EXAMPLE" in b["text"]

    def test_extra_suffix_is_appended(self):
        blocks = build_cached_system("Use British spellings.")
        body = blocks[0]["text"]
        assert SYSTEM_PROMPT_BODY in body
        assert "ADDITIONAL DOMAIN-SPECIFIC INSTRUCTIONS" in body
        assert "Use British spellings." in body

    def test_empty_suffix_no_extra_section(self):
        blocks = build_cached_system("   ")
        assert "ADDITIONAL DOMAIN-SPECIFIC INSTRUCTIONS" not in blocks[0]["text"]


# ── parse_response ──────────────────────────────────────────────────────────

class TestParseResponse:
    def test_clean_json_object(self):
        payload = json.dumps({
            "cleaned_text": "Hello world.",
            "propositions": ["Hello world."],
        })
        cleaned, props, err = parse_response(payload)
        assert err is None
        assert cleaned == "Hello world."
        assert props == ["Hello world."]

    def test_fenced_json(self):
        payload = (
            "```json\n"
            '{"cleaned_text": "ok.", "propositions": ["ok."]}\n'
            "```"
        )
        cleaned, props, err = parse_response(payload)
        assert err is None
        assert cleaned == "ok."

    def test_extra_prose_before_json(self):
        payload = (
            "Sure, here is the result:\n"
            '{"cleaned_text": "abc.", "propositions": ["abc."]}'
        )
        cleaned, props, err = parse_response(payload)
        assert err is None
        assert cleaned == "abc."

    def test_malformed_json_returns_error(self):
        cleaned, props, err = parse_response('{"cleaned_text": "abc", oops')
        assert cleaned is None
        assert props == []
        assert err is not None and "json parse error" in err

    def test_missing_cleaned_text_returns_error(self):
        cleaned, props, err = parse_response('{"propositions": ["a", "b"]}')
        assert err is not None and "cleaned_text" in err

    def test_missing_propositions_returns_error(self):
        cleaned, props, err = parse_response('{"cleaned_text": "abc"}')
        assert err is not None and "propositions" in err

    def test_cap_truncates_proposition_list(self):
        many = ["fact " + str(i) for i in range(50)]
        payload = json.dumps({"cleaned_text": "x", "propositions": many})
        cleaned, props, err = parse_response(payload)
        assert err is None
        assert len(props) == PROPOSITION_CAP

    def test_non_string_props_filtered_out(self):
        payload = json.dumps({
            "cleaned_text": "x.",
            "propositions": ["good.", 42, None, "", "  ", "also good."],
        })
        cleaned, props, err = parse_response(payload)
        assert err is None
        assert props == ["good.", "also good."]

    def test_empty_response_returns_error(self):
        _, _, err = parse_response("")
        assert err == "empty response"


# ── Mocking infrastructure ──────────────────────────────────────────────────

def _make_mock_response(text: str, *, in_tok=120, out_tok=80, cache_read=0, cache_create=0):
    """Build a MagicMock that mimics anthropic.types.Message."""
    block = SimpleNamespace(text=text)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
        ),
    )


def _make_mock_client(response_or_responses):
    """Build a mock AsyncAnthropic. response_or_responses can be a single
    response (every call returns it) or a list (one per call, in order)."""
    client = MagicMock()
    if isinstance(response_or_responses, list):
        client.messages = MagicMock()
        client.messages.create = AsyncMock(side_effect=response_or_responses)
    else:
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=response_or_responses)
    return client


# ── extract_dual_task ───────────────────────────────────────────────────────

class TestExtractDualTask:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_resp = _make_mock_response(
            json.dumps({
                "cleaned_text": "The heart pumps blood.",
                "propositions": ["The heart pumps blood."],
            })
        )
        client = _make_mock_client(mock_resp)
        sem = asyncio.Semaphore(1)
        chunk = {"chunk_id": "c1", "text": "The heart pumps blood. (Figure 1.1)"}
        result = await extract_dual_task(
            client, chunk, sem,
            model=DEFAULT_MODEL,
            cached_system=build_cached_system(),
        )
        assert result.error is None
        assert result.chunk_id == "c1"
        assert result.cleaned_text == "The heart pumps blood."
        assert len(result.propositions) == 1
        prop = result.propositions[0]
        assert prop["text"] == "The heart pumps blood."
        assert prop["parent_chunk_id"] == "c1"
        assert prop["parent_chunk_text"] == "The heart pumps blood."
        assert "proposition_id" in prop
        assert result.usage["input_tokens"] == 120
        assert result.usage["output_tokens"] == 80

    @pytest.mark.asyncio
    async def test_empty_chunk_text_returns_error(self):
        client = _make_mock_client(_make_mock_response("{}"))
        sem = asyncio.Semaphore(1)
        result = await extract_dual_task(
            client, {"chunk_id": "c1", "text": "   "}, sem,
            cached_system=build_cached_system(),
        )
        assert result.error == "empty chunk text"
        # No API call should have happened
        assert client.messages.create.call_count == 0

    @pytest.mark.asyncio
    async def test_api_error_does_not_crash(self):
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("rate limit"))
        sem = asyncio.Semaphore(1)
        result = await extract_dual_task(
            client, {"chunk_id": "c1", "text": "real text"}, sem,
            cached_system=build_cached_system(),
        )
        assert result.error is not None
        assert "rate limit" in result.error
        # cleaned_text falls through to original chunk text on error
        assert result.cleaned_text == "real text"

    @pytest.mark.asyncio
    async def test_malformed_json_is_isolated(self):
        client = _make_mock_client(_make_mock_response("not json at all"))
        sem = asyncio.Semaphore(1)
        result = await extract_dual_task(
            client, {"chunk_id": "c1", "text": "real text"}, sem,
            cached_system=build_cached_system(),
        )
        assert result.error is not None
        assert "json parse error" in result.error
        # Usage still captured (we paid for the call)
        assert result.usage is not None
        assert result.usage["input_tokens"] == 120

    @pytest.mark.asyncio
    async def test_usage_callback_fires(self):
        mock_resp = _make_mock_response(
            json.dumps({"cleaned_text": "x.", "propositions": ["x."]})
        )
        client = _make_mock_client(mock_resp)
        sem = asyncio.Semaphore(1)
        captured = []
        await extract_dual_task(
            client, {"chunk_id": "c1", "text": "x"}, sem,
            cached_system=build_cached_system(),
            usage_callback=lambda u: captured.append(u),
        )
        assert len(captured) == 1
        assert captured[0]["input_tokens"] == 120

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_crash(self):
        mock_resp = _make_mock_response(
            json.dumps({"cleaned_text": "x.", "propositions": ["x."]})
        )
        client = _make_mock_client(mock_resp)
        sem = asyncio.Semaphore(1)

        def boom(_):
            raise RuntimeError("callback bug")

        result = await extract_dual_task(
            client, {"chunk_id": "c1", "text": "x"}, sem,
            cached_system=build_cached_system(),
            usage_callback=boom,
        )
        # Result is still successful
        assert result.error is None


# ── run_dual_task_batch ─────────────────────────────────────────────────────

class TestRunDualTaskBatch:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_list(self):
        out = await run_dual_task_batch([])
        assert out == []

    @pytest.mark.asyncio
    async def test_preserves_input_order(self):
        responses = [
            _make_mock_response(json.dumps({"cleaned_text": "a.", "propositions": ["a."]})),
            _make_mock_response(json.dumps({"cleaned_text": "b.", "propositions": ["b."]})),
            _make_mock_response(json.dumps({"cleaned_text": "c.", "propositions": ["c."]})),
        ]
        client = _make_mock_client(responses)
        chunks = [
            {"chunk_id": "c1", "text": "alpha"},
            {"chunk_id": "c2", "text": "beta"},
            {"chunk_id": "c3", "text": "gamma"},
        ]
        results = await run_dual_task_batch(chunks, client=client, concurrency=2)
        assert len(results) == 3
        assert [r.chunk_id for r in results] == ["c1", "c2", "c3"]

    @pytest.mark.asyncio
    async def test_progress_callback_fires_for_every_chunk(self):
        # Same response for all three calls
        client = _make_mock_client(
            _make_mock_response(json.dumps({"cleaned_text": "x.", "propositions": ["x."]}))
        )
        progress_log: list[tuple[int, int]] = []
        chunks = [{"chunk_id": f"c{i}", "text": f"t{i}"} for i in range(5)]
        await run_dual_task_batch(
            chunks, client=client, concurrency=2,
            progress_callback=lambda done, total: progress_log.append((done, total)),
        )
        assert len(progress_log) == 5
        # Total is constant; done counter monotone non-decreasing
        for done, total in progress_log:
            assert total == 5
            assert 1 <= done <= 5
        assert max(d for d, _ in progress_log) == 5

    @pytest.mark.asyncio
    async def test_usage_callback_fires_for_every_call(self):
        client = _make_mock_client(
            _make_mock_response(
                json.dumps({"cleaned_text": "x.", "propositions": ["x."]}),
                in_tok=42, out_tok=7,
            )
        )
        captured: list[dict] = []
        chunks = [{"chunk_id": f"c{i}", "text": "t"} for i in range(4)]
        await run_dual_task_batch(
            chunks, client=client,
            usage_callback=lambda u: captured.append(u),
        )
        assert len(captured) == 4
        for u in captured:
            assert u["input_tokens"] == 42
            assert u["output_tokens"] == 7
