"""Tests for per-user cost tracking and lazy tool loading."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock

from qanot.agent import ToolRegistry
from qanot.context import CostTracker


class TestCostTracker:
    def test_add_usage(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        tracker.add_usage("user_1", input_tokens=1000, output_tokens=200, cost=0.05)
        tracker.add_usage("user_1", input_tokens=500, output_tokens=100, cost=0.02)

        stats = tracker.get_user_stats("user_1")
        assert stats["input_tokens"] == 1500
        assert stats["output_tokens"] == 300
        assert stats["api_calls"] == 2
        assert abs(stats["total_cost"] - 0.07) < 0.001

    def test_per_user_isolation(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        tracker.add_usage("user_a", input_tokens=1000, output_tokens=200, cost=0.05)
        tracker.add_usage("user_b", input_tokens=500, output_tokens=100, cost=0.02)

        stats_a = tracker.get_user_stats("user_a")
        stats_b = tracker.get_user_stats("user_b")
        assert stats_a["input_tokens"] == 1000
        assert stats_b["input_tokens"] == 500
        assert stats_a["total_cost"] != stats_b["total_cost"]

    def test_add_turn(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        tracker.add_turn("user_1")
        tracker.add_turn("user_1")
        tracker.add_turn("user_1")

        stats = tracker.get_user_stats("user_1")
        assert stats["turns"] == 3

    def test_get_total_cost(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        tracker.add_usage("user_a", cost=0.10)
        tracker.add_usage("user_b", cost=0.20)
        tracker.add_usage("user_c", cost=0.30)

        assert abs(tracker.get_total_cost() - 0.60) < 0.001

    def test_get_all_stats(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        tracker.add_usage("user_a", cost=0.10)
        tracker.add_usage("user_b", cost=0.20)

        all_stats = tracker.get_all_stats()
        assert "user_a" in all_stats
        assert "user_b" in all_stats
        assert len(all_stats) == 2

    def test_persistence(self, tmp_path):
        """Cost data survives save/reload."""
        tracker1 = CostTracker(str(tmp_path))
        tracker1.add_usage("user_1", input_tokens=5000, output_tokens=1000, cost=0.25)
        tracker1.add_turn("user_1")
        tracker1.save()

        # New tracker loads from same directory
        tracker2 = CostTracker(str(tmp_path))
        stats = tracker2.get_user_stats("user_1")
        assert stats["input_tokens"] == 5000
        assert stats["output_tokens"] == 1000
        assert abs(stats["total_cost"] - 0.25) < 0.001
        assert stats["turns"] == 1

    def test_cache_tracking(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        tracker.add_usage(
            "user_1",
            input_tokens=1000,
            output_tokens=200,
            cache_read=800,
            cache_write=500,
            cost=0.03,
        )

        stats = tracker.get_user_stats("user_1")
        assert stats["cache_read_tokens"] == 800
        assert stats["cache_write_tokens"] == 500

    def test_new_user_defaults(self, tmp_path):
        tracker = CostTracker(str(tmp_path))
        stats = tracker.get_user_stats("new_user")
        assert stats["input_tokens"] == 0
        assert stats["output_tokens"] == 0
        assert stats["total_cost"] == 0.0
        assert stats["turns"] == 0
        assert stats["api_calls"] == 0


class TestLazyToolLoading:
    def _make_registry(self):
        registry = ToolRegistry()
        # Core tools (always loaded)
        registry.register("read_file", "Read file", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"))
        registry.register("write_file", "Write file", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"))
        registry.register("run_command", "Run command", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"))
        # Extended tools
        registry.register("rag_search", "Search RAG", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"), category="rag")
        registry.register("generate_image", "Gen image", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"), category="image")
        registry.register("web_search", "Web search", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"), category="web")
        registry.register("cron_create", "Create cron", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"), category="cron")
        registry.register("delegate_to_agent", "Delegate", {"type": "object", "properties": {}},
                          AsyncMock(return_value="ok"), category="agent")
        return registry

    def test_all_definitions_returns_everything(self):
        reg = self._make_registry()
        defs = reg.get_definitions()
        names = {d["name"] for d in defs}
        assert len(names) == 8
        assert "rag_search" in names
        assert "generate_image" in names

    def test_lazy_no_message_returns_core_only(self):
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("")
        names = {d["name"] for d in defs}
        assert "read_file" in names
        assert "write_file" in names
        assert "run_command" in names
        # Extended should NOT be included
        assert "rag_search" not in names
        assert "generate_image" not in names
        assert "web_search" not in names
        assert "cron_create" not in names
        assert "delegate_to_agent" not in names

    def test_lazy_includes_rag_on_search(self):
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("xotiradan qidir")
        names = {d["name"] for d in defs}
        assert "rag_search" in names
        assert "read_file" in names  # Core always included
        assert "generate_image" not in names  # Image not needed

    def test_lazy_includes_image_on_request(self):
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("rasm chiz")
        names = {d["name"] for d in defs}
        assert "generate_image" in names
        assert "rag_search" not in names

    def test_lazy_includes_web_on_search(self):
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("search the internet for news")
        names = {d["name"] for d in defs}
        assert "web_search" in names

    def test_lazy_includes_cron_on_schedule(self):
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("schedule a reminder for tomorrow")
        names = {d["name"] for d in defs}
        assert "cron_create" in names

    def test_lazy_includes_agent_on_delegate(self):
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("delegate this task to agent")
        names = {d["name"] for d in defs}
        assert "delegate_to_agent" in names

    def test_lazy_multiple_categories(self):
        """Message mentioning multiple categories loads all relevant tools."""
        reg = self._make_registry()
        defs = reg.get_lazy_definitions("search memory and generate an image")
        names = {d["name"] for d in defs}
        assert "rag_search" in names
        assert "generate_image" in names
        assert "cron_create" not in names

    def test_lazy_saves_tokens(self):
        """Lazy loading returns fewer tools than full list."""
        reg = self._make_registry()
        full = reg.get_definitions()
        lazy = reg.get_lazy_definitions("hello, how are you?")
        assert len(lazy) < len(full)
