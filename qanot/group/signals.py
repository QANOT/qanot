"""Heuristic signal scoring — Layer 2 of the zen classifier.

A "signal" is a cheap, deterministic check that nudges the bot toward
or away from replying to a group message that wasn't explicitly
addressed (no @mention, not a reply to the bot). We sum signal weights
into a single score; the classifier compares it to a threshold.

Design rule: every signal here MUST be implementable without an LLM
call and without touching Telegram's HTTP API. Anything that needs the
LLM lives in Layer 3 (not part of Phase A).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# How fresh "bot recently active" has to be for the recency signal to
# fire. The number is generous — group conversations often slow to a
# 60-second cadence between turns.
RECENT_REPLY_SECONDS = 60.0

# Common Uzbek/English direct-address words. Word-boundary matched
# case-insensitively. Tuned conservative to avoid false positives on
# generic "ai/bot/qanot" mentions in unrelated text (e.g. someone
# discussing AI ethics).
DIRECT_ADDRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Start-of-message addressing: "qanot, do X" or "bot, please…"
    re.compile(r"^\s*qanot\b", re.IGNORECASE),
    re.compile(r"^\s*qanot\s+(ai|bot)\b", re.IGNORECASE),
    re.compile(r"^\s*bot\b", re.IGNORECASE),
    re.compile(r"^\s*ai\s*,", re.IGNORECASE),
    # Vocative: "<X>, qanot" at the very end of the message
    re.compile(r"\bqanot\s*[!?.]*\s*$", re.IGNORECASE),
)


@dataclass
class SignalScore:
    """The collected score plus a human-readable breakdown for logs."""

    total: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, weight: int, reason: str) -> None:
        if weight <= 0:
            return
        self.total += weight
        self.reasons.append(f"{reason}(+{weight})")


def collect_signals(
    *,
    text: str,
    bot_username: str | None,
    seconds_since_bot_reply: float | None,
    last_bot_reply_text: str | None,
    is_reply_to_recent_bot: bool,
) -> SignalScore:
    """Score how strongly a group message looks "addressed to the bot".

    All inputs are cheap to compute from the Telegram Message + per-chat
    state. No network calls, no LLM.

    Args:
        text: User message text/caption (already extracted by adapter).
        bot_username: Bot's @-less username (e.g. "topkeydevbot").
        seconds_since_bot_reply: How long ago the bot last spoke in this
            chat, or ``None`` if it hasn't.
        last_bot_reply_text: The bot's last reply text in this chat,
            for "follow-up to a question" detection. ``None`` if unknown.
        is_reply_to_recent_bot: True iff the Telegram message is a reply
            to a message-id authored by the bot within the recency
            window. (Layer 1 catches direct reply-to-bot's-last-message
            for free; this picks up replies to *older* bot messages.)
    """
    score = SignalScore()
    if not text:
        return score
    lower_text = text.lower()

    # Signal 1: bot username appears as substring (no @ prefix).
    # Someone typing "qanotbot, help me" without @ is addressing us,
    # but Layer 1's @mention check wouldn't fire.
    if bot_username and bot_username.lower() in lower_text:
        # Avoid double-counting the @mention case — the adapter would
        # already have routed that through Layer 1.
        if f"@{bot_username.lower()}" not in lower_text:
            score.add(1, "username-substring")

    # Signal 2: direct address words at start/end of message.
    # Strong signal — the vocative "qanot, …" or "…, qanot" is the
    # text-only equivalent of @-mention. Production-tuned: +1 was too
    # quiet ("salom qanot" stayed unanswered in real chats).
    if _matches_direct_address(text):
        score.add(3, "direct-address")

    # Signal 3: bot was active in this chat very recently. WEAK signal
    # by design — recency alone must NOT trigger a response. Otherwise
    # any message in a group becomes a reply candidate just because the
    # bot spoke 30 seconds ago (false positive observed in production:
    # "salom hammaga" got answered because bot had just replied with "?").
    if (
        seconds_since_bot_reply is not None
        and seconds_since_bot_reply <= RECENT_REPLY_SECONDS
    ):
        score.add(1, "recent-bot-activity")

    # Signal 4: user replied (Telegram-level) to a bot message. Explicit
    # addressee signal — high weight. Layer 1 catches the immediate
    # prior-turn reply; this picks up replies to older bot messages.
    if is_reply_to_recent_bot:
        score.add(3, "reply-to-bot-thread")

    # NOTE: an earlier prototype scored "bot's last reply ended with ?
    # AND user spoke within the recency window" as +2 ("answering-bot-
    # question"). Removed because it fires regardless of WHAT the user
    # said — a bot that asked "how can I help?" then saw "salom hammaga"
    # (a greeting to everyone in the group, not the bot) would respond.
    # The signal lacked addressee evidence. The user can still trigger
    # a reply by using Telegram's reply gesture (caught by Layer 1 /
    # reply-to-bot-thread above) or by addressing the bot vocatively.

    return score


def _matches_direct_address(text: str) -> bool:
    for pat in DIRECT_ADDRESS_PATTERNS:
        if pat.search(text):
            return True
    return False


# Public re-exports for tests / external code that wants to extend
# the direct-address list at runtime.
__all__ = [
    "DIRECT_ADDRESS_PATTERNS",
    "RECENT_REPLY_SECONDS",
    "SignalScore",
    "collect_signals",
]
