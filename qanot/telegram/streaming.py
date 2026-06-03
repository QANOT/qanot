"""Telegram response strategies — stream, partial edit, blocked."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.enums import ChatAction, ParseMode
from aiogram.methods import SendMessageDraft, SetMessageReaction
from aiogram.types import BotCommand, ReactionTypeEmoji

from qanot.telegram.formatting import MAX_MSG_LEN, _md_to_html, _sanitize_response, _split_text

if TYPE_CHECKING:
    from aiogram import Bot
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)

# Above this, a reply is too long for the Haiku reply-validator to echo
# back without truncating (its budget is validator.VALIDATOR_MAX_TOKENS).
# The artifact this layer is meant to catch — a stray Notion-title /
# filename / daily-note heading in the narrative — is always far shorter
# than this, so skipping long replies loses no real coverage while
# removing the truncation→retry-loop failure mode.
_REPLY_VALIDATE_MAX_CHARS = 1200


# Per-tool emoji for progress bubbles. Matched by substring so families of
# tools (web_*, channel_*, smartup_*) share an icon without enumerating each.
_TOOL_EMOJI_EXACT = {
    "web_search": "🔍", "web_fetch": "🌐", "run_command": "⚙️",
    "generate_image": "🎨", "edit_image": "🖌️", "send_file": "📎",
    "create_reel": "🎬", "clip_video": "✂️",
}
_TOOL_EMOJI_PREFIX = (
    ("web", "🌐"), ("search", "🔍"), ("rag", "📚"), ("memory", "🧠"),
    ("read", "📖"), ("write", "📝"), ("edit", "✏️"), ("list", "📂"),
    ("create_", "📝"), ("send", "📤"), ("channel_", "📣"), ("voice", "🎙️"),
    ("image", "🎨"), ("cron", "⏰"), ("spawn", "🤖"), ("delegate", "🤖"),
    ("agent", "🤖"), ("smartup", "📦"), ("sheets", "📊"), ("doc", "📄"),
)


def _tool_emoji(name: str) -> str:
    """Pick a progress-bubble emoji for a tool name."""
    if name in _TOOL_EMOJI_EXACT:
        return _TOOL_EMOJI_EXACT[name]
    low = name.lower()
    for frag, emoji in _TOOL_EMOJI_PREFIX:
        if frag in low:
            return emoji
    return "🔧"


def _tool_arg_preview(tool_input: dict | None, limit: int = 80) -> str:
    """One-line preview of the most salient tool argument, for 'detailed' mode."""
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    # Prefer the human-meaningful field if present, else the first scalar.
    for key in ("query", "q", "url", "name", "path", "title", "text", "prompt", "command", "code"):
        if key in tool_input and isinstance(tool_input[key], (str, int, float)):
            val = str(tool_input[key]).strip().replace("\n", " ")
            return val[:limit] + ("…" if len(val) > limit else "")
    for v in tool_input.values():
        if isinstance(v, (str, int, float)):
            val = str(v).strip().replace("\n", " ")
            return val[:limit] + ("…" if len(val) > limit else "")
    return ""


def _tool_progress_text(mode: str, tool_call) -> str:
    """Render a progress bubble for a tool call, or '' when nothing to show."""
    if mode == "off" or tool_call is None:
        return ""
    name = getattr(tool_call, "name", "") or "tool"
    emoji = _tool_emoji(name)
    if mode == "detailed":
        preview = _tool_arg_preview(getattr(tool_call, "input", None))
        return f"{emoji} {name}: {preview}" if preview else f"{emoji} {name}…"
    return f"{emoji} {name}…"


class StreamingMixin:
    """Mixin providing response strategy methods for TelegramAdapter."""

    # These will be set by TelegramAdapter
    bot: "Bot"
    agent: "Agent"
    config: "Config"
    _draft_counter: int

    def _next_draft_id(self) -> int:
        """Generate a unique draft_id for sendMessageDraft."""
        self._draft_counter += 1
        return self._draft_counter

    async def _respond_stream(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None, thread_id: int | None = None, message_id: int | None = None, system_prompt_override: str | None = None) -> None:
        """Stream response via sendMessageDraft → sendMessage."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        draft_id = self._next_draft_id()
        accumulated = ""
        last_flush = 0.0
        last_sent_text = ""
        interval = self.config.stream_flush_interval
        drafting_paused = False
        saw_tool_use = False
        progress_mode = getattr(self.config, "tool_progress", "off")
        progress_ids: list[int] = []
        last_progress_text = ""
        hb_task = self._start_heartbeat(chat_id, thread_id, progress_ids)

        done_response = None
        try:
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images, chat_id=chat_id, message_id=message_id, thread_id=thread_id, system_prompt_override=system_prompt_override):
                if event.type == "text_delta":
                    accumulated += event.text
                    if drafting_paused:
                        drafting_paused = False
                    else:
                        now = asyncio.get_running_loop().time()
                        if now - last_flush >= interval and accumulated != last_sent_text:
                            typing_task.cancel()
                            await self._send_draft(chat_id, draft_id, accumulated, thread_id=thread_id)
                            last_sent_text = accumulated
                            last_flush = now

                elif event.type == "tool_use":
                    saw_tool_use = True
                    drafting_paused = True
                    if accumulated and accumulated != last_sent_text:
                        await self._send_draft(chat_id, draft_id, accumulated, thread_id=thread_id)
                        last_sent_text = accumulated
                    typing_task.cancel()
                    typing_task = asyncio.create_task(self._typing_loop(chat_id))
                    if progress_mode != "off":
                        bubble = _tool_progress_text(progress_mode, event.tool_call)
                        # Dedupe consecutive identical bubbles — a batch of the
                        # same tool (or minimal mode where args don't vary) would
                        # otherwise spam one line per call.
                        if bubble and bubble != last_progress_text:
                            mid = await self._send_tool_progress(chat_id, bubble, thread_id=thread_id)
                            if mid:
                                progress_ids.append(mid)
                            last_progress_text = bubble

                elif event.type == "done":
                    done_response = event.response
                    break
        finally:
            typing_task.cancel()
            if hb_task is not None:
                hb_task.cancel()

        done_content = (done_response.content if done_response and done_response.content else "")
        cleanup = getattr(self.config, "tool_progress_cleanup", True)
        if accumulated:
            final_text = accumulated
        elif done_content:
            final_text = done_content
        elif saw_tool_use:
            # Tools spoke for the model (e.g. burst of tg_send_poll ending in
            # end_turn with no narrative). Not an error — just nothing left to say.
            if cleanup and progress_ids:
                await self._delete_messages(chat_id, progress_ids)
            return
        else:
            final_text = "Xatolik yuz berdi, qaytadan urinib ko'ring."
            cleanup = False  # keep progress breadcrumbs on the error path
        await self._send_final(chat_id, final_text, reply_to=reply_to, thread_id=thread_id)
        if cleanup and progress_ids:
            await self._delete_messages(chat_id, progress_ids)

    async def _respond_partial(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None, thread_id: int | None = None, message_id: int | None = None, system_prompt_override: str | None = None) -> None:
        """Stream response via editMessageText (pre-9.5 fallback)."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        accumulated = ""
        last_flush = 0.0
        interval = self.config.stream_flush_interval
        sent_msg_id: int | None = None
        saw_tool_use = False

        done_response = None
        try:
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images, chat_id=chat_id, message_id=message_id, thread_id=thread_id, system_prompt_override=system_prompt_override):
                if event.type == "text_delta":
                    accumulated += event.text
                    now = asyncio.get_running_loop().time()
                    if now - last_flush >= interval and accumulated.strip():
                        if sent_msg_id is None:
                            try:
                                send_kwargs: dict = {"chat_id": chat_id, "text": accumulated[:MAX_MSG_LEN]}
                                if reply_to:
                                    send_kwargs["reply_to_message_id"] = reply_to
                                if thread_id:
                                    send_kwargs["message_thread_id"] = thread_id
                                sent_msg_id = (await self.bot.send_message(**send_kwargs)).message_id
                            except Exception as e:
                                logger.warning("Partial send failed: %s", e)
                        else:
                            try:
                                await self.bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=sent_msg_id,
                                    text=accumulated[:MAX_MSG_LEN],
                                )
                            except Exception as e:
                                logger.debug("Partial edit skipped (unchanged text): %s", e)
                        last_flush = now

                elif event.type == "tool_use":
                    saw_tool_use = True

                elif event.type == "done":
                    done_response = event.response
                    break
        finally:
            typing_task.cancel()

        done_content = (done_response.content if done_response and done_response.content else "")
        if accumulated:
            final_text = accumulated
        elif done_content:
            final_text = done_content
        elif saw_tool_use and sent_msg_id is None:
            # Tools spoke for the model; no narrative text to send.
            return
        else:
            final_text = "Xatolik yuz berdi, qaytadan urinib ko'ring."
        if sent_msg_id:
            html = _md_to_html(final_text)
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=sent_msg_id,
                    text=html[:MAX_MSG_LEN], parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.debug("Final partial edit failed: %s", e)
            if len(html) > MAX_MSG_LEN:
                for chunk in _split_text(html[MAX_MSG_LEN:]):
                    await self._send_final_chunk(chat_id, chunk, thread_id=thread_id)
        else:
            await self._send_final(chat_id, final_text, reply_to=reply_to, thread_id=thread_id)

    async def _respond_blocked(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None, thread_id: int | None = None, message_id: int | None = None, system_prompt_override: str | None = None) -> None:
        """Wait for full response, then send."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        hb_ids: list[int] = []
        hb_task = self._start_heartbeat(chat_id, thread_id, hb_ids)
        try:
            response = await self.agent.run_turn(text, user_id=user_id, images=images, chat_id=chat_id, message_id=message_id, thread_id=thread_id, system_prompt_override=system_prompt_override)
        finally:
            typing_task.cancel()
            if hb_task is not None:
                hb_task.cancel()
        await self._send_final(chat_id, response or "(No response)", reply_to=reply_to, thread_id=thread_id)
        if getattr(self.config, "tool_progress_cleanup", True) and hb_ids:
            await self._delete_messages(chat_id, hb_ids)

    # ── Low-level send methods ───────────────────────────────

    async def _send_draft(
        self, chat_id: int, draft_id: int, text: str,
        *, thread_id: int | None = None,
    ) -> None:
        """Send a streaming draft via sendMessageDraft.

        Each flush carries the FULL accumulated text (not a delta), so
        running it through ``_md_to_html`` produces valid HTML on every
        tick: only markdown spans that have reached their closing marker
        are converted, unclosed spans stay literal. ``sendMessageDraft``
        supports ``parse_mode`` since Bot API 9.5 — without it the user
        sees raw ``**``/``##``/``<b>`` in the draft and the message only
        becomes pretty once ``_send_final`` fires at end-of-stream.

        When ``thread_id`` is set, the draft is delivered into that
        thread (Bot API 10.0 Threaded Mode). Without it, drafts go to
        the base view — which would land the streaming output in the
        wrong place when the user is reading inside a thread.
        """
        html = _md_to_html(text)[:4096]
        kwargs: dict = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": html,
            "parse_mode": ParseMode.HTML,
        }
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        try:
            await self.bot(SendMessageDraft(**kwargs))
            return
        except Exception as e:
            # Rare: a partial chunk slipped past the markdown rules with
            # a tag Telegram's strict HTML parser rejects. Degrade to
            # plain text for THIS flush so the user still sees progress;
            # the next flush will re-attempt with parse_mode and the
            # final send_message renders the full reply correctly.
            logger.debug("sendMessageDraft HTML failed (%s) — plain retry", e)
            kwargs.pop("parse_mode", None)
            kwargs["text"] = text[:4096]
            try:
                await self.bot(SendMessageDraft(**kwargs))
            except Exception as e2:
                logger.debug("sendMessageDraft plain retry failed: %s", e2)

    async def _send_final(self, chat_id: int, text: str, *, reply_to: int | None = None, thread_id: int | None = None) -> None:
        """Send the final formatted message, splitting if needed."""
        if not text:
            return
        # Run the reply text through the memo validator BEFORE HTML formatting
        # so a feedback-typed memo can rewrite the response before the user
        # sees it. This is the final-reply layer of the buried-bullet bug
        # fix — the registry validator catches tool-call text, this catches
        # the assistant's narrative reply. No-op when no rules in scope.
        text = await self._maybe_validate_reply(
            text, thread_id=thread_id,
        )
        text = _sanitize_response(text)
        html = _md_to_html(text)
        chunks = _split_text(html)
        total = len(chunks)
        last_message_id = 0
        for i, chunk in enumerate(chunks):
            # On a split reply, footer each part with "(i/n)" so the reader
            # knows more is coming and in what order. Appended outside any
            # <pre> span (split guarantees balanced chunks), so it's safe in
            # both the HTML and plain-text send paths. Single chunk: no footer.
            body = chunk if total == 1 else f"{chunk}\n\n({i + 1}/{total})"
            sent_id = await self._send_final_chunk(
                chat_id, body,
                reply_to=reply_to if i == 0 else None,
                thread_id=thread_id,
            )
            if sent_id:
                last_message_id = sent_id
            await asyncio.sleep(0.1)

        # Track bot replies in group chats for zen-mode signal scoring.
        # No-op when the adapter doesn't have group state wired (e.g.
        # in older callers, tests, or sub-agent bots).
        state = getattr(self, "_group_state", None)
        if state is not None and chat_id < 0 and last_message_id:
            # chat_id<0 distinguishes Telegram groups/supergroups from
            # private chats without an extra API call. Bots always have
            # positive ids; users always positive; groups always negative.
            state.record_bot_reply(
                chat_id, text=text, message_id=last_message_id,
            )

    async def _send_final_chunk(
        self, chat_id: int, html_chunk: str,
        *, reply_to: int | None = None, thread_id: int | None = None,
    ) -> int:
        """Send a single chunk with HTML fallback to plain text.
        Returns the sent message_id (0 on failure)."""
        kwargs: dict = {"chat_id": chat_id, "text": html_chunk}
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        try:
            msg = await self.bot.send_message(**kwargs, parse_mode=ParseMode.HTML)
            return int(getattr(msg, "message_id", 0) or 0)
        except Exception as e:
            logger.debug("HTML parse failed, falling back to plain text: %s", e)
            try:
                msg = await self.bot.send_message(**kwargs)
                return int(getattr(msg, "message_id", 0) or 0)
            except Exception as e:
                logger.error("Failed to send message: %s", e)
                return 0

    async def _maybe_validate_reply(
        self, text: str, *, thread_id: int | None = None,
    ) -> str:
        """Run the assistant's reply through the memo validator.

        Used by ``_send_final`` to catch rule violations the registry
        couldn't (e.g. the LLM put a banned phrase in the narrative
        reply rather than in a tool input). Falls back to the original
        text on any failure — we never block a response on validator
        issues. Returns the verified (possibly rewritten) text.
        """
        if not text or not text.strip():
            return text
        # The leak this layer guards against — a write-artifact-format
        # rule (Notion title, filename, daily-note heading) bleeding into
        # the narrative reply — is short by construction. A long answer
        # (a script, a how-to, code) is never that artifact, but it IS
        # too big for Haiku to round-trip within its output budget: the
        # echo gets truncated, the user sees a cut-off reply, and the
        # agent retries into a tool-call loop. Skip the reply validator
        # above this size; the structured-write registry validator still
        # guards the actual tool inputs regardless of reply length.
        if len(text) > _REPLY_VALIDATE_MAX_CHARS:
            return text
        agent = getattr(self, "agent", None)
        if agent is None:
            return text
        provider = getattr(agent, "provider", None)
        client = getattr(provider, "client", None) if provider else None
        if client is None or not hasattr(client, "messages"):
            return text
        user_id = (
            str(agent.current_user_id) if getattr(agent, "current_user_id", None) else None
        )
        thread_str = str(thread_id) if thread_id else None
        try:
            from qanot.memos import build_runtime
            runtime = build_runtime(
                client=client,
                workspace_dir=self.config.workspace_dir,
                user_id=user_id,
                thread_id=thread_str,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memo reply validator build failed: %s", exc)
            return text
        if runtime is None:
            return text
        try:
            result = await runtime(text, field_context="Telegram reply text")
        except Exception as exc:  # noqa: BLE001
            logger.warning("memo reply validator call failed: %s", exc)
            return text
        if result is None or not getattr(result, "was_changed", False):
            return text
        logger.info(
            "memo reply validator rewrote response: %d violation(s)",
            len(getattr(result, "violations", []) or []),
        )
        return result.verified

    async def send_message(self, chat_id: int, text: str) -> None:
        """Public method to send a message to a chat (used by sub-agents)."""
        await self._send_final(chat_id, text)

    async def _action_loop(self, chat_id: int, action: ChatAction = ChatAction.TYPING) -> None:
        """Send a chat action indicator every 4 seconds until cancelled."""
        try:
            while True:
                await self.bot.send_chat_action(chat_id=chat_id, action=action)
                await asyncio.sleep(4)
        except (asyncio.CancelledError, Exception):
            pass

    async def _typing_loop(self, chat_id: int) -> None:
        """Send typing indicator until cancelled."""
        await self._action_loop(chat_id, ChatAction.TYPING)

    async def _voice_action_loop(self, chat_id: int) -> None:
        """Send 'recording voice' indicator until cancelled."""
        await self._action_loop(chat_id, ChatAction.RECORD_VOICE)

    async def _react(self, chat_id: int, message_id: int, emoji: str) -> None:
        """Set a reaction emoji on a message. Silently fails if unsupported."""
        if not self.config.reactions_enabled:
            return
        try:
            await self.bot(SetMessageReaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            ))
        except Exception as e:
            logger.debug("Reaction unavailable in chat %s: %s", chat_id, e)

    async def _send_tool_progress(
        self, chat_id: int, text: str, *, thread_id: int | None = None,
    ) -> int:
        """Send a transient tool-progress bubble. Returns message_id (0 on failure)."""
        kwargs: dict = {"chat_id": chat_id, "text": text}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        try:
            msg = await self.bot.send_message(**kwargs)
            return int(getattr(msg, "message_id", 0) or 0)
        except Exception as e:
            logger.debug("tool-progress bubble send failed: %s", e)
            return 0

    def _start_heartbeat(
        self, chat_id: int, thread_id: int | None, sink: list[int],
    ) -> "asyncio.Task | None":
        """Start a 'still working…' heartbeat task, or None when disabled.

        Sends a progress message every ``long_run_notice_seconds`` so long
        turns (heavy tools, many iterations) don't look frozen. Sent ids are
        appended to ``sink`` so they're cleaned up alongside tool bubbles.
        """
        interval = int(getattr(self.config, "long_run_notice_seconds", 0) or 0)
        if interval <= 0:
            return None
        return asyncio.create_task(
            self._heartbeat_loop(chat_id, interval, thread_id, sink)
        )

    async def _heartbeat_loop(
        self, chat_id: int, interval: int, thread_id: int | None, sink: list[int],
    ) -> None:
        """Emit '⏳ Ishlayapman… (elapsed)' every ``interval`` seconds until cancelled."""
        elapsed = 0
        try:
            while True:
                await asyncio.sleep(interval)
                elapsed += interval
                mins, secs = divmod(elapsed, 60)
                if mins and secs:
                    human = f"{mins} daq {secs} s"
                elif mins:
                    human = f"{mins} daqiqa"
                else:
                    human = f"{secs} soniya"
                mid = await self._send_tool_progress(
                    chat_id, f"⏳ Ishlayapman… ({human})", thread_id=thread_id,
                )
                if mid:
                    sink.append(mid)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 — heartbeat must never break a turn
            logger.debug("heartbeat loop stopped: %s", e)

    async def _delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        """Best-effort delete of bot messages (progress bubbles, heartbeats).

        Silently ignores failures — in groups the bot may lack delete rights,
        and a bubble older than 48h can't be deleted. Either way a leftover
        progress line is harmless, so we never surface the error.
        """
        for mid in message_ids:
            if not mid:
                continue
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception as e:
                logger.debug("progress bubble delete failed (%s): %s", mid, e)

    async def _register_commands(self) -> None:
        """Register dynamic bot commands with Telegram (appears in / menu)."""
        commands = [
            BotCommand(command="model", description="Model tanlash"),
            BotCommand(command="think", description="Fikrlash darajasi"),
            BotCommand(command="voice", description="Ovoz rejimi"),
            BotCommand(command="voiceprovider", description="Ovoz provayderi"),
            BotCommand(command="lang", description="STT tili"),
            BotCommand(command="mode", description="Javob rejimi"),
            BotCommand(command="routing", description="Model routing on/off"),
            BotCommand(command="group", description="Guruh rejimi"),
            BotCommand(command="topic", description="Topic-agent bog'lash"),
            BotCommand(command="exec", description="Xavfsizlik darajasi"),
            BotCommand(command="code", description="Code execution (sandbox)"),
            BotCommand(command="mcp", description="MCP serverlar"),
            BotCommand(command="plugins", description="Pluginlar boshqaruvi"),
            BotCommand(command="status", description="Sessiya holati"),
            BotCommand(command="usage", description="Token sarfi va narxi"),
            BotCommand(command="context", description="Kontekst tafsilotlari"),
            BotCommand(command="config", description="Barcha sozlamalar"),
            BotCommand(command="reset", description="Suhbatni tozalash"),
            BotCommand(command="resume", description="Oldingi suhbatni tiklash"),
            BotCommand(command="compact", description="Kontekstni siqish"),
            BotCommand(command="export", description="Sessiyani eksport"),
            BotCommand(command="joincall", description="Ovozli suhbatga qo'shilish"),
            BotCommand(command="leavecall", description="Ovozli suhbatdan chiqish"),
            BotCommand(command="callstatus", description="Qo'ng'iroq holati"),
            BotCommand(command="stop", description="Amalni to'xtatish"),
            BotCommand(command="id", description="Foydalanuvchi ID"),
            BotCommand(command="help", description="Barcha buyruqlar"),
        ]
        try:
            await self.bot.set_my_commands(commands)
            logger.info("Bot commands registered: %s", [c.command for c in commands])
        except Exception as e:
            logger.warning("Failed to register bot commands: %s", e)
