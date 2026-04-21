"""Markdown ↔ Notion blocks converter.

Covers the subset every agent workflow actually needs:

  Markdown                       → Notion block type
  --------------------------------------------------
  # H1                           → heading_1
  ## H2                          → heading_2
  ### H3                         → heading_3
  - item                         → bulleted_list_item
  1. item                        → numbered_list_item
  - [ ] task   / - [x] task      → to_do
  > quote                        → quote
  ```lang\ncode\n```             → code (with language)
  ---  or  ***                   → divider
  (anything else)                → paragraph

  Inline:
    **bold**   *italic*   `code`
    [text](url)

Everything else degrades gracefully to plain paragraph text so we never
drop content silently.

This is intentionally hand-written — `md2notion`/`martian` are either
unmaintained or JS-only. The subset here is ~200 lines, fully tested.
"""

from __future__ import annotations

import re
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^[\-\*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_TODO_RE = re.compile(r"^[\-\*]\s+\[([ xX])\]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s*(.*)$")
_CODE_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_DIVIDER_RE = re.compile(r"^(---+|\*\*\*+)\s*$")

# Notion supports a fixed set of code languages; unknown languages fall back
# to "plain text". The list comes from the official Notion API reference.
_KNOWN_CODE_LANGS = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++",
    "c#", "css", "dart", "diff", "docker", "elixir", "elm", "erlang", "flow",
    "fortran", "f#", "gherkin", "glsl", "go", "graphql", "groovy", "haskell",
    "html", "java", "javascript", "js", "json", "julia", "kotlin", "latex",
    "less", "lisp", "livescript", "lua", "makefile", "markdown", "markup",
    "matlab", "mermaid", "nix", "objective-c", "ocaml", "pascal", "perl",
    "php", "plain text", "powershell", "prolog", "protobuf", "python", "py",
    "r", "reason", "ruby", "rust", "sass", "scala", "scheme", "scss", "shell",
    "sh", "sql", "swift", "typescript", "ts", "vb.net", "verilog", "vhdl",
    "visual basic", "webassembly", "xml", "yaml", "yml", "tsx", "jsx",
}

_LANG_ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "sh": "shell",
    "yml": "yaml",
    "tsx": "typescript",
    "jsx": "javascript",
}


def _normalise_code_lang(lang: str) -> str:
    lang = (lang or "").strip().lower()
    if not lang:
        return "plain text"
    lang = _LANG_ALIASES.get(lang, lang)
    return lang if lang in _KNOWN_CODE_LANGS else "plain text"


# Rich-text inline parsing. We split each line into `rich_text` segments
# carrying bold/italic/code annotations and optional hyperlinks. Notion's
# per-text-segment cap is 2000 chars; we split longer runs.

_INLINE_TOKEN_RE = re.compile(
    r"(\[[^\]]+\]\([^)]+\))"          # link: [text](url)
    r"|(\*\*[^\*]+\*\*)"              # bold: **x**
    r"|(\*[^\*]+\*)"                  # italic: *x*
    r"|(`[^`]+`)"                     # code: `x`
)


def _emit_text(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    link: str | None = None,
) -> list[dict[str, Any]]:
    """Chunk text into ≤2000-char Notion `rich_text` entries."""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for i in range(0, len(text), 1900):
        chunk = text[i:i + 1900]
        obj: dict[str, Any] = {
            "type": "text",
            "text": {"content": chunk, "link": {"url": link} if link else None},
            "annotations": {
                "bold": bold,
                "italic": italic,
                "strikethrough": False,
                "underline": False,
                "code": code,
                "color": "default",
            },
            "plain_text": chunk,
        }
        out.append(obj)
    return out


def markdown_to_rich_text(text: str) -> list[dict[str, Any]]:
    """Parse a single line into Notion rich_text segments."""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.extend(_emit_text(text[pos:m.start()]))
        token = m.group(0)
        if token.startswith("[") and "](" in token:
            # [label](url)
            label_end = token.index("](")
            label = token[1:label_end]
            url = token[label_end + 2:-1]
            out.extend(_emit_text(label, link=url))
        elif token.startswith("**"):
            out.extend(_emit_text(token[2:-2], bold=True))
        elif token.startswith("*"):
            out.extend(_emit_text(token[1:-1], italic=True))
        elif token.startswith("`"):
            out.extend(_emit_text(token[1:-1], code=True))
        pos = m.end()
    if pos < len(text):
        out.extend(_emit_text(text[pos:]))
    return out


def _mk_block(block_type: str, rich_text: list[dict] | None = None, **extra) -> dict:
    payload: dict[str, Any] = {}
    if rich_text is not None:
        payload["rich_text"] = rich_text
    payload.update(extra)
    return {
        "object": "block",
        "type": block_type,
        block_type: payload,
    }


