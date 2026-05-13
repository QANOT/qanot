"""Persistent poll registry for conversational quiz flow.

When the agent calls ``tg_send_poll``, we record the poll's context here
keyed by Telegram's ``poll_id``. When the user taps an option, Telegram
fires a ``poll_answer`` update — but that update carries only the
``poll_id`` and the user's vote, NOT the original chat/thread. The
registry is what lets us route the answer back to the agent in the same
conversation.

Persistence: a single JSON file at ``<workspace_dir>/poll_state.json``.
Reloaded on adapter init so restarts don't lose pending polls.
Entries are auto-evicted after 7 days — quizzes typically live for
minutes, so this only matters for forgotten polls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_STATE_FILENAME = "poll_state.json"
# How long a registered poll lives before the sweeper drops it. Quizzes
# typically resolve within minutes; this only cleans up forgotten state.
POLL_TTL_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass
class PollRecord:
    """Everything we need to route a poll_answer back to the right
    conversation and let the LLM evaluate the response."""

    poll_id: str
    chat_id: int
    thread_id: int | None
    question: str
    options: list[str]
    correct_option_ids: list[int]  # empty list = regular (non-quiz) poll
    sent_at: float  # unix timestamp
    # Telegram message_id of the poll itself. Used so the evaluator's
    # reply can anchor visually to the poll via ``reply_to_message_id``
    # — keeps per-question feedback discoverable in all-at-once mode
    # where the user might answer polls out of order.
    message_id: int = 0
    explanation: str = ""
    # Track who's already answered so revotes don't double-fire the
    # agent turn. user_id → list of selected option indices.
    answers: dict[int, list[int]] = field(default_factory=dict)


class PollRegistry:
    """In-memory + on-disk store for sent polls.

    Thread-safe enough for asyncio: dict writes are atomic on CPython,
    and the persistence path is guarded by an asyncio.Lock so concurrent
    saves can't interleave JSON.
    """

    def __init__(self, workspace_dir: str) -> None:
        self._state_path = Path(workspace_dir) / POLL_STATE_FILENAME
        self._polls: dict[str, PollRecord] = self._load()
        self._lock = asyncio.Lock()

    # ── Persistence ─────────────────────────────────────────────────

    def _load(self) -> dict[str, PollRecord]:
        if not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("poll_state.json corrupt, ignoring: %s", e)
            return {}
        out: dict[str, PollRecord] = {}
        cutoff = time.time() - POLL_TTL_SECONDS
        for pid, data in (raw or {}).items():
            if not isinstance(data, dict):
                continue
            try:
                ts = float(data.get("sent_at", 0))
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                # Already too old — drop on load.
                continue
            try:
                out[str(pid)] = PollRecord(
                    poll_id=str(pid),
                    chat_id=int(data["chat_id"]),
                    thread_id=(
                        int(data["thread_id"])
                        if data.get("thread_id") is not None
                        else None
                    ),
                    question=str(data.get("question", "")),
                    options=list(data.get("options", [])),
                    correct_option_ids=[
                        int(x) for x in data.get("correct_option_ids", [])
                    ],
                    sent_at=ts,
                    message_id=int(data.get("message_id", 0) or 0),
                    explanation=str(data.get("explanation", "")),
                    answers={
                        int(uid): [int(x) for x in opts]
                        for uid, opts in (data.get("answers") or {}).items()
                    },
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("skipping corrupt poll entry %s: %s", pid, e)
        return out

    async def _save_locked(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            payload = {pid: asdict(rec) for pid, rec in self._polls.items()}
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
        except OSError as e:
            logger.warning("poll_state.json write failed: %s", e)

    # ── Public API ──────────────────────────────────────────────────

    async def register(
        self,
        *,
        poll_id: str,
        chat_id: int,
        thread_id: int | None,
        question: str,
        options: list[str],
        correct_option_ids: list[int],
        message_id: int = 0,
        explanation: str = "",
    ) -> None:
        """Record a poll the bot just sent so we can route answers later."""
        record = PollRecord(
            poll_id=poll_id,
            chat_id=chat_id,
            thread_id=thread_id,
            question=question,
            options=list(options),
            correct_option_ids=list(correct_option_ids),
            sent_at=time.time(),
            message_id=int(message_id or 0),
            explanation=explanation,
        )
        async with self._lock:
            self._polls[poll_id] = record
            await self._save_locked()

    def get(self, poll_id: str) -> PollRecord | None:
        return self._polls.get(poll_id)

    async def record_answer(
        self, poll_id: str, user_id: int, option_ids: list[int],
    ) -> bool:
        """Store a user's answer. Returns True iff this is a new answer
        (so the caller can decide whether to fire the agent turn). A
        repeated answer with the same options is a no-op; a revote with
        different options updates the record and counts as new."""
        record = self._polls.get(poll_id)
        if record is None:
            return False
        prev = record.answers.get(user_id)
        new = list(option_ids)
        if prev == new:
            return False
        async with self._lock:
            record.answers[user_id] = new
            await self._save_locked()
        return True

    async def evict_stale(self, *, now: float | None = None) -> int:
        """Drop poll records older than the TTL. Returns count evicted."""
        ts = now if now is not None else time.time()
        cutoff = ts - POLL_TTL_SECONDS
        stale = [pid for pid, r in self._polls.items() if r.sent_at < cutoff]
        if not stale:
            return 0
        async with self._lock:
            for pid in stale:
                self._polls.pop(pid, None)
            await self._save_locked()
        return len(stale)

    # ── Synthetic message construction ─────────────────────────────

    @staticmethod
    def build_answer_message(
        record: PollRecord, option_ids: list[int],
    ) -> str:
        """Render a poll answer as a synthetic user message the agent
        can evaluate naturally.

        The agent sees this as if the user typed it. The format is
        deliberately structured so the LLM doesn't have to guess what
        the user meant — it can immediately say "✅ correct" or
        "❌ wrong, the answer is X because…".
        """
        chosen = ", ".join(
            f"{_letter(i)}) {record.options[i]}"
            for i in option_ids
            if 0 <= i < len(record.options)
        ) or "(no option)"

        if record.correct_option_ids:
            correct = ", ".join(
                f"{_letter(i)}) {record.options[i]}"
                for i in record.correct_option_ids
                if 0 <= i < len(record.options)
            )
            is_correct = sorted(option_ids) == sorted(record.correct_option_ids)
            result = "✅ TO'G'RI" if is_correct else "❌ NOTO'G'RI"
            lines = [
                "[Test javobi keldi]",
                f"Savol: {record.question}",
                f"Tanlangan: {chosen}",
                f"To'g'ri javob: {correct}",
                f"Natija: {result}",
            ]
            if record.explanation:
                lines.append(f"Izoh: {record.explanation}")
            lines.append(
                "Foydalanuvchiga qisqa va aniq reaksiya bering (3-4 jumla). "
                "Agar test seriyasidan bo'lsa, keyingi savolni yuboring."
            )
        else:
            lines = [
                "[Poll javobi keldi]",
                f"Savol: {record.question}",
                f"Tanlangan: {chosen}",
                "Foydalanuvchining tanlovini tan oling va kontekst bo'yicha javob bering.",
            ]
        return "\n".join(lines)


def _letter(index: int) -> str:
    """0 → 'A', 1 → 'B', etc. Matches the lettering convention used in
    quiz UIs and how tg_send_poll's text representation reads."""
    if 0 <= index < 26:
        return chr(ord("A") + index)
    return str(index)
