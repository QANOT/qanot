"""Group chat zen-mode classifier — Phase A (no LLM).

Four layers, evaluated in order. The first one that produces a verdict
wins:

  1. **Hard rules** — @mention, reply-to-bot, slash command. The bot
     MUST respond. No score, no budget, no mute override.

  2. **Mute** — explicit "qanot mute" / "qanot jim tur" puts the chat
     in silent mode for ``zen_mute_minutes``. While muted, ONLY hard
     rules trigger a reply (so the operator can still snap the bot
     awake with an @mention).

  3. **Soft signals** — collected by ``signals.collect_signals``. If
     the total score is below ``zen_signal_threshold``, stay quiet.

  4. **Anti-spam** — once we've decided to respond, check:
       - per-chat cooldown (``zen_response_cooldown_seconds``)
       - per-minute hard ceiling (``zen_max_responses_per_minute``)
     If either fires, suppress the reply.

Phase A is heuristic-only. Layer 3 (LLM "should I respond?" classifier)
is documented in the design doc but not implemented — we want
production traces from this phase first before adding the cost.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from qanot.group.signals import RECENT_REPLY_SECONDS, collect_signals

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.group.state import GroupChatState

logger = logging.getLogger(__name__)


# User-facing mute / unmute trigger words. Matched against the trimmed
# lowercase message body. We deliberately keep this list tight: every
# phrase here MUST be one a normal user would only say to silence the
# bot, not in unrelated conversation.
_MUTE_TRIGGERS = (
    "qanot mute",
    "qanot jim",
    "qanot jim tur",
    "qanot stop",
    "qanot, jim tur",
    "qanot, mute",
)
_UNMUTE_TRIGGERS = (
    "qanot unmute",
    "qanot ovoz",
    "qanot gapir",
    "qanot, ovoz",
    "qanot, unmute",
)
_MUTE_NORMALISE = re.compile(r"\s+")


@dataclass
class ZenDecision:
    """Outcome of one classifier pass. The adapter uses ``respond`` to
    gate the reply; ``reason`` and ``score`` are logged for tuning.
    """

    respond: bool
    reason: str
    score: int = 0
    # When set, the adapter must take an additional side-effect:
    #   "mute"   — silence the chat for the configured duration
    #   "unmute" — clear the silent state
    mute_action: str | None = None


class GroupZenClassifier:
    """Heuristic 'should the bot reply in this group?' decision-maker."""

    def __init__(self, *, config: "Config", state: "GroupChatState") -> None:
        self._config = config
        self._state = state

    # ── Public entry point ─────────────────────────────────────────

    async def decide(
        self,
        *,
        chat_id: int,
        text: str,
        bot_username: str | None,
        is_command: bool,
        is_at_mention: bool,
        is_reply_to_bot_last: bool,
        is_reply_to_recent_bot: bool,
        now: float | None = None,
    ) -> ZenDecision:
        """Return whether the bot should reply, plus the reason.

        Args:
            chat_id: Telegram chat id (negative for groups).
            text: User message text/caption.
            bot_username: Bot's @-less username.
            is_command: True if message starts with "/".
            is_at_mention: True if "@<bot_username>" appears in text.
            is_reply_to_bot_last: True if Telegram reply target is the
                bot's most recent message (Layer 1).
            is_reply_to_recent_bot: True if Telegram reply target is any
                bot message within the recency window (Layer 2 input).
            now: Override for tests; defaults to ``time.time()``.
        """
        ts = now if now is not None else time.time()

        # Mute trigger first — even if a hard rule would fire, the user
        # asking for silence should still take effect. We DO still
        # respond to the mute message (with a confirmation in the
        # adapter), but we mark the chat muted for future turns.
        mute_action = self._detect_mute_action(text)

        # ── Layer 1: hard rules ───────────────────────────────────
        if is_command:
            return ZenDecision(
                respond=True, reason="cmd-slash", mute_action=mute_action,
            )
        if is_at_mention:
            return ZenDecision(
                respond=True, reason="hard-at-mention", mute_action=mute_action,
            )
        if is_reply_to_bot_last:
            return ZenDecision(
                respond=True, reason="hard-reply-to-bot",
                mute_action=mute_action,
            )

        # ── Layer 2: mute ─────────────────────────────────────────
        if self._state.is_muted(chat_id, now=ts):
            # If the user is asking to unmute, that itself is a signal
            # to wake up. The adapter applies the mute_action.
            if mute_action == "unmute":
                return ZenDecision(
                    respond=True, reason="unmute-request",
                    mute_action="unmute",
                )
            return ZenDecision(
                respond=False, reason="muted", mute_action=mute_action,
            )

        # ── Layer 3: soft signals ─────────────────────────────────
        seconds_since = self._state.seconds_since_last_reply(chat_id, now=ts)
        last_reply = self._state.last_reply(chat_id)
        last_text = last_reply.text if last_reply is not None else None

        score = collect_signals(
            text=text,
            bot_username=bot_username,
            seconds_since_bot_reply=seconds_since,
            last_bot_reply_text=last_text,
            is_reply_to_recent_bot=is_reply_to_recent_bot,
        )

        if score.total < self._config.zen_signal_threshold:
            return ZenDecision(
                respond=False,
                reason=f"below-threshold ({score.total}<{self._config.zen_signal_threshold}) "
                       f"[{','.join(score.reasons) or 'no-signals'}]",
                score=score.total,
                mute_action=mute_action,
            )

        # ── Layer 4: anti-spam ────────────────────────────────────
        # Per-chat cooldown — if the bot replied very recently we hold
        # off to avoid a back-to-back firehose. Score-passing messages
        # that fall inside the cooldown are dropped, not deferred.
        #
        # Exception: explicit-address signals (direct-address vocative,
        # reply-to-bot-thread) bypass the cooldown. Q&A pairs and
        # follow-ups need to flow naturally; the user explicitly chose
        # to address the bot, so we shouldn't punish them for the bot's
        # own recent activity.
        if (
            seconds_since is not None
            and seconds_since < self._config.zen_response_cooldown_seconds
        ):
            explicit_address = any(
                ("direct-address" in r) or ("reply-to-bot-thread" in r)
                for r in score.reasons
            )
            if not explicit_address:
                return ZenDecision(
                    respond=False,
                    reason=f"cooldown ({seconds_since:.1f}s<{self._config.zen_response_cooldown_seconds}s)",
                    score=score.total,
                    mute_action=mute_action,
                )

        # Hard ceiling — never exceed N replies per minute in one chat.
        recent = self._state.replies_in_last_minute(chat_id, now=ts)
        if recent >= self._config.zen_max_responses_per_minute:
            return ZenDecision(
                respond=False,
                reason=f"rate-cap ({recent}>={self._config.zen_max_responses_per_minute}/min)",
                score=score.total,
                mute_action=mute_action,
            )

        return ZenDecision(
            respond=True,
            reason=f"engaged [{','.join(score.reasons)}]",
            score=score.total,
            mute_action=mute_action,
        )

    # ── Internal ───────────────────────────────────────────────────

    def _detect_mute_action(self, text: str) -> str | None:
        if not text:
            return None
        normalised = _MUTE_NORMALISE.sub(" ", text.strip().lower())
        # Test unmute FIRST — "qanot unmute" matches "qanot mute" as a
        # substring otherwise.
        for trigger in _UNMUTE_TRIGGERS:
            if trigger in normalised:
                return "unmute"
        for trigger in _MUTE_TRIGGERS:
            if trigger in normalised:
                return "mute"
        return None
