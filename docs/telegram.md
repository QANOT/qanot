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

## Group Chat

```json
{
  "group_mode": "mention"
}
```

The `group_mode` setting controls how the bot behaves in group and supergroup chats. Default is `"mention"`.

| Mode | Behavior |
|------|----------|
| `off` | Bot ignores all group messages |
| `mention` | Bot responds only when @mentioned by username or when someone replies to the bot's own message |
| `all` | Bot responds to every message in the group |

**How mention mode works:**

1. The bot caches its own username on startup
2. When a group message arrives, it checks for `@bot_username` in the message text or caption
3. It also checks if the message is a reply to one of the bot's own messages
4. If neither condition is met, the message is silently ignored

**Group conversation isolation:** In group chats, all members share a single conversation keyed by `group_{chat_id}`. This means the bot maintains one conversation context per group, not per user. In DMs, conversations are keyed by user ID as usual.

**Sender identification:** Group messages are prefixed with the sender's name (e.g., `[Ahmad]: message text`) so the agent can distinguish between group members. The `@bot_username` mention is stripped from the text before processing.

## Voice Messages

Voice messages and video notes are automatically transcribed when a voice API key is configured.

**Processing flow:**

1. Voice message or video note arrives
2. Bot sends a typing indicator immediately
3. Audio is downloaded to a temporary file
4. Audio is transcribed using the configured `voice_provider`
5. Transcribed text replaces the voice message content
6. The turn is processed normally, with `voice_request` flag set

**Audio format handling:**

| Provider | Accepts OGG | Requires Conversion |
|----------|-------------|---------------------|
| Muxlisa | Yes (native) | No |
| Whisper | Yes | No |
| KotibAI | No | OGG to MP3 via ffmpeg |
| Aisha | No | OGG to MP3 via ffmpeg |

For video notes, audio is extracted via ffmpeg (to OGG for Muxlisa, to MP3 for others).

**TTS voice replies:**

When `voice_mode` is `"always"`, or `"inbound"` and the user sent a voice message, the bot sends a TTS voice reply after the text response. The flow:

1. A "recording voice" indicator is shown
2. The last assistant response text is sent to the TTS provider
3. The returned audio (WAV or URL) is converted to OGG Opus for Telegram
4. The voice message is sent via `bot.send_voice()`

**Four voice providers:**

| Provider | STT | TTS | Voices | Notes |
|----------|-----|-----|--------|-------|
| Muxlisa (default) | Yes | Yes | maftuna, asomiddin | Native OGG, no ffmpeg for STT |
| KotibAI | Yes | Yes | aziza, nargiza, soliha, sherzod, rachel, arnold | 6 voices, multi-language |
| Aisha | Yes | Yes | gulnoza, jaxongir | Mood control (happy/sad/neutral) |
| Whisper | Yes | No | N/A | OpenAI, 50+ languages, STT only |

**Quoted voice messages:** When a user replies to a voice message, the quoted voice is also transcribed and included as `[voice: transcribed text]` in the reply annotation.

## Reactions

```json
{
  "reactions_enabled": false
}
```

When `reactions_enabled` is `true`, the bot sends emoji reactions on messages to indicate processing status:

| Emoji | When |
|-------|------|
| `eyes` | Message received, processing started |
| `white_check_mark` | Processing completed successfully |
| `x` | An error occurred during processing |

When messages are coalesced (multiple rapid messages batched into one turn), earlier messages in the batch receive a `white_check_mark` reaction to indicate they were included.

Reactions are sent via the `SetMessageReaction` API method. If reactions are not supported in a chat (e.g., older groups), failures are silently ignored.

Default is `false` (no reactions sent).

## Reply Mode

```json
{
  "reply_mode": "coalesced"
}
```

Controls when the bot uses Telegram's reply-to feature to link its response to the user's message.

| Mode | Behavior |
|------|----------|
| `off` | Never reply-to; responses are sent as standalone messages |
| `coalesced` | Reply-to only when multiple rapid messages were batched into one turn |
| `always` | Always reply-to the triggering message |

Default is `"coalesced"`.

## Telegram Slash Commands

Qanot AI provides 22 slash commands for settings management, information display, and actions. Most settings commands display inline keyboard buttons for easy selection.

### Settings Commands

| Command | Description |
|---------|-------------|
| `/model` | Switch the AI model. Shows inline buttons for available models (e.g., claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5-20251001). |
| `/think` | Toggle extended thinking level. Inline buttons: off, low, medium, high. |
| `/voice` | Change TTS voice name for the current voice provider. |
| `/voiceprovider` | Switch voice provider. Inline buttons: Muxlisa, KotibAI, Aisha, Whisper. |
| `/lang` | Set STT language. Inline buttons: uz, ru, en, auto. |
| `/mode` | Switch response mode. Inline buttons: stream, partial, blocked. |
| `/routing` | Toggle 3-tier model routing on/off. |
| `/group` | Set group chat mode. Inline buttons: off, mention, all. |
| `/exec` | Set execution security level. Inline buttons: open, cautious, strict. |
| `/code` | Toggle Anthropic server-side code execution on/off. |

### Info Commands

| Command | Description |
|---------|-------------|
| `/status` | Current bot status: uptime, context usage %, token count, active conversations. |
| `/usage` | Token usage and cost statistics for the current user. |
| `/context` | Detailed context window information: tokens used, buffer status, compaction mode. |
| `/config` | Show current configuration (secrets are masked). |
| `/id` | Show your Telegram user ID. |
| `/mcp` | View connected MCP servers, tool counts, and connection status. |
| `/plugins` | List installed plugins. Enable/disable via inline buttons. |

### Action Commands

| Command | Description |
|---------|-------------|
| `/reset` | Reset conversation history. Includes a model hint in the fresh context. |
| `/compact` | Force context compaction immediately (useful when context is filling up). |
| `/export` | Export current conversation to a file and send it via Telegram. |
| `/stop` | Stop the current response generation. |

### Inline Keyboard Buttons

Settings commands use Telegram inline keyboard buttons for selection. When you send `/model`, the bot responds with a message containing buttons for each available model. Tapping a button updates the config and confirms the change.

Buttons follow the OpenClaw-style settings management pattern, making it easy to adjust settings without typing values manually.

## Typing Indicator

During processing, the bot sends a typing indicator every 4 seconds until the response is ready. This is visible as "Bot is typing..." in the Telegram client. The typing loop is cancelled as soon as the first streaming draft is sent.
