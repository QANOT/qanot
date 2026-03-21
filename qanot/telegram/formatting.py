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


def _md_to_html(text: str) -> str:
    """Convert agent markdown to Telegram HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _RE_CODE_BLOCK.sub(r"<pre>\2</pre>", text)

    def wrap_table(m: re.Match) -> str:
        return f"\n<pre>{m.group(0).strip()}</pre>\n"
    text = _RE_TABLE.sub(wrap_table, text)
    text = _RE_HR.sub("\u2501" * 18, text)
    text = _RE_BOLD.sub(r"<b>\1</b>", text)
    text = _RE_INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _RE_HEADING.sub(r"<b>\1</b>", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)
    return text


def _split_text(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Split text into chunks respecting line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
