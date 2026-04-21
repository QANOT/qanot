"""Tests for ChannelStore persistence + resolution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from tg_engine.channels import ChannelStore  # noqa: E402


def test_empty_store_no_file(tmp_path):
    s = ChannelStore(tmp_path)
    assert s.channels == []
    assert s.default_channel_id is None
    # No file created until first write
    assert not (tmp_path / "memory" / "channels.json").exists()


def test_add_persists_to_disk(tmp_path):
    s = ChannelStore(tmp_path)
    added = s.add({"id": -1001, "title": "News", "username": "news_uz"})
    assert added is True
    assert len(s.channels) == 1
    assert s.default_channel_id == -1001

    # New instance reads the same data
    s2 = ChannelStore(tmp_path)
    assert len(s2.channels) == 1
    assert s2.channels[0]["title"] == "News"
    assert s2.default_channel_id == -1001


def test_add_upserts_by_id(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "title": "Old Title", "username": "foo"})
    added = s.add({"id": -1001, "title": "New Title", "username": "foo"})
    assert added is False  # updated, not added
    assert len(s.channels) == 1
    assert s.channels[0]["title"] == "New Title"


def test_remove(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "title": "A"})
    s.add({"id": -1002, "title": "B"})
    assert s.default_channel_id == -1001

    removed = s.remove(-1001)
    assert removed is True
    # Default falls back to remaining channel
    assert s.default_channel_id == -1002

    # Non-existent
    assert s.remove(-9999) is False


def test_remove_last_clears_default(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001})
    s.remove(-1001)
    assert s.channels == []
    assert s.default_channel_id is None


def test_resolve_default(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "username": "news"})
    assert s.resolve(None) == -1001
    assert s.resolve("") == -1001


def test_resolve_numeric_id_string(tmp_path):
    s = ChannelStore(tmp_path)
    assert s.resolve("-1001234567890") == -1001234567890


def test_resolve_numeric_id_int(tmp_path):
    s = ChannelStore(tmp_path)
    assert s.resolve(-1001234567890) == -1001234567890


def test_resolve_at_username(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "username": "news_uz"})
    assert s.resolve("@news_uz") == -1001
    # Bare username also works
    assert s.resolve("news_uz") == -1001
    # Case-insensitive
    assert s.resolve("@News_UZ") == -1001


def test_resolve_title_substring(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "title": "Qanot News Channel", "username": "news"})
    # Falls back from @username to title substring
    assert s.resolve("qanot") == -1001


def test_resolve_unknown_non_numeric_returns_none(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "title": "Foo"})
    assert s.resolve("nonexistent-handle") is None


def test_set_default(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001})
    s.add({"id": -1002})
    assert s.default_channel_id == -1001
    assert s.set_default(-1002) is True
    assert s.default_channel_id == -1002
    # Can't set to an unknown id
    assert s.set_default(-9999) is False


def test_atomic_write_on_disk_format(tmp_path):
    s = ChannelStore(tmp_path)
    s.add({"id": -1001, "title": "News", "username": "news_uz"})
    written = json.loads(
        (tmp_path / "memory" / "channels.json").read_text(encoding="utf-8")
    )
    assert written["default_channel_id"] == -1001
    assert written["channels"][0]["username"] == "news_uz"
