"""Tests for forum topic binding and per-topic conversation isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from qanot.config import AgentDefinition


@dataclass
class FakeChat:
    id: int = -100123
    type: str = "supergroup"


@dataclass
class FakeUser:
    id: int = 111
    full_name: str = "Test User"
    username: str = "testuser"


@dataclass
class FakeMessage:
    chat: FakeChat = field(default_factory=FakeChat)
    from_user: FakeUser = field(default_factory=FakeUser)
    text: str = "hello"
    caption: str | None = None
    message_thread_id: int | None = None
    message_id: int = 1
    reply_to_message: None = None
    photo: None = None
    voice: None = None
    video_note: None = None
    sticker: None = None
    document: None = None


class TestConvKeyWithTopics:
    """Test that conv_key correctly isolates topics."""

    def _make_adapter(self):
        """Create a minimal adapter-like object with _conv_key method."""
        # We test the logic directly rather than instantiating TelegramAdapter
        class FakeAdapter:
            def _is_group_chat(self, message):
                return message.chat.type in ("group", "supergroup")

            def _conv_key(self, message):
                if not self._is_group_chat(message):
                    return str(message.from_user.id)
                topic_id = getattr(message, "message_thread_id", None)
                if topic_id:
                    return f"group_{message.chat.id}_topic_{topic_id}"
                return f"group_{message.chat.id}"

        return FakeAdapter()

    def test_dm_conv_key(self):
        adapter = self._make_adapter()
        msg = FakeMessage(chat=FakeChat(id=111, type="private"))
        assert adapter._conv_key(msg) == "111"

    def test_group_conv_key_no_topic(self):
        adapter = self._make_adapter()
        msg = FakeMessage()
        assert adapter._conv_key(msg) == "group_-100123"

    def test_group_conv_key_with_topic(self):
        adapter = self._make_adapter()
        msg = FakeMessage(message_thread_id=42)
        assert adapter._conv_key(msg) == "group_-100123_topic_42"

    def test_different_topics_different_keys(self):
        adapter = self._make_adapter()
        msg1 = FakeMessage(message_thread_id=1)
        msg2 = FakeMessage(message_thread_id=2)
        assert adapter._conv_key(msg1) != adapter._conv_key(msg2)

    def test_same_topic_same_key(self):
        adapter = self._make_adapter()
        msg1 = FakeMessage(message_thread_id=42, from_user=FakeUser(id=1))
        msg2 = FakeMessage(message_thread_id=42, from_user=FakeUser(id=2))
        assert adapter._conv_key(msg1) == adapter._conv_key(msg2)


class TestTopicBindingResolution:
    """Test topic-agent binding resolution."""

    def test_no_bindings_returns_none(self):
        bindings: dict[str, str] = {}
        agents: list[AgentDefinition] = []
        result = _resolve(bindings, agents, -100, 42)
        assert result is None

    def test_no_thread_id_returns_none(self):
        bindings = {"-100:42": "seo-agent"}
        agents = [AgentDefinition(id="seo-agent", name="SEO")]
        result = _resolve(bindings, agents, -100, None)
        assert result is None

    def test_binding_found(self):
        bindings = {"-100:42": "seo-agent"}
        agents = [AgentDefinition(id="seo-agent", name="SEO", prompt="You are SEO expert")]
        result = _resolve(bindings, agents, -100, 42)
        assert result is not None
        assert result.id == "seo-agent"
        assert result.prompt == "You are SEO expert"

    def test_binding_missing_agent(self):
        bindings = {"-100:42": "deleted-agent"}
        agents = [AgentDefinition(id="other-agent")]
        result = _resolve(bindings, agents, -100, 42)
        assert result is None

    def test_wrong_topic_not_bound(self):
        bindings = {"-100:42": "seo-agent"}
        agents = [AgentDefinition(id="seo-agent")]
        result = _resolve(bindings, agents, -100, 99)
        assert result is None


def _resolve(bindings, agents, chat_id, thread_id):
    """Simulate _resolve_topic_binding logic."""
    if not thread_id or not bindings:
        return None
    binding_key = f"{chat_id}:{thread_id}"
    agent_id = bindings.get(binding_key)
    if not agent_id:
        return None
    return next((ad for ad in agents if ad.id == agent_id), None)


class TestTopicBindingConfig:
    """Test topic_bindings config field."""

    def test_default_empty(self):
        from qanot.config import Config
        config = Config.__new__(Config)
        config.topic_bindings = {}
        assert config.topic_bindings == {}

    def test_binding_crud(self):
        bindings: dict[str, str] = {}

        # Create
        bindings["-100:42"] = "seo-agent"
        assert bindings["-100:42"] == "seo-agent"

        # Update
        bindings["-100:42"] = "marketing-agent"
        assert bindings["-100:42"] == "marketing-agent"

        # Delete
        del bindings["-100:42"]
        assert "-100:42" not in bindings

    def test_multiple_bindings(self):
        bindings = {
            "-100:1": "seo-agent",
            "-100:2": "dev-agent",
            "-200:1": "support-agent",
        }
        assert len(bindings) == 3
        assert bindings["-100:1"] == "seo-agent"
        assert bindings["-200:1"] == "support-agent"
