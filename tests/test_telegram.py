"""Tests for Telegram adapter utilities."""

from __future__ import annotations

from qanot.telegram import _md_to_html, _split_text


class TestMdToHtml:
    def test_bold(self):
        assert "<b>hello</b>" in _md_to_html("**hello**")

    def test_inline_code(self):
        assert "<code>foo</code>" in _md_to_html("`foo`")

    def test_code_block(self):
        result = _md_to_html("```python\nprint('hi')\n```")
        assert "<pre>" in result
        assert "print" in result

    def test_heading(self):
        result = _md_to_html("## Section Title")
        assert "<b>Section Title</b>" in result

    def test_html_escaping(self):
        result = _md_to_html("x < y & z > w")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_horizontal_rule(self):
        result = _md_to_html("---")
        assert "━" in result


class TestSplitText:
    def test_short_text(self):
        assert _split_text("hello", limit=100) == ["hello"]

    def test_splits_on_newline(self):
        text = "line1\nline2\nline3"
        chunks = _split_text(text, limit=10)
        assert len(chunks) >= 2
        # All content preserved
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")

    def test_no_newline_fallback(self):
        text = "a" * 20
        chunks = _split_text(text, limit=10)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 10
        assert chunks[1] == "a" * 10
