"""Unit tests for markdown ↔ Notion block conversion."""

from __future__ import annotations

import sys
from pathlib import Path

# Make `engine.markdown` importable without running the full plugin loader.
PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from nt_engine.markdown import (  # noqa: E402
    _normalise_code_lang,
    blocks_to_markdown,
    markdown_to_blocks,
    markdown_to_rich_text,
)


def _types(blocks):
    return [b["type"] for b in blocks]


def _plain(blk):
    """Concat plain_text from a block's rich_text."""
    inner = blk[blk["type"]]
    return "".join(rt["plain_text"] for rt in inner.get("rich_text", []))


# ── markdown_to_blocks ─────────────────────────────────────────

def test_empty_input_returns_empty():
    assert markdown_to_blocks("") == []
    assert markdown_to_blocks("   \n\n   ") == []


def test_headings():
    blocks = markdown_to_blocks("# One\n## Two\n### Three")
    assert _types(blocks) == ["heading_1", "heading_2", "heading_3"]
    assert _plain(blocks[0]) == "One"
    assert _plain(blocks[1]) == "Two"


def test_paragraphs_and_blank_lines():
    md = "First paragraph line one.\nline two.\n\nSecond paragraph."
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["paragraph", "paragraph"]
    assert "line one" in _plain(blocks[0])
    assert "Second paragraph" in _plain(blocks[1])


def test_bulleted_list():
    blocks = markdown_to_blocks("- a\n- b\n* c")
    assert _types(blocks) == ["bulleted_list_item"] * 3
    assert _plain(blocks[2]) == "c"


def test_numbered_list():
    blocks = markdown_to_blocks("1. one\n2. two\n10. ten")
    assert _types(blocks) == ["numbered_list_item"] * 3
    assert _plain(blocks[2]) == "ten"


def test_todo_checked_and_unchecked():
    blocks = markdown_to_blocks("- [ ] todo\n- [x] done\n- [X] also done")
    assert _types(blocks) == ["to_do"] * 3
    assert blocks[0]["to_do"]["checked"] is False
    assert blocks[1]["to_do"]["checked"] is True
    assert blocks[2]["to_do"]["checked"] is True


def test_todo_not_confused_with_bullet():
    # Regression: TODO regex must run BEFORE the bullet regex.
    blocks = markdown_to_blocks("- [ ] buy milk")
    assert blocks[0]["type"] == "to_do"


def test_quote():
    blocks = markdown_to_blocks("> an important note")
    assert blocks[0]["type"] == "quote"
    assert _plain(blocks[0]) == "an important note"


def test_code_block_with_language():
    md = "```python\nprint(1)\n```"
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "python"
    assert "print(1)" in _plain(blocks[0])


def test_code_block_unknown_language_falls_back():
    blocks = markdown_to_blocks("```brainfuck\n+\n```")
    assert blocks[0]["code"]["language"] == "plain text"


def test_code_block_alias_js_to_javascript():
    blocks = markdown_to_blocks("```js\nx\n```")
    assert blocks[0]["code"]["language"] == "javascript"


# ── whole-document fence unwrap (2026-05-19 empty-page incident) ──

def test_whole_doc_bare_fence_is_unwrapped():
    """An LLM wrapping the entire note in a bare ``` fence must not
    collapse the page into one code block — it should parse as the doc."""
    md = "```\n# 19-may, 2026\n\n- Chorvoq sayohati\n- Zo'r dam olish\n```"
    blocks = markdown_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert "code" not in types
    assert types[0] == "heading_1"
    assert any(t == "bulleted_list_item" for t in types)


def test_whole_doc_markdown_fence_is_unwrapped():
    md = "```markdown\n## Kun yakuni\n\nMatn shu yerda.\n```"
    blocks = markdown_to_blocks(md)
    assert [b["type"] for b in blocks] == ["heading_2", "paragraph"]


def test_real_code_fence_is_preserved():
    """A genuine ```python snippet is NOT a whole-doc artifact — keep it."""
    blocks = markdown_to_blocks("```python\nprint(1)\n```")
    assert len(blocks) == 1 and blocks[0]["type"] == "code"


def test_fenced_block_among_other_content_is_preserved():
    """Interior fence (real embedded snippet) must stay a code block."""
    md = "# Title\n\nIntro\n\n```python\nx = 1\n```\n\nOutro"
    blocks = markdown_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert "code" in types
    assert types[0] == "heading_1"


def test_divider():
    blocks = markdown_to_blocks("---")
    assert blocks[0]["type"] == "divider"


# ── GFM tables (2026-05-24 iteration-cap incident) ─────────────────

def test_table_basic():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    blk = blocks[0]
    assert blk["type"] == "table"
    assert blk["table"]["table_width"] == 2
    assert blk["table"]["has_column_header"] is True
    rows = blk["table"]["children"]
    assert len(rows) == 2
    assert rows[0]["type"] == "table_row"
    assert rows[0]["table_row"]["cells"][0][0]["plain_text"] == "A"
    assert rows[1]["table_row"]["cells"][1][0]["plain_text"] == "2"


