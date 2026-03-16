"""Tests for the doctor diagnostics tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from qanot.registry import ToolRegistry
from qanot.config import Config
from qanot.context import ContextTracker
from qanot.tools.doctor import (
    _check_config,
    _check_context,
    _check_disk,
    _check_memory,
    _check_provider,
    _check_rag,
    _check_sessions,
    _dir_size,
    _file_size_str,
    register_doctor_tool,
)


class TestFileSizeStr:
    def test_bytes(self):
        assert _file_size_str(500) == "500B"

    def test_kilobytes(self):
        assert _file_size_str(2048) == "2.0KB"

    def test_megabytes(self):
        assert _file_size_str(5 * 1024 * 1024) == "5.0MB"

    def test_gigabytes(self):
        assert _file_size_str(2 * 1024 * 1024 * 1024) == "2.0GB"


class TestDirSize:
    def test_nonexistent_dir(self, tmp_path: Path):
        assert _dir_size(tmp_path / "nonexistent") == 0

    def test_empty_dir(self, tmp_path: Path):
        assert _dir_size(tmp_path) == 0

    def test_dir_with_files(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world!")
        size = _dir_size(tmp_path)
        assert size == 5 + 6  # "hello" + "world!"


class TestCheckConfig:
    def test_healthy_config(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        for fname in ("SOUL.md", "TOOLS.md", "IDENTITY.md"):
            (ws / fname).write_text("test")

        config = Config(
            bot_token="123:ABC",
            api_key="sk-test",
            workspace_dir=str(ws),
            sessions_dir=str(sessions),
        )
        result = _check_config(config)
        assert result["status"] == "ok"

    def test_missing_bot_token(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = Config(
            bot_token="",
            api_key="sk-test",
            workspace_dir=str(ws),
        )
        result = _check_config(config)
        assert result["status"] == "error"
        assert "bot_token: MISSING" in result["details"]

    def test_missing_api_key(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = Config(
            bot_token="123:ABC",
            api_key="",
            workspace_dir=str(ws),
        )
        result = _check_config(config)
        assert result["status"] == "error"

    def test_missing_workspace_files(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        config = Config(
            bot_token="123:ABC",
            api_key="sk-test",
            workspace_dir=str(ws),
            sessions_dir=str(sessions),
        )
        result = _check_config(config)
        assert result["status"] == "warning"
        assert "SOUL.md: MISSING" in result["details"]


class TestCheckMemory:
    def test_healthy_memory(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("# Memory\nTest content")
        (ws / "SESSION-STATE.md").write_text("small state")
        memory_dir = ws / "memory"
        memory_dir.mkdir()

        config = Config(workspace_dir=str(ws))
        result, warnings = _check_memory(config)
        assert result["status"] == "ok"
        assert len(warnings) == 0

    def test_large_session_state(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("# Memory")
        # Create a >100KB SESSION-STATE.md
        (ws / "SESSION-STATE.md").write_text("x" * (150 * 1024))

        config = Config(workspace_dir=str(ws))
        result, warnings = _check_memory(config)
        assert result["status"] == "warning"
        assert len(warnings) == 1
        assert "consider compaction" in warnings[0]

    def test_missing_memory_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = Config(workspace_dir=str(ws))
        result, warnings = _check_memory(config)
        assert result["status"] == "warning"


class TestCheckContext:
    def test_healthy_context(self):
        ctx = ContextTracker(max_tokens=200000, workspace_dir="/tmp")
        result = _check_context(ctx)
        assert result["status"] == "ok"

    def test_high_context_usage(self):
        ctx = ContextTracker(max_tokens=200000, workspace_dir="/tmp")
        ctx.last_prompt_tokens = 170000  # 85%
        result = _check_context(ctx)
        assert result["status"] == "warning"


class TestCheckProvider:
    def test_single_provider(self):
        config = Config(provider="anthropic", model="claude-sonnet-4-6")
        result = _check_provider(config)
        assert result["status"] == "ok"
        assert "single" in result["details"]

    def test_multi_provider(self):
        from qanot.config import ProviderConfig
        config = Config(
            providers=[
                ProviderConfig(name="main", provider="anthropic", model="claude-sonnet-4-6", api_key="sk-1"),
                ProviderConfig(name="fallback", provider="openai", model="gpt-4o", api_key="sk-2"),
            ]
        )
        result = _check_provider(config)
        assert result["status"] == "ok"
        assert "2 in failover chain" in result["details"]


class TestCheckRag:
    def test_rag_disabled(self):
        config = Config(rag_enabled=False)
        result = _check_rag(config)
        assert result["status"] == "ok"
        assert "disabled" in result["details"]

    def test_rag_no_db(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = Config(rag_enabled=True, workspace_dir=str(ws))
        result = _check_rag(config)
        assert result["status"] == "warning"
        assert "not found" in result["details"]


class TestCheckSessions:
    def test_no_sessions_dir(self, tmp_path: Path):
        config = Config(sessions_dir=str(tmp_path / "nonexistent"))
        result = _check_sessions(config)
        assert result["status"] == "warning"

    def test_with_session_files(self, tmp_path: Path):
        sd = tmp_path / "sessions"
        sd.mkdir()
        (sd / "session1.jsonl").write_text('{"test": true}\n')
        config = Config(sessions_dir=str(sd))
        result = _check_sessions(config)
        assert result["status"] == "ok"
        assert "session files (7d): 1" in result["details"]


class TestCheckDisk:
    def test_disk_check(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "test.txt").write_text("hello")
        config = Config(workspace_dir=str(ws))
        result, warnings = _check_disk(config)
        assert result["status"] == "ok"
        assert "workspace size" in result["details"]


class TestRegisterDoctorTool:
    def test_registration(self, tmp_path: Path):
        registry = ToolRegistry()
        config = Config(workspace_dir=str(tmp_path))
        ctx = ContextTracker(workspace_dir=str(tmp_path))

        register_doctor_tool(registry, config, ctx)

        assert "doctor" in registry.tool_names

    def test_handler_returns_json(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        sessions = tmp_path / "sessions"
        sessions.mkdir()

        registry = ToolRegistry()
        config = Config(
            bot_token="123:ABC",
            api_key="sk-test",
            workspace_dir=str(ws),
            sessions_dir=str(sessions),
            rag_enabled=False,
        )
        ctx = ContextTracker(workspace_dir=str(ws))
        register_doctor_tool(registry, config, ctx)

        result = asyncio.run(registry.execute("doctor", {}))
        data = json.loads(result)

        assert "status" in data
        assert "checks" in data
        assert "warnings" in data
        assert "timestamp" in data
        assert data["status"] in ("healthy", "warning", "error")

        # Verify all 7 check categories present
        expected_checks = {"config", "memory", "context", "provider", "rag", "sessions", "disk"}
        assert set(data["checks"].keys()) == expected_checks
