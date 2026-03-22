"""Telegram command handlers — /reset, /status, /help, /model, callbacks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Message
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)


class HandlersMixin:
    """Mixin providing command handler methods for TelegramAdapter."""

    bot: "Bot"
    agent: "Agent"
    config: "Config"
    _pending_approvals: dict[str, dict]

    async def _handle_reset(self, message: "Message") -> None:
        """Handle /reset — clear conversation history."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access
        self.agent.reset(conv_key)
        await self._send_final(
            message.chat.id,
            "Suhbat tozalandi. Yangi suhbatni boshlashingiz mumkin.",
        )
        logger.info("Conversation reset: %s", conv_key)

    async def _handle_status(self, message: "Message") -> None:
        """Handle /status — show session info."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access
        status = self.agent.context.session_status()
        conv = self.agent.get_conversation(conv_key)

        # Provider health info
        provider = self.agent.provider
        provider_info = f"Provider: {self.config.provider}\nModel: {self.config.model}"
        if hasattr(provider, "status"):
            try:
                ps_data = provider.status()
                if isinstance(ps_data, list):
                    lines = []
                    for ps in ps_data:
                        icon = "\U0001f7e2" if ps.get("available") else "\U0001f534"
                        active = " \u25c0" if ps.get("active") else ""
                        err = f" ({ps['last_error']})" if ps.get("last_error") else ""
                        lines.append(f"{icon} {ps['name']} \u2014 {ps['model']}{err}{active}")
                    provider_info = "Providers:\n" + "\n".join(lines)
                elif isinstance(ps_data, dict):
                    stats = ps_data.get("stats", {})
                    provider_info = (
                        f"Routing: {ps_data.get('cheap_model', '?')} / "
                        f"{ps_data.get('primary_model', '?')}\n"
                        f"Savings: {stats.get('savings_pct', 0)}% "
                        f"({stats.get('routed_cheap', 0)} cheap / {stats.get('total', 0)} total)"
                    )
            except Exception as e:
                logger.debug("Failed to get provider stats: %s", e)

        status_text = (
            f"**Session Status**\n\n"
            f"Context: {status['context_percent']}%\n"
            f"Tokens: {status['total_tokens']:,}\n"
            f"Turns: {status['turn_count']}\n"
            f"Messages: {len(conv)}\n"
            f"Buffer: {'active' if status['buffer_active'] else 'inactive'}\n"
            f"{provider_info}"
        )
        await self._send_final(message.chat.id, status_text)

    async def _handle_help(self, message: "Message") -> None:
        """Handle /help — show available commands."""
        if not self._check_command_access(message):
            return

        help_text = (
            "**Buyruqlar:**\n\n"
            "/reset \u2014 Suhbatni tozalash\n"
            "/status \u2014 Sessiya holati va statistika\n"
            "/model \u2014 Model tanlash (Opus/Sonnet/Haiku)\n"
            "/help \u2014 Yordam\n\n"
            "**Imkoniyatlar:**\n"
            "\U0001f4dd Matn \u2014 savol, buyruq, suhbat\n"
            "\U0001f399 Ovozli xabar \u2014 avtomatik transcribe\n"
            "\U0001f4f7 Rasm \u2014 tahlil qilish (vision)\n"
            "\U0001f4ce Fayl \u2014 PDF, doc, excel o\u2018qish\n"
            "\U0001f517 Link \u2014 avtomatik tushunish\n"
            "\U0001f3a8 Rasm yaratish \u2014 \"rasm chiz: ...\" deb yozing\n"
        )
        await self._send_final(message.chat.id, help_text)

    async def _handle_model(self, message: "Message") -> None:
        """Handle /model — show current model and switch via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.model
        models = [
            ("claude-opus-4-6", "Opus 4.6", "Eng kuchli"),
            ("claude-sonnet-4-6", "Sonnet 4.6", "Tez va sifatli"),
            ("claude-haiku-4-5-20251001", "Haiku 4.5", "Eng arzon"),
        ]

        buttons = []
        for model_id, label, desc in models:
            marker = " \u25c0" if model_id == current else ""
            check = "\u2705 " if model_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}{marker}",
                callback_data=f"model:{model_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        routing_text = ""
        if self.config.routing_enabled:
            routing_text = "\n\n\U0001f500 Routing: ON (auto Haiku/Sonnet/Opus)"

        await message.reply(
            f"\U0001f916 **Joriy model:** `{current}`{routing_text}\n\nModel tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def _handle_callback_query(self, callback: "CallbackQuery") -> None:
        """Handle inline button callbacks (approvals, model picker, etc.)."""
        data = callback.data or ""
        user_id = callback.from_user.id

        # Exec approval: approve:<id> or deny:<id>
        if data.startswith("approve:") or data.startswith("deny:"):
            action, approval_id = data.split(":", 1)
            pending = self._pending_approvals.pop(approval_id, None)
            if not pending:
                await callback.answer("Bu so\u2018rov muddati tugagan.", show_alert=True)
                return
            if pending["user_id"] != user_id:
                await callback.answer("Faqat so\u2018rov egasi ruxsat berishi mumkin.", show_alert=True)
                self._pending_approvals[approval_id] = pending
                return

            approved = action == "approve"
            pending["future"].set_result(approved)

            status = "\u2705 Ruxsat berildi" if approved else "\u274c Rad etildi"
            try:
                await callback.message.edit_text(
                    f"{callback.message.text}\n\n{status}",
                )
            except Exception as e:
                logger.debug("Failed to update approval message: %s", e)
            await callback.answer(status)
            return

        # Model switch: model:<model_id>
        if data.startswith("model:"):
            model_id = data.split(":", 1)[1]
            provider = self.agent.provider
            if hasattr(provider, '_provider'):
                provider._provider.model = model_id
                provider._primary_model = model_id
                provider.model = model_id
            else:
                provider.model = model_id
            self.config.model = model_id

            model_names = {
                "claude-opus-4-6": "Opus 4.6",
                "claude-sonnet-4-6": "Sonnet 4.6",
                "claude-haiku-4-5-20251001": "Haiku 4.5",
            }
            name = model_names.get(model_id, model_id)
            await callback.answer(f"\u2705 Model: {name}")
            try:
                await callback.message.edit_text(f"\u2705 Model o\u2018zgartirildi: **{name}** (`{model_id}`)", parse_mode="Markdown")
            except Exception as e:
                logger.debug("Failed to update model switch message: %s", e)
            return

        await callback.answer("Noma\u2018lum buyruq", show_alert=True)

    async def request_approval(
        self, chat_id: int, user_id: int, command: str, reason: str,
    ) -> bool:
        """Send inline approval buttons and wait for user response."""
        loop = asyncio.get_running_loop()
        approval_id = hashlib.sha256(f"{user_id}:{command}:{loop.time()}".encode()).hexdigest()[:12]

        future: asyncio.Future[bool] = loop.create_future()
        self._pending_approvals[approval_id] = {
            "command": command,
            "user_id": user_id,
            "chat_id": chat_id,
            "future": future,
        }

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="\u2705 Ruxsat", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton(text="\u274c Rad", callback_data=f"deny:{approval_id}"),
            ]
        ])

        text = f"\u26a0\ufe0f **Buyruq ruxsat talab qiladi**\n\n`{command}`\n\nSabab: {reason}"
        await self.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")

        try:
            return await asyncio.wait_for(future, timeout=120)
        except asyncio.TimeoutError:
            self._pending_approvals.pop(approval_id, None)
            return False
