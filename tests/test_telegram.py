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

    def test_table_renders_as_bullet_groups_not_pre(self):
        """GFM tables render as mobile-readable bullet groups (#5), not a
        <pre> dump. First column = bold bullet header; other columns become
        'Label: value' lines. Cell **markers** are stripped from headers."""
        md = (
            "| Hafta | Mavzu |\n"
            "|---|---|\n"
            "| **Präteritum** | war, hatte |\n"
            "| **Konjunktiv II** | würde |\n"
        )
        out = _md_to_html(md)
        # No <pre> wrapper for tables anymore.
        assert "<pre>" not in out
        # Bullet header is the first column, bolded, markers stripped.
        assert "• <b>Präteritum</b>" in out
        assert "• <b>Konjunktiv II</b>" in out
        # Second column rendered as "Label: value".
        assert "Mavzu: war, hatte" in out
        assert "Mavzu: würde" in out

    def test_code_block_with_inner_markers_no_nested_tags(self):
        """Same contract for fenced code blocks: ** and ` markers inside
        the body must not become <b>/<code> tags nested in <pre>."""
        import re

        out = _md_to_html("```\nplain text with **bold** and `inline`\n```")
        for block in re.findall(r"<pre>(.*?)</pre>", out, re.DOTALL):
            assert "<b>" not in block and "<code>" not in block, block
        assert "bold" in out and "inline" in out


class TestSafeSplit:
    """Regression: _split_text must never cut inside a <pre>...</pre>
    block, or both chunks become unclosed-tag HTML that Telegram rejects
    (plain-text fallback → raw tags visible — 2026-05-21 incident)."""

    def test_split_skips_inside_pre(self):
        prose1 = "para A\n" * 50          # ~350 chars
        pre_body = "row\n" * 200          # ~800 chars — single big block
        prose2 = "para B\n" * 50
        text = prose1 + "<pre>" + pre_body + "</pre>\n" + prose2
        chunks = _split_text(text, limit=900)
        # ≥ 2 chunks (text is well over 900 chars).
        assert len(chunks) >= 2
        # Every chunk must have balanced <pre> tags (open count == close count).
        for c in chunks:
            assert c.count("<pre>") == c.count("</pre>"), (
                f"unbalanced <pre> in chunk: {c[:80]!r}"
            )

    def test_short_text_unsplit(self):
        assert _split_text("hello", limit=4000) == ["hello"]


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
