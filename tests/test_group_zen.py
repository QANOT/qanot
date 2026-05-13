"""Tests for the group-chat zen-mode classifier (Phase A — no LLM)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from qanot.group.signals import (
    DIRECT_ADDRESS_PATTERNS,
    RECENT_REPLY_SECONDS,
    collect_signals,
)
from qanot.group.state import GroupChatState
from qanot.group.zen_classifier import GroupZenClassifier


# ────────── Stub config ──────────


@dataclass
class _Cfg:
    """Minimal stand-in for qanot.config.Config — only the zen fields."""
    zen_signal_threshold: int = 3
    zen_response_cooldown_seconds: int = 30
    zen_max_responses_per_minute: int = 4
    zen_history_lookback_turns: int = 5
    zen_mute_minutes: int = 10


# ────────── Signals: pure logic ──────────


def test_collect_signals_returns_zero_on_empty_text():
    s = collect_signals(
        text="", bot_username="qanotbot",
        seconds_since_bot_reply=None, last_bot_reply_text=None,
        is_reply_to_recent_bot=False,
    )
    assert s.total == 0
    assert s.reasons == []


def test_username_substring_without_at_prefix_scores_one():
    s = collect_signals(
        text="qanotbot help me with this",
        bot_username="qanotbot",
        seconds_since_bot_reply=None, last_bot_reply_text=None,
        is_reply_to_recent_bot=False,
    )
    assert s.total == 1
    assert any("username-substring" in r for r in s.reasons)


def test_at_mention_does_not_double_count_username():
    """The @<bot> form is Layer 1's job — Layer 2 must not also add a
    signal for it."""
    s = collect_signals(
        text="@qanotbot please help",
        bot_username="qanotbot",
        seconds_since_bot_reply=None, last_bot_reply_text=None,
        is_reply_to_recent_bot=False,
    )
    # No username-substring signal because @ form is excluded
    assert not any("username-substring" in r for r in s.reasons)


def test_direct_address_qanot_at_start():
    s = collect_signals(
        text="qanot, oltin narxi qancha?",
        bot_username="qanotbot",
        seconds_since_bot_reply=None, last_bot_reply_text=None,
        is_reply_to_recent_bot=False,
    )
    assert any("direct-address" in r for r in s.reasons)


def test_direct_address_at_end_of_message():
    s = collect_signals(
        text="bu nima degani qanot?",
        bot_username="qanotbot",
        seconds_since_bot_reply=None, last_bot_reply_text=None,
        is_reply_to_recent_bot=False,
    )
    assert any("direct-address" in r for r in s.reasons)


def test_recent_bot_activity_strong_signal():
    s = collect_signals(
        text="rahmat",
        bot_username="qanotbot",
        seconds_since_bot_reply=15.0,  # within RECENT_REPLY_SECONDS
        last_bot_reply_text="Bu javob.",
        is_reply_to_recent_bot=False,
    )
    assert any("recent-bot-activity" in r for r in s.reasons)
    # +2 weight
    assert s.total >= 2


def test_stale_bot_reply_does_not_fire_recency():
    s = collect_signals(
        text="rahmat",
        bot_username="qanotbot",
        seconds_since_bot_reply=RECENT_REPLY_SECONDS + 10,
        last_bot_reply_text="Bu javob.",
        is_reply_to_recent_bot=False,
    )
    assert not any("recent-bot-activity" in r for r in s.reasons)


def test_answering_bot_question_strong_signal():
    """Bot asked '...?' just now, user replies — strong follow-up."""
    s = collect_signals(
        text="ha, asosan oltin",
        bot_username="qanotbot",
        seconds_since_bot_reply=10.0,
        last_bot_reply_text="Qaysi mavzu sizni qiziqtiradi?",
        is_reply_to_recent_bot=False,
    )
    assert any("answering-bot-question" in r for r in s.reasons)


def test_question_signal_requires_recency():
    """Stale question doesn't count — user might be discussing
    something else now."""
    s = collect_signals(
        text="ha",
        bot_username="qanotbot",
        seconds_since_bot_reply=300.0,  # 5 min — outside window
        last_bot_reply_text="Qaysi mavzu?",
        is_reply_to_recent_bot=False,
    )
    assert not any("answering-bot-question" in r for r in s.reasons)


def test_reply_to_recent_bot_strong_signal():
    s = collect_signals(
        text="aniq",
        bot_username="qanotbot",
        seconds_since_bot_reply=None, last_bot_reply_text=None,
        is_reply_to_recent_bot=True,
    )
    assert any("reply-to-bot-thread" in r for r in s.reasons)
    assert s.total >= 2


def test_signals_compose_additively():
    """Multiple signals should stack into a high score."""
    s = collect_signals(
        text="qanot, rahmat!",       # direct-address +3
        bot_username="qanotbot",
        seconds_since_bot_reply=5.0,  # recent +2
        last_bot_reply_text="Yaxshi!",  # not a question
        is_reply_to_recent_bot=True,    # reply-thread +2
    )
    assert s.total == 3 + 2 + 2  # direct + recent + reply
    assert len(s.reasons) == 3


# ────────── State ──────────


def test_state_records_bot_reply():
    state = GroupChatState()
    assert state.last_reply(-100) is None
    state.record_bot_reply(-100, text="hi", message_id=42, now=1000.0)
    last = state.last_reply(-100)
    assert last is not None
    assert last.text == "hi"
    assert last.message_id == 42
    assert state.seconds_since_last_reply(-100, now=1010.0) == 10.0


def test_state_rate_limit_window_evicts_old():
    state = GroupChatState()
    # Old reply at t=0, recent at t=50
    state.record_bot_reply(-100, text="x", message_id=1, now=0.0)
    state.record_bot_reply(-100, text="x", message_id=2, now=50.0)
    # At t=55: both inside the 60-second window (cutoff = -5)
    assert state.replies_in_last_minute(-100, now=55.0) == 2
    # At t=65: cutoff = 5, so only t=50 survives
    assert state.replies_in_last_minute(-100, now=65.0) == 1
    # At t=120: cutoff = 60, both evicted
    assert state.replies_in_last_minute(-100, now=120.0) == 0


def test_mute_sets_and_clears():
    state = GroupChatState()
    state.mute(-100, minutes=5, now=1000.0)
    assert state.is_muted(-100, now=1000.0) is True
    assert state.is_muted(-100, now=1000.0 + 60 * 5 - 1) is True
    # At exactly the boundary the mute expires.
    assert state.is_muted(-100, now=1000.0 + 60 * 5) is False


def test_explicit_unmute_clears_immediately():
    state = GroupChatState()
    state.mute(-100, minutes=10, now=1000.0)
    assert state.is_muted(-100, now=1000.0) is True
    state.unmute(-100)
    assert state.is_muted(-100, now=1000.0) is False


# ────────── Classifier: Layer 1 hard rules ──────────


def _decide(classifier, **kwargs):
    """Synchronously run the async ``decide`` for terser tests."""
    import asyncio
    return asyncio.run(classifier.decide(**kwargs))


def test_command_always_responds():
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="/help", bot_username="qanotbot",
        is_command=True, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.respond is True
    assert "cmd" in d.reason


def test_at_mention_always_responds():
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="@qanotbot help",
        bot_username="qanotbot",
        is_command=False, is_at_mention=True,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.respond is True


def test_reply_to_bot_last_always_responds():
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="aniq",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=True, is_reply_to_recent_bot=True,
    )
    assert d.respond is True


# ────────── Classifier: Layer 2 mute ──────────


def test_mute_silences_subsequent_messages():
    state = GroupChatState()
    state.mute(-100, minutes=10, now=1000.0)
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="qanot, rahmat!",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
        now=1001.0,
    )
    assert d.respond is False
    assert "muted" in d.reason


def test_at_mention_overrides_mute():
    state = GroupChatState()
    state.mute(-100, minutes=10, now=1000.0)
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="@qanotbot wake up",
        bot_username="qanotbot",
        is_command=False, is_at_mention=True,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
        now=1001.0,
    )
    # Hard rule beats mute — operator can always snap the bot awake
    assert d.respond is True


def test_mute_trigger_detected_and_propagated():
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="qanot mute",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.mute_action == "mute"


def test_unmute_trigger_takes_priority_over_mute_substring():
    """'qanot unmute' contains 'qanot mute' as a substring — the
    classifier must detect unmute, not mute."""
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="qanot unmute",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.mute_action == "unmute"


def test_unmute_request_while_muted_responds():
    state = GroupChatState()
    state.mute(-100, minutes=10, now=1000.0)
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100, text="qanot unmute",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
        now=1001.0,
    )
    assert d.respond is True
    assert d.mute_action == "unmute"


# ────────── Classifier: Layer 3 signals ──────────


def test_below_threshold_stays_quiet():
    """A weak signal (username substring only, no direct address) must
    stay below the default threshold of 3."""
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(zen_signal_threshold=3), state=state)
    d = _decide(
        c, chat_id=-100,
        # Username substring (+1) only — no vocative pattern matches.
        text="o'rtoqlar qanotbot haqida eshitganmisizlar?",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.respond is False
    assert "below-threshold" in d.reason
    assert d.score == 1


def test_direct_address_alone_now_responds():
    """Regression: production-tuned — vocative 'salom qanot' was
    previously +1 (under threshold) and went unanswered. Now +3 =
    threshold, so plain direct address responds even without recency
    or reply-to-bot signals."""
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(zen_signal_threshold=3), state=state)
    d = _decide(
        c, chat_id=-100, text="salom qanot",
        bot_username="topkeydevbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.respond is True
    assert d.score >= 3


def test_score_above_threshold_responds():
    """Score >= threshold AND outside the cooldown window → respond.
    (We avoid recent bot activity here so the cooldown doesn't block
    despite the high score — that interaction is tested separately.)"""
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(zen_signal_threshold=3), state=state)
    d = _decide(
        c, chat_id=-100,
        # qanot direct-address (+1) + reply-to-bot-thread (+2) = 3
        text="qanot, rahmat!",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=True,
    )
    assert d.respond is True
    assert d.score >= 3


def test_random_chatter_ignored():
    """A normal group conversation without any bot signals must NOT
    trigger a response."""
    state = GroupChatState()
    c = GroupZenClassifier(config=_Cfg(), state=state)
    d = _decide(
        c, chat_id=-100,
        text="O'rtoqlar, ertangi yig'ilishga necha kishi keladi?",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
    )
    assert d.respond is False


# ────────── Classifier: Layer 4 anti-spam ──────────


def test_cooldown_blocks_rapid_followup():
    state = GroupChatState()
    state.record_bot_reply(-100, text="OK", message_id=1, now=1000.0)
    c = GroupZenClassifier(
        config=_Cfg(zen_signal_threshold=2, zen_response_cooldown_seconds=30),
        state=state,
    )
    # 5s after bot reply: still in cooldown. User typing a generic
    # follow-up that DOESN'T look like answering a question.
    d = _decide(
        c, chat_id=-100, text="qanotbot",  # username +1
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=True,  # +2
        now=1005.0,
    )
    assert d.respond is False
    assert "cooldown" in d.reason


def test_cooldown_bypassed_when_answering_question():
    """User answering bot's question shouldn't be eaten by cooldown."""
    state = GroupChatState()
    state.record_bot_reply(
        -100, text="Qaysi mavzu?", message_id=1, now=1000.0,
    )
    c = GroupZenClassifier(
        config=_Cfg(zen_signal_threshold=2, zen_response_cooldown_seconds=30),
        state=state,
    )
    d = _decide(
        c, chat_id=-100, text="oltin haqida",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=False,
        now=1005.0,
    )
    # answering-bot-question +2 + recent-bot-activity +2 = 4 → above
    # threshold AND should bypass cooldown
    assert d.respond is True


def test_rate_cap_per_minute_blocks_burst():
    state = GroupChatState()
    # 4 replies all within the last minute
    for t in (1000.0, 1010.0, 1020.0, 1030.0):
        state.record_bot_reply(-100, text="x", message_id=1, now=t)
    c = GroupZenClassifier(
        config=_Cfg(
            zen_signal_threshold=2,
            zen_max_responses_per_minute=4,
            zen_response_cooldown_seconds=0,  # disable cooldown for this test
        ),
        state=state,
    )
    d = _decide(
        c, chat_id=-100, text="qanot, again",
        bot_username="qanotbot",
        is_command=False, is_at_mention=False,
        is_reply_to_bot_last=False, is_reply_to_recent_bot=True,
        now=1040.0,
    )
    assert d.respond is False
    assert "rate-cap" in d.reason
