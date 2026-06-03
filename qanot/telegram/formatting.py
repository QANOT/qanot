"""Telegram message formatting — markdown to HTML, text splitting, sanitization."""

from __future__ import annotations

import re

MAX_MSG_LEN = 4000

# Regex to strip leaked tool-call text (Llama models sometimes output these as text)
_TOOL_LEAK_RE = re.compile(
    r'<function[^>]*>.*?</function>|'
    r'<tool_call>.*?</tool_call>|'
    r'\{"name"\s*:\s*"[^"]+"\s*,\s*"parameters"\s*:',
    re.DOTALL,
)

# Pre-compiled regex patterns for _md_to_html (avoid recompilation per call)
_RE_CODE_BLOCK = re.compile(r"```(\w*)\n([\s\S]*?)```")
_RE_TABLE = re.compile(r"(?:^[|].*\n?)+", re.MULTILINE)
_RE_HR = re.compile(r"^---+$", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")


def _sanitize_response(text: str) -> str:
    """Strip leaked tool call artifacts from LLM output."""
    cleaned = _TOOL_LEAK_RE.sub("", text).strip()
    return cleaned if cleaned else text


def _strip_inner_markers(body: str) -> str:
    """Drop ``**bold**`` / ```` `code` ```` markers from text destined for a
    ``<pre>`` wrapper.

    Telegram's HTML parser REJECTS nested formatting inside ``<pre>``
    (it'd parse the whole message as malformed and the client falls back
    to literal text \u2014 the 2026-05-21 second-chunk regression). The
    wrapper provides monospace already, so stripping the markers is the
    right loss: visual sameness, valid HTML.
    """
    body = _RE_BOLD.sub(r"\1", body)
    body = _RE_INLINE_CODE.sub(r"\1", body)
    return body


def _table_cells(line: str) -> list[str]:
    """Split one markdown table row into trimmed cells (drops the edge pipes)."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """True for the ``|---|:--:|`` divider row beneath a table header."""
    return bool(cells) and all(
        c and set(c) <= {"-", ":", " "} and "-" in c for c in cells
    )


def _render_table_to_bullets(table_text: str) -> str:
    """Render a GFM table as Telegram-friendly bullet groups.

    A pipe table is unreadable on a phone — it wraps and misaligns inside a
    ``<pre>`` block. Instead, each data row becomes a bullet whose header is
    the first column and whose remaining columns render as ``Label: value``
    lines, e.g.::

        • Apple
          Narx: 100
          Soni: 5

    Falls back to the raw rows when the block isn't a real table.
    """
    rows = [r for r in table_text.splitlines() if r.strip()]
    if len(rows) < 2:
        return _strip_inner_markers(table_text.strip())

    header = _table_cells(rows[0])
    body_start = 1
    if len(rows) > 1 and _is_separator_row(_table_cells(rows[1])):
        body_start = 2
    data_rows = rows[body_start:]
    if not data_rows:
        # Header only (no data) — show the header cells as a simple line.
        return _strip_inner_markers(" · ".join(c for c in header if c))

    groups: list[str] = []
    for row in data_rows:
        cells = _table_cells(row)
        if not cells:
            continue
        first = _strip_inner_markers(cells[0]) or "—"
        lines = [f"• <b>{first}</b>"]
        for i in range(1, len(cells)):
            val = _strip_inner_markers(cells[i])
            if not val:
                continue
            label = _strip_inner_markers(header[i]) if i < len(header) and header[i] else ""
            lines.append(f"   {label}: {val}" if label else f"   {val}")
        groups.append("\n".join(lines))
    return "\n\n".join(groups)


def _md_to_html(text: str) -> str:
    """Convert agent markdown to Telegram HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def wrap_code(m: re.Match) -> str:
        return f"<pre>{_strip_inner_markers(m.group(2))}</pre>"
    text = _RE_CODE_BLOCK.sub(wrap_code, text)

    def wrap_table(m: re.Match) -> str:
        return f"\n{_render_table_to_bullets(m.group(0))}\n"
    text = _RE_TABLE.sub(wrap_table, text)
    text = _RE_HR.sub("\u2501" * 18, text)
    text = _RE_BOLD.sub(r"<b>\1</b>", text)
    text = _RE_INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _RE_HEADING.sub(r"<b>\1</b>", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)
    return text


_PRE_SPAN_RE = re.compile(r"<pre\b[^>]*>.*?</pre>", re.DOTALL)


def _split_text(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Split HTML text into chunks respecting tag boundaries.

    The naive ``text.rfind('\\n', 0, limit)`` cuts inside a ``<pre>``
    block when the limit falls there — Telegram rejects the resulting
    unclosed-tag HTML on BOTH halves, falls back to plain text, and the
    user sees raw tags (the 2026-05-21 second-chunk regression). Walk
    backwards from the limit until we land on a newline that is NOT
    inside any ``<pre>`` span, so each chunk's HTML stays balanced.
    Only ``<pre>`` matters here because every other Telegram-supported
    tag (``<b>``, ``<code>``, ``<i>``…) is single-line by construction
    in ``_md_to_html``.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = _safe_cut(text, limit)
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def _safe_cut(text: str, limit: int) -> int:
    """Rightmost newline ≤ limit that is OUTSIDE every ``<pre>...</pre>``.

    Returns ``limit`` only when no safe newline exists (e.g. one giant
    ``<pre>`` dominates the message) — same degraded mode as before,
    and rare in practice.
    """
    spans = [(m.start(), m.end()) for m in _PRE_SPAN_RE.finditer(text)]

    def inside_pre(pos: int) -> bool:
        for s, e in spans:
            if s < pos < e:
                return True
            if s >= pos:
                break
        return False

    upper = limit
    while upper > 0:
        cut = text.rfind("\n", 0, upper)
        if cut <= 0:
            break
        if not inside_pre(cut):
            return cut
        upper = cut  # retry against the next-earlier newline

    # No safe newline → fall back to the legacy behaviour.
    cut = text.rfind("\n", 0, limit)
    return cut if cut > 0 else limit
