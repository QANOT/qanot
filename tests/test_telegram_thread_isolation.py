"""Tests for thread-aware conversation isolation (Bot API 10.0 Threaded Mode).

When a bot has Threaded Mode enabled, users can open multiple parallel
conversation threads in PRIVATE chats — not just in groups/forums.
``_conv_key`` must separate those threads so an AI agent doesn't mix
"Work" and "Personal" context across them.
"""

from __future__ import annotations

from types import SimpleNamespace

from qanot.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    """Build a TelegramAdapter without invoking __init__ (which needs a
    full Config, bot token, agent, scheduler). We only exercise the
    pure ``_conv_key`` method below."""
    return TelegramAdapter.__new__(TelegramAdapter)


def _msg(
    *,
    user_id: int = 42,
    chat_id: int = 42,
    chat_type: str = "private",
    thread_id: int | None = None,
):
    """Build a minimal message namespace shaped like aiogram's Message."""
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=chat,
        message_thread_id=thread_id,
    )


def _is_group_chat(message) -> bool:
    """Stand-in for the adapter's group-chat detection."""
    return message.chat.type in ("group", "supergroup")


# ── Private chat without thread (base view) ────────────────────────


def test_private_chat_base_view_uses_user_id_key():
    """The pre-Threaded-Mode behaviour: no thread_id → bare user id key.
    Preserves backwards compatibility for existing conversations."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]
    msg = _msg(user_id=42, thread_id=None)
    assert adapter._conv_key(msg) == "42"


# ── Private chat with thread (Threaded Mode enabled) ───────────────


def test_private_chat_thread_gets_separate_key():
    """Two threads from the same user must produce different keys —
    otherwise their conversations merge into one history."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]

    base = adapter._conv_key(_msg(user_id=42, thread_id=None))
    thread_1 = adapter._conv_key(_msg(user_id=42, thread_id=10))
    thread_2 = adapter._conv_key(_msg(user_id=42, thread_id=20))

    # All three keys are distinct.
    assert len({base, thread_1, thread_2}) == 3
    # Threaded keys carry both user id and thread id for grep-ability.
    assert "42" in thread_1 and "10" in thread_1
    assert "42" in thread_2 and "20" in thread_2


def test_private_chat_same_thread_returns_stable_key():
    """The key must be deterministic — repeat calls with the same input
    produce the same key (otherwise conversations would never accumulate)."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]
    k1 = adapter._conv_key(_msg(user_id=42, thread_id=10))
    k2 = adapter._conv_key(_msg(user_id=42, thread_id=10))
    assert k1 == k2


def test_different_users_in_same_thread_id_get_different_keys():
    """Thread ids are local to each user's private chat. Two different
    users with the same numeric thread_id must NOT share state."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]
    k_user_a = adapter._conv_key(_msg(user_id=100, thread_id=5))
    k_user_b = adapter._conv_key(_msg(user_id=200, thread_id=5))
    assert k_user_a != k_user_b


# ── Group chat parity ──────────────────────────────────────────────


def test_group_chat_base_view_uses_chat_key():
    """Group chats with no topic still use the chat-id key."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]
    msg = _msg(user_id=42, chat_id=-100123, chat_type="supergroup", thread_id=None)
    key = adapter._conv_key(msg)
    assert "group" in key
    assert "100123" in key


def test_group_chat_topic_uses_topic_key():
    """Forum topics in groups are separated — already existed before
    Threaded Mode but we verify the path still works."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]
    msg = _msg(user_id=42, chat_id=-100123, chat_type="supergroup", thread_id=7)
    key = adapter._conv_key(msg)
    assert "group" in key
    assert "topic" in key
    assert "7" in key


def test_private_thread_key_distinct_from_group_topic_key():
    """A private chat thread for user 42 in thread 7 must not collide
    with a group chat topic 7 — they're separate scopes."""
    adapter = _make_adapter()
    adapter._is_group_chat = _is_group_chat  # type: ignore[method-assign]
    private = adapter._conv_key(_msg(user_id=42, thread_id=7))
    group = adapter._conv_key(_msg(
        user_id=42, chat_id=-100999, chat_type="supergroup", thread_id=7,
    ))
    assert private != group


# ── Send-draft thread propagation ──────────────────────────────────


def test_send_draft_signature_accepts_thread_id():
    """Regression: _send_draft must accept message_thread_id so streaming
    drafts land inside the user's open thread (Bot API 10.0)."""
    import inspect
    from qanot.telegram.streaming import StreamingMixin

    sig = inspect.signature(StreamingMixin._send_draft)
    assert "thread_id" in sig.parameters
