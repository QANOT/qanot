"""Telegram settings handlers — model, thinking, voice, routing, group, exec, code, plugins."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Message
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)

# 7-level thinking granularity: level -> (label, description, budget_tokens)
# "off" disables thinking entirely. Higher levels = more reasoning = more cost.
THINKING_LEVELS: dict[str, dict] = {
    "off":      {"label": "Off",      "desc": "Fikrlashsiz",                "budget": 0},
    "minimal":  {"label": "Minimal",  "desc": "Eng kam (1K token)",         "budget": 1024},
    "low":      {"label": "Low",      "desc": "Kam fikrlash (4K)",          "budget": 4096},
    "medium":   {"label": "Medium",   "desc": "O'rtacha (10K)",             "budget": 10000},
    "high":     {"label": "High",     "desc": "Chuqur (25K)",               "budget": 25000},
    "extended": {"label": "Extended", "desc": "Kengaytirilgan (50K)",       "budget": 50000},
    "max":      {"label": "Max",      "desc": "Maksimal chuqurlik (100K)",  "budget": 100000},
}


class SettingsHandlersMixin:
    """Mixin providing settings command handlers for TelegramAdapter."""

    bot: "Bot"
    agent: "Agent"
    config: "Config"

    # ── Model switching helper ────────────────────────────

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

    # ── /model ────────────────────────────────────────────

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

        buttons = []
        for level_id, info in THINKING_LEVELS.items():
            check = "\u2705 " if level_id == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{check}{info['label']} \u2014 {info['desc']}",
                callback_data=f"think:{level_id}",
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        current_info = THINKING_LEVELS.get(current, {})
        budget_text = f" ({current_info.get('budget', 0):,} token)" if current != "off" else ""
        await message.reply(
            f"\U0001f9e0 **Joriy daraja:** `{current}`{budget_text}\n\nFikrlash darajasini tanlang:",
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
            ("off", "Off", "Faqat matn javob"),
            ("inbound", "Ovozga ovoz", "Ovozga \u2014 ovoz, matnga \u2014 matn"),
            ("always", "Har doim ovoz", "Har javob ovoz bilan ham"),
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

    def _build_voiceprovider_menu(self):
        """Render the /voiceprovider picker. Used by the command handler
        AND by the cancel-key callback to restore the list in place."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        current = self.config.voice_provider
        providers = [
            ("muxlisa", "Muxlisa"),
            ("kotib", "Kotib AI"),
            ("aisha", "Aisha"),
            ("whisper", "Whisper"),
        ]

        buttons = []
        for prov_id, label in providers:
            check = "\u2705 " if prov_id == current else ""
            has_key = bool(self.config.get_voice_api_key(prov_id))
            key_icon = "\U0001f511" if has_key else "\U0001f512"
            buttons.append([InlineKeyboardButton(
                text=f"{check}{key_icon} {label}",
                callback_data=f"vprov:{prov_id}",
            )])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        voice_name = self.config.voice_name or "default"
        text = (
            f"\U0001f3a4 **Joriy provayder:** `{current}`\n"
            f"**Ovoz:** `{voice_name}`\n\n"
            f"Provayderni tanlang (\U0001f511 = kalit bor, \U0001f512 = kalit yo'q):"
        )
        return text, keyboard

    async def _handle_voiceprovider(self, message: "Message") -> None:
        """Handle /voiceprovider — change voice STT/TTS provider via inline buttons."""
        if not self._check_command_access(message):
            return
        text, keyboard = self._build_voiceprovider_menu()
        await message.reply(text, parse_mode="Markdown", reply_markup=keyboard)

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

    # ── /topic ────────────────────────────────────────────

    async def _handle_topic(self, message: "Message") -> None:
        """Handle /topic — bind/unbind an agent to a forum topic.

        Usage:
            /topic                — show current binding for this topic
            /topic <agent_id>    — bind agent to this topic
            /topic unbind        — unbind agent from this topic
            /topic list          — show all bindings
        """
        if not self._check_command_access(message):
            return

        thread_id = getattr(message, "message_thread_id", None)
        chat_id = message.chat.id
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        # /topic list — show all bindings
        if arg == "list":
            bindings = self.config.topic_bindings
            if not bindings:
                await self._send_final(chat_id, "Hech qanday topic-agent bog'lanishi yo'q.",
                                       thread_id=thread_id)
                return
            lines = []
            for key, agent_id in bindings.items():
                agent_name = agent_id
                for ad in self.config.agents:
                    if ad.id == agent_id:
                        agent_name = f"{ad.name or ad.id} ({ad.id})"
                        break
                lines.append(f"  {key} \u2192 {agent_name}")
            await self._send_final(chat_id, "**Topic bindings:**\n" + "\n".join(lines),
                                   thread_id=thread_id)
            return

        # All other operations need a topic
        if not thread_id:
            await self._send_final(
                chat_id,
                "Bu buyruqni faqat forum topic ichida ishlatish mumkin.\n"
                "Guruhda Topics ni yoqing va topic ichidan /topic yuboring.",
            )
            return

        binding_key = f"{chat_id}:{thread_id}"

        # /topic unbind — remove binding
        if arg == "unbind":
            removed = self.config.topic_bindings.pop(binding_key, None)
            if removed:
                self._save_config_field("topic_bindings", self.config.topic_bindings)
                await self._send_final(chat_id, f"Agent '{removed}' bu topicdan ajratildi.",
                                       thread_id=thread_id)
            else:
                await self._send_final(chat_id, "Bu topicda bog'langan agent yo'q.",
                                       thread_id=thread_id)
            return

        # /topic <agent_id> — bind agent
        if arg:
            target = next((ad for ad in self.config.agents if ad.id == arg), None)
            if target is None:
                agent_ids = ", ".join(ad.id for ad in self.config.agents) or "(agentlar yo'q)"
                await self._send_final(
                    chat_id,
                    f"Agent '{arg}' topilmadi.\nMavjud agentlar: {agent_ids}",
                    thread_id=thread_id,
                )
                return

            self.config.topic_bindings[binding_key] = target.id
            self._save_config_field("topic_bindings", self.config.topic_bindings)
            await self._send_final(
                chat_id,
                f"\u2705 Bu topicga **{target.name or target.id}** agent bog'landi.\n"
                f"Endi bu topicdagi barcha xabarlarga shu agent javob beradi.",
                thread_id=thread_id,
            )
            return

        # /topic (no arg) — show current binding
        current = self.config.topic_bindings.get(binding_key)
        if current:
            agent_name = current
            for ad in self.config.agents:
                if ad.id == current:
                    agent_name = ad.name or ad.id
                    break
            await self._send_final(
                chat_id,
                f"Bu topicga **{agent_name}** (`{current}`) bog'langan.\n"
                f"O'zgartirish: `/topic <agent_id>`\nAjratish: `/topic unbind`",
                thread_id=thread_id,
            )
        else:
            agent_ids = ", ".join(ad.id for ad in self.config.agents) or "(agentlar yo'q)"
            await self._send_final(
                chat_id,
                f"Bu topicda agent bog'lanmagan.\n"
                f"Bog'lash: `/topic <agent_id>`\n"
                f"Mavjud agentlar: {agent_ids}",
                thread_id=thread_id,
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

    # ── Thinking provider sync ────────────────────────────

    def _sync_thinking_to_provider(self, level: str, budget: int) -> None:
        """Propagate thinking level change to the actual LLM provider."""
        provider = self.agent.provider
        # FailoverProvider wraps inner providers
        if hasattr(provider, "profiles"):
            for profile in provider.profiles:
                profile.thinking_level = level
                profile.thinking_budget = budget
            # Update already-initialized providers
            for p in getattr(provider, "_providers", {}).values():
                if hasattr(p, "set_thinking"):
                    p.set_thinking(level, budget)
        elif hasattr(provider, "set_thinking"):
            provider.set_thinking(level, budget)
        # Routing provider wraps an inner _provider
        if hasattr(provider, "_provider"):
            inner = provider._provider
            if hasattr(inner, "set_thinking"):
                inner.set_thinking(level, budget)
            if hasattr(inner, "profiles"):
                for profile in inner.profiles:
                    profile.thinking_level = level
                    profile.thinking_budget = budget
                for p in getattr(inner, "_providers", {}).values():
                    if hasattr(p, "set_thinking"):
                        p.set_thinking(level, budget)

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
        # Map level to budget
        budget = THINKING_LEVELS.get(level, {}).get("budget", self.config.thinking_budget)
        self.config.thinking_level = level
        self.config.thinking_budget = budget
        self._save_config_field("thinking_level", level)
        self._save_config_field("thinking_budget", budget)
        self._sync_thinking_to_provider(level, budget)
        name = THINKING_LEVELS.get(level, {}).get("label", level)
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
        # Cancel button pressed on the "waiting for key" prompt \u2014 clear
        # the pending state and re-render the voiceprovider menu in place so
        # the user can pick a different provider without typing /voiceprovider
        # again.
        if prov == "__cancel__":
            user_id = str(callback.from_user.id) if callback.from_user else ""
            self._pending_voice_key.pop(user_id, None)
            await callback.answer("\u2705 Bekor qilindi")
            try:
                text, markup = self._build_voiceprovider_menu()
                await callback.message.edit_text(
                    text, parse_mode="Markdown", reply_markup=markup,
                )
            except Exception as e:
                logger.debug("Failed to re-render voiceprovider menu: %s", e)
            return

        labels = {"muxlisa": "Muxlisa", "kotib": "Kotib AI", "aisha": "Aisha", "whisper": "Whisper"}
        name = labels.get(prov, prov)

        if not self.config.get_voice_api_key(prov):
            # No key yet \u2014 enter "waiting for key" state instead of a
            # dead-end alert. Next text message from this user is treated as
            # the API key (intercepted in _handle_message).
            user_id = str(callback.from_user.id) if callback.from_user else ""
            if user_id:
                self._pending_voice_key[user_id] = prov

            # Cancel button stays on the message so the user can abort
            # without remembering a slash command.
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="\u274c Bekor qilish",
                    callback_data="vprov:__cancel__",
                )],
            ])

            await callback.answer()
            try:
                await callback.message.edit_text(
                    f"\U0001f511 <b>{name}</b> uchun API kalit kerak.\n\n"
                    "Keyingi xabarda kalitni yuboring (faqat kalit, boshqa "
                    "matn qo'shmang). Kalit saqlanadi va xabaringiz xavfsizlik "
                    "uchun avtomatik o'chiriladi.",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as e:
                logger.debug("Failed to edit voiceprovider prompt: %s", e)
            return

        self.config.voice_provider = prov
        self._save_config_field("voice_provider", prov)
        await callback.answer(f"\u2705 Voice provider: {name}")
        try:
            await callback.message.edit_text(
                f"\u2705 Ovoz provayderi: **{name}** (`{prov}`)",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Failed to update vprov message: %s", e)

    async def _handle_pending_voice_key(self, message: "Message") -> bool:
        """If the sender has a pending voice-key request, consume this message
        as their API key, save it, and configure the provider. Returns True if
        handled (caller stops processing); False otherwise.
        """
        if not message.from_user:
            return False
        user_id = str(message.from_user.id)
        prov = self._pending_voice_key.pop(user_id, None)
        if not prov:
            return False

        key = (message.text or "").strip()
        # Remove the user's plaintext-key message immediately.
        try:
            await self.bot.delete_message(
                chat_id=message.chat.id, message_id=message.message_id,
            )
        except Exception as e:
            logger.debug("Couldn't delete key message: %s", e)

        if not key or len(key) < 8:
            await self.bot.send_message(
                message.chat.id,
                "\u274c Kalit juda qisqa yoki bo'sh. Bekor qilindi. "
                "/voiceprovider orqali qaytadan urining.",
            )
            return True

        labels = {
            "muxlisa": "Muxlisa", "kotib": "Kotib AI",
            "aisha": "Aisha", "whisper": "Whisper",
        }
        name = labels.get(prov, prov)

        # Persist into config.voice_api_keys[provider] and set provider.
        # voice_api_keys isn't restart-required \u2014 get_voice_api_key reads
        # it from the in-memory Config on every TTS call.
        if not isinstance(getattr(self.config, "voice_api_keys", None), dict):
            self.config.voice_api_keys = {}
        self.config.voice_api_keys[prov] = key
        self._save_config_field("voice_api_keys", dict(self.config.voice_api_keys))

        # Flip the active provider to the one the user was setting up.
        self.config.voice_provider = prov
        self._save_config_field("voice_provider", prov)

        await self.bot.send_message(
            message.chat.id,
            f"\u2705 <b>{name}</b> kaliti saqlandi va faol qilindi.\n"
            f"Endi /voice bilan ovozli rejimga o'ting yoki bot sizga javobni "
            f"ovozda yuborsin.",
            parse_mode="HTML",
        )
        return True

    async def _handle_cancel_voice_key(self, message: "Message") -> None:
        """Cancel a pending voice-key prompt."""
        if not message.from_user:
            return
        user_id = str(message.from_user.id)
        cleared = self._pending_voice_key.pop(user_id, None)
        if cleared:
            await message.reply(
                f"\u2705 Bekor qilindi ({cleared} kaliti yuborilmadi).",
            )
        else:
            await message.reply("Kutilayotgan kalit so'rovi yo'q.")

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
