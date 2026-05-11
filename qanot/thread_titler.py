"""Auto-title fresh threads in private chats with Threaded Mode (Bot API 10.0).

By default Telegram names every new thread "New Chat" — so a user with
five threads sees "New Chat", "New Chat", "New Chat", "New Chat",
"New Chat" in their sidebar and can't distinguish them. This module
generates a 2-4 word title from the first user message using a cheap
Haiku call and renames the thread via ``editForumTopic`` (which Bot
API 9.3 extended to private-chat threads).

Design notes:
- **Fire-and-forget**: titling never blocks the conversation. The
  agent's reply ships first; the title arrives a few seconds later.
- **One-shot per thread**: persisted to ``titled_threads.json`` so
  restarts don't re-title every existing thread.
- **Graceful degradation**: any failure (no permission, model down,
  Telegram API rejection) logs and moves on. The conversation works
  even if titles never get set.
- **Permission**: requires the bot to have Threaded Mode enabled via
  BotFather. Without it, ``editForumTopic`` returns Bad Request and we
  just leave threads named "New Chat" — acceptable degraded state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TITLED_FILENAME = "titled_threads.json"
TITLE_MODEL = "claude-haiku-4-5-20251001"
# Telegram allows up to 128 chars but ~24 fits the sidebar nicely.
TITLE_MAX_LEN = 24
# Soft cap on how much of the user message we send to the title model —
# long messages just waste tokens for a 2-4 word output.
USER_MESSAGE_MAX_CHARS = 500

_TITLE_SYSTEM_PROMPT = (
    "You generate short Telegram thread titles. Rules:\n"
    "- Output ONLY the title, nothing else (no quotes, no period).\n"
    "- 2-4 words maximum.\n"
    "- Uzbek (Latin script). Avoid English unless the user asked in English.\n"
    "- Descriptive of the topic, not generic.\n"
    "- No emoji.\n"
    "Examples:\n"
    "  User says 'salom' → 'Tanishuv'\n"
    "  User asks about oltin narxi → 'Oltin tahlili'\n"
    "  User asks to write Python code → 'Python yordami'\n"
    "  User asks about mahalla → 'Mahalla savollari'"
)


class ThreadTitler:
    """Renames fresh threads from 'New Chat' to a topic-derived title."""

    def __init__(
        self,
        *,
        bot: Any,
        provider: Any,
        workspace_dir: str,
    ) -> None:
        self._bot = bot
        self._provider = provider
        self._state_path = Path(workspace_dir) / TITLED_FILENAME
        # Persistent: {"chat_id:thread_id": True}. Treat the JSON as a
        # set serialised to disk — values are always True.
        self._titled: set[str] = self._load_state()
        # In-flight guards so two near-simultaneous first messages don't
        # both fire title tasks for the same thread.
        self._in_flight: set[str] = set()
        self._state_lock = asyncio.Lock()

    # ── Persistence ─────────────────────────────────────────────────

    def _load_state(self) -> set[str]:
        if not self._state_path.exists():
            return set()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("titled_threads.json corrupt, ignoring: %s", e)
            return set()
        if isinstance(raw, dict):
            return {str(k) for k in raw}
        if isinstance(raw, list):
            return {str(k) for k in raw}
        return set()

    async def _save_state_locked(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            payload = {key: True for key in self._titled}
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as e:
            logger.warning("titled_threads.json write failed: %s", e)

    # ── Public API ──────────────────────────────────────────────────

    @staticmethod
    def _key(chat_id: int, thread_id: int) -> str:
        return f"{chat_id}:{thread_id}"

    def should_title(self, chat_id: int, thread_id: int | None) -> bool:
        """True iff this thread is brand new (never titled) and not
        already mid-titling from a concurrent message."""
        if not thread_id:
            return False
        key = self._key(chat_id, thread_id)
        return key not in self._titled and key not in self._in_flight

    async def maybe_title(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        user_message: str,
    ) -> None:
        """Fire-and-forget title generation. Safe to call after every
        user message — the should_title gate keeps this near-free for
        threads that have already been titled.
        """
        if not thread_id or not user_message:
            return
        if not self.should_title(chat_id, thread_id):
            return
        key = self._key(chat_id, thread_id)
        self._in_flight.add(key)
        loop = asyncio.get_running_loop()
        loop.create_task(self._title_task(chat_id, thread_id, user_message))

    async def _title_task(
        self, chat_id: int, thread_id: int, user_message: str,
    ) -> None:
        key = self._key(chat_id, thread_id)
        try:
            title = await self._generate_title(user_message)
            if not title:
                logger.info(
                    "thread_titler: empty title for chat=%s thread=%s — "
                    "leaving as 'New Chat'", chat_id, thread_id,
                )
                # Still record the attempt so we don't retry on every
                # message — empty title is a stable property of this
                # thread (e.g. the user only sent emoji).
                await self._record(key)
                return
            await self._apply_title(chat_id, thread_id, title)
            await self._record(key)
            logger.info(
                "thread_titler: chat=%s thread=%s title=%r",
                chat_id, thread_id, title,
            )
        except Exception:
            logger.exception(
                "thread_titler failed chat=%s thread=%s — retry on next msg",
                chat_id, thread_id,
            )
        finally:
            self._in_flight.discard(key)

    async def _generate_title(self, user_message: str) -> str:
        """Single Haiku call. Returns trimmed title or empty string."""
        truncated = user_message.strip()[:USER_MESSAGE_MAX_CHARS]
        if not truncated:
            return ""

        # Provider model swap pattern — same as qanot/flush.py. Restore
        # the original model so subsequent normal chats keep their tier.
        original_model = getattr(self._provider, "model", None)
        try:
            try:
                self._provider.model = TITLE_MODEL
            except (AttributeError, TypeError):
                # Provider doesn't expose a writable .model — use whatever
                # the caller's default is; cost is still small for one call.
                pass

            response = await self._provider.chat(
                messages=[{"role": "user", "content": truncated}],
                tools=[],
                system=_TITLE_SYSTEM_PROMPT,
            )
        finally:
            if original_model is not None:
                try:
                    self._provider.model = original_model
                except (AttributeError, TypeError):
                    pass

        raw = (response.content or "").strip()
        return _normalise_title(raw)

    async def _apply_title(
        self, chat_id: int, thread_id: int, title: str,
    ) -> None:
        # Lazy import — keeps this module testable without aiogram in scope.
        from aiogram.methods import EditForumTopic

        await self._bot(EditForumTopic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=title,
        ))

    async def _record(self, key: str) -> None:
        async with self._state_lock:
            self._titled.add(key)
            await self._save_state_locked()


def _normalise_title(raw: str) -> str:
    """Strip quotes/punctuation the LLM may have wrapped around the title."""
    cleaned = raw.strip()
    # The model sometimes wraps in quotes despite the prompt.
    for q in ("\"", "'", "“", "”", "‘", "’", "«", "»", "`"):
        if cleaned.startswith(q):
            cleaned = cleaned[1:]
        if cleaned.endswith(q):
            cleaned = cleaned[:-1]
    cleaned = cleaned.strip()
    # Trim trailing punctuation.
    while cleaned and cleaned[-1] in ".!?,:;…":
        cleaned = cleaned[:-1].rstrip()
    # If the model returned multiple lines, take only the first.
    cleaned = cleaned.split("\n", 1)[0].strip()
    # Telegram caps thread names; cut on a word boundary if we can.
    if len(cleaned) > TITLE_MAX_LEN:
        cut = cleaned[:TITLE_MAX_LEN].rstrip()
        # Don't end in a half-cut word — back off to the previous space.
        last_space = cut.rfind(" ")
        if last_space > 0 and last_space >= TITLE_MAX_LEN - 8:
            cut = cut[:last_space].rstrip()
        cleaned = cut
    return cleaned