def test_table_pads_short_rows():
    """Notion requires every row to have exactly table_width cells."""
    md = "| A | B | C |\n|---|---|---|\n| 1 | 2 |"
    blocks = markdown_to_blocks(md)
    rows = blocks[0]["table"]["children"]
    assert blocks[0]["table"]["table_width"] == 3
    assert len(rows[1]["table_row"]["cells"]) == 3
    assert rows[1]["table_row"]["cells"][2] == []


def test_table_with_inline_formatting():
    md = "| Word | Meaning |\n|---|---|\n| **bold** | *italic* |"
    blocks = markdown_to_blocks(md)
    cells = blocks[0]["table"]["children"][1]["table_row"]["cells"]
    assert cells[0][0]["annotations"]["bold"] is True
    assert cells[1][0]["annotations"]["italic"] is True


def test_table_with_surrounding_content():
    md = "# German Lesson\n\n| DE | UZ |\n|---|---|\n| Haus | uy |\n\nMore notes."
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["heading_1", "table", "paragraph"]


def test_table_accepts_alignment_colons():
    md = "| A | B | C |\n|:---|:---:|---:|\n| 1 | 2 | 3 |"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "table"
    assert blocks[0]["table"]["table_width"] == 3


def test_table_accepts_short_separator_dashes():
    """Some markdown emitters use single dashes (`|-|-|`) — accept them too."""
    md = "| A | B |\n|-|-|\n| 1 | 2 |"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "table"


def test_pipe_line_without_separator_is_paragraph():
    """A `| ... | ... |` line NOT followed by a `|---|` separator is text."""
    md = "| not a table | line |"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "paragraph"


def test_table_roundtrip_via_blocks_to_markdown():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    blocks = markdown_to_blocks(md)
    rendered = blocks_to_markdown(blocks)
    assert "| A | B |" in rendered
    assert "| 1 | 2 |" in rendered
    assert "---" in rendered


def test_blocks_to_markdown_table_without_children_is_placeholder():
    """Top-level table fetched without recursing into children → placeholder."""
    blocks = [{"object": "block", "type": "table",
               "table": {"table_width": 2, "has_column_header": True,
                         "has_row_header": False, "children": []}}]
    assert "[table block]" in blocks_to_markdown(blocks)


def test_inline_bold_italic_code():
    rt = markdown_to_rich_text("hello **world** and *there* with `code`")
    # One plain, one bold, one plain, one italic, one plain, one code
    annotations = [(r["annotations"]["bold"], r["annotations"]["italic"],
                    r["annotations"]["code"]) for r in rt]
    assert (True, False, False) in annotations
    assert (False, True, False) in annotations
    assert (False, False, True) in annotations


def test_inline_link():
    rt = markdown_to_rich_text("see [docs](https://x.com)")
    link_rt = [r for r in rt if r["text"]["link"] is not None]
    assert len(link_rt) == 1
    assert link_rt[0]["text"]["link"]["url"] == "https://x.com"
    assert link_rt[0]["plain_text"] == "docs"


def test_long_text_chunks_to_2000_char_limit():
    rt = markdown_to_rich_text("x" * 5000)
    # Every chunk must be ≤2000 (we chose 1900 internally for safety margin).
    assert all(len(r["plain_text"]) <= 2000 for r in rt)
    assert sum(len(r["plain_text"]) for r in rt) == 5000


def test_normalise_code_lang():
    assert _normalise_code_lang("Python") == "python"
    assert _normalise_code_lang("JS") == "javascript"
    assert _normalise_code_lang("totally-unknown") == "plain text"
    assert _normalise_code_lang("") == "plain text"


def test_mixed_document():
    md = (
        "# Title\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "- bullet a\n"
        "- bullet b\n"
        "\n"
        "- [ ] task 1\n"
        "- [x] task 2\n"
        "\n"
        "```python\nprint('hi')\n```\n"
        "\n"
        "> quote here\n"
        "\n"
        "---\n"
        "\n"
        "Final paragraph."
    )
    blocks = markdown_to_blocks(md)
    types = _types(blocks)
    assert types == [
        "heading_1",
        "paragraph",
        "bulleted_list_item", "bulleted_list_item",
        "to_do", "to_do",
        "code",
        "quote",
        "divider",
        "paragraph",
    ]


# ── blocks_to_markdown ─────────────────────────────────────────

def test_round_trip_preserves_content():
    md = (
        "# Title\n"
        "\n"
        "Body text.\n"
        "\n"
        "- a\n"
        "- b\n"
    )
    blocks = markdown_to_blocks(md)
    rendered = blocks_to_markdown(blocks)
    assert "# Title" in rendered
    assert "Body text" in rendered
    assert "- a" in rendered
    assert "- b" in rendered


def test_blocks_to_markdown_handles_unknown_type():
    blocks = [{
        "object": "block",
        "type": "mystery",
        "mystery": {"rich_text": [{"plain_text": "hi",
                                   "text": {"content": "hi", "link": None},
                                   "annotations": {}}]},
    }]
    out = blocks_to_markdown(blocks)
    assert "[mystery block]" in out


def test_blocks_to_markdown_todo():
    blocks = [{
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [{"plain_text": "done",
                           "text": {"content": "done", "link": None},
                           "annotations": {}}],
            "checked": True,
        },
    }]
    assert blocks_to_markdown(blocks) == "- [x] done"
