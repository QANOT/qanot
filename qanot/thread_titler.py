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
            # Persist thread topic as a project memo. This memo carries
            # thread_scope so the router pulls it back into the prompt
            # whenever activity resumes in this thread — even if the
            # 7-day in-memory conversation TTL has long evicted the
            # history. The memo file lives forever; the titler task is
            # fire-and-forget per thread, so this write happens exactly
            # once per (chat, thread) pair.
            await self._save_thread_context_memo(
                chat_id=chat_id, thread_id=thread_id,
                title=title, first_message=user_message,
            )
        except Exception:
            logger.exception(
                "thread_titler failed chat=%s thread=%s — retry on next msg",
                chat_id, thread_id,
            )
        finally:
            self._in_flight.discard(key)

    async def _save_thread_context_memo(
        self,
        *,
        chat_id: int,
        thread_id: int,
        title: str,
        first_message: str,
    ) -> None:
        """Write a ``project-thread-<id>.md`` memo so the topic survives
        forever — even past the in-memory conversation TTL.

        Failures are swallowed; the titler primary job (Telegram title
        rename) already succeeded. We never want a memo-write hiccup
        to look like a titler failure to the operator.

        Scope: ``user_scope = str(chat_id)`` is correct only for private
        DMs, which is the only case ``maybe_title`` fires today (the
        adapter gates on ``not _is_group_chat``). Group chats need a
        per-sender user_id we don't have here; defer until group
        threading lands.
        """
        try:
            # Local import — workspace_dir is the only filesystem dep
            # the titler has, and we don't want to pay the memos package
            # import on every titler instantiation.
            from qanot.memos import MemoStore, MemoType
        except Exception as exc:  # noqa: BLE001
            logger.debug("memos package unavailable to titler: %s", exc)
            return

        workspace_dir = self._state_path.parent
        store = MemoStore(workspace_dir)

        excerpt = " ".join((first_message or "").split())[:300]
        body = (
            f"Thread mavzusi: {title}\n\n"
            f"Birinchi xabar: {excerpt}\n\n"
            f"Bu thread'da har doim shu mavzu kontekstida javob ber. "
            f"Foydalanuvchi qaytadan yozsa va aniq boshqa mavzuga "
            f"o'tmasa — generic javob emas, shu thread'ning mavzusiga "
            f"mos javob qaytar."
        )
        description = f"Thread {thread_id} mavzusi — {title}"
        # Description cap is 200 chars in MemoSpec; trim defensively.
        if len(description) > 200:
            description = description[:199] + "…"

        try:
            store.upsert(
                name=f"project-thread-{thread_id}",
                description=description,
                memo_type=MemoType.PROJECT,
                body=body,
                user_scope=str(chat_id),
                thread_scope=str(thread_id),
                why=(
                    f"Auto-captured by thread_titler on first message in "
                    f"thread {thread_id}."
                ),
                how_to_apply=(
                    "Surface this memo when activity resumes in this "
                    "thread — pulls the topic back into context even if "
                    "the in-memory conversation history has expired."
                ),
            )
            logger.info(
                "thread_titler: project memo saved for thread=%s", thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "thread_titler: memo save failed for thread=%s: %s",
                thread_id, exc,
            )

    async def _generate_title(self, user_message: str) -> str:
        """Single Haiku call. Returns trimmed title or empty string.

        We bypass ``provider.chat`` and hit the raw Anthropic client
        directly. The full provider path injects extended thinking,
        server-side tools (code_execution, memory hint), and context
        editing — none of which we want for a 2-4 word title. The
        original symptom: Haiku would emit a thinking block then jump
        to a tool_use block with zero text content, returning an empty
        title and leaving threads named "New Chat".
        """
        truncated = user_message.strip()[:USER_MESSAGE_MAX_CHARS]
        if not truncated:
            return ""

        client = getattr(self._provider, "client", None)
        if client is None:
            logger.warning(
                "thread_titler: provider has no .client attribute; "
                "cannot generate title without hitting the full provider "
                "chain (which strips text blocks under thinking/server-tools)",
            )
            return ""

        # Intentionally do NOT catch here — API exceptions (network blip,
        # auth failure, model overload) must propagate to ``_title_task``
        # which logs and skips ``_record``. That way the next user message
        # in this thread retries. Catching here and returning empty would
        # mark the thread "titled" and permanently leave it as "New Chat".
        response = await client.messages.create(
            model=TITLE_MODEL,
            max_tokens=64,  # 2-4 words easily fits; cheap + fast
            system=_TITLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": truncated}],
        )

        # Walk content blocks; only "text" blocks contribute to the title.
        # Some models emit thinking/tool_use blocks even on simple prompts;
        # those are not user-visible output.
        text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        raw = "".join(text_parts).strip()

        if not raw:
            stop_reason = getattr(response, "stop_reason", None)
            block_types = [
                getattr(b, "type", None)
                for b in (getattr(response, "content", []) or [])
            ]
            logger.info(
                "thread_titler: empty model output stop_reason=%s blocks=%r",
                stop_reason, block_types,
            )
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
