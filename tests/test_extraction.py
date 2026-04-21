"""Tests for the pre-turn image extraction pipeline.

We stub the Anthropic client so no network calls happen. Focus on:
  - JSON parsing resilience (fenced, malformed, partial)
  - Coercion / validation into ExtractionResult
  - Timeout, empty response, network-exception handling
  - Memory persistence: write path, dedup by image hash
  - Batch concurrency
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from qanot.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    ExtractionResult,
    ImageExtractor,
    _coerce_result,
    _hash_image_block,
    _parse_json_safe,
    _strip_code_fences,
    extract_images,
    persist_extraction,
)


# ── Fixtures ─────────────────────────────────────────────────────


def _fake_image_block(data: str = "fakedata") -> dict:
    """Minimal Anthropic-style image block with base64 payload."""
    b64 = base64.b64encode(data.encode()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": b64,
        },
    }


class _StubText:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _StubMessage:
    def __init__(self, text: str) -> None:
        self.content = [_StubText(text)]


class _StubMessages:
    def __init__(self, behavior) -> None:
        self._behavior = behavior
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs) -> _StubMessage | Any:
        self.calls.append(kwargs)
        result = self._behavior
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        if isinstance(result, _StubMessage):
            return result
        return _StubMessage(result)


class _StubClient:
    """Quacks like anthropic.AsyncAnthropic for the extractor's purposes."""

    def __init__(self, behavior) -> None:
        self.messages = _StubMessages(behavior)


# ── _strip_code_fences ───────────────────────────────────────────


def test_strip_code_fences_json_variant():
    text = '```json\n{"x": 1}\n```'
    assert _strip_code_fences(text) == '{"x": 1}'


def test_strip_code_fences_plain_variant():
    text = '```\n{"x": 1}\n```'
    assert _strip_code_fences(text) == '{"x": 1}'


def test_strip_code_fences_no_fences_unchanged():
    assert _strip_code_fences('{"x": 1}') == '{"x": 1}'


# ── _parse_json_safe ─────────────────────────────────────────────


def test_parse_json_happy_path():
    parsed, err = _parse_json_safe('{"doc_type": "receipt"}')
    assert err is None
    assert parsed == {"doc_type": "receipt"}


def test_parse_json_with_fences():
    parsed, err = _parse_json_safe('```json\n{"a": 1}\n```')
    assert err is None
    assert parsed == {"a": 1}


def test_parse_json_malformed_returns_error():
    parsed, err = _parse_json_safe("{not valid json")
    assert parsed == {}
    assert err is not None
    assert "Malformed" in err


def test_parse_json_non_object_root_errors():
    parsed, err = _parse_json_safe('["a", "b"]')
    assert parsed == {}
    assert "not an object" in err


# ── _coerce_result ──────────────────────────────────────────────


def test_coerce_full_payload():
    raw = {
        "doc_type": "receipt",
        "title": "Korzinka 2026-04-21",
        "fields": {"vendor": "Korzinka", "total": "156000"},
        "entities": {
            "people": ["Alisher"],
            "amounts": [{"value": 156000, "currency": "UZS"}],
        },
        "raw_text": "Korzinka\nTotal: 156000",
        "confidence": 0.92,
        "warnings": ["bottom blurry"],
    }
    r = _coerce_result(raw, "abc123", "image/jpeg")
    assert r.doc_type == "receipt"
    assert r.fields["vendor"] == "Korzinka"
    assert r.entities["amounts"][0]["value"] == 156000
    assert r.confidence == 0.92
    assert r.warnings == ["bottom blurry"]
    assert r.image_hash == "abc123"


def test_coerce_missing_fields_defaults():
    r = _coerce_result({}, "h", "image/png")
    assert r.doc_type == "other"
    assert r.title == ""
    assert r.fields == {}
    assert r.entities == {}
    assert r.confidence == 0.0


def test_coerce_clamps_confidence_high():
    r = _coerce_result({"confidence": 5}, "h", "image/jpeg")
    assert r.confidence == 1.0


