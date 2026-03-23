"""Telegram command handlers — full OpenClaw-style settings from Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
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

    # ── Config persistence helper ─────────────────────────

    def _save_config_field(self, field: str, value) -> None:
        """Persist a single config field change to config.json."""
        try:
            config_path = Path(os.environ.get("QANOT_CONFIG", "config.json"))
            if config_path.exists():
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                raw[field] = value
                config_path.write_text(
                    json.dumps(raw, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception as e:
            logger.warning("Failed to save config field %s: %s", field, e)

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
        await self._send_final(
            message.chat.id,
            "Suhbat tozalandi. Yangi suhbatni boshlashingiz mumkin.",
        )
        logger.info("Conversation reset: %s", conv_key)

    # ── /status ───────────────────────────────────────────

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

    # ── /help ─────────────────────────────────────────────

    async def _handle_help(self, message: "Message") -> None:
        """Handle /help — show available commands."""
        if not self._check_command_access(message):
            return

        help_text = (
            "**Buyruqlar:**\n\n"
            "**Suhbat:**\n"
            "/reset \u2014 Suhbatni tozalash (+ model: /reset opus)\n"
            "/compact \u2014 Kontekstni siqish\n"
            "/export \u2014 Sessiyani eksport qilish\n"
            "/stop \u2014 Joriy amalni to'xtatish\n\n"
            "**Sozlamalar:**\n"
            "/model \u2014 Model tanlash\n"
            "/think \u2014 Fikrlash darajasi\n"
            "/voice \u2014 Ovoz sozlamalari\n"
            "/voiceprovider \u2014 Ovoz provayderi\n"
            "/lang \u2014 Til sozlash\n"
            "/mode \u2014 Javob rejimi\n"
            "/routing \u2014 Model routing\n"
            "/group \u2014 Guruh rejimi\n"
            "/exec \u2014 Xavfsizlik darajasi\n"
            "/code \u2014 Code execution (sandbox)\n\n"
            "**Ma'lumot:**\n"
            "/status \u2014 Sessiya holati\n"
            "/usage \u2014 Token sarfi va narxi\n"
            "/context \u2014 Kontekst tafsilotlari\n"
            "/id \u2014 Foydalanuvchi ID\n"
            "/config \u2014 Barcha sozlamalar\n"
            "/mcp \u2014 MCP serverlar holati\n"
            "/plugins \u2014 Pluginlar boshqaruvi\n"
            "/help \u2014 Shu yordam\n\n"
            "**Imkoniyatlar:**\n"
            "\U0001f4dd Matn \u2014 savol, buyruq, suhbat\n"
            "\U0001f399 Ovozli xabar \u2014 avtomatik transcribe\n"
            "\U0001f4f7 Rasm \u2014 tahlil qilish (vision)\n"
            "\U0001f4ce Fayl \u2014 PDF, doc, excel o\u2018qish\n"
            "\U0001f517 Link \u2014 avtomatik tushunish\n"
            "\U0001f3a8 Rasm yaratish \u2014 \"rasm chiz: ...\" deb yozing\n"
        )
        await self._send_final(message.chat.id, help_text)

    # ── /model ────────────────────────────────────────────

    def _switch_model(self, model_id: str) -> None:
        """Switch the active model at runtime."""
        provider = self.agent.provider
        if hasattr(provider, '_provider'):
            provider._provider.model = model_id
            provider._primary_model = model_id
            provider.model = model_id
        else:
            provider.model = model_id
        self.config.model = model_id
        self._save_config_field("model", model_id)

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
            check = "\u2705 " if model_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
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

    # ── /think ────────────────────────────────────────────

    async def _handle_think(self, message: "Message") -> None:
        """Handle /think — change thinking/reasoning level via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.thinking_level
        levels = [
            ("off", "Off", "Fikrlashsiz"),
            ("low", "Low", "Kam fikrlash"),
            ("medium", "Medium", "O'rtacha"),
            ("high", "High", "Chuqur fikrlash"),
        ]

        buttons = []
        for level_id, label, desc in levels:
            check = "\u2705 " if level_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
                callback_data=f"think:{level_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"\U0001f9e0 **Joriy daraja:** `{current}`\n\nFikrlash darajasini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /voice ────────────────────────────────────────────

    async def _handle_voice(self, message: "Message") -> None:
        """Handle /voice — change TTS mode via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.voice_mode
        modes = [
            ("off", "Off", "Ovoz o'chirilgan"),
            ("inbound", "Inbound", "Faqat ovozli xabarga javob"),
            ("always", "Always", "Har doim ovozli javob"),
        ]

        buttons = []
        for mode_id, label, desc in modes:
            check = "\u2705 " if mode_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
                callback_data=f"voice:{mode_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"\U0001f399 **Joriy rejim:** `{current}`\n\nOvoz rejimini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /voiceprovider ────────────────────────────────────

    async def _handle_voiceprovider(self, message: "Message") -> None:
        """Handle /voiceprovider — change voice STT/TTS provider via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.voice_provider
        providers = [
            ("muxlisa", "Muxlisa", "O'zbek, OGG native"),
            ("kotib", "Kotib AI", "6 ovoz, ko'p tilli"),
            ("aisha", "Aisha", "O'zbek, kayfiyat boshqaruvi"),
            ("whisper", "Whisper", "OpenAI, ko'p tilli"),
        ]

        buttons = []
        for prov_id, label, desc in providers:
            check = "\u2705 " if prov_id == current else ""
            has_key = bool(self.config.get_voice_api_key(prov_id))
            key_icon = "\U0001f511" if has_key else "\U0001f512"
            buttons.append([InlineKeyboardButton(
                text=f"{check}{key_icon} {label} \u2014 {desc}",
                callback_data=f"vprov:{prov_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        voice_name = self.config.voice_name or "default"
        await message.reply(
            f"\U0001f3a4 **Joriy provayder:** `{current}`\n"
            f"**Ovoz:** `{voice_name}`\n\n"
            f"Provayderni tanlang (\U0001f511 = kalit bor, \U0001f512 = kalit yo'q):",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /lang ─────────────────────────────────────────────

    async def _handle_lang(self, message: "Message") -> None:
        """Handle /lang — change STT language via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.voice_language or "auto"
        langs = [
            ("", "Auto", "Avtomatik aniqlash"),
            ("uz", "O'zbek", "O'zbek tili"),
            ("ru", "Rus", "Rus tili"),
            ("en", "English", "Ingliz tili"),
        ]

        buttons = []
        for lang_id, label, desc in langs:
            check = "\u2705 " if (lang_id == current or (not lang_id and current == "auto")) else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
                callback_data=f"lang:{lang_id or 'auto'}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"\U0001f310 **Joriy til:** `{current}`\n\nSTT tilini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /mode ─────────────────────────────────────────────

    async def _handle_mode(self, message: "Message") -> None:
        """Handle /mode — change response mode via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.response_mode
        modes = [
            ("stream", "Stream", "Live streaming (sendMessageDraft)"),
            ("partial", "Partial", "Vaqti-vaqti bilan yangilash"),
            ("blocked", "Blocked", "To'liq javob kutish"),
        ]

        buttons = []
        for mode_id, label, desc in modes:
            check = "\u2705 " if mode_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
                callback_data=f"mode:{mode_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"\u26a1 **Joriy rejim:** `{current}`\n\nJavob rejimini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /routing ──────────────────────────────────────────

    async def _handle_routing(self, message: "Message") -> None:
        """Handle /routing — toggle model routing via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.routing_enabled
        on_check = "\u2705 " if current else ""
        off_check = "\u2705 " if not current else ""
        buttons = [
            [InlineKeyboardButton(
                text=f"{on_check}ON \u2014 Haiku/Sonnet/Opus avtomatik",
                callback_data="routing:on",
            )],
            [InlineKeyboardButton(
                text=f"{off_check}OFF \u2014 Faqat tanlangan model",
                callback_data="routing:off",
            )],
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        status = "ON" if current else "OFF"
        await message.reply(
            f"\U0001f500 **Model routing:** `{status}`\n\nRouting rejimini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /group ────────────────────────────────────────────

    async def _handle_group(self, message: "Message") -> None:
        """Handle /group — change group chat mode via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.group_mode
        modes = [
            ("off", "Off", "Guruhda javob bermaydi"),
            ("mention", "Mention", "Faqat @bot va reply"),
            ("all", "All", "Barcha xabarlarga javob"),
        ]

        buttons = []
        for mode_id, label, desc in modes:
            check = "\u2705 " if mode_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
                callback_data=f"group:{mode_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"\U0001f465 **Guruh rejimi:** `{current}`\n\nGuruh rejimini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /exec ─────────────────────────────────────────────

    async def _handle_exec(self, message: "Message") -> None:
        """Handle /exec — change execution security level via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.exec_security
        levels = [
            ("open", "Open", "Barcha buyruqlar ruxsat"),
            ("cautious", "Cautious", "Xavfli buyruqlarda so'raydi"),
            ("strict", "Strict", "Faqat allowlist buyruqlar"),
        ]

        buttons = []
        for level_id, label, desc in levels:
            check = "\u2705 " if level_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{label} \u2014 {desc}",
                callback_data=f"exec:{level_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(
            f"\U0001f6e1 **Xavfsizlik:** `{current}`\n\nXavfsizlik darajasini tanlang:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /code ──────────────────────────────────────────────

    async def _handle_code(self, message: "Message") -> None:
        """Handle /code — toggle Anthropic server-side code execution."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        current = self.config.code_execution
        on_check = "\u2705 " if current else ""
        off_check = "\u2705 " if not current else ""
        buttons = [
            [InlineKeyboardButton(
                text=f"{on_check}ON \u2014 Claude sandbox (Python, Bash, vizualizatsiya)",
                callback_data="code:on",
            )],
            [InlineKeyboardButton(
                text=f"{off_check}OFF \u2014 O'chirilgan",
                callback_data="code:off",
            )],
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        status = "ON" if current else "OFF"
        free_note = ""
        if self.config.brave_api_key:
            free_note = "\n\U0001f4b0 Web search bilan birga **bepul**!"
        await message.reply(
            f"\U0001f4bb **Code execution:** `{status}`{free_note}\n\n"
            f"Claude sandbox: Python, Bash, fayl yaratish, grafik chizish:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── /context ──────────────────────────────────────────

    async def _handle_context(self, message: "Message") -> None:
        """Handle /context — show detailed context usage."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        ctx = self.agent.context
        status = ctx.session_status()
        conv = self.agent.get_conversation(conv_key)

        # Count tokens by role
        user_msgs = sum(1 for m in conv if m.get("role") == "user")
        asst_msgs = sum(1 for m in conv if m.get("role") == "assistant")
        tool_msgs = sum(1 for m in conv if m.get("role") == "tool")

        pct = status["context_percent"]
        bar_len = 20
        filled = int(pct / 100 * bar_len)
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)

        text = (
            f"**Context Usage**\n\n"
            f"`[{bar}]` {pct}%\n\n"
            f"Tokens: {status['total_tokens']:,} / {ctx.max_tokens:,}\n"
            f"Turns: {status['turn_count']}\n"
            f"Buffer: {'active' if status['buffer_active'] else 'inactive'}\n"
            f"Compacted: {status.get('compacted', False)}\n\n"
            f"**Messages breakdown:**\n"
            f"User: {user_msgs} | Assistant: {asst_msgs} | Tool: {tool_msgs}\n"
            f"Total: {len(conv)}"
        )
        await self._send_final(message.chat.id, text)

    # ── /usage ────────────────────────────────────────────

    async def _handle_usage(self, message: "Message") -> None:
        """Handle /usage — show token cost and usage stats."""
        access = self._check_command_access(message)
        if not access:
            return
        user_id, _ = access

        stats = self.agent.cost_tracker.get_user_stats(str(user_id))
        total_cost = self.agent.cost_tracker.get_total_cost()

        text = (
            f"**Token Usage & Cost**\n\n"
            f"**Sizning sarfingiz:**\n"
            f"Input: {stats['input_tokens']:,} tokens\n"
            f"Output: {stats['output_tokens']:,} tokens\n"
            f"Cache read: {stats['cache_read_tokens']:,}\n"
            f"Cache write: {stats['cache_write_tokens']:,}\n"
            f"API calls: {stats['api_calls']:,}\n"
            f"Turns: {stats['turns']:,}\n"
            f"Cost: ${stats['total_cost']:.4f}\n\n"
            f"**Umumiy:** ${total_cost:.4f}"
        )
        await self._send_final(message.chat.id, text)

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
        """Handle /export — export current session as JSON file."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        conv = self.agent.get_conversation(conv_key)
        if not conv:
            await self._send_final(message.chat.id, "Suhbat tarixi bo'sh.")
            return

        import tempfile
        from aiogram.types import FSInputFile

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

        try:
            doc = FSInputFile(tmp_path, filename=f"session_{conv_key}.json")
            await self.bot.send_document(message.chat.id, doc)
        except Exception as e:
            logger.error("Export failed: %s", e)
            await self._send_final(message.chat.id, f"\u274c Eksport xatosi: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── /id ───────────────────────────────────────────────

    async def _handle_id(self, message: "Message") -> None:
        """Handle /id — show user and chat info."""
        if not message.from_user:
            return

        user = message.from_user
        chat = message.chat
        is_owner = user.id in (self.config.allowed_users or [])

        uname = user.username or "yo'q"
        owner_str = "ha" if is_owner else "yo'q"
        title = chat.title or "DM"
        text = (
            f"**Foydalanuvchi:**\n"
            f"ID: `{user.id}`\n"
            f"Ism: {user.full_name}\n"
            f"Username: @{uname}\n"
            f"Owner: {owner_str}\n\n"
            f"**Chat:**\n"
            f"ID: `{chat.id}`\n"
            f"Type: {chat.type}\n"
            f"Title: {title}"
        )
        await self._send_final(message.chat.id, text)

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

    # ── /config ───────────────────────────────────────────

    async def _handle_config(self, message: "Message") -> None:
        """Handle /config — show all current config (read-only overview)."""
        if not self._check_command_access(message):
            return

        c = self.config
        text = (
            f"**Qanot AI Config**\n\n"
            f"**Model:** `{c.model}`\n"
            f"**Provider:** `{c.provider}`\n"
            f"**Routing:** `{'ON' if c.routing_enabled else 'OFF'}`\n"
            f"**Thinking:** `{c.thinking_level}` (budget: {c.thinking_budget})\n"
            f"**Response mode:** `{c.response_mode}`\n"
            f"**Voice mode:** `{c.voice_mode}`\n"
            f"**Voice provider:** `{c.voice_provider}`\n"
            f"**Voice name:** `{c.voice_name or 'default'}`\n"
            f"**STT language:** `{c.voice_language or 'auto'}`\n"
            f"**Group mode:** `{c.group_mode}`\n"
            f"**Exec security:** `{c.exec_security}`\n"
            f"**Code execution:** `{'ON' if c.code_execution else 'OFF'}`\n"
            f"**RAG:** `{c.rag_mode}` ({'ON' if c.rag_enabled else 'OFF'})\n"
            f"**Compaction:** `{c.compaction_mode}`\n"
            f"**Max context:** `{c.max_context_tokens:,}`\n"
            f"**Reactions:** `{'ON' if c.reactions_enabled else 'OFF'}`\n"
            f"**Dashboard:** `{'ON' if c.dashboard_enabled else 'OFF'}` (:{c.dashboard_port})\n"
            f"**Allowed users:** `{c.allowed_users}`\n"
        )
        await self._send_final(message.chat.id, text)

    # ── /mcp ──────────────────────────────────────────────

    async def _handle_mcp(self, message: "Message") -> None:
        """Handle /mcp — show MCP servers, enable/disable via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        mcp_mgr = getattr(self, "_mcp_manager", None)
        servers = self.config.mcp_servers or []

        if not servers and not mcp_mgr:
            await self._send_final(
                message.chat.id,
                "\U0001f50c **MCP Serverlar**\n\n"
                "Hech qanday MCP server sozlanmagan.\n\n"
                "config.json ga qo'shing:\n"
                '```\n"mcp_servers": [\n'
                '  {"name": "context7", "command": "uvx", "args": ["context7-mcp"]}\n'
                "]\n```",
            )
            return

        # Build status display
        lines = ["\U0001f50c **MCP Serverlar**\n"]

        connected = mcp_mgr.connected_servers if mcp_mgr else []

        for cfg in servers:
            name = cfg.get("name", "unnamed")
            cmd = cfg.get("command", "?")
            is_connected = name in connected
            icon = "\U0001f7e2" if is_connected else "\U0001f534"

            tool_count = 0
            if mcp_mgr and is_connected:
                srv = mcp_mgr._servers.get(name)
                if srv:
                    tool_count = len(srv.tools)

            tools_str = f" ({tool_count} tools)" if tool_count else ""
            lines.append(f"{icon} **{name}** \u2014 `{cmd}`{tools_str}")

            # List tool names for connected servers
            if mcp_mgr and is_connected:
                srv = mcp_mgr._servers.get(name)
                if srv and srv.tools:
                    tool_names = ", ".join(t["name"] for t in srv.tools[:10])
                    if len(srv.tools) > 10:
                        tool_names += f" +{len(srv.tools) - 10} more"
                    lines.append(f"  \u2514 {tool_names}")

        total = mcp_mgr.total_tools if mcp_mgr else 0
        lines.append(f"\n**Jami:** {len(connected)}/{len(servers)} server, {total} tools")

        await self._send_final(message.chat.id, "\n".join(lines))

    # ── /plugins ──────────────────────────────────────────

    async def _handle_plugins(self, message: "Message") -> None:
        """Handle /plugins — list plugins, enable/disable via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from qanot.plugins.loader import get_plugin_manager

        pm = get_plugin_manager()
        loaded = pm.loaded_plugins
        all_plugins = self.config.plugins

        if not all_plugins:
            await self._send_final(
                message.chat.id,
                "\U0001f9e9 **Pluginlar**\n\n"
                "Hech qanday plugin sozlanmagan.\n\n"
                "`qanot plugin install <name>` bilan o'rnating.",
            )
            return

        buttons = []
        lines = ["\U0001f9e9 **Pluginlar**\n"]

        for pcfg in all_plugins:
            name = pcfg.name
            is_loaded = name in loaded
            is_enabled = pcfg.enabled

            if is_loaded:
                plugin = loaded[name]
                tool_count = len(plugin.get_tools())
                icon = "\U0001f7e2"
                status = f"{tool_count} tools"
            elif is_enabled:
                icon = "\U0001f7e1"
                status = "enabled, not loaded"
            else:
                icon = "\u26aa"
                status = "disabled"

            lines.append(f"{icon} **{name}** \u2014 {status}")

            # Toggle button
            if is_enabled:
                buttons.append([InlineKeyboardButton(
                    text=f"\u274c Disable {name}",
                    callback_data=f"plg_off:{name}",
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"\u2705 Enable {name}",
                    callback_data=f"plg_on:{name}",
                )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

        note = "\n\n_O'zgartirish uchun restart kerak_" if buttons else ""
        await message.reply(
            "\n".join(lines) + note,
            reply_markup=keyboard,
            parse_mode="Markdown",
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

    # ── Callback: model ───────────────────────────────────

    async def _cb_model(self, callback: "CallbackQuery", model_id: str) -> None:
        self._switch_model(model_id)
        model_names = {
            "claude-opus-4-6": "Opus 4.6",
            "claude-sonnet-4-6": "Sonnet 4.6",
            "claude-haiku-4-5-20251001": "Haiku 4.5",
        }
        name = model_names.get(model_id, model_id)
        await callback.answer(f"\u2705 Model: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Model o\u2018zgartirildi: **{name}** (`{model_id}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update model message: %s", e)

    # ── Callback: think ───────────────────────────────────

    async def _cb_think(self, callback: "CallbackQuery", level: str) -> None:
        self.config.thinking_level = level
        self._save_config_field("thinking_level", level)
        labels = {"off": "Off", "low": "Low", "medium": "Medium", "high": "High"}
        name = labels.get(level, level)
        await callback.answer(f"\u2705 Thinking: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Fikrlash darajasi: **{name}** (`{level}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update think message: %s", e)

    # ── Callback: voice mode ──────────────────────────────

    async def _cb_voice(self, callback: "CallbackQuery", mode: str) -> None:
        self.config.voice_mode = mode
        self._save_config_field("voice_mode", mode)
        labels = {"off": "Off", "inbound": "Inbound", "always": "Always"}
        name = labels.get(mode, mode)
        await callback.answer(f"\u2705 Voice: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Ovoz rejimi: **{name}** (`{mode}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update voice message: %s", e)

    # ── Callback: voice provider ──────────────────────────

    async def _cb_voiceprovider(self, callback: "CallbackQuery", prov: str) -> None:
        if not self.config.get_voice_api_key(prov):
            await callback.answer(
                f"\u274c {prov} uchun API kalit sozlanmagan. config.json da voice_api_keys ni tekshiring.",
                show_alert=True,
            )
            return
        self.config.voice_provider = prov
        self._save_config_field("voice_provider", prov)
        labels = {"muxlisa": "Muxlisa", "kotib": "Kotib AI", "aisha": "Aisha", "whisper": "Whisper"}
        name = labels.get(prov, prov)
        await callback.answer(f"\u2705 Voice provider: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Ovoz provayderi: **{name}** (`{prov}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update vprov message: %s", e)

    # ── Callback: language ────────────────────────────────

    async def _cb_lang(self, callback: "CallbackQuery", lang: str) -> None:
        actual = "" if lang == "auto" else lang
        self.config.voice_language = actual
        self._save_config_field("voice_language", actual)
        labels = {"auto": "Auto", "uz": "O'zbek", "ru": "Rus", "en": "English"}
        name = labels.get(lang, lang)
        await callback.answer(f"\u2705 Til: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 STT tili: **{name}** (`{lang}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update lang message: %s", e)

    # ── Callback: response mode ───────────────────────────

    async def _cb_mode(self, callback: "CallbackQuery", mode: str) -> None:
        self.config.response_mode = mode
        self._save_config_field("response_mode", mode)
        labels = {"stream": "Stream", "partial": "Partial", "blocked": "Blocked"}
        name = labels.get(mode, mode)
        await callback.answer(f"\u2705 Mode: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Javob rejimi: **{name}** (`{mode}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update mode message: %s", e)

    # ── Callback: routing ─────────────────────────────────

    async def _cb_routing(self, callback: "CallbackQuery", value: str) -> None:
        enabled = value == "on"
        self.config.routing_enabled = enabled
        self._save_config_field("routing_enabled", enabled)
        status = "ON" if enabled else "OFF"
        await callback.answer(f"\u2705 Routing: {status}")
        try:
            await callback.message.edit_text(
                f"\u2705 Model routing: **{status}**",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update routing message: %s", e)

    # ── Callback: group mode ──────────────────────────────

    async def _cb_group(self, callback: "CallbackQuery", mode: str) -> None:
        self.config.group_mode = mode
        self._save_config_field("group_mode", mode)
        labels = {"off": "Off", "mention": "Mention", "all": "All"}
        name = labels.get(mode, mode)
        await callback.answer(f"\u2705 Group: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Guruh rejimi: **{name}** (`{mode}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update group message: %s", e)

    # ── Callback: exec security ───────────────────────────

    async def _cb_exec(self, callback: "CallbackQuery", level: str) -> None:
        self.config.exec_security = level
        self._save_config_field("exec_security", level)
        labels = {"open": "Open", "cautious": "Cautious", "strict": "Strict"}
        name = labels.get(level, level)
        await callback.answer(f"\u2705 Exec: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Xavfsizlik: **{name}** (`{level}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update exec message: %s", e)

    # ── Callback: code execution ────────────────────────────

    async def _cb_code(self, callback: "CallbackQuery", value: str) -> None:
        enabled = value == "on"
        self.config.code_execution = enabled
        self._save_config_field("code_execution", enabled)
        # Update provider if it's Anthropic
        provider = self.agent.provider
        for p in [provider, getattr(provider, "_provider", None)]:
            if p and hasattr(p, "_code_execution"):
                p._code_execution = enabled
        status = "ON" if enabled else "OFF"
        await callback.answer(f"\u2705 Code execution: {status}")
        try:
            await callback.message.edit_text(
                f"\u2705 Code execution: **{status}**",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update code message: %s", e)

    # ── Callback: plugin enable/disable ─────────────────────

    async def _cb_plugin_enable(self, callback: "CallbackQuery", name: str) -> None:
        for pcfg in self.config.plugins:
            if pcfg.name == name:
                pcfg.enabled = True
                break
        self._save_plugins_config()
        await callback.answer(f"\u2705 {name} enabled (restart kerak)")
        try:
            await callback.message.edit_text(
                f"\u2705 Plugin **{name}** enabled.\nRestart qiling: `/restart` yoki `qanot restart`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update plugin message: %s", e)

    async def _cb_plugin_disable(self, callback: "CallbackQuery", name: str) -> None:
        for pcfg in self.config.plugins:
            if pcfg.name == name:
                pcfg.enabled = False
                break
        self._save_plugins_config()
        await callback.answer(f"\u274c {name} disabled (restart kerak)")
        try:
            await callback.message.edit_text(
                f"\u274c Plugin **{name}** disabled.\nRestart qiling: `/restart` yoki `qanot restart`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update plugin message: %s", e)

    def _save_plugins_config(self) -> None:
        """Persist plugins enable/disable state to config.json."""
        plugins_data = []
        for pcfg in self.config.plugins:
            plugins_data.append({
                "name": pcfg.name,
                "enabled": pcfg.enabled,
                **({"config": pcfg.config} if pcfg.config else {}),
            })
        self._save_config_field("plugins", plugins_data)

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
