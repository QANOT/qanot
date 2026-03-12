"""Tests for sub-agent tool — spawn/list/limits."""

from __future__ import annotations

import asyncio
import json
import pytest

from qanot.tools.subagent import (
    MAX_CONCURRENT_PER_USER,
    SUB_AGENT_TIMEOUT,
    MAX_RESULT_CHARS,
    _get_active_count,
    _format_result,
    _active_tasks,
)


class TestFormatResult:

    def test_basic_format(self):
        result = _format_result("abc12345", "research AI", "Some findings", 12.5)
        assert "Sub-agent completed" in result
        assert "research AI" in result
        assert "Some findings" in result
        assert "12s" in result  # elapsed

    def test_truncates_long_result(self):
        long_text = "x" * (MAX_RESULT_CHARS + 500)
        result = _format_result("task1234", "long task", long_text, 5.0)
        assert "[... truncated]" in result
        assert len(result) < MAX_RESULT_CHARS + 500  # shorter than original

    def test_short_result_not_truncated(self):
        result = _format_result("task1234", "short task", "short", 1.0)
        assert "[... truncated]" not in result


class TestGetActiveCount:

    def setup_method(self):
        _active_tasks.clear()

    def test_no_active_tasks(self):
        assert _get_active_count("user1") == 0

    def test_with_running_task(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)
        _active_tasks["user1"] = {"task1": task}
        assert _get_active_count("user1") == 1
        task.cancel()
        loop.close()

    def test_cleans_up_done_tasks(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_result(None)  # Mark as done
        task = asyncio.ensure_future(future, loop=loop)
        _active_tasks["user1"] = {"task1": task}
        assert _get_active_count("user1") == 0  # Cleaned up
        loop.close()

    def teardown_method(self):
        _active_tasks.clear()


class TestConstants:

    def test_concurrent_limit(self):
        assert MAX_CONCURRENT_PER_USER == 3

    def test_timeout(self):
        assert SUB_AGENT_TIMEOUT == 300

    def test_max_result_chars(self):
        assert MAX_RESULT_CHARS == 6000