def test_coerce_clamps_confidence_low():
    r = _coerce_result({"confidence": -0.5}, "h", "image/jpeg")
    assert r.confidence == 0.0


def test_coerce_tolerates_wrong_types():
    # fields as a string (Haiku going off-schema) should degrade to empty dict
    r = _coerce_result(
        {"fields": "not a dict", "entities": [], "warnings": None},
        "h", "image/jpeg",
    )
    assert r.fields == {}
    assert r.entities == {}
    assert r.warnings == []


# ── _hash_image_block ────────────────────────────────────────────


def test_hash_same_data_same_hash():
    b1 = _fake_image_block("hello")
    b2 = _fake_image_block("hello")
    assert _hash_image_block(b1) == _hash_image_block(b2)


def test_hash_different_data_different_hash():
    b1 = _fake_image_block("hello")
    b2 = _fake_image_block("world")
    assert _hash_image_block(b1) != _hash_image_block(b2)


def test_hash_missing_data_returns_empty():
    assert _hash_image_block({"type": "image", "source": {}}) == ""
    assert _hash_image_block({}) == ""


# ── ImageExtractor.extract ───────────────────────────────────────


def test_extract_happy_path():
    payload = json.dumps({
        "doc_type": "receipt",
        "title": "Korzinka",
        "fields": {"total": "156000"},
        "entities": {"amounts": [{"value": 156000, "currency": "UZS"}]},
        "raw_text": "Korzinka\nTotal 156000",
        "confidence": 0.9,
        "warnings": [],
    })
    client = _StubClient(payload)
    extractor = ImageExtractor(client)
    result = asyncio.run(extractor.extract(_fake_image_block()))
    assert result.ok
    assert result.doc_type == "receipt"
    assert result.fields["total"] == "156000"
    assert result.confidence == 0.9
    # System prompt must be passed
    assert client.messages.calls[0]["system"] == EXTRACTION_SYSTEM_PROMPT


def test_extract_parses_fenced_json():
    payload = '```json\n{"doc_type": "invoice", "confidence": 0.8}\n```'
    client = _StubClient(payload)
    extractor = ImageExtractor(client)
    result = asyncio.run(extractor.extract(_fake_image_block()))
    assert result.ok
    assert result.doc_type == "invoice"


def test_extract_malformed_json_falls_back_to_raw_text():
    payload = "I'm sorry I can't do JSON but here is what I see: Korzinka, 156000"
    client = _StubClient(payload)
    extractor = ImageExtractor(client)
    result = asyncio.run(extractor.extract(_fake_image_block()))
    # Falls back: doc_type=other, raw_text populated, error set
    assert result.ok is False
    assert result.error is not None
    assert "Korzinka" in result.raw_text
    assert result.doc_type == "other"


def test_extract_empty_response_errors():
    client = _StubClient("")
    extractor = ImageExtractor(client)
    result = asyncio.run(extractor.extract(_fake_image_block()))
    assert not result.ok
    assert result.error == "empty response"


def test_extract_network_exception_caught():
    client = _StubClient(RuntimeError("boom"))
    extractor = ImageExtractor(client)
    result = asyncio.run(extractor.extract(_fake_image_block()))
    assert not result.ok
    assert "boom" in result.error


def test_extract_timeout_caught():
    async def slow():
        await asyncio.sleep(10)
        return _StubMessage('{"doc_type": "receipt"}')

    client = _StubClient(lambda: slow())
    # Slow mock returns a coroutine we can't await in create — patch differently:
    # Use a behavior that sleeps inside an async fn.
    class SlowMessages:
        async def create(self, **kwargs):
            await asyncio.sleep(10)
            return _StubMessage('{"doc_type": "receipt"}')

    class SlowClient:
        messages = SlowMessages()

    extractor = ImageExtractor(SlowClient(), timeout_seconds=0.05)
    result = asyncio.run(extractor.extract(_fake_image_block()))
    assert not result.ok
    assert "timed out" in result.error


# ── persist_extraction ───────────────────────────────────────────


