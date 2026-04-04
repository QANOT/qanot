"""Tests for session resume — conversation snapshots and restore."""

import json
import time
from pathlib import Path

import pytest

from qanot.conversation import Conversation, ConversationManager


@pytest.fixture
def tmp_snapshot_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def manager():
    return ConversationManager(history_limit=50, ttl=3600)


class TestConversationSnapshot:
    """Test snapshot save/load lifecycle."""

    def test_save_empty_returns_zero(self, manager, tmp_snapshot_dir):
        assert manager.save_snapshot(tmp_snapshot_dir) == 0

    def test_save_and_load_roundtrip(self, manager, tmp_snapshot_dir):
        # Populate conversations
        msgs1 = manager.ensure_messages("user1")
        msgs1.append({"role": "user", "content": "hello"})
        msgs1.append({"role": "assistant", "content": "hi there"})

        msgs2 = manager.ensure_messages("user2")
        msgs2.append({"role": "user", "content": "test"})

        # Save
        saved = manager.save_snapshot(tmp_snapshot_dir)
        assert saved == 2

        # Verify file exists
        snapshot_path = Path(tmp_snapshot_dir) / "conversations_snapshot.json"
        assert snapshot_path.exists()

        # Load into fresh manager
        new_manager = ConversationManager(history_limit=50, ttl=3600)
        loaded = new_manager.load_snapshot(tmp_snapshot_dir)
        assert loaded == 2

        # Verify messages restored
        assert len(new_manager.get_messages("user1")) == 2
        assert new_manager.get_messages("user1")[0]["content"] == "hello"
        assert len(new_manager.get_messages("user2")) == 1

        # Snapshot file should be deleted after load
        assert not snapshot_path.exists()

    def test_snapshot_skips_none_user(self, manager, tmp_snapshot_dir):
        msgs = manager.ensure_messages(None)
        msgs.append({"role": "user", "content": "anon"})
        assert manager.save_snapshot(tmp_snapshot_dir) == 0

    def test_snapshot_respects_history_limit(self, tmp_snapshot_dir):
        manager = ConversationManager(history_limit=3, ttl=3600)
        msgs = manager.ensure_messages("user1")
        for i in range(10):
            msgs.append({"role": "user", "content": f"msg {i}"})

        manager.save_snapshot(tmp_snapshot_dir)

        new_manager = ConversationManager(history_limit=3, ttl=3600)
        new_manager.load_snapshot(tmp_snapshot_dir)
        restored = new_manager.get_messages("user1")
        assert len(restored) == 3
        assert restored[0]["content"] == "msg 7"

    def test_load_nonexistent_returns_zero(self, manager, tmp_snapshot_dir):
        assert manager.load_snapshot(tmp_snapshot_dir) == 0

    def test_load_corrupted_json(self, tmp_snapshot_dir):
        snapshot_path = Path(tmp_snapshot_dir) / "conversations_snapshot.json"
        snapshot_path.write_text("not json", encoding="utf-8")

        manager = ConversationManager()
        assert manager.load_snapshot(tmp_snapshot_dir) == 0

    def test_load_invalid_format(self, tmp_snapshot_dir):
        snapshot_path = Path(tmp_snapshot_dir) / "conversations_snapshot.json"
        snapshot_path.write_text('"just a string"', encoding="utf-8")

        manager = ConversationManager()
        assert manager.load_snapshot(tmp_snapshot_dir) == 0


class TestRestoredFlag:
    """Test the restored flag for session resume notification."""

    def test_restored_flag_set_on_session_restore(self, manager):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        manager.restore_from_session("user1", messages)
        assert manager.is_restored("user1") is True

    def test_restored_flag_not_set_on_empty_restore(self, manager):
        manager.restore_from_session("user1", [])
        assert manager.is_restored("user1") is False

    def test_restored_flag_cleared(self, manager):
        messages = [{"role": "user", "content": "hi"}]
        manager.restore_from_session("user1", messages)
        assert manager.is_restored("user1") is True

        manager.clear_restored_flag("user1")
        assert manager.is_restored("user1") is False

    def test_restored_flag_set_on_snapshot_load(self, manager, tmp_snapshot_dir):
        msgs = manager.ensure_messages("user1")
        msgs.append({"role": "user", "content": "test"})
        manager.save_snapshot(tmp_snapshot_dir)

        new_manager = ConversationManager()
        new_manager.load_snapshot(tmp_snapshot_dir)
        assert new_manager.is_restored("user1") is True

    def test_is_restored_false_for_unknown_user(self, manager):
        assert manager.is_restored("unknown") is False

    def test_clear_restored_flag_noop_for_unknown_user(self, manager):
        # Should not raise
        manager.clear_restored_flag("unknown")
