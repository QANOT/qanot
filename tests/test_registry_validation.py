"""Tests for the registry's memo-validator hook.

Verifies that ``ToolRegistry.execute()`` runs the validator on declared
fields, rewrites in place, swallows failures, and never blocks the
tool when validation goes wrong.
"""

from __future__ import annotations

import asyncio
import json
import pytest

from qanot.memos.validator import ValidationResult
from qanot.registry import ToolRegistry, _read_nested, _write_nested


def _run(coro):
    return asyncio.run(coro)


# ─── nested-path helpers ────────────────────────────────────────


class TestNestedPath:
    def test_read_flat(self):
        assert _read_nested({"a": 1}, "a") == 1

    def test_read_dotted(self):
        assert _read_nested({"props": {"title": "x"}}, "props.title") == "x"

    def test_read_missing_raises(self):
        with pytest.raises(KeyError):
            _read_nested({"a": 1}, "b")

    def test_read_into_non_dict_raises(self):
        with pytest.raises(TypeError):
            _read_nested({"a": "string"}, "a.b")

    def test_write_flat(self):
        d = {}
        _write_nested(d, "a", 1)
        assert d == {"a": 1}

    def test_write_creates_intermediate(self):
        d = {}
        _write_nested(d, "a.b.c", "deep")
        assert d == {"a": {"b": {"c": "deep"}}}

    def test_write_preserves_siblings(self):
        d = {"a": {"x": 1}}
        _write_nested(d, "a.y", 2)
        assert d == {"a": {"x": 1, "y": 2}}


# ─── execute() validation hook ──────────────────────────────────


class StubRuntime:
    """Stand-in for MemoValidatorRuntime; returns predetermined results."""

    def __init__(self, *, rewrite_map: dict[str, str] | None = None,
                 raise_exc: Exception | None = None):
        self.rewrite_map = rewrite_map or {}
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []  # (text, field_context)

    async def __call__(self, text: str, *, field_context: str) -> ValidationResult:
        self.calls.append((text, field_context))
        if self.raise_exc is not None:
            raise self.raise_exc
        if text in self.rewrite_map:
            new = self.rewrite_map[text]
            return ValidationResult(
                original=text, verified=new, was_changed=True,
                violations=[f"rewrote {text} -> {new}"],
            )
        return ValidationResult(original=text, verified=text, was_changed=False)


def _make_registry_with_runtime(runtime: StubRuntime | None) -> ToolRegistry:
    r = ToolRegistry()

    async def factory():
        return runtime

    r.set_memo_validator(factory)
    return r


class TestExecuteValidation:
    def test_no_validate_fields_no_call(self):
        runtime = StubRuntime(rewrite_map={"x": "y"})
        r = _make_registry_with_runtime(runtime)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return json.dumps({"ok": True})

        r.register("plain", "d", {"type": "object", "properties": {}}, handler)
        result = _run(r.execute("plain", {"x": "x", "y": "y"}))
        assert json.loads(result) == {"ok": True}
        # Validator was not invoked because the tool didn't declare any
        # validate_fields.
        assert runtime.calls == []
        assert captured == {"x": "x", "y": "y"}

    def test_field_rewritten_before_handler(self):
        runtime = StubRuntime(rewrite_map={
            "Daily Entry — 2026": "14-may, 2026",
        })
        r = _make_registry_with_runtime(runtime)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return json.dumps({"ok": True})

        r.register(
            "notion_create_page", "d",
            {"type": "object", "properties": {
                "title": {"type": "string"},
            }},
            handler,
            validate_fields={"title": "Notion page title"},
        )
        _run(r.execute(
            "notion_create_page",
            {"title": "Daily Entry — 2026", "extra": "untouched"},
        ))
        # Validator was called with the original title.
        assert runtime.calls == [("Daily Entry — 2026", "Notion page title")]
        # Handler received the REWRITTEN title.
        assert captured["title"] == "14-may, 2026"
        assert captured["extra"] == "untouched"

    def test_compliant_field_not_rewritten(self):
        runtime = StubRuntime(rewrite_map={})  # no rewrites configured
        r = _make_registry_with_runtime(runtime)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return "ok"

        r.register(
            "notion_create_page", "d",
            {"type": "object", "properties": {"title": {"type": "string"}}},
            handler, validate_fields={"title": "title"},
        )
        _run(r.execute("notion_create_page", {"title": "14-may, 2026"}))
        # Validator was called but no rewrite happened.
        assert runtime.calls == [("14-may, 2026", "title")]
        assert captured["title"] == "14-may, 2026"

    def test_nested_field_rewritten(self):
        runtime = StubRuntime(rewrite_map={"old heading": "new heading"})
        r = _make_registry_with_runtime(runtime)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return "ok"

        r.register(
            "docx_create", "d",
            {"type": "object", "properties": {
                "properties": {"type": "object"},
            }},
            handler,
            validate_fields={"properties.heading": "DOCX heading"},
        )
        _run(r.execute(
            "docx_create",
            {"properties": {"heading": "old heading", "other": "x"}},
        ))
        assert captured["properties"]["heading"] == "new heading"
        assert captured["properties"]["other"] == "x"  # untouched sibling

    def test_validator_failure_passes_through(self):
        # If the validator runtime itself raises, the tool must STILL run
        # with the original input (no silent data drop).
        runtime = StubRuntime(raise_exc=RuntimeError("haiku down"))
        r = _make_registry_with_runtime(runtime)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return "ok"

        r.register(
            "t", "d",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            handler, validate_fields={"text": "text"},
        )
        _run(r.execute("t", {"text": "original"}))
        assert captured["text"] == "original"

    def test_no_runtime_factory_passes_through(self):
        r = ToolRegistry()
        # Never call set_memo_validator → factory is None.

        async def handler(p):
            return p["text"]

        r.register(
            "t", "d",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            handler, validate_fields={"text": "text"},
        )
        result = _run(r.execute("t", {"text": "passthrough"}))
        # Handler received the original value.
        assert result == "passthrough"

    def test_factory_returns_none_passes_through(self):
        # build_runtime returns None when no feedback memos in scope.
        r = ToolRegistry()

        async def factory():
            return None

        r.set_memo_validator(factory)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return "ok"

        r.register(
            "t", "d",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            handler, validate_fields={"text": "text"},
        )
        _run(r.execute("t", {"text": "passthrough"}))
        assert captured["text"] == "passthrough"

    def test_missing_field_skipped(self):
        # validate_fields names "title" but the input lacks it — the
        # validator must NOT crash; the tool runs with whatever it has.
        runtime = StubRuntime(rewrite_map={})
        r = _make_registry_with_runtime(runtime)
        captured: dict = {}

        async def handler(p):
            captured.update(p)
            return "ok"

        r.register(
            "t", "d",
            {"type": "object", "properties": {"title": {"type": "string"}}},
            handler, validate_fields={"title": "title"},
        )
        _run(r.execute("t", {"other_field": "x"}))
        assert runtime.calls == []  # no field to validate
        assert captured == {"other_field": "x"}

    def test_non_string_field_skipped(self):
        # The validator only makes sense for strings; numeric/dict fields
        # in validate_fields are silently skipped.
        runtime = StubRuntime(rewrite_map={})
        r = _make_registry_with_runtime(runtime)

        async def handler(p):
            return "ok"

        r.register(
            "t", "d",
            {"type": "object", "properties": {"n": {"type": "number"}}},
            handler, validate_fields={"n": "numeric field"},
        )
        _run(r.execute("t", {"n": 42}))
        assert runtime.calls == []
