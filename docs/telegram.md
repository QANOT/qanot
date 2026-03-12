# Telegram Integration

Qanot AI uses [aiogram 3.x](https://docs.aiogram.dev/) for Telegram bot communication. It supports three response modes, two transport modes, file uploads, and user access control.

## Response Modes

Set the response mode in config:

```json
{
  "response_mode": "stream"
}
```

### stream (default)

Uses Telegram Bot API 9.5 `sendMessageDraft` for real-time character-by-character streaming.

**How it works:**

1. The agent starts streaming tokens from the LLM
2. Accumulated text is sent as drafts at `stream_flush_interval` intervals (default 0.8s)
3. During tool execution, drafting pauses to avoid race conditions
4. After tool results, drafting resumes with new text
5. A final `sendMessage` sends the complete formatted response

**Pros:** Lowest perceived latency. Users see text appearing in real time.
**Cons:** Requires recent Telegram client versions that support `sendMessageDraft`.

**Race condition handling:** When the agent calls a tool, draft updates are paused. This prevents the situation where a draft update and a tool result arrive simultaneously, which can cause rendering artifacts. The last draft text is tracked to avoid redundant updates.

### partial

Uses `editMessageText` to periodically update a sent message with accumulated text.

**How it works:**

1. First text delta sends an initial message
2. Subsequent text is accumulated and the message is edited at intervals
3. The final edit applies HTML formatting
4. If the response exceeds the Telegram message limit (4,000 chars), additional chunks are sent as separate messages

**Pros:** Works on all Telegram clients. Compatible with older Bot API versions.
**Cons:** Users see message edits (flashing), which is less smooth than streaming.

### blocked

Waits for the complete response, then sends a single message.

**How it works:**

1. Typing indicator is shown while processing
2. The full agent loop runs to completion
3. The final response is sent as a formatted message

**Pros:** Simplest mode. No partial updates or drafts.
**Cons:** Users wait with no visible progress. Long responses cause a noticeable delay.

## Transport Modes

### Polling (default)

```json
{
  "telegram_mode": "polling"
}
```

Long polling -- the bot connects to Telegram servers and waits for updates. No public URL needed. Best for development and simple deployments.

Pending updates are dropped on startup (`drop_pending_updates=True`) to avoid processing stale messages.

### Webhook

```json
{
  "telegram_mode": "webhook",
  "webhook_url": "https://bot.example.com",
  "webhook_port": 8443
}
```

Runs an aiohttp web server that receives updates from Telegram. Requires a public HTTPS URL.

The webhook endpoint is `{webhook_url}/webhook`. Qanot:

1. Sets the webhook URL with Telegram on startup
2. Starts an aiohttp server on `0.0.0.0:{webhook_port}`
3. Processes incoming updates via the same dispatcher
4. Deletes the webhook on shutdown

**Typical setup with a reverse proxy:**

```
Internet --> nginx (443) --> Qanot (8443)
```

```nginx
location /webhook {
    proxy_pass http://localhost:8443;
}
```

## Message Handling

The adapter handles three message types:

### Text Messages

Plain text messages are forwarded directly to the agent.

### Photos

Photos are noted with a `[Photo received]` prefix. The caption (if any) is included. Photo content is not processed (no vision support in the current version).

### Documents (File Uploads)

Documents are automatically downloaded to the workspace:

1. File is downloaded to `{workspace_dir}/uploads/{filename}`
2. Message is prefixed with `[Fayl yuklandi: uploads/{filename}]`
3. The agent can then read the file using the `read_file` tool

If the download fails, the message notes the failure and the agent proceeds with the conversation.

## Message Formatting

Agent responses are converted from Markdown to Telegram HTML before sending:

| Markdown | HTML Output |
|----------|-------------|
| `**bold**` | `<b>bold</b>` |
| `` `code` `` | `<code>code</code>` |
| ```` ```code block``` ```` | `<pre>code block</pre>` |
| `# Heading` | `<b>Heading</b>` |
| Tables (`\|...\|`) | `<pre>table</pre>` |
| `---` | Horizontal line (Unicode) |

HTML special characters (`&`, `<`, `>`) are escaped before conversion to prevent injection.

If HTML parsing fails when sending, the adapter falls back to plain text.

### Message Splitting

Telegram has a 4,096 character limit per message (Qanot uses a 4,000 char working limit). Long responses are split at line boundaries and sent as multiple messages with a 100ms delay between them.

## Tool Call Sanitization

Some LLM providers (particularly Llama models via Groq) occasionally output tool call syntax as text instead of structured tool calls. The adapter strips these leaked artifacts:

- `<function>...</function>` tags
- `<tool_call>...</tool_call>` tags
- Raw JSON tool call objects

This prevents users from seeing internal tool call syntax in the bot's responses.

## User Access Control

```json
{
  "allowed_users": [123456789, 987654321]
}
```

When `allowed_users` is set, only those Telegram user IDs can interact with the bot. Messages from other users are silently ignored.

When `allowed_users` is empty (the default), all users can interact with the bot.

To find your Telegram user ID, send a message to [@userinfobot](https://t.me/userinfobot).

## Concurrency

```json
{
  "max_concurrent": 4
}
```

The adapter uses an asyncio semaphore to limit concurrent message processing. If 4 messages are being processed simultaneously, additional messages wait until a slot opens. This prevents overwhelming the LLM provider with too many parallel requests.

## Proactive Messages

The Telegram adapter runs a proactive message loop that checks the scheduler's message queue. When a cron job produces output:

- **`proactive` messages:** Sent to all allowed users
- **`system_event` messages:** Injected into the main agent's conversation

See [Scheduler](scheduler.md) for details on how cron jobs produce proactive messages.

## Error Handling

The adapter catches agent errors and sends user-friendly messages:

- **Rate limit errors:** "Limitga yetdik. Iltimos, 20-30 soniya kutib qayta yozing."
- **Other errors:** "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring."

Errors are logged with full stack traces for debugging, but users only see the friendly message.

## Typing Indicator

During processing, the bot sends a typing indicator every 4 seconds until the response is ready. This is visible as "Bot is typing..." in the Telegram client. The typing loop is cancelled as soon as the first streaming draft is sent.
