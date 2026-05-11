"""User profile enrichment via Bot API 10.0 ``getUserPersonalChatMessages``.

When a new user first interacts with the bot, we fetch the recent posts
from the channel they pinned to their personal Telegram profile and
index that content into the workspace RAG. The agent can then quote or
reason about who the user is without the operator having to explain it
every time.

The Telegram method requires Bot API 10.0 (released 2026-05-08) and
aiogram >= 3.28. On older deployments the bot method call raises and
the enricher silently no-ops — the rest of the bot keeps working.

Design notes:
- Fire-and-forget: enrichment never blocks the first response. ``maybe_enrich``
  returns immediately and schedules a background task.
- Re-enrichment cadence: once per ``REENRICH_AFTER_SECONDS`` (default 7
  days). Users update their pinned channel infrequently, and we don't
  want to spam the Bot API on every restart.
- Persistence: ``<workspace_dir>/user_profiles.json`` keeps the
  per-user "last enriched" timestamp so the cadence survives restarts.
- Privacy: only content the user has *publicly* pinned to their profile
  is fetched. We do not read DMs, contacts, or private channels.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROFILES_FILENAME = "user_profiles.json"
# Bot API caps the response at 20 messages per call — fetch the max.
FETCH_LIMIT = 20
REENRICH_AFTER_SECONDS = 7 * 24 * 3600  # 7 days


class UserProfileEnricher:
    """Fetches pinned-channel posts for new users and feeds them to RAG."""

    def __init__(
        self,
        *,
        bot: Any,
        indexer: Any,
        workspace_dir: str,
    ) -> None:
        self._bot = bot
        self._indexer = indexer
        self._state_path = Path(workspace_dir) / PROFILES_FILENAME
        # In-memory: { user_id_str: last_enriched_ts_seconds }
        self._state: dict[str, float] = self._load_state()
        # Tracks user_ids whose enrichment task is currently running, so
        # concurrent messages from the same user don't fire duplicate
        # network calls.
        self._in_flight: set[str] = set()
        # Guards _state mutations + file writes. Reads are atomic on
        # CPython but the JSON dump is multi-step.
        self._state_lock = asyncio.Lock()

    # ── State persistence ───────────────────────────────────────────

    def _load_state(self) -> dict[str, float]:
        if not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("user_profiles.json corrupt, ignoring: %s", e)
            return {}
        # Coerce: keys must be str, values must be numeric.
        out: dict[str, float] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        return out

    async def _save_state_locked(self) -> None:
        """Caller must hold ``_state_lock``."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as e:
            logger.warning("user_profiles.json write failed: %s", e)

    # ── Public API ──────────────────────────────────────────────────

    def should_enrich(self, user_id: str) -> bool:
        """True iff we've never enriched this user OR the last attempt is
        older than ``REENRICH_AFTER_SECONDS``."""
        ts = self._state.get(str(user_id))
        if ts is None:
            return True
        return (time.time() - ts) > REENRICH_AFTER_SECONDS

    async def maybe_enrich(self, user_id: str | int | None) -> None:
        """Fire-and-forget enrichment. Safe to call on every message.

        Returns immediately. Background task records the new timestamp on
        completion (success or failure — we don't want to retry-storm on
        users whose personal chat isn't reachable).
        """
        if user_id is None:
            return
        uid = str(user_id)
        if not uid or uid in self._in_flight:
            return
        if not self.should_enrich(uid):
            return
        self._in_flight.add(uid)

        loop = asyncio.get_running_loop()
        loop.create_task(self._enrich_task(uid))

    async def _enrich_task(self, user_id: str) -> int:
        try:
            return await self._do_enrich(user_id)
        except Exception:
            # Top-level catch: enrichment runs in the background — a
            # bare-task exception would otherwise just log "Task
            # exception was never retrieved" and lose context.
            logger.exception(
                "user profile enrichment failed user_id=%s", user_id,
            )
            return 0
        finally:
            self._in_flight.discard(user_id)

    async def _do_enrich(self, user_id: str) -> int:
        try:
            uid_int = int(user_id)
        except (TypeError, ValueError):
            return 0

        # Bot API 10.0 method. If aiogram < 3.28, AttributeError fires
        # and we record the timestamp anyway so we don't retry until the
        # cadence elapses (i.e. don't spam the network on every message).
        try:
            messages = await self._bot.get_user_personal_chat_messages(
                user_id=uid_int, limit=FETCH_LIMIT,
            )
        except AttributeError:
            logger.info(
                "aiogram bot has no get_user_personal_chat_messages; "
                "needs aiogram>=3.28 and Bot API 10.0",
            )
            await self._record_attempt(user_id)
            return 0
        except Exception as e:
            # Most common: user has no pinned channel → API returns an
            # error. Don't escalate — record the attempt and move on.
            logger.info(
                "no personal chat for user_id=%s: %s", user_id, e,
            )
            await self._record_attempt(user_id)
            return 0

        texts: list[str] = []
        for m in messages or []:
            text = getattr(m, "text", None) or getattr(m, "caption", None)
            if text and text.strip():
                texts.append(text.strip())

        if not texts:
            await self._record_attempt(user_id)
            return 0

        combined = "\n\n".join(texts)
        try:
            await self._indexer.index_text(
                combined,
                source=f"user_profile:{user_id}",
                user_id=user_id,
                metadata={
                    "kind": "user_personal_channel",
                    "count": len(texts),
                },
            )
        except Exception:
            logger.exception(
                "RAG ingest of user profile failed user_id=%s", user_id,
            )
            await self._record_attempt(user_id)
            return 0

        logger.info(
            "user profile enriched user_id=%s posts=%d", user_id, len(texts),
        )
        await self._record_attempt(user_id)
        return len(texts)

    async def _record_attempt(self, user_id: str) -> None:
        async with self._state_lock:
            self._state[user_id] = time.time()
            await self._save_state_locked()