def markdown_to_blocks(md: str) -> list[dict[str, Any]]:
    """Convert a markdown string into a list of Notion block objects.

    Empty lines are preserved as paragraph separators (they break paragraph
    runs) rather than emitted as empty paragraph blocks.
    """
    if not md:
        return []

    blocks: list[dict[str, Any]] = []
    lines = md.splitlines()
    i = 0
    paragraph_buf: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_buf
        if paragraph_buf:
            joined = " ".join(line.rstrip() for line in paragraph_buf).strip()
            if joined:
                blocks.append(
                    _mk_block("paragraph", markdown_to_rich_text(joined))
                )
            paragraph_buf = []

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # Blank line closes the current paragraph.
        if not line.strip():
            flush_paragraph()
            i += 1
            continue

        # Fenced code block — consume until closing fence.
        fence = _CODE_FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            lang = _normalise_code_lang(fence.group(1))
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not _CODE_FENCE_RE.match(lines[i].rstrip()):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # consume closing fence
            blocks.append(
                _mk_block(
                    "code",
                    markdown_to_rich_text("\n".join(code_lines)),
                    language=lang,
                )
            )
            continue

        # Divider
        if _DIVIDER_RE.match(line):
            flush_paragraph()
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # Heading
        m = _HEADING_RE.match(line)
        if m:
            flush_paragraph()
            depth = len(m.group(1))
            btype = f"heading_{depth}"
            blocks.append(_mk_block(btype, markdown_to_rich_text(m.group(2).strip())))
            i += 1
            continue

        # TODO — must be checked BEFORE the bullet regex.
        m = _TODO_RE.match(line)
        if m:
            flush_paragraph()
            checked = m.group(1).lower() == "x"
            blocks.append(
                _mk_block(
                    "to_do",
                    markdown_to_rich_text(m.group(2).strip()),
                    checked=checked,
                )
            )
            i += 1
            continue

        # Bulleted list item
        m = _BULLET_RE.match(line)
        if m:
            flush_paragraph()
            blocks.append(
                _mk_block("bulleted_list_item", markdown_to_rich_text(m.group(1).strip()))
            )
            i += 1
            continue

        # Numbered list item
        m = _NUMBERED_RE.match(line)
        if m:
            flush_paragraph()
            blocks.append(
                _mk_block("numbered_list_item", markdown_to_rich_text(m.group(2).strip()))
            )
            i += 1
            continue

        # Quote
        m = _QUOTE_RE.match(line)
        if m:
            flush_paragraph()
            blocks.append(_mk_block("quote", markdown_to_rich_text(m.group(1).strip())))
            i += 1
            continue

        # Fallback: accumulate into current paragraph.
        paragraph_buf.append(line)
        i += 1

    flush_paragraph()
    return blocks


# ── Reverse direction: blocks → markdown ─────────────────────────

def _rich_text_to_markdown(rich_text: list[dict] | None) -> str:
    if not rich_text:
        return ""
    out: list[str] = []
    for rt in rich_text:
        text = rt.get("plain_text") or rt.get("text", {}).get("content") or ""
        ann = rt.get("annotations") or {}
        link = (rt.get("text", {}) or {}).get("link") or {}
        href = link.get("url") if link else None
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def blocks_to_markdown(blocks: list[dict]) -> str:
    """Render a list of Notion blocks back to markdown.

    Child blocks / nested lists are NOT recursed here — callers that need
    full trees must paginate + fetch children separately. This keeps the
    converter synchronous and predictable for the MVP tool surface.
    """
    lines: list[str] = []
    for blk in blocks or []:
        btype = blk.get("type")
        if not btype:
            continue
        inner = blk.get(btype) or {}
        rt = inner.get("rich_text")
        text = _rich_text_to_markdown(rt)
        if btype == "paragraph":
            lines.append(text)
        elif btype == "heading_1":
            lines.append(f"# {text}")
        elif btype == "heading_2":
            lines.append(f"## {text}")
        elif btype == "heading_3":
            lines.append(f"### {text}")
        elif btype == "bulleted_list_item":
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"1. {text}")
        elif btype == "to_do":
            mark = "x" if inner.get("checked") else " "
            lines.append(f"- [{mark}] {text}")
        elif btype == "quote":
            lines.append(f"> {text}")
        elif btype == "code":
            lang = inner.get("language", "")
            code = text
            lines.append(f"```{lang}")
            lines.append(code)
            lines.append("```")
        elif btype == "divider":
            lines.append("---")
        elif btype == "callout":
            icon = inner.get("icon", {}).get("emoji", "💡")
            lines.append(f"> {icon} {text}")
        elif btype == "child_page":
            title = inner.get("title", "")
            lines.append(f"📄 **Child page:** {title}")
        elif btype == "child_database":
            title = inner.get("title", "")
            lines.append(f"🗄 **Database:** {title}")
        else:
            # Unknown block type — emit a placeholder so the user can see
            # something is there (instead of silently dropping).
            lines.append(f"[{btype} block]" + (f" {text}" if text else ""))
    return "\n\n".join(line for line in lines if line is not None)
