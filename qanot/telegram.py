"""Telegram adapter — aiogram 3.x long polling."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.types import Message

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.scheduler import CronScheduler

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000


def _md_to_html(text: str) -> str:
    """Convert agent markdown to Telegram HTML."""
    # Escape HTML entities
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ```code blocks``` → <pre>
    text = re.sub(r"```(\w*)\n([\s\S]*?)```", r"<pre>\2</pre>", text)

    # Markdown tables → <pre>
    def wrap_table(m: re.Match) -> str:
        return f"\n<pre>{m.group(0).strip()}</pre>\n"
    text = re.sub(r"(?:^[|].*\n?)+", wrap_table, text, flags=re.MULTILINE)

    # Horizontal rules
    text = re.sub(r"^---+$", "\u2501" * 18, text, flags=re.MULTILINE)

    # **bold** → <b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # `inline code` → <code>
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # ## Headings → <b>
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Clean extra blanks
    text = re.sub(r"\n{3,}", "\n\n", text)

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


class TelegramAdapter:
    """Handles Telegram bot communication via aiogram long polling."""

    def __init__(
        self,
        config: "Config",
        agent: "Agent",
        scheduler: "CronScheduler | None" = None,
    ):
        self.config = config
        self.agent = agent
        self.scheduler = scheduler
        self.bot = Bot(token=config.bot_token)
        self.dp = Dispatcher()
        self._setup_handlers()
        self._lock = asyncio.Lock()
        self._concurrent = asyncio.Semaphore(config.max_concurrent)

    def _setup_handlers(self) -> None:
        """Register message handlers."""

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.photo)
        async def handle_photo(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.document)
        async def handle_document(message: Message) -> None:
            await self._handle_message(message)

    def _is_allowed(self, user_id: int) -> bool:
        """Check if user is in allowed list."""
        if not self.config.allowed_users:
            return True  # No restriction
        return user_id in self.config.allowed_users

    async def _handle_message(self, message: Message) -> None:
        """Process an incoming message."""
        if not message.from_user:
            return

        user_id = message.from_user.id

        # Check allowlist
        if not self._is_allowed(user_id):
            return

        # Extract text
        text = message.text or message.caption or ""

        # Handle photos
        if message.photo:
            text = f"[Photo received] {text}".strip()

        # Handle documents — download to workspace
        if message.document:
            fname = message.document.file_name or "file"
            try:
                file = await self.bot.get_file(message.document.file_id)
                dl_dir = Path(self.config.workspace_dir) / "uploads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                dl_path = dl_dir / fname
                await self.bot.download_file(file.file_path, dl_path)
                text = f"[Fayl yuklandi: uploads/{fname}] {text}".strip()
                logger.info("Downloaded file: %s", dl_path)
            except Exception as e:
                logger.error("File download failed: %s", e)
                text = f"[Document: {fname} — yuklab bo'lmadi] {text}".strip()

        if not text:
            return

        async with self._concurrent:
            # Send typing indicator
            try:
                await self.bot.send_chat_action(
                    chat_id=message.chat.id,
                    action=ChatAction.TYPING,
                )
            except Exception:
                pass

            # Run agent
            try:
                response = await self.agent.run_turn(text)
            except Exception as e:
                logger.error("Agent error: %s", e)
                response = "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring."

            # Send response
            await self._send_response(message.chat.id, response)

    async def _send_response(self, chat_id: int, text: str) -> None:
        """Send a response, splitting long messages."""
        if not text:
            return

        html = _md_to_html(text)
        chunks = _split_text(html)

        for chunk in chunks:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
                logger.info("sendMessage ok")
            except Exception:
                # Fallback to plain text
                try:
                    plain_chunks = _split_text(text)
                    for pc in plain_chunks:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=pc,
                        )
                        logger.info("sendMessage ok")
                except Exception as e:
                    logger.error("Failed to send message: %s", e)
                break

            await asyncio.sleep(0.1)

    async def _proactive_loop(self) -> None:
        """Check scheduler queue for proactive messages to send."""
        if not self.scheduler:
            return

        while True:
            try:
                msg = await asyncio.wait_for(
                    self.scheduler.message_queue.get(), timeout=5.0
                )
                msg_type = msg.get("type", "")
                text = msg.get("text", "")

                if msg_type == "proactive" and text:
                    # Send to all allowed users
                    for user_id in self.config.allowed_users:
                        await self._send_response(user_id, text)
                elif msg_type == "system_event" and text:
                    # Inject into agent as if user sent it
                    response = await self.agent.run_turn(text)
                    # System events don't produce user-visible output by default
                    # unless the agent writes to proactive-outbox
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Proactive loop error: %s", e)
                await asyncio.sleep(5)

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        print("[telegram] starting provider")
        logger.info("[telegram] starting provider")

        # Start proactive message delivery loop
        asyncio.create_task(self._proactive_loop())

        try:
            await self.dp.start_polling(self.bot, drop_pending_updates=True)
        finally:
            await self.bot.session.close()
