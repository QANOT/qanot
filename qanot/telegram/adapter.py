"""Core Telegram adapter — wires handlers, media, streaming into TelegramAdapter."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING


def _b64_to_bytes(value) -> bytes:
    """Decode a base64 string to raw bytes; return ``b""`` on any failure.

    The image-download helper sometimes returns bytes directly, sometimes
    a base64 string (depending on caller). The multimodal memo writer
    needs raw bytes either way, so we coerce here.
    """
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=False)
        except (ValueError, TypeError):
            return b""
    return b""

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from qanot.telegram.formatting import _sanitize_response
from qanot.telegram.handlers import HandlersMixin
from qanot.telegram.media import (
    download_photo, download_sticker, save_photo_to_uploads,
    send_pending_files, send_pending_images, send_pending_videos,
    send_voice_reply, transcribe_voice,
)
from qanot.telegram.streaming import StreamingMixin

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.scheduler import CronScheduler

logger = logging.getLogger(__name__)


class TelegramAdapter(HandlersMixin, StreamingMixin):
    """Handles Telegram bot communication via aiogram long polling.

    Response modes (config.response_mode):
      - "stream":  Live streaming via sendMessageDraft (Bot API 9.5)
      - "partial": Periodic edits via editMessageText (fallback)
      - "blocked": Wait for full response, then send (simplest)
    """

    def __init__(
        self,
        config: "Config",
        agent: "Agent",
        scheduler: "CronScheduler | None" = None,
        subagent_manager=None,
    ):
        self.config = config
        self.agent = agent
        self.scheduler = scheduler
        self.subagent_manager = subagent_manager

        # Optional: point Bot at a self-hosted telegram-bot-api server for
        # 2GB file upload/download (vs 20MB on the public API).
        # Set TELEGRAM_API_URL env OR config.telegram_api_url to enable.
        import os as _os
        api_url = _os.environ.get("TELEGRAM_API_URL") or getattr(config, "telegram_api_url", None)
        if api_url:
            from aiogram.client.session.aiohttp import AiohttpSession
            from aiogram.client.telegram import TelegramAPIServer
            logger.info("Using self-hosted Bot API at %s (local mode)", api_url)
            # is_local=True tells aiogram that file_path is an absolute path
            # on our local filesystem — it reads the file directly instead of
            # HTTP-downloading. Required for the --local flag on telegram-bot-api.
            server = TelegramAPIServer.from_base(api_url, is_local=True)
            session = AiohttpSession(api=server)
            self.bot = Bot(token=config.bot_token, session=session)
        else:
            self.bot = Bot(token=config.bot_token)
        self.dp = Dispatcher()
        self._setup_handlers()
        self._concurrent = asyncio.Semaphore(config.max_concurrent)
        self._draft_counter = 0
        self._bot_username: str | None = None
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._pending_messages: dict[str, list[tuple]] = {}
        # In-flight agent turn per conversation, so /stop can cancel it mid-run
        # (aiogram processes the /stop update on a separate task, concurrently).
        self._active_turns: dict[str, asyncio.Task] = {}
        self._pending_approvals: dict[str, dict] = {}
        # Agent-driven clarify questions awaiting a button tap (in-memory, TTL via timeout)
        self._pending_clarifications: dict[str, dict] = {}
        # MCP install/remove proposals awaiting user approval (10-min TTL, in-memory only)
        self._pending_mcp_proposals: dict[str, dict] = {}
        self._pending_mcp_removals: dict[str, dict] = {}
        # Config secret-set proposals awaiting user approval (10-min TTL, in-memory only)
        self._pending_config_proposals: dict[str, dict] = {}
        # Per-user: "I'm waiting for this provider's API key on your next message".
        # Populated by /voiceprovider tap on a key-less provider; consumed by
        # the next incoming text message from that user.
        self._pending_voice_key: dict[str, str] = {}
        from qanot.ratelimit import RateLimiter
        self._rate_limiter = RateLimiter()
        self.voicecall_manager = None  # Set by main.py if voicecall_enabled
        # Admin notification throttle: key -> last-sent monotonic time.
        self._admin_notify_last: dict[str, float] = {}
        # Group zen-mode state + classifier. The classifier is consulted
        # only when config.group_mode == "zen"; the state is harmless to
        # keep around either way (it's just an empty in-memory dict).
        from qanot.group.state import GroupChatState
        from qanot.group.zen_classifier import GroupZenClassifier
        self._group_state = GroupChatState()
        self._zen_classifier = GroupZenClassifier(
            config=config, state=self._group_state,
        )
        # Conversational poll flow: registry maps poll_id → context
        # (chat, thread, question, correct answers) so the poll_answer
        # handler can rebuild a synthetic agent turn when the user taps.
        # The evaluator is a lightweight Haiku-only path that bypasses
        # the full agent loop — see qanot/poll_evaluator.py for why.
        from qanot.poll_evaluator import PollEvaluator
        from qanot.poll_state import PollRegistry
        self._poll_registry = PollRegistry(config.workspace_dir)
        self._poll_evaluator = PollEvaluator(agent.provider)

    async def notify_admins(self, text: str, throttle_key: str | None = None,
                            throttle_seconds: float = 3600.0) -> None:
        """Send a short alert to each admin_chat_ids. Throttled per key so
        recurring failures don't spam. Silently drops if no admin is set."""
        ids = getattr(self.config, "admin_chat_ids", None) or []
        if not ids:
            return
        if throttle_key is not None:
            import time as _time
            now = _time.monotonic()
            last = self._admin_notify_last.get(throttle_key, 0.0)
            if now - last < throttle_seconds:
                return
            self._admin_notify_last[throttle_key] = now
        for admin_id in ids:
            try:
                await self.bot.send_message(admin_id, text[:4000])
            except Exception as e:
                logger.warning("notify_admins to %s failed: %r", admin_id, e)

    def _setup_handlers(self) -> None:
        @self.dp.message(F.text == "/start")
        async def handle_start(message: Message) -> None:
            await self._handle_start(message)

        @self.dp.message(F.text.startswith("/reset"))
        async def handle_reset(message: Message) -> None:
            await self._handle_reset(message)

        @self.dp.message(F.text.startswith("/resume"))
        async def handle_resume(message: Message) -> None:
            await self._handle_resume(message)

        @self.dp.message(F.text.startswith("/status"))
        async def handle_status(message: Message) -> None:
            await self._handle_status(message)

        @self.dp.message(F.text.startswith("/help"))
        async def handle_help(message: Message) -> None:
            await self._handle_help(message)

        @self.dp.message(F.text.startswith("/model"))
        async def handle_model(message: Message) -> None:
            await self._handle_model(message)

        @self.dp.message(F.text.startswith("/think"))
        async def handle_think(message: Message) -> None:
            await self._handle_think(message)

        @self.dp.message(F.text.startswith("/voiceprovider"))
        async def handle_voiceprovider(message: Message) -> None:
            await self._handle_voiceprovider(message)

        @self.dp.message(F.text.startswith("/cancel_voice_key"))
        async def handle_cancel_voice_key(message: Message) -> None:
            await self._handle_cancel_voice_key(message)

        @self.dp.message(F.text.startswith("/voice"))
        async def handle_voice(message: Message) -> None:
            await self._handle_voice(message)

        @self.dp.message(F.text.startswith("/lang"))
        async def handle_lang(message: Message) -> None:
            await self._handle_lang(message)

        @self.dp.message(F.text.startswith("/mode"))
        async def handle_mode(message: Message) -> None:
            await self._handle_mode(message)

        @self.dp.message(F.text.startswith("/routing"))
        async def handle_routing(message: Message) -> None:
            await self._handle_routing(message)

        @self.dp.message(F.text.startswith("/group"))
        async def handle_group(message: Message) -> None:
            await self._handle_group(message)

        @self.dp.message(F.text.startswith("/topic"))
        async def handle_topic(message: Message) -> None:
            await self._handle_topic(message)

        @self.dp.message(F.text.startswith("/exec"))
        async def handle_exec(message: Message) -> None:
            await self._handle_exec(message)

        @self.dp.message(F.text.startswith("/code"))
        async def handle_code(message: Message) -> None:
            await self._handle_code(message)

        @self.dp.message(F.text.startswith("/context"))
        async def handle_context(message: Message) -> None:
            await self._handle_context(message)

        @self.dp.message(F.text.startswith("/usage"))
        async def handle_usage(message: Message) -> None:
            await self._handle_usage(message)

        @self.dp.message(F.text.startswith("/insights"))
        async def handle_insights(message: Message) -> None:
            await self._handle_insights(message)

        @self.dp.message(F.text.startswith("/compact"))
        async def handle_compact(message: Message) -> None:
            await self._handle_compact(message)

        @self.dp.message(F.text.startswith("/export"))
        async def handle_export(message: Message) -> None:
            await self._handle_export(message)

        @self.dp.message(F.text.startswith("/id"))
        async def handle_id(message: Message) -> None:
            await self._handle_id(message)

        @self.dp.message(F.text.startswith("/joincall"))
        async def handle_joincall(message: Message) -> None:
            await self._handle_joincall(message)

        @self.dp.message(F.text.startswith("/leavecall"))
        async def handle_leavecall(message: Message) -> None:
            await self._handle_leavecall(message)

        @self.dp.message(F.text.startswith("/callstatus"))
        async def handle_callstatus(message: Message) -> None:
            await self._handle_callstatus(message)

        @self.dp.message(F.text.startswith("/stop"))
        async def handle_stop(message: Message) -> None:
            await self._handle_stop(message)

        @self.dp.message(F.text.startswith("/config"))
        async def handle_config(message: Message) -> None:
            await self._handle_config(message)

        @self.dp.message(F.text.startswith("/mcp"))
        async def handle_mcp(message: Message) -> None:
            await self._handle_mcp(message)

        @self.dp.message(F.text.startswith("/plugins"))
        async def handle_plugins(message: Message) -> None:
            await self._handle_plugins(message)

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.photo)
        async def handle_photo(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.sticker)
        async def handle_sticker(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.document)
        async def handle_document(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.voice)
        async def handle_voice(message: Message) -> None:
            await self._handle_message(message, is_voice=True)

        @self.dp.message(F.video_note)
        async def handle_video_note(message: Message) -> None:
            await self._handle_message(message, is_voice=True)

        @self.dp.message(F.video)
        async def handle_video(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.animation)
        async def handle_animation(message: Message) -> None:
            await self._handle_message(message)

        from aiogram.types import CallbackQuery, PollAnswer

        @self.dp.callback_query()
        async def handle_callback(callback: CallbackQuery) -> None:
            await self._handle_callback_query(callback)

        # Conversational poll flow — when a user taps an option on a
        # poll the bot sent (non-anonymous, per ``tg_send_poll`` config),
        # Telegram fires this event. We synthesise an agent turn from
        # the answer so the bot can react ("✅ to'g'ri!") and continue
        # the quiz naturally.
        @self.dp.poll_answer()
        async def handle_poll_answer(answer: PollAnswer) -> None:
            await self._handle_poll_answer(answer)

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.allowed_users:
            self.config.allowed_users = [user_id]
            self._save_owner(user_id)
            logger.info("Auto-owner: user %d is now the owner", user_id)
            return True
        return user_id in self.config.allowed_users

    def _save_owner(self, user_id: int) -> None:
        """Persist the auto-owner to config.json (atomic)."""
        try:
            from qanot.config import read_config_json, write_config_json
            raw = read_config_json()
            raw["allowed_users"] = [user_id]
            write_config_json(raw)
        except Exception as e:
            logger.warning("Failed to save auto-owner: %s", e)

    async def _get_bot_username(self) -> str:
        """Get and cache the bot's username."""
        if self._bot_username is None:
            me = await self.bot.me()
            self._bot_username = me.username or ""
        return self._bot_username

    def _is_group_chat(self, message: Message) -> bool:
        return message.chat.type in ("group", "supergroup")

    async def _should_respond_in_group(self, message: Message) -> bool:
        """Determine if the bot should respond to a group message."""
        mode = self.config.group_mode
        if mode == "off":
            return False
        if mode == "all":
            return True
        if mode == "mention":
            bot_username = await self._get_bot_username()
            text = message.text or message.caption or ""
            if bot_username and f"@{bot_username}" in text:
                return True
            if message.reply_to_message and message.reply_to_message.from_user:
                if message.reply_to_message.from_user.username == bot_username:
                    return True
            return False
        if mode == "zen":
            return await self._zen_should_respond(message)
        return False

    async def _zen_should_respond(self, message: Message) -> bool:
        """Phase A heuristic-only zen classifier. See qanot/group/zen_classifier."""
        classifier = getattr(self, "_zen_classifier", None)
        if classifier is None:
            # Misconfiguration safety net — fall back to mention semantics
            # rather than going silent on every message.
            logger.warning(
                "group_mode=zen but no classifier wired; falling back to mention",
            )
            self.config.group_mode = "mention"
            return await self._should_respond_in_group(message)

        bot_username = await self._get_bot_username()
        text = message.text or message.caption or ""

        # Compute Layer 1 inputs in adapter terms (telegram-aware).
        is_command = bool(text.startswith("/"))
        is_at_mention = bool(bot_username) and f"@{bot_username}" in text

        # "Reply to bot's last message" is the existing mention-mode
        # signal. We keep its precise semantics so existing chats don't
        # regress when switching to zen.
        is_reply_to_bot_last = False
        is_reply_to_recent_bot = False
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.username == bot_username:
                is_reply_to_bot_last = True
                # If we've tracked any recent bot reply in this chat,
                # this also counts as a "thread continuation" signal.
                state = getattr(self, "_group_state", None)
                if state is not None and state.last_reply(message.chat.id):
                    is_reply_to_recent_bot = True

        decision = await classifier.decide(
            chat_id=message.chat.id,
            text=text,
            bot_username=bot_username,
            is_command=is_command,
            is_at_mention=is_at_mention,
            is_reply_to_bot_last=is_reply_to_bot_last,
            is_reply_to_recent_bot=is_reply_to_recent_bot,
        )

        # Apply mute/unmute side-effect from the classifier so future
        # messages see the updated state. Confirmation reply is the
        # bot's own response if respond=True; otherwise we send a
        # quiet acknowledgement so the user knows it took effect.
        state = getattr(self, "_group_state", None)
        if state is not None and decision.mute_action == "mute":
            state.mute(message.chat.id, minutes=self.config.zen_mute_minutes)
            try:
                await self.bot.send_message(
                    chat_id=message.chat.id,
                    text=f"🤫 {self.config.zen_mute_minutes} daqiqa jim turaman.",
                    message_thread_id=getattr(message, "message_thread_id", None),
                )
            except Exception:
                pass
        elif state is not None and decision.mute_action == "unmute":
            state.unmute(message.chat.id)

        logger.info(
            "zen chat=%s score=%d respond=%s reason=%s",
            message.chat.id, decision.score, decision.respond, decision.reason,
        )

        # A "mute" action is its own conversation event — we already
        # sent the confirmation above, so suppress the agent loop.
        if decision.mute_action == "mute":
            return False

        return decision.respond

    def _strip_bot_mention(self, text: str, bot_username: str) -> str:
        if not bot_username:
            return text
        return text.replace(f"@{bot_username}", "").strip()

    async def _handle_poll_answer(self, answer) -> None:
        """Route a poll_answer Telegram update into a synthetic agent
        turn so the conversational quiz flow works end-to-end.

        Steps:
          1. Look up the poll in PollRegistry (set by tg_send_poll).
          2. Auth-check the responder against allowed_users.
          3. Render the answer as a synthetic user message.
          4. Compute the same conv_key the user's regular messages
             would route to — keeps per-thread / per-group isolation.
          5. Fire agent.run_turn(...) and stream the reply back via
             ``_send_final`` so the bot's reaction lands in the right
             chat + thread.

        Failures are silent: a missing/expired poll, a non-allowed
        responder, or a duplicate revote all return without raising.
        """
        try:
            poll_id = str(getattr(answer, "poll_id", "") or "")
            user = getattr(answer, "user", None)
            user_id_int = int(getattr(user, "id", 0) or 0) if user else 0
            option_ids = list(getattr(answer, "option_ids", []) or [])

            if not poll_id or not user_id_int:
                return

            record = self._poll_registry.get(poll_id)
            if record is None:
                # We didn't send this poll, or its TTL passed. Either
                # way, nothing to do.
                logger.debug(
                    "poll_answer for unknown poll_id=%s — ignoring", poll_id,
                )
                return

            if not self._is_allowed(user_id_int):
                logger.info(
                    "poll_answer from non-allowed user=%s — ignoring",
                    user_id_int,
                )
                return

            # Dedupe revotes — only the FIRST answer (or a true change)
            # fires an agent turn. Revoting with the same options is a
            # no-op so the bot doesn't double-respond.
            is_new = await self._poll_registry.record_answer(
                poll_id, user_id_int, option_ids,
            )
            if not is_new:
                logger.debug(
                    "poll_answer is a duplicate for poll_id=%s user=%s",
                    poll_id, user_id_int,
                )
                return

            # Build a conv_key that matches the regular-message routing
            # so per-thread / per-group conversation isolation is
            # preserved. PollRegistry stored chat_id + thread_id from
            # the original sendPoll context.
            chat_id = record.chat_id
            thread_id = record.thread_id
            if chat_id < 0:
                # Group / supergroup
                if thread_id:
                    conv_key = f"group_{chat_id}_topic_{thread_id}"
                else:
                    conv_key = f"group_{chat_id}"
            else:
                # Private chat
                if thread_id:
                    conv_key = f"user_{user_id_int}_thread_{thread_id}"
                else:
                    conv_key = str(user_id_int)

            logger.info(
                "poll_answer routed: poll_id=%s user=%s chat=%s thread=%s "
                "options=%s",
                poll_id, user_id_int, chat_id, thread_id, option_ids,
            )

            # Lightweight evaluator (direct Haiku call) instead of
            # full agent.run_turn — quiz feedback is a self-contained
            # task that doesn't need workspace context, tools, or the
            # main provider model. Cuts per-answer cost ~200x and lets
            # the user burst-answer 10 polls without hitting the OAuth
            # TPM ceiling on Sonnet. Production trace 07:29:11 captured
            # the bug: 3rd poll answer in 35s → 429.
            try:
                reply = await self._poll_evaluator.evaluate(
                    record, option_ids,
                )
            except Exception:
                logger.exception(
                    "poll evaluator failed for poll_id=%s", poll_id,
                )
                return

            if reply:
                try:
                    # ``reply_to`` anchors the feedback to the specific
                    # poll the user just answered. With all-at-once
                    # quiz flow (bot sends N polls, user answers in any
                    # order), this is what visually links each piece of
                    # feedback to its question — Telegram draws the
                    # "↳ replying to poll" connector.
                    await self._send_final(
                        chat_id, reply,
                        reply_to=(record.message_id or None),
                        thread_id=thread_id,
                    )

                    # Inject the Q&A pair into the agent's conversation
                    # history so future turns ("natijalar qanday?", "menga
                    # xulosa ber") can see the answers. Without this the
                    # evaluator's reply only lives in Telegram — the
                    # agent's conv_manager doesn't know any polls were
                    # answered, so it says "Hali javoblar kelmagan" the
                    # next time the user asks. Bug captured in production
                    # 12:48 when the user finished Section 3 and the bot
                    # claimed no answers had arrived.
                    try:
                        synthetic = self._poll_registry.build_answer_message(
                            record, option_ids,
                        )
                        messages = self.agent._conv_manager.ensure_messages(
                            conv_key,
                        )
                        messages.append({
                            "role": "user", "content": synthetic,
                        })
                        messages.append({
                            "role": "assistant", "content": reply,
                        })
                    except Exception:
                        logger.exception(
                            "could not inject poll Q&A into conv history "
                            "for conv_key=%s", conv_key,
                        )
                except Exception:
                    logger.exception(
                        "_send_final failed for poll reply chat=%s", chat_id,
                    )
        except Exception:
            # Top-level safety net — never let an unexpected exception
            # crash the dispatcher (would stop all updates).
            logger.exception("poll_answer handler crashed")

    def _conv_key(self, message: Message) -> str:
        thread_id = getattr(message, "message_thread_id", None)
        if not self._is_group_chat(message):
            # Private chats with Threaded Mode (Bot API 10.0) on the bot
            # carry message_thread_id when the user opens a thread from
            # the base view. Each thread is a separate conversation —
            # otherwise "Work" and "Personal" threads bleed history.
            # Base view (no thread_id) keeps the user-id key so existing
            # conversations from non-threaded bots stay intact.
            if thread_id:
                return f"user_{message.from_user.id}_thread_{thread_id}"
            return str(message.from_user.id)
        # Forum topics in group chats: isolate conversations per topic thread.
        if thread_id:
            return f"group_{message.chat.id}_topic_{thread_id}"
        return f"group_{message.chat.id}"

    def _check_command_access(self, message: Message) -> tuple[int, str] | None:
        if not message.from_user:
            return None
        user_id = message.from_user.id
        if not self._is_allowed(user_id):
            return None
        return user_id, self._conv_key(message)

    # ── Multimodal memo helpers ─────────────────────────────────
    #
    # These run as background tasks fired from ``_handle_message`` so the
    # user's reply isn't delayed by the disk/embedding work. Either method
    # is a strict no-op when the agent has no memo router attached.

    async def _save_voice_memo_from_message(
        self, message: Message, transcript: str,
    ) -> None:
        """Download the voice payload again and hand it to save_voice_memo.

        We deliberately re-download rather than threading the file path
        through ``transcribe_voice`` — keeps the two responsibilities
        decoupled and the second fetch is small (~50KB typical voice
        notes; Telegram CDN is fast). Failure here is fatal-silent: the
        user's reply already shipped, we don't want a memo write
        retry storm.
        """
        agent = getattr(self, "agent", None)
        if agent is None or getattr(agent, "_memo_router", None) is None:
            return
        if not transcript or not transcript.strip():
            return

        voice = message.voice or message.video_note
        if voice is None:
            return
        duration_sec = int(getattr(voice, "duration", 0) or 0)

        import tempfile
        from qanot.memos import save_voice_memo

        suffix = ".ogg" if message.voice else ".mp4"
        tmp = tempfile.mktemp(suffix=suffix)
        try:
            await self.bot.download(voice, destination=tmp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice memo: download failed: %s", exc)
            return

        thread_id = (
            str(message.message_thread_id) if message.message_thread_id else ""
        )
        user_id = str(message.from_user.id) if message.from_user else ""
        try:
            await save_voice_memo(
                audio_src_path=tmp,
                transcript=transcript,
                duration_sec=duration_sec,
                workspace_dir=self.config.workspace_dir,
                user_id=user_id,
                thread_id=thread_id,
                audio_suffix=suffix,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice memo: save failed: %s", exc)
        finally:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass

    async def _save_image_memo_from_message(
        self, message: Message, image_data: dict, caption_or_text: str,
    ) -> None:
        """Persist a photo + caption-derived description as a memo.

        We don't run Haiku extraction here — that's the agent loop's
        pre-turn responsibility (``qanot/extraction.py``). For the memo
        we use the user's caption (when present) as the description,
        falling back to a generic one. The richer post-extraction
        description can be promoted onto the memo by a follow-up task
        later; for now this gives us "the image exists, here's what the
        user said about it" which is enough for semantic recall.
        """
        agent = getattr(self, "agent", None)
        if agent is None or getattr(agent, "_memo_router", None) is None:
            return

        # download_photo returns {"type": "image", "source": {...}, "data": bytes}
        # OR {"type": "image", "media_type": "image/jpeg", "data": b64} — we
        # accept the raw-bytes form. Image_data may also carry a "bytes" key
        # depending on caller; be tolerant.
        raw = (
            image_data.get("raw_bytes")
            or image_data.get("bytes")
            or _b64_to_bytes(image_data.get("data"))
        )
        if not raw:
            logger.debug("image memo: no raw bytes available, skipping")
            return

        description = (caption_or_text or "User-supplied image.").strip()
        if not description:
            description = "User-supplied image."

        thread_id = (
            str(message.message_thread_id) if message.message_thread_id else ""
        )
        user_id = str(message.from_user.id) if message.from_user else ""

        from qanot.memos import save_image_memo

        try:
            await save_image_memo(
                image_bytes=raw,
                description_text=description,
                workspace_dir=self.config.workspace_dir,
                user_id=user_id,
                thread_id=thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("image memo: save failed: %s", exc)

    async def _handle_message(self, message: Message, *, is_voice: bool = False) -> None:
        if not message.from_user:
            return

        # Intercept: if this user is mid-flow entering a voice-provider API
        # key (tapped 🔒 on /voiceprovider), this message is the key — not
        # a prompt to the agent. Save it, don't forward to the LLM.
        if (
            not is_voice
            and message.text
            and str(message.from_user.id) in self._pending_voice_key
        ):
            try:
                if await self._handle_pending_voice_key(message):
                    return
            except Exception as e:
                logger.warning("pending voice-key handler failed: %s", e)

        # Group orchestration: route messages in the orchestration group
        if (
            self.config.group_orchestration
            and message.chat.id == self.config.orchestration_group_id
            and hasattr(self, "_group_orchestrator")
            and self._group_orchestrator
        ):
            try:
                handled = await self._group_orchestrator.route_message(message)
                if handled:
                    return
            except Exception as e:
                logger.warning("Group orchestrator routing failed: %s", e)
            # Fall through to default handling if not routed

        user_id_int = message.from_user.id
        if not self._is_allowed(user_id_int):
            return

        # Fire-and-forget profile enrichment (Bot API 10.0) with the RAW
        # Telegram user id — getUserPersonalChatMessages expects an int,
        # not the synthetic conv_key we'll build below for per-thread
        # conversation isolation. The enricher self-throttles via cadence
        # + per-user dedupe, so calling on every message is cheap.
        enricher = getattr(self, "_profile_enricher", None)
        if enricher is not None:
            try:
                await enricher.maybe_enrich(user_id_int)
            except Exception as e:
                logger.warning("profile_enricher.maybe_enrich failed: %s", e)

        user_id = str(user_id_int)  # Convert once at Telegram boundary
        allowed, reason = self._rate_limiter.check(user_id)
        if not allowed:
            await message.reply(reason)
            return
        self._rate_limiter.record(user_id)

        is_group = self._is_group_chat(message)
        if is_group:
            # Always respond in bound topics (regardless of group_mode)
            thread_id = getattr(message, "message_thread_id", None)
            has_binding = bool(
                thread_id
                and self.config.topic_bindings.get(f"{message.chat.id}:{thread_id}")
            )
            if not has_binding and not await self._should_respond_in_group(message):
                return

        text = message.text or message.caption or ""
        voice_request = False

        if is_voice and (message.voice or message.video_note):
            await self.bot.send_chat_action(
                chat_id=message.chat.id, action=ChatAction.TYPING,
            )
            transcript = await transcribe_voice(self.bot, message, self.config)
            if transcript:
                text = f"{transcript} {text}".strip()
                voice_request = True
                # Fire-and-forget: persist the audio + transcript as a
                # retrievable memo. Costs one extra HTTP round trip to
                # Telegram CDN and an embedding pass — both cheap. Failure
                # is logged but never blocks the user's reply.
                asyncio.create_task(
                    self._save_voice_memo_from_message(message, transcript),
                    name=f"voice-memo-{message.chat.id}",
                )
            else:
                await self._send_final(
                    message.chat.id,
                    "Ovozli xabarni qayta ishlab bo'lmadi. Iltimos, matn yozing.",
                )
                return

        images: list[dict] = []
        if message.photo:
            image_data = await download_photo(self.bot, message)
            if image_data:
                images.append(image_data)
                if not text:
                    text = "Bu rasmni tahlil qiling."
                # Expose the photo to tool calls (channel_post_photo,
                # read_file, edit_image). Without this the model can SEE
                # the image (vision) but has no way to RE-USE it, and ends
                # up grep-ing /tmp for the file (2026-05-24 incident with
                # channel_post_photo). Save to workspace and inject both
                # the path AND the Telegram file_id into the user-message
                # context so downstream tools have direct handles.
                rel_path = await save_photo_to_uploads(
                    self.bot, message, self.config.workspace_dir,
                )
                marker_bits: list[str] = []
                if rel_path:
                    marker_bits.append(f"yuklandi: {rel_path}")
                marker_bits.append(f"file_id: {message.photo[-1].file_id}")
                text = f"[Rasm | {' | '.join(marker_bits)}] {text}".strip()
                # Background: persist the image as a retrievable memo.
                # Skipped silently when the agent has no memo router
                # attached (RAG/FastEmbed disabled). The actual image
                # description is filled in later — see _save_image_memo
                # — because the pre-turn extractor runs inside the agent
                # loop, not here.
                asyncio.create_task(
                    self._save_image_memo_from_message(
                        message, image_data, text,
                    ),
                    name=f"image-memo-{message.chat.id}",
                )

        if message.sticker:
            sticker_data = await download_sticker(self.bot, message)
            if sticker_data:
                emoji = message.sticker.emoji or ""
                sticker_ctx = (
                    f"[The user sent a sticker {emoji}. "
                    f"Treat it as a conversational expression \u2014 react naturally like a human would. "
                    f"Do NOT describe the image. Respond to the emotion/intent behind it.]"
                )
                if isinstance(sticker_data, dict) and sticker_data.get("type") == "image":
                    images.append(sticker_data)
                    text = f"{sticker_ctx} {text}".strip() if text else sticker_ctx
                elif isinstance(sticker_data, str):
                    text = f"{sticker_ctx} {text}".strip() if text else sticker_ctx

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
                text = f"[Document: {fname} \u2014 yuklab bo'lmadi] {text}".strip()

        # Video uploads (not document-type) — important for clipper plugin
        if message.video:
            try:
                file = await self.bot.get_file(message.video.file_id)
                dl_dir = Path(self.config.workspace_dir) / "uploads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(file.file_path or "video.mp4").suffix or ".mp4"
                fname = f"{message.video.file_unique_id}{ext}"
                dl_path = dl_dir / fname

                expected_size = message.video.file_size or 0

                # For local Bot API mode: telegram-bot-api writes the file
                # asynchronously. aiogram's is_local=True reads whatever is
                # on disk NOW, which can return a partial file. Poll until
                # file size matches Telegram's reported size (or stabilizes).
                needs_wait = (
                    not dl_path.exists()
                    or (expected_size and dl_path.stat().st_size < expected_size)
                )
                if needs_wait:
                    await self.bot.download_file(file.file_path, dl_path)

                    # Wait for telegram-bot-api to finish writing (up to 5 min)
                    if expected_size:
                        deadline = asyncio.get_running_loop().time() + 300
                        while asyncio.get_running_loop().time() < deadline:
                            try:
                                # Read from the source (telegram-bot-api side)
                                if file.file_path and Path(file.file_path).exists():
                                    src_size = Path(file.file_path).stat().st_size
                                    if src_size >= expected_size:
                                        # Source complete — re-copy to be safe
                                        if dl_path.stat().st_size < src_size:
                                            await self.bot.download_file(file.file_path, dl_path)
                                        break
                                elif dl_path.exists() and dl_path.stat().st_size >= expected_size:
                                    break
                            except OSError:
                                pass
                            await asyncio.sleep(2)

                duration = message.video.duration or 0
                mm, ss = divmod(int(duration), 60)
                size_mb = dl_path.stat().st_size / 1_048_576 if dl_path.exists() else 0
                text = (
                    f"[Video yuklandi: {dl_path} "
                    f"({mm}:{ss:02d}, {message.video.width}x{message.video.height}, {size_mb:.0f}MB)] {text}"
                ).strip()
                logger.info("Downloaded video: %s (%ds, %.0fMB)", dl_path, duration, size_mb)
            except Exception as e:
                logger.error("Video download failed: %s", e, exc_info=True)
                text = f"[Video yuklab bo'lmadi: {e}] {text}".strip()

        if message.reply_to_message:
            quoted = message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            if len(quoted_text) > 1000:
                quoted_text = quoted_text[:1000] + "\u2026"
            quoted_from = "a message"
            if quoted.from_user:
                if quoted.from_user.is_bot:
                    quoted_from = "your previous message"
                else:
                    name = quoted.from_user.full_name or str(quoted.from_user.id)
                    quoted_from = f"a message from {name}"
            if quoted.photo and not images:
                quoted_img = await download_photo(self.bot, quoted)
                if quoted_img:
                    images.append(quoted_img)
                    if not quoted_text:
                        quoted_text = "[image]"
            if quoted.sticker and not images:
                sticker_data = await download_sticker(self.bot, quoted)
                if isinstance(sticker_data, dict) and sticker_data.get("type") == "image":
                    images.append(sticker_data)
                    emoji = quoted.sticker.emoji or ""
                    if not quoted_text:
                        quoted_text = f"[sticker {emoji}]"
            if quoted.voice and not voice_request:
                transcript = await transcribe_voice(self.bot, quoted, self.config)
                if transcript:
                    quoted_text = f"{quoted_text} [voice: {transcript}]".strip()
            if quoted_text:
                text = f"[Replying to {quoted_from}: \"{quoted_text}\"]\n\n{text}"

        if not text:
            return

        if is_group:
            bot_username = await self._get_bot_username()
            text = self._strip_bot_mention(text, bot_username)
            sender_name = message.from_user.full_name or str(user_id)
            text = f"[{sender_name}]: {text}"

        if self.scheduler:
            self.scheduler.record_user_activity()

        await self._react(message.chat.id, message.message_id, "\U0001f440")

        coalesce_key = self._conv_key(message)
        self._pending_messages.setdefault(coalesce_key, []).append(
            (message, text, images, voice_request)
        )

        lock = self._user_locks.setdefault(coalesce_key, asyncio.Lock())
        async with lock:
            batch = self._pending_messages.pop(coalesce_key, [])
            if not batch:
                return

            if len(batch) == 1:
                msg, text, images, voice_req = batch[0]
            else:
                text = "\n\n".join(t for _, t, _, _ in batch)
                images = [img for _, _, imgs, _ in batch if imgs for img in imgs] or None
                msg = batch[-1][0]
                voice_req = any(vr for _, _, _, vr in batch)
                logger.info(
                    "Coalesced %d messages into one turn (key=%s)",
                    len(batch), coalesce_key,
                )
                for earlier_msg, _, _, _ in batch[:-1]:
                    await self._react(earlier_msg.chat.id, earlier_msg.message_id, "\u2705")

            coalesced = len(batch) > 1
            async with self._concurrent:
                await self._process_turn(msg, coalesce_key, text, images, voice_req, coalesced=coalesced,
                                         thread_id=getattr(msg, "message_thread_id", None))

    def _resolve_topic_binding(self, chat_id: int, thread_id: int | None):
        """Resolve topic-agent binding. Returns AgentDefinition or None."""
        if not thread_id or not self.config.topic_bindings:
            return None
        binding_key = f"{chat_id}:{thread_id}"
        agent_id = self.config.topic_bindings.get(binding_key)
        if not agent_id:
            return None
        return next((ad for ad in self.config.agents if ad.id == agent_id), None)

    async def _process_turn(
        self,
        message: Message,
        conv_key: str,
        text: str,
        images: list[dict] | None,
        voice_request: bool,
        *,
        coalesced: bool = False,
        thread_id: int | None = None,
    ) -> None:
        """Process a single (possibly coalesced) turn for a conversation."""
        # Topic-agent binding: per-turn system prompt override (thread-safe)
        bound_agent = self._resolve_topic_binding(message.chat.id, thread_id)
        system_prompt_override: str | None = None
        if bound_agent and bound_agent.prompt:
            system_prompt_override = bound_agent.prompt
            logger.info("Topic binding active: %s → agent %s", conv_key, bound_agent.id)

        # Fire-and-forget thread auto-titling (Bot API 10.0 Threaded Mode).
        # The titler self-dedupes once per (chat,thread), so calling on
        # every message is cheap — most calls hit the in-memory set and
        # return immediately. Only the FIRST message in a fresh thread
        # triggers the LLM call + editForumTopic.
        titler = getattr(self, "_thread_titler", None)
        if (
            titler is not None
            and thread_id
            and not self._is_group_chat(message)
            and text
        ):
            try:
                await titler.maybe_title(
                    chat_id=message.chat.id,
                    thread_id=thread_id,
                    user_message=text,
                )
            except Exception as e:
                logger.warning("thread titler maybe_title failed: %s", e)

        mode = self.config.response_mode
        rm = self.config.reply_mode
        if rm == "always":
            reply_to = message.message_id
        elif rm == "coalesced" and coalesced:
            reply_to = message.message_id
        else:
            reply_to = None
        try:
            if mode == "stream":
                coro = self._respond_stream(message.chat.id, conv_key, text, images=images, reply_to=reply_to, thread_id=thread_id, message_id=message.message_id, system_prompt_override=system_prompt_override)
            elif mode == "partial":
                coro = self._respond_partial(message.chat.id, conv_key, text, images=images, reply_to=reply_to, thread_id=thread_id, message_id=message.message_id, system_prompt_override=system_prompt_override)
            else:
                coro = self._respond_blocked(message.chat.id, conv_key, text, images=images, reply_to=reply_to, thread_id=thread_id, message_id=message.message_id, system_prompt_override=system_prompt_override)

            # Run the turn as a tracked task so /stop can cancel it mid-run.
            turn_task = asyncio.create_task(coro)
            self._active_turns[conv_key] = turn_task
            try:
                await turn_task
            finally:
                if self._active_turns.get(conv_key) is turn_task:
                    self._active_turns.pop(conv_key, None)

            await send_pending_images(self.bot, message.chat.id, conv_key, self.agent, thread_id=thread_id)
            await send_pending_files(self.bot, message.chat.id, conv_key, self.agent, thread_id=thread_id)
            await send_pending_videos(self.bot, message.chat.id, conv_key, self.agent, thread_id=thread_id)

            should_tts = (
                self.config.voice_mode == "always"
                or (self.config.voice_mode == "inbound" and voice_request)
            )
            if should_tts and self.config.get_voice_api_key():
                await send_voice_reply(self.bot, message.chat.id, conv_key, self.agent, self.config)

            await self._react(message.chat.id, message.message_id, "\u2705")

        except asyncio.CancelledError:
            # /stop cancelled this turn (the cancellation targets turn_task, not
            # this handler). _handle_stop already sent the confirmation, so just
            # acknowledge on the user's message and stop \u2014 no error reply.
            logger.info("Turn cancelled by /stop: %s", conv_key)
            try:
                await self._react(message.chat.id, message.message_id, "\u26d4")
            except Exception:
                pass
            return
        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
            await self._react(message.chat.id, message.message_id, "\u274c")
            err_msg = str(e).lower()
            if "rate_limit" in err_msg or "429" in err_msg:
                await self._send_final(
                    message.chat.id,
                    "Limitga yetdik. Iltimos, 20-30 soniya kutib qayta yozing.",
                )
            else:
                await self._send_final(
                    message.chat.id,
                    "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
                )

    # ── Proactive & lifecycle ────────────────────────────────

    async def _proactive_loop(self) -> None:
        if not self.scheduler:
            return
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.scheduler.message_queue.get(), timeout=5.0,
                )
                msg_type = msg.get("type", "")
                text = msg.get("text", "")
                source = msg.get("source", "")
                chat_id = msg.get("chat_id")
                thread_id = msg.get("thread_id")
                if msg_type == "proactive" and text:
                    await self._deliver_proactive(
                        text, source,
                        chat_id=chat_id, thread_id=thread_id,
                    )
                elif msg_type == "system_event" and text:
                    await self.agent.run_turn(text)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Proactive loop error: %s", e)
                await asyncio.sleep(5)

    async def _deliver_proactive(
        self, text: str, source: str = "",
        *, chat_id: int | None = None, thread_id: int | None = None,
    ) -> None:
        """Deliver a proactive message to the owner (first allowed user).

        When the cron job carries an origin (``chat_id``/``thread_id`` from
        the calling turn \u2014 captured by ``cron_create``), route the result
        to that exact thread so a scheduled reminder lands where the user
        asked from. Falls back to the owner's base DM (the legacy
        behaviour, correct for builtin system jobs without an origin).
        """
        if not self.config.allowed_users:
            logger.warning("No allowed_users configured \u2014 proactive message dropped")
            return

        source_tag = f" #{source}" if source else ""
        formatted = f"#agent{source_tag}\n{text}"

        target_chat = chat_id or self.config.allowed_users[0]
        try:
            await self._send_final(target_chat, formatted, thread_id=thread_id)
            logger.info(
                "Proactive message delivered to chat=%d thread=%s (source=%s)",
                target_chat, thread_id, source,
            )
        except Exception as e:
            logger.warning("Failed to deliver proactive message: %s", e)
            return

        # Record the proactive message into THIS thread's conversation
        # history so when the user replies the agent has context for
        # what it sent. Without this the cron's "Mini Dialog — Kun 4
        # … translate it" message landed in Telegram but never in the
        # conv buffer; the user's translation looked like a cold
        # message and the agent had to ask "are you translating into
        # German?" before catching up (2026-05-25 13:00 incident).
        #
        # conv_key mirrors _conv_key() so a future incoming user
        # message in the same thread hits the same conversation.
        try:
            if target_chat < 0:
                conv_key = (
                    f"group_{target_chat}_topic_{thread_id}"
                    if thread_id else f"group_{target_chat}"
                )
            else:
                conv_key = (
                    f"user_{target_chat}_thread_{thread_id}"
                    if thread_id else str(target_chat)
                )
            messages = self.agent._conv_manager.ensure_messages(conv_key)
            messages.append({"role": "assistant", "content": formatted})
        except Exception as e:  # noqa: BLE001 — recording must never break delivery
            logger.debug("Proactive conv-history record skipped: %s", e)

    async def start(self) -> None:
        """Start the Telegram bot (polling or webhook based on config)."""
        logger.info(
            "[telegram] starting \u2014 transport=%s, response=%s, flush=%.1fs",
            self.config.telegram_mode,
            self.config.response_mode,
            self.config.stream_flush_interval,
        )
        await self._register_commands()
        _proactive_task = asyncio.create_task(self._proactive_loop())
        _proactive_task.add_done_callback(
            lambda t: logger.warning("Proactive loop failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )

        if self.config.telegram_mode == "webhook" and self.config.webhook_url:
            await self._start_webhook()
        else:
            await self._start_polling()

    async def _start_polling(self) -> None:
        try:
            await self.dp.start_polling(self.bot, drop_pending_updates=True)
        finally:
            await self.bot.session.close()

    async def _start_webhook(self) -> None:
        webhook_url = self.config.webhook_url.rstrip("/")
        webhook_path = "/webhook"
        full_url = f"{webhook_url}{webhook_path}"

        await self.bot.set_webhook(full_url, drop_pending_updates=True)
        logger.info("[telegram] webhook set: %s", full_url)

        app = web.Application()
        handler = SimpleRequestHandler(dispatcher=self.dp, bot=self.bot)
        handler.register(app, path=webhook_path)
        setup_application(app, self.dp, bot=self.bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.webhook_port)
        try:
            await site.start()
            logger.info("[telegram] webhook server listening on :%d", self.config.webhook_port)
            await asyncio.Event().wait()
        finally:
            await self.bot.delete_webhook()
            await runner.cleanup()
            await self.bot.session.close()
