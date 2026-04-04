"""HTML session export — renders conversation history as a styled HTML file."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone


def render_session_html(
    messages: list[dict],
    bot_name: str = "Qanot AI",
    model: str = "",
) -> str:
    """Render conversation messages as a self-contained HTML file.

    Args:
        messages: List of {"role": "user"|"assistant", "content": str|list}
        bot_name: Bot display name
        model: Model name for header

    Returns:
        Complete HTML string ready to write to file.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    turn_count = sum(1 for m in messages if m.get("role") == "user")

    body_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Extract text from structured content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "tool")
                        parts.append(f"[Tool: {name}]")
            content = "\n".join(parts)

        if not content or not content.strip():
            continue

        if role == "user":
            body_parts.append(_render_user_message(content))
        elif role == "assistant":
            body_parts.append(_render_assistant_message(content, bot_name))

    messages_html = "\n".join(body_parts)

    return _HTML_TEMPLATE.format(
        bot_name=html.escape(bot_name),
        model=html.escape(model),
        timestamp=timestamp,
        turn_count=turn_count,
        message_count=len(messages),
        messages=messages_html,
    )


def _render_user_message(content: str) -> str:
    """Render a user message bubble."""
    formatted = _format_content(content)
    return f'<div class="message user"><div class="role">You</div><div class="content">{formatted}</div></div>'


def _render_assistant_message(content: str, bot_name: str) -> str:
    """Render an assistant message bubble."""
    formatted = _format_content(content)
    name = html.escape(bot_name)
    return f'<div class="message assistant"><div class="role">{name}</div><div class="content">{formatted}</div></div>'


# Regex for fenced code blocks: ```lang\n...\n```
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
# Regex for inline code: `...`
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# Regex for bold: **...**
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# Regex for italic: *...*
_ITALIC_RE = re.compile(r"\*(.+?)\*")


def _format_content(text: str) -> str:
    """Convert markdown-like content to HTML."""
    # Escape HTML first
    text = html.escape(text)

    # Code blocks (must be before inline code)
    def _code_block_repl(m: re.Match) -> str:
        lang = m.group(1)
        code = m.group(2).rstrip()
        lang_attr = f' data-lang="{lang}"' if lang else ""
        lang_label = f'<span class="code-lang">{lang}</span>' if lang else ""
        return f'<div class="code-block"{lang_attr}>{lang_label}<pre><code>{code}</code></pre></div>'

    text = _CODE_BLOCK_RE.sub(_code_block_repl, text)

    # Inline code
    text = _INLINE_CODE_RE.sub(r'<code class="inline">\1</code>', text)

    # Bold
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)

    # Italic
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)

    # Line breaks (but not inside code blocks)
    text = text.replace("\n", "<br>\n")

    return text


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{bot_name} — Suhbat eksporti</title>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-dim: #8b949e;
    --user-bg: #1a2332;
    --user-border: #1f6feb;
    --bot-bg: #1c2128;
    --bot-border: #388bfd;
    --code-bg: #0d1117;
    --accent: #58a6ff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 0;
  }}
  .header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 24px 32px;
    text-align: center;
  }}
  .header h1 {{
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 8px;
  }}
  .header .meta {{
    color: var(--text-dim);
    font-size: 0.85rem;
  }}
  .header .meta span {{
    margin: 0 8px;
  }}
  .conversation {{
    max-width: 800px;
    margin: 24px auto;
    padding: 0 16px;
  }}
  .message {{
    margin-bottom: 16px;
    padding: 16px 20px;
    border-radius: 12px;
    border-left: 3px solid;
  }}
  .message.user {{
    background: var(--user-bg);
    border-color: var(--user-border);
  }}
  .message.assistant {{
    background: var(--bot-bg);
    border-color: var(--bot-border);
  }}
  .role {{
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 8px;
    color: var(--accent);
  }}
  .message.user .role {{ color: var(--user-border); }}
  .message.assistant .role {{ color: var(--bot-border); }}
  .content {{
    font-size: 0.95rem;
    word-wrap: break-word;
    overflow-wrap: break-word;
  }}
  .code-block {{
    position: relative;
    margin: 12px 0;
    border-radius: 8px;
    overflow: hidden;
    background: var(--code-bg);
    border: 1px solid var(--border);
  }}
  .code-block .code-lang {{
    display: block;
    padding: 4px 12px;
    font-size: 0.7rem;
    color: var(--text-dim);
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }}
  .code-block pre {{
    margin: 0;
    padding: 12px 16px;
    overflow-x: auto;
  }}
  .code-block code {{
    font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
  }}
  code.inline {{
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.85em;
  }}
  .footer {{
    text-align: center;
    padding: 24px;
    color: var(--text-dim);
    font-size: 0.8rem;
    border-top: 1px solid var(--border);
    margin-top: 32px;
  }}
  @media (max-width: 600px) {{
    .header {{ padding: 16px; }}
    .conversation {{ padding: 0 8px; }}
    .message {{ padding: 12px 14px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <h1>{bot_name}</h1>
  <div class="meta">
    <span>{model}</span> &middot;
    <span>{turn_count} turn</span> &middot;
    <span>{message_count} xabar</span> &middot;
    <span>{timestamp}</span>
  </div>
</div>
<div class="conversation">
{messages}
</div>
<div class="footer">
  Exported by {bot_name} &middot; Powered by Qanot AI
</div>
</body>
</html>"""
