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

    def test_table_with_bold_cells_no_nested_b_inside_pre(self):
        """Telegram's HTML parser rejects nested formatting inside <pre> —
        the 2026-05-21 regression where the second-chunk fallback dumped
        raw tags. Table cells with **bold** must not produce <b> inside
        the <pre> wrapper."""
        import re

        md = (
            "| Hafta | Mavzu |\n"
            "|---|---|\n"
            "| **Präteritum** | war, hatte |\n"
            "| **Konjunktiv II** | würde |\n"
        )
        out = _md_to_html(md)
        for block in re.findall(r"<pre>(.*?)</pre>", out, re.DOTALL):
            assert "<b>" not in block and "</b>" not in block, block
        # cell text itself must survive (only the markers are dropped)
        assert "Präteritum" in out
        assert "Konjunktiv II" in out

    def test_code_block_with_inner_markers_no_nested_tags(self):
        """Same contract for fenced code blocks: ** and ` markers inside
        the body must not become <b>/<code> tags nested in <pre>."""
        import re

        out = _md_to_html("```\nplain text with **bold** and `inline`\n```")
        for block in re.findall(r"<pre>(.*?)</pre>", out, re.DOTALL):
            assert "<b>" not in block and "<code>" not in block, block
        assert "bold" in out and "inline" in out


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
