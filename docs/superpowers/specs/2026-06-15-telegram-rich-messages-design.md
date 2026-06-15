# Telegram Rich Messages (Bot API 10.1) — Design

**Date:** 2026-06-15
**Branch:** `feature/telegram-rich-messages`
**Status:** Approved design, pending implementation plan

## Background

Telegram Bot API 10.1 (2026-06-11) added **Rich Messages**: server-parsed
structured content (headings, tables, lists, LaTeX, quotes, code, media
blocks) plus a streaming variant for AI replies. aiogram 3.29.0 (2026-06-14)
ships native support: `bot.send_rich_message`, `bot.send_rich_message_draft`,
`InputRichMessage`, and the full `RichBlock*`/`RichText*` type tree.

Key discovery: `InputRichMessage` accepts a raw `markdown: str` field — Telegram
parses it into rich blocks **server-side**. No client-side markdown→RichBlock
converter is needed; the agent's existing markdown is passed through verbatim.

## Goal

Replace qanot's current HTML send path (`_md_to_html` + `parse_mode=HTML`) with
Rich Messages as the **primary** rendering path, so tables, headings, task
lists, nested blockquotes, LaTeX, and long documents render natively in
Telegram. Keep the existing HTML path as an **exception fallback** so a parse
error or API rejection never produces a broken reply.

Applies to both the streaming draft loop and the final send.

## Decisions

- **Scope:** Streaming + final (full). Both `_send_draft` and `_send_final`
  use rich.
- **Rollout:** Always-on replacement. Rich is primary; no config flag, no new
  response mode. HTML is reached only via `try/except` fallback.
- **No converter:** Pass the agent's raw markdown into
  `InputRichMessage(markdown=...)`. `_md_to_html` survives only on the fallback
  path.

## Architecture

The existing `stream` pipeline (draft flushes → final send) is unchanged in
shape. Only the two send primitives swap their first attempt to rich:

```
agent stream → _respond_stream (unchanged orchestration)
                 ├─ each flush → _send_draft
                 │     try:  SendRichMessageDraft(InputRichMessage(markdown=raw))
                 │     except: SendMessageDraft(_md_to_html(raw), HTML) → plain   [existing]
                 └─ end      → _send_final
                       validate + sanitize (raw text, unchanged)
                       try:  bot.send_rich_message(InputRichMessage(markdown=raw))
                       except: _md_to_html → _split_text → HTML chunks            [existing]
```

### Component changes (`qanot/telegram/streaming.py`)

1. **`_send_draft(chat_id, draft_id, text, *, thread_id)`**
   - First attempt: `SendRichMessageDraft(chat_id=, draft_id=, rich_message=InputRichMessage(markdown=text[:LIMIT]), message_thread_id=thread_id)`.
   - On any exception: existing behavior unchanged (`SendMessageDraft` with
     `_md_to_html(text)` + `ParseMode.HTML`, then plain-text retry).
   - Draft length cap kept conservative (current 4096) to avoid oversized drafts.

2. **`_send_final(chat_id, text, *, reply_to, thread_id)`**
   - `_maybe_validate_reply` + `_sanitize_response` run on raw text first
     (unchanged — memo validator must see the reply before send).
   - First attempt: `bot.send_rich_message(chat_id=, rich_message=InputRichMessage(markdown=text), message_thread_id=thread_id, reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None)`.
     - Rich messages carry long content natively, so the rich path does **not**
       split at 4096 and does **not** append `(i/n)` footers.
     - Returns a `Message`; its `message_id` feeds group reply tracking
       (`_group_state.record_bot_reply`) exactly as today.
   - On any exception: fall back to the current path
     (`_md_to_html` → `_split_text` → `_send_final_chunk` per chunk with HTML→plain).
   - Group reply tracking and the `chat_id < 0` guard are preserved on both paths.

3. **`send_message` (public, sub-agent path)** continues to delegate to
   `_send_final`, so sub-agents inherit rich rendering for free.

### What does NOT change

- `_respond_stream` / `_respond_partial` / `_respond_blocked` orchestration,
  typing/heartbeat loops, tool-progress bubbles, draft-pause-on-tool logic.
- `_respond_partial` (pre-9.5 `editMessageText` fallback) stays HTML — it
  targets clients that predate even drafts, so rich is inappropriate there.
- `formatting.py` (`_md_to_html`, `_split_text`, `_sanitize_response`) — kept,
  now used only on the fallback path and by `_respond_partial`.
- Memo reply validator, rate limiting, per-user isolation.

## Error handling / fallback

- **Mid-stream malformed markdown** (e.g. a half-open table during streaming):
  if Telegram's rich parser rejects the partial, that single flush degrades to
  the HTML draft path; the next flush retries rich; the final send carries
  complete markdown and renders correctly.
- **Old clients:** Rich Messages degrade server-side (Telegram's
  responsibility). We always send rich; our `try/except` only catches API-level
  errors, not cosmetic degradation — acceptable per the always-on decision.
- **Fallback is per-call**, never global state — preserves per-user isolation.

## Testing strategy

New tests in `tests/test_streaming.py` (mock `Bot`):
- `_send_draft`: rich attempt issues `SendRichMessageDraft`; when it raises,
  falls back to `SendMessageDraft` (HTML then plain).
- `_send_final`: rich attempt issues `bot.send_rich_message`; when it raises,
  falls back to `_md_to_html` + chunked HTML send.
- Memo validator still invoked on raw text before the rich send.
- Group reply tracking records the `message_id` returned by `send_rich_message`.
- Long (>4096) reply: rich path sends one message (no split / no `(i/n)`);
  fallback path splits.
- Regression: existing 78 telegram/streaming tests pass unchanged.

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_streaming.py tests/test_telegram*.py tests/test_agent_bot.py -q`

## Out of scope (possible follow-ups)

- Tuning the system prompt to actively emit tables / LaTeX / task-list markdown
  (the converter already passes them through; the model just isn't prompted to
  produce them yet).
- Guard-bot join-request handling and poll-option media (other Bot API 10.1
  features) — separate specs.
- Rich rendering in `_respond_partial`.

## Risks / trade-offs

- **Telegram rich-markdown dialect** may differ from what `_md_to_html`
  expects (e.g. heading `#` levels, `$...$` LaTeX delimiters). Pass-through +
  fallback contains the blast radius, but some constructs may render plainly
  until the prompt is tuned. Verify against
  https://core.telegram.org/bots/api#rich-message-formatting-options during
  implementation.
- **No kill-switch** (per decision): a parse-level regression that does not
  raise (renders poorly but succeeds) can only be reverted by redeploy.
