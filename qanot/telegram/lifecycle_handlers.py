"""Telegram lifecycle handlers — start, reset, resume, compact, export, stop, approvals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Message
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)


class LifecycleHandlersMixin:
    """Mixin providing lifecycle command handlers for TelegramAdapter."""

    bot: "Bot"
    agent: "Agent"
    config: "Config"
    _pending_approvals: dict[str, dict]

    # ── Config persistence helper ─────────────────────────

    def _save_config_field(self, field: str, value) -> None:
        """Persist a single config field change to config.json (atomic)."""
        try:
            from qanot.config import read_config_json, write_config_json
            raw = read_config_json()
            raw[field] = value
            write_config_json(raw)
        except Exception as e:
            logger.warning("Failed to save config field %s: %s", field, e)

    # ── /start ────────────────────────────────────────────

    async def _handle_start(self, message: "Message") -> None:
        """Handle /start — interactive onboarding wizard."""
        if not message.from_user:
            return

        user = message.from_user
        user_name = user.first_name or user.full_name or "do'st"
        bot_name = self.config.bot_name or "Qanot AI"

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="\U0001f1fa\U0001f1ff O'zbekcha",
                callback_data="onboard_lang:uz",
            )],
            [InlineKeyboardButton(
                text="\U0001f1f7\U0001f1fa Русский",
                callback_data="onboard_lang:ru",
            )],
            [InlineKeyboardButton(
                text="\U0001f1ec\U0001f1e7 English",
                callback_data="onboard_lang:en",
            )],
        ])

        welcome = (
            f"Salom, **{user_name}**! \U0001f44b\n\n"
            f"Men **{bot_name}** — shaxsiy AI yordamchingizman.\n\n"
            f"\U0001f9e0 Fikrlash, yozish, kod yozish, rasm yaratish\n"
            f"\U0001f50d Web qidirish va tahlil\n"
            f"\U0001f4c1 Fayllar bilan ishlash\n"
            f"\U0001f399 Ovozli xabarlarni tushunish\n"
            f"\U0001f310 MCP orqali tashqi xizmatlar\n\n"
            f"Tilni tanlang:"
        )

        await message.reply(welcome, reply_markup=keyboard, parse_mode="Markdown")

    async def _cb_onboard_lang(self, callback: "CallbackQuery", lang: str) -> None:
        """Handle onboarding language selection -> show use case picker."""
        lang_names = {"uz": "O'zbekcha", "ru": "Русский", "en": "English"}
        lang_name = lang_names.get(lang, lang)

        # Save voice language preference
        self.config.voice_language = lang
        self._save_config_field("voice_language", lang)

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="\U0001f4bb Dasturlash (coding)",
                callback_data="onboard_role:developer",
            )],
            [InlineKeyboardButton(
                text="\U0001f4bc Biznes va marketing",
                callback_data="onboard_role:business",
            )],
            [InlineKeyboardButton(
                text="\U0001f393 O'rganish va tadqiqot",
                callback_data="onboard_role:student",
            )],
            [InlineKeyboardButton(
                text="\U0001f3a8 Ijodiy ishlar (yozish, rasm)",
                callback_data="onboard_role:creative",
            )],
            [InlineKeyboardButton(
                text="\u2699\ufe0f Umumiy yordamchi",
                callback_data="onboard_role:general",
            )],
        ])

        await callback.answer(f"\u2705 {lang_name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Til: **{lang_name}**\n\n"
                f"Asosiy ishlatish maqsadingiz nima?",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to edit onboarding message: %s", e)

    async def _cb_onboard_role(self, callback: "CallbackQuery", role: str) -> None:
        """Handle onboarding role selection -> complete setup."""
        role_names = {
            "developer": "\U0001f4bb Dasturchi",
            "business": "\U0001f4bc Biznes",
            "student": "\U0001f393 O'rganuvchi",
            "creative": "\U0001f3a8 Ijodkor",
            "general": "\u2699\ufe0f Umumiy",
        }
        role_name = role_names.get(role, role)
        bot_name = self.config.bot_name or "Qanot AI"

        await callback.answer(f"\u2705 {role_name}")

        # Build final welcome with relevant commands
        tips = {
            "developer": (
                "**Foydali buyruqlar:**\n"
                "- Kod yozing: \"Python da API yoz\"\n"
                "- Xato toping: faylni yuboring va \"xato top\" deng\n"
                "- /code \u2014 sandbox rejimini yoqish\n"
                "- /think high \u2014 murakkab masalalar uchun"
            ),
            "business": (
                "**Foydali buyruqlar:**\n"
                "- Tahlil: \"Bu bozorni tahlil qil\"\n"
                "- Hujjat: \"Shartnoma yoz\"\n"
                "- Web: \"Bu kompaniya haqida ma'lumot top\"\n"
                "- /voice always \u2014 ovozli javoblar"
            ),
            "student": (
                "**Foydali buyruqlar:**\n"
                "- Tushuntiring: \"Kvant fizikasini sodda tushuntir\"\n"
                "- Tarjima: \"Bu matnni inglizchaga tarjima qil\"\n"
                "- Referat: \"AI haqida referat yoz\"\n"
                "- /think medium \u2014 chuqur javoblar"
            ),
            "creative": (
                "**Foydali buyruqlar:**\n"
                "- Rasm: \"Rasm chiz: bahor manzarasi\"\n"
                "- Yozish: \"Hikoya yoz: sarguzasht janrida\"\n"
                "- She'r: \"Vatan haqida she'r yoz\"\n"
                "- /voice always \u2014 ovozli suhbat"
            ),
            "general": (
                "**Foydali buyruqlar:**\n"
                "- Savol bering: istalgan mavzuda\n"
                "- Rasm yuboring: tahlil uchun\n"
                "- Fayl yuboring: o'qish uchun\n"
                "- /help \u2014 barcha buyruqlar"
            ),
        }

        tip = tips.get(role, tips["general"])

        try:
            await callback.message.edit_text(
                f"\u2705 **{bot_name} tayyor!**\n\n"
                f"Profil: {role_name}\n\n"
                f"{tip}\n\n"
                f"Xabar yozing yoki ovozli xabar yuboring \u2014 "
                f"men har doim tayyorman! \U0001f680",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to edit onboarding message: %s", e)

    # ── /reset ────────────────────────────────────────────

    async def _handle_reset(self, message: "Message") -> None:
        """Handle /reset — clear conversation history."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        # /reset <model> — reset + switch model
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            model_hint = parts[1].strip().lower()
            model_map = {
                "opus": "claude-opus-4-6",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            }
            if model_hint in model_map:
                new_model = model_map[model_hint]
                self._switch_model(new_model)

        self.agent.reset(conv_key)

        # Cancel all running sub-agents for this user
        if hasattr(self, 'subagent_manager') and self.subagent_manager:
            try:
                cancelled = await self.subagent_manager.cancel_all_for_user(conv_key)
                if cancelled:
                    logger.info("Cancelled %d sub-agents on reset for %s", cancelled, conv_key)
            except Exception as e:
                logger.debug("Failed to cancel sub-agents on reset: %s", e)

        await self._send_final(
            message.chat.id,
            "Suhbat tozalandi. Yangi suhbatni boshlashingiz mumkin.",
        )
        logger.info("Conversation reset: %s", conv_key)

    # ── /resume ───────────────────────────────────────────

    async def _handle_resume(self, message: "Message") -> None:
        """Handle /resume — restore conversation from last session."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        # Check if user already has an active conversation
        existing = self.agent.get_conversation(conv_key)
        if existing:
            await self._send_final(
                message.chat.id,
                f"Suhbat allaqachon faol ({len(existing)} xabar). "
                f"Tozalash uchun /reset yuboring.",
            )
            return

        # Restore from JSONL session history
        msg_count = self.agent.restore_user_session(str(conv_key))

        if msg_count > 0:
            await self._send_final(
                message.chat.id,
                f"Oldingi suhbat tiklandi ({msg_count} xabar). Davom etishingiz mumkin.",
            )
            logger.info("Session resumed for %s: %d messages", conv_key, msg_count)
        else:
            await self._send_final(
                message.chat.id,
                "Oldingi suhbat topilmadi. Yangi suhbatni boshlashingiz mumkin.",
            )

    # ── /compact ──────────────────────────────────────────

    async def _handle_compact(self, message: "Message") -> None:
        """Handle /compact — force context compaction."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        pct = self.agent.context.get_context_percent()
        if pct < 20:
            await self._send_final(
                message.chat.id,
                f"Kontekst faqat {pct:.0f}% to'lgan. Siqish kerak emas.",
            )
            return

        await self._send_final(message.chat.id, "\u23f3 Kontekst siqilmoqda...")
        try:
            messages = self.agent.get_conversation(conv_key)
            messages = await self.agent._handle_overflow(messages, conv_key)
            new_pct = self.agent.context.get_context_percent()
            await self._send_final(
                message.chat.id,
                f"\u2705 Kontekst siqildi: {pct:.0f}% \u2192 {new_pct:.0f}%",
            )
        except Exception as e:
            logger.error("Manual compaction failed: %s", e)
            await self._send_final(
                message.chat.id,
                f"\u274c Siqishda xatolik: {e}",
            )

    # ── /export ───────────────────────────────────────────

    async def _handle_export(self, message: "Message") -> None:
        """Handle /export — export session as HTML (default) or JSON.

        Usage: /export       -> HTML file
               /export json  -> raw JSON file
        """
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        conv = self.agent.get_conversation(conv_key)
        if not conv:
            await self._send_final(message.chat.id, "Suhbat tarixi bo'sh.")
            return

        # Parse format arg
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        fmt = parts[1].strip().lower() if len(parts) > 1 else "html"

        import tempfile
        from aiogram.types import FSInputFile

        if fmt == "json":
            export_data = {
                "conversation_key": conv_key,
                "model": self.config.model,
                "provider": self.config.provider,
                "turns": len([m for m in conv if m.get("role") == "user"]),
                "messages": conv,
            }
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="qanot_export_", delete=False,
            ) as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
                tmp_path = f.name
            filename = f"session_{conv_key}.json"
        else:
            # HTML export
            from qanot.export_html import render_session_html
            html = render_session_html(
                messages=conv,
                bot_name=self.config.bot_name or "Qanot AI",
                model=self.config.model,
            )
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", prefix="qanot_export_", delete=False,
                encoding="utf-8",
            ) as f:
                f.write(html)
                tmp_path = f.name
            filename = f"session_{conv_key}.html"

        try:
            doc = FSInputFile(tmp_path, filename=filename)
            await self.bot.send_document(message.chat.id, doc)
        except Exception as e:
            logger.error("Export failed: %s", e)
            await self._send_final(message.chat.id, f"\u274c Eksport xatosi: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── /stop ─────────────────────────────────────────────

    async def _handle_stop(self, message: "Message") -> None:
        """Handle /stop — cancel current running operation."""
        if not self._check_command_access(message):
            return

        # Cancel pending approvals for this user
        user_id = message.from_user.id
        cancelled = 0
        for aid, pending in list(self._pending_approvals.items()):
            if pending.get("user_id") == user_id:
                pending["future"].set_result(False)
                self._pending_approvals.pop(aid, None)
                cancelled += 1

        await self._send_final(
            message.chat.id,
            f"\u26d4 To'xtatildi. ({cancelled} so'rov bekor qilindi)" if cancelled
            else "\u26d4 Hech narsa ishlamayotgan edi.",
        )

    # ── Callback query router ─────────────────────────────

    async def _handle_callback_query(self, callback: "CallbackQuery") -> None:
        """Handle inline button callbacks for all settings."""
        data = callback.data or ""
        user_id = callback.from_user.id

        # ── Exec approval: approve:<id> or deny:<id> ──
        if data.startswith("approve:") or data.startswith("deny:"):
            await self._cb_approval(callback, data, user_id)
            return

        # ── MCP install proposals: mcp_approve / mcp_deny / mcp_approve_trust ──
        if (
            data.startswith("mcp_approve:")
            or data.startswith("mcp_deny:")
            or data.startswith("mcp_approve_trust:")
        ):
            from qanot.tools.mcp_manage import handle_mcp_approve_callback
            prefix, proposal_id = data.split(":", 1)
            action_map = {
                "mcp_approve": "approve",
                "mcp_deny": "deny",
                "mcp_approve_trust": "approve_trust",
            }
            await handle_mcp_approve_callback(
                self, self.config, callback, action_map[prefix], proposal_id,
            )
            return

        # ── MCP removal proposals: mcp_remove_approve / mcp_remove_deny ──
        if data.startswith("mcp_remove_approve:") or data.startswith("mcp_remove_deny:"):
            from qanot.tools.mcp_manage import handle_mcp_remove_callback
            prefix, proposal_id = data.split(":", 1)
            action = "approve" if prefix == "mcp_remove_approve" else "deny"
            await handle_mcp_remove_callback(
                self, self.config, callback, action, proposal_id,
            )
            return

        # ── Config secret proposals: cfg_approve / cfg_deny ──
        if data.startswith("cfg_approve:") or data.startswith("cfg_deny:"):
            from qanot.tools.config_manage import (
                handle_config_approve_callback,
                handle_config_deny_callback,
            )
            prefix, proposal_id = data.split(":", 1)
            if prefix == "cfg_approve":
                await handle_config_approve_callback(self, self.config, callback, proposal_id)
            else:
                await handle_config_deny_callback(self, self.config, callback, proposal_id)
            return

        # ── Settings callbacks: <type>:<value> ──
        handlers = {
            "model": self._cb_model,
            "think": self._cb_think,
            "voice": self._cb_voice,
            "vprov": self._cb_voiceprovider,
            "lang": self._cb_lang,
            "mode": self._cb_mode,
            "routing": self._cb_routing,
            "group": self._cb_group,
            "exec": self._cb_exec,
            "code": self._cb_code,
            "plg_on": self._cb_plugin_enable,
            "plg_off": self._cb_plugin_disable,
            "onboard_lang": self._cb_onboard_lang,
            "onboard_role": self._cb_onboard_role,
        }

        prefix = data.split(":", 1)[0] if ":" in data else ""
        handler = handlers.get(prefix)
        if handler:
            value = data.split(":", 1)[1]
            await handler(callback, value)
            return

        await callback.answer("Noma\u2018lum buyruq", show_alert=True)

    # ── Callback: approval ────────────────────────────────

    async def _cb_approval(self, callback: "CallbackQuery", data: str, user_id: int) -> None:
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
            await callback.message.edit_text(f"{callback.message.text}\n\n{status}")
        except Exception as e:
            logger.debug("Failed to update approval message: %s", e)
        await callback.answer(status)

    # ── Approval request ──────────────────────────────────

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
