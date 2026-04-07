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

        done_response = None
        try:
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images, chat_id=chat_id, message_id=message_id, system_prompt_override=system_prompt_override):
                if event.type == "text_delta":
                    accumulated += event.text
                    if drafting_paused:
                        drafting_paused = False
                    else:
                        now = asyncio.get_running_loop().time()
                        if now - last_flush >= interval and accumulated != last_sent_text:
                            typing_task.cancel()
                            await self._send_draft(chat_id, draft_id, accumulated)
                            last_sent_text = accumulated
                            last_flush = now

                elif event.type == "tool_use":
                    drafting_paused = True
                    if accumulated and accumulated != last_sent_text:
                        await self._send_draft(chat_id, draft_id, accumulated)
                        last_sent_text = accumulated
                    typing_task.cancel()
                    typing_task = asyncio.create_task(self._typing_loop(chat_id))

                elif event.type == "done":
                    done_response = event.response
                    break
        finally:
            typing_task.cancel()

        # Use accumulated stream text, fall back to done response content, then error message
        done_content = (done_response.content if done_response and done_response.content else "")
        final_text = accumulated or done_content or "Xatolik yuz berdi, qaytadan urinib ko'ring."
        await self._send_final(chat_id, final_text, reply_to=reply_to, thread_id=thread_id)

    async def _respond_partial(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None, thread_id: int | None = None, message_id: int | None = None, system_prompt_override: str | None = None) -> None:
        """Stream response via editMessageText (pre-9.5 fallback)."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        accumulated = ""
        last_flush = 0.0
        interval = self.config.stream_flush_interval
        sent_msg_id: int | None = None

        done_response = None
        try:
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images, chat_id=chat_id, message_id=message_id, system_prompt_override=system_prompt_override):
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

                elif event.type == "done":
                    done_response = event.response
                    break
        finally:
            typing_task.cancel()

        done_content = (done_response.content if done_response and done_response.content else "")
        final_text = accumulated or done_content or "Xatolik yuz berdi, qaytadan urinib ko'ring."
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
        try:
            response = await self.agent.run_turn(text, user_id=user_id, images=images, chat_id=chat_id, message_id=message_id, system_prompt_override=system_prompt_override)
        finally:
            typing_task.cancel()
        await self._send_final(chat_id, response or "(No response)", reply_to=reply_to, thread_id=thread_id)

    # ── Low-level send methods ───────────────────────────────

    async def _send_draft(self, chat_id: int, draft_id: int, text: str) -> None:
        """Send a streaming draft via sendMessageDraft."""
        try:
            await self.bot(SendMessageDraft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text[:4096],
            ))
        except Exception as e:
            logger.debug("sendMessageDraft failed: %s", e)

    async def _send_final(self, chat_id: int, text: str, *, reply_to: int | None = None, thread_id: int | None = None) -> None:
        """Send the final formatted message, splitting if needed."""
        if not text:
            return
        text = _sanitize_response(text)
        html = _md_to_html(text)
        chunks = _split_text(html)
        for i, chunk in enumerate(chunks):
            await self._send_final_chunk(chat_id, chunk, reply_to=reply_to if i == 0 else None, thread_id=thread_id)
            await asyncio.sleep(0.1)

    async def _send_final_chunk(self, chat_id: int, html_chunk: str, *, reply_to: int | None = None, thread_id: int | None = None) -> None:
        """Send a single chunk with HTML fallback to plain text."""
        kwargs: dict = {"chat_id": chat_id, "text": html_chunk}
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        try:
            await self.bot.send_message(**kwargs, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug("HTML parse failed, falling back to plain text: %s", e)
            try:
                await self.bot.send_message(**kwargs)
            except Exception as e:
                logger.error("Failed to send message: %s", e)

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