def test_persist_writes_markdown_file(tmp_path):
    r = ExtractionResult(
        doc_type="receipt",
        title="test",
        fields={"vendor": "Korzinka"},
        entities={"amounts": [{"value": 100, "currency": "UZS"}]},
        raw_text="raw",
        confidence=0.8,
        image_hash="hash1",
        image_media_type="image/jpeg",
        created_at="2026-04-21T14-40-00+00:00",
    )
    rel = persist_extraction(r, tmp_path)
    assert rel is not None
    assert rel.startswith("memory/extractions/")
    assert rel.endswith("_hash1.md")
    content = (tmp_path / rel).read_text(encoding="utf-8")
    assert "Korzinka" in content
    assert "doc_type" not in content or "receipt" in content
    assert r.source_path == rel


def test_persist_dedupes_by_hash(tmp_path):
    r1 = ExtractionResult(
        image_hash="same",
        fields={"v": "1"},
        created_at="2026-04-21T10-00-00",
    )
    r2 = ExtractionResult(
        image_hash="same",
        fields={"v": "2"},  # different content, same hash
        created_at="2026-04-21T11-00-00",
    )
    p1 = persist_extraction(r1, tmp_path)
    p2 = persist_extraction(r2, tmp_path)
    # Second call must return the existing file path — no duplicate write
    assert p1 == p2
    files = list((tmp_path / "memory" / "extractions").glob("*.md"))
    assert len(files) == 1
    # First write wins (the one with v=1)
    assert "v:** 1" in files[0].read_text(encoding="utf-8")


# ── extract_images batch ─────────────────────────────────────────


def test_extract_images_runs_in_parallel_and_persists(tmp_path):
    payloads = [
        json.dumps({"doc_type": "receipt", "fields": {"id": "1"}, "confidence": 0.9}),
        json.dumps({"doc_type": "business_card", "fields": {"id": "2"}, "confidence": 0.8}),
    ]
    call_count = {"n": 0}

    def next_payload():
        i = call_count["n"]
        call_count["n"] += 1
        return payloads[i]

    client = _StubClient(next_payload)
    extractor = ImageExtractor(client)
    images = [_fake_image_block("a"), _fake_image_block("b")]
    results = asyncio.run(extract_images(extractor, images, tmp_path))
    assert len(results) == 2
    types = {r.doc_type for r in results}
    assert types == {"receipt", "business_card"}
    assert call_count["n"] == 2
    # Both persisted
    files = list((tmp_path / "memory" / "extractions").glob("*.md"))
    assert len(files) == 2


def test_extract_images_empty_list_returns_empty(tmp_path):
    client = _StubClient("{}")
    extractor = ImageExtractor(client)
    results = asyncio.run(extract_images(extractor, [], tmp_path))
    assert results == []


# ── ExtractionResult formatting ──────────────────────────────────


def test_context_markdown_skips_empty_sections():
    r = ExtractionResult(
        doc_type="receipt",
        fields={"vendor": "Korzinka"},
        confidence=0.9,
    )
    md = r.to_context_markdown()
    assert "doc_type: receipt" in md
    assert "vendor: Korzinka" in md
    # Empty entities/warnings should NOT appear
    assert "people" not in md
    assert "warnings" not in md


def test_memory_markdown_full():
    r = ExtractionResult(
        doc_type="invoice",
        title="Acme invoice",
        fields={"total": "5000"},
        entities={
            "people": ["Ivan"],
            "amounts": [{"value": 5000, "currency": "USD"}],
        },
        raw_text="line1\nline2",
        confidence=0.85,
        warnings=["partial"],
        image_hash="h",
        image_media_type="image/png",
        created_at="2026-04-21T14:00:00",
    )
    md = r.to_memory_markdown()
    assert "# Rasm extraction" in md
    assert "**Doc type:** invoice" in md
    assert "Acme invoice" in md
    assert "**total:** 5000" in md
    assert "Ivan" in md
    assert "5000 USD" in md
    assert "line1" in md
    assert "partial" in md
