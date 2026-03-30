"""Tests for qanot.orchestrator — unified multi-agent system.

Covers: types, registry, tool_policy, context_scope, announce, manager, tools, monitor.
Mirrors test coverage from test_delegate.py + test_subagent.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from qanot.config import Config, AgentDefinition
from qanot.registry import ToolRegistry
from qanot.orchestrator.types import (
    SubagentRun,
    SpawnParams,
    AnnouncePayload,
    make_run_id,
    _fmt_tokens,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
    TERMINAL_STATUSES,
    MODE_SYNC,
    MODE_ASYNC,
    MODE_CONVERSATION,
    ROLE_MAIN,
    ROLE_ORCHESTRATOR,
    ROLE_LEAF,
)
from qanot.orchestrator.registry import SubagentRegistry
from qanot.orchestrator.tool_policy import (
    resolve_role,
    build_child_registry,
    MAX_SPAWN_DEPTH,
    ALWAYS_DENIED,
    SPAWN_TOOLS,
)
from qanot.orchestrator.context_scope import (
    build_scoped_prompt,
    fence_result,
    get_board_summary,
    load_agent_identity,
)
from qanot.orchestrator.announce import (
    build_announce_payload,
    format_sync_result,
    format_telegram_announce,
    post_to_board,
)
from qanot.orchestrator.manager import (
    SubagentManager,
    BUILTIN_ROLES,
    MAX_CONCURRENT_PER_USER,
    _task_similarity,
)


# ── Helpers ─────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    cfg = MagicMock(spec=Config)
    cfg.agents = overrides.get("agents", [])
    cfg.provider = "anthropic"
    cfg.model = "claude-sonnet-4-6"
    cfg.api_key = "test-key"
    cfg.bot_token = "main-bot-token"
    cfg.bot_name = "TestBot"
    cfg.workspace_dir = overrides.get("workspace_dir", "/tmp/test_workspace")
    cfg.sessions_dir = overrides.get("sessions_dir", "/tmp/test_sessions")
    cfg.max_context_tokens = 200000
    cfg.history_limit = 50
    cfg.monitor_group_id = overrides.get("monitor_group_id", 0)
    return cfg


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for name in ["read_file", "write_file", "run_command", "web_search",
                  "spawn_agent", "cancel_agent", "list_agents",
                  "create_agent", "update_agent", "delete_agent", "restart_self"]:
        reg.register(name, f"Test {name}", {"type": "object", "properties": {}},
                     AsyncMock(return_value="ok"), category="core")
    return reg


def _make_run(**overrides) -> SubagentRun:
    defaults = dict(
        run_id=make_run_id(),
        parent_user_id="user1",
        parent_chat_id=12345,
        task="Test task",
        agent_id="researcher",
        agent_name="Tadqiqotchi",
        role=ROLE_LEAF,
        depth=1,
        status=STATUS_PENDING,
        mode=MODE_SYNC,
    )
    defaults.update(overrides)
    return SubagentRun(**defaults)


# ═══════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════


class TestTypes:
    def test_make_run_id(self):
        rid = make_run_id()
        assert len(rid) == 12
        assert rid.isalnum()
        assert make_run_id() != make_run_id()

    def test_fmt_tokens(self):
        assert _fmt_tokens(0) == "0"
        assert _fmt_tokens(500) == "500"
        assert _fmt_tokens(1500) == "1.5k"
        assert _fmt_tokens(1_500_000) == "1.5m"

    def test_subagent_run_is_terminal(self):
        run = _make_run(status=STATUS_PENDING)
        assert not run.is_terminal
        for status in TERMINAL_STATUSES:
            run.status = status
            assert run.is_terminal

    def test_subagent_run_elapsed(self):
        run = _make_run(started_at=time.time() - 10, ended_at=time.time())
        assert 9.5 < run.elapsed_seconds < 11.0

    def test_subagent_run_elapsed_not_started(self):
        run = _make_run()
        assert run.elapsed_seconds == 0.0

    def test_subagent_run_token_total(self):
        run = _make_run(token_input=500, token_output=200)
        assert run.token_total == 700

    def test_subagent_run_serialization(self):
        run = _make_run()
        d = run.to_dict()
        assert isinstance(d, dict)
        assert d["agent_id"] == "researcher"
        restored = SubagentRun.from_dict(d)
        assert restored.agent_id == run.agent_id
        assert restored.run_id == run.run_id

    def test_announce_payload_stats(self):
        payload = AnnouncePayload(
            run_id="test", agent_id="researcher", agent_name="Test",
            status="completed", result="ok", elapsed_seconds=5.2,
            token_input=1000, token_output=500, cost=0.01,
        )
        stats = payload.format_stats_line()
        assert "5.2s" in stats
        assert "1.5k" in stats
        assert "$0.0100" in stats

    def test_spawn_params_defaults(self):
        params = SpawnParams(task="test")
        assert params.mode == MODE_SYNC
        assert params.timeout == 120
        assert params.max_turns == 5


# ═══════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════


class TestRegistry:
    def test_register_and_get(self):
        reg = SubagentRegistry()
        run = _make_run()
        reg.register(run)
        assert reg.get(run.run_id) is run

    def test_update(self):
        reg = SubagentRegistry()
        run = _make_run()
        reg.register(run)
        reg.update(run.run_id, status=STATUS_COMPLETED)
        assert reg.get(run.run_id).status == STATUS_COMPLETED

    def test_update_nonexistent(self):
        reg = SubagentRegistry()
        assert reg.update("nonexistent", status="x") is None

    def test_active_for_user(self):
        reg = SubagentRegistry()
        r1 = _make_run(parent_user_id="u1")
        r2 = _make_run(parent_user_id="u1", status=STATUS_COMPLETED)
        r3 = _make_run(parent_user_id="u2")
        reg.register(r1)
        reg.register(r2)
        reg.register(r3)
        assert reg.count_active_for_user("u1") == 1
        assert reg.count_active_for_user("u2") == 1

    def test_recent_for_user(self):
        reg = SubagentRegistry()
        for i in range(5):
            r = _make_run(parent_user_id="u1")
            r.created_at = time.time() + i
            reg.register(r)
        recent = reg.get_recent_for_user("u1", limit=3)
        assert len(recent) == 3
        # Should be sorted by created_at descending
        assert recent[0].created_at >= recent[1].created_at

    def test_cleanup_stale(self):
        reg = SubagentRegistry()
        old = _make_run(status=STATUS_COMPLETED)
        old.created_at = time.time() - 7200  # 2 hours ago
        reg.register(old)
        new = _make_run()
        reg.register(new)
        removed = reg.cleanup_stale(max_age=3600)
        assert removed == 1
        assert reg.get(old.run_id) is None
        assert reg.get(new.run_id) is not None

    def test_persist_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "reg.json")
            reg = SubagentRegistry(path)
            run = _make_run(status=STATUS_COMPLETED)
            reg.register(run)

            reg2 = SubagentRegistry(path)
            reg2.restore()
            r = reg2.get(run.run_id)
            assert r is not None
            assert r.status == STATUS_COMPLETED

    def test_restore_orphaned_runs(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "reg.json")
            reg = SubagentRegistry(path)
            run = _make_run(status=STATUS_RUNNING)
            reg.register(run)

            reg2 = SubagentRegistry(path)
            reg2.restore()
            r = reg2.get(run.run_id)
            assert r.status == STATUS_FAILED
            assert "Orphaned" in r.error


# ═══════════════════════════════════════════════════════
# Tool Policy
# ═══════════════════════════════════════════════════════


class TestToolPolicy:
    def test_resolve_role(self):
        assert resolve_role(0) == ROLE_MAIN
        assert resolve_role(1) == ROLE_ORCHESTRATOR
        assert resolve_role(2) == ROLE_ORCHESTRATOR
        assert resolve_role(3) == ROLE_LEAF
        assert resolve_role(10) == ROLE_LEAF

    def test_max_spawn_depth(self):
        assert MAX_SPAWN_DEPTH == 3

    def test_always_denied(self):
        assert "create_agent" in ALWAYS_DENIED
        assert "delete_agent" in ALWAYS_DENIED
        assert "restart_self" in ALWAYS_DENIED

    def test_spawn_tools(self):
        assert "spawn_agent" in SPAWN_TOOLS
        assert "cancel_agent" in SPAWN_TOOLS

    def test_build_child_registry_removes_admin(self):
        parent = _make_registry()
        child = build_child_registry(parent, depth=1)
        assert "create_agent" not in child.tool_names
        assert "delete_agent" not in child.tool_names
        assert "read_file" in child.tool_names

    def test_leaf_removes_spawn_tools(self):
        parent = _make_registry()
        child = build_child_registry(parent, depth=MAX_SPAWN_DEPTH)
        assert "spawn_agent" not in child.tool_names
        assert "cancel_agent" not in child.tool_names
        assert "read_file" in child.tool_names

    def test_orchestrator_keeps_spawn_tools(self):
        parent = _make_registry()
        child = build_child_registry(parent, depth=1)
        assert "spawn_agent" in child.tool_names
        assert "cancel_agent" in child.tool_names

    def test_tools_allow_whitelist(self):
        parent = _make_registry()
        child = build_child_registry(parent, depth=1, tools_allow=["read_file", "web_search"])
        assert "read_file" in child.tool_names
        assert "web_search" in child.tool_names
        assert "write_file" not in child.tool_names

    def test_tools_deny_blacklist(self):
        parent = _make_registry()
        child = build_child_registry(parent, depth=1, tools_deny=["run_command"])
        assert "run_command" not in child.tool_names
        assert "read_file" in child.tool_names


# ═══════════════════════════════════════════════════════
# Context Scope
# ═══════════════════════════════════════════════════════


class TestContextScope:
    def test_build_scoped_prompt_basic(self):
        prompt = build_scoped_prompt(
            task="Research Python 3.13",
            agent_identity="You are a researcher.",
        )
        assert "Research Python 3.13" in prompt
        assert "You are a researcher." in prompt
        assert "Stay focused" in prompt

    def test_build_scoped_prompt_with_context(self):
        prompt = build_scoped_prompt(
            task="Analyze data",
            agent_identity="Analyst",
            parent_context="Here is some context",
        )
        assert "Here is some context" in prompt

    def test_build_scoped_prompt_can_spawn(self):
        prompt = build_scoped_prompt(
            task="Orchestrate",
            agent_identity="Orchestrator",
            can_spawn=True,
        )
        assert "push-based" in prompt

    def test_build_scoped_prompt_no_spawn(self):
        prompt = build_scoped_prompt(
            task="Simple task",
            agent_identity="Worker",
            can_spawn=False,
        )
        assert "push-based" not in prompt

    def test_fence_result(self):
        fenced = fence_result("hello world")
        assert "<<<BEGIN_AGENT_RESULT>>>" in fenced
        assert "hello world" in fenced
        assert "<<<END_AGENT_RESULT>>>" in fenced

    def test_get_board_summary_empty(self):
        assert get_board_summary([]) == ""

    def test_get_board_summary(self):
        board = [
            {"agent_id": "researcher", "agent_name": "R", "result": "Found stuff", "task": "Research"},
            {"agent_id": "coder", "agent_name": "C", "result": "Wrote code", "task": "Code"},
        ]
        summary = get_board_summary(board)
        assert "R" in summary
        assert "C" in summary

    def test_get_board_summary_excludes_agent(self):
        board = [
            {"agent_id": "researcher", "agent_name": "Researcher", "result": "Found stuff"},
            {"agent_id": "coder", "agent_name": "Coder", "result": "Wrote code"},
        ]
        summary = get_board_summary(board, exclude_agent="researcher")
        assert "Researcher" not in summary
        assert "Coder" in summary

    def test_load_agent_identity(self):
        with tempfile.TemporaryDirectory() as td:
            soul_dir = Path(td) / "agents" / "myagent"
            soul_dir.mkdir(parents=True)
            (soul_dir / "SOUL.md").write_text("I am a custom agent.")
            identity = load_agent_identity(td, "myagent")
            assert identity == "I am a custom agent."

    def test_load_agent_identity_missing(self):
        with tempfile.TemporaryDirectory() as td:
            identity = load_agent_identity(td, "nonexistent")
            assert identity == ""


# ═══════════════════════════════════════════════════════
# Announce
# ═══════════════════════════════════════════════════════


class TestAnnounce:
    def test_build_announce_payload(self):
        run = _make_run(
            status=STATUS_COMPLETED,
            result_text="Found 5 articles",
            started_at=time.time() - 10,
            ended_at=time.time(),
            token_input=1000,
            token_output=500,
        )
        payload = build_announce_payload(run)
        assert payload.status == "completed"
        assert "5 articles" in payload.result
        assert payload.token_input == 1000

    def test_build_announce_payload_truncates(self):
        run = _make_run(status=STATUS_COMPLETED, result_text="x" * 10000)
        payload = build_announce_payload(run)
        assert len(payload.result) < 9000

    def test_format_sync_result(self):
        payload = AnnouncePayload(
            run_id="test", agent_id="researcher", agent_name="R",
            status="completed", result="Found stuff",
            elapsed_seconds=5.0, token_input=100, token_output=50,
        )
        result = format_sync_result(payload)
        assert result["status"] == "completed"
        assert "AGENT_RESULT" in result["result"]
        assert result["tokens"]["total"] == 150

    def test_format_telegram_announce(self):
        payload = AnnouncePayload(
            run_id="test", agent_id="researcher", agent_name="Tadqiqotchi",
            status="completed", result="Found 5 articles",
            elapsed_seconds=5.0,
        )
        msg = format_telegram_announce(payload)
        assert "Tadqiqotchi" in msg
        assert "5 articles" in msg

    def test_post_to_board(self):
        board: dict[str, list] = {}
        payload = AnnouncePayload(
            run_id="r1", agent_id="researcher", agent_name="R",
            status="completed", result="OK", elapsed_seconds=1.0,
        )
        post_to_board(board, "u1", payload, task="Research Python")
        assert len(board["u1"]) == 1
        assert board["u1"][0]["task"] == "Research Python"
        assert board["u1"][0]["agent_id"] == "researcher"

    def test_post_to_board_eviction(self):
        board: dict[str, list] = {}
        payload = AnnouncePayload(
            run_id="r1", agent_id="researcher", agent_name="R",
            status="completed", result="OK", elapsed_seconds=1.0,
        )
        for i in range(25):
            post_to_board(board, "u1", payload, task=f"Task {i}")
        assert len(board["u1"]) == 20  # MAX_BOARD_ENTRIES


# ═══════════════════════════════════════════════════════
# Manager
# ═══════════════════════════════════════════════════════


class TestManager:
    def test_builtin_roles(self):
        assert len(BUILTIN_ROLES) == 5
        for role_id, info in BUILTIN_ROLES.items():
            assert "name" in info
            assert "prompt" in info
            assert len(info["prompt"]) > 20

    def test_expected_roles(self):
        assert set(BUILTIN_ROLES.keys()) == {"researcher", "analyst", "coder", "reviewer", "writer"}

    def test_get_available_agents_builtin(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        agents = mgr.get_available_agents()
        assert "researcher" in agents
        assert agents["researcher"]["source"] == "builtin"

    def test_get_available_agents_config(self):
        agent_def = AgentDefinition(id="custom", name="Custom Agent", prompt="Do stuff")
        config = _make_config(agents=[agent_def])
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        agents = mgr.get_available_agents()
        assert "custom" in agents
        assert agents["custom"]["source"] == "config"
        assert agents["custom"]["name"] == "Custom Agent"

    def test_get_available_agents_config_overrides_builtin(self):
        agent_def = AgentDefinition(id="researcher", name="My Researcher", prompt="Custom")
        config = _make_config(agents=[agent_def])
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        agents = mgr.get_available_agents()
        assert agents["researcher"]["name"] == "My Researcher"
        assert agents["researcher"]["source"] == "config"

    def test_task_similarity(self):
        assert _task_similarity("research python", "research python") == 1.0
        assert _task_similarity("research python features", "research python features") == 1.0
        assert _task_similarity("hello", "goodbye") == 0.0
        assert _task_similarity("research python", "") == 0.0
        assert 0.5 < _task_similarity("research python features", "research python bugs") < 1.0

    def test_check_access_main_always_allowed(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        assert mgr._check_access("main", "researcher") is True

    def test_check_access_restricted(self):
        agent_def = AgentDefinition(id="limited", delegate_allow=["researcher"])
        config = _make_config(agents=[agent_def])
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        assert mgr._check_access("limited", "researcher") is True
        assert mgr._check_access("limited", "coder") is False

    def test_check_access_empty_allows_all(self):
        agent_def = AgentDefinition(id="open")
        config = _make_config(agents=[agent_def])
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        assert mgr._check_access("open", "anyone") is True

    def test_board_operations(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        assert mgr.get_board("u1") == []
        mgr._project_board["u1"] = [{"agent_id": "r", "result": "ok"}]
        assert len(mgr.get_board("u1")) == 1
        mgr.clear_board("u1")
        assert mgr.get_board("u1") == []

    def test_is_conversation_done(self):
        assert SubagentManager._is_conversation_done("DONE") is True
        assert SubagentManager._is_conversation_done("RESULT: here is the answer") is True
        assert SubagentManager._is_conversation_done("Done with everything") is True
        assert SubagentManager._is_conversation_done("Not done yet") is False
        assert SubagentManager._is_conversation_done("Working on it...") is False

    def test_collect_stats(self):
        run = _make_run()

        class MockCT:
            def get_user_stats(self, uid):
                return {"input_tokens": 500, "output_tokens": 200, "total_cost": 0.01}

        class MockAgent:
            cost_tracker = MockCT()

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._collect_stats(run, MockAgent())
        assert run.token_input == 500
        assert run.token_output == 200
        assert run.cost == 0.01


# ═══════════════════════════════════════════════════════
# Tools Registration
# ═══════════════════════════════════════════════════════


class TestToolsRegistration:
    def test_all_tools_registered_depth_0(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=0)
        names = reg.tool_names
        assert "spawn_agent" in names
        assert "cancel_agent" in names
        assert "list_agents" in names
        assert "view_board" in names
        assert "clear_board" in names
        assert "agent_history" in names
        assert "set_monitor_group" in names

    def test_spawn_tools_hidden_at_leaf_depth(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=MAX_SPAWN_DEPTH)
        names = reg.tool_names
        assert "spawn_agent" not in names
        assert "cancel_agent" not in names
        # Board tools still available
        assert "list_agents" in names
        assert "view_board" in names

    def test_no_monitor_group_at_depth_1(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=1)
        assert "set_monitor_group" not in reg.tool_names

    def test_config_agents_in_spawn_enum(self):
        agent_def = AgentDefinition(id="custom_agent", name="Custom")
        config = _make_config(agents=[agent_def])
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=0)
        tool_def = reg._tools["spawn_agent"]
        agent_enum = tool_def["input_schema"]["properties"]["agent_id"].get("enum", [])
        assert "custom_agent" in agent_enum
        assert "researcher" in agent_enum


# ═══════════════════════════════════════════════════════
# Tool Handlers (async)
# ═══════════════════════════════════════════════════════


class TestToolHandlers:
    @pytest.mark.asyncio
    async def test_list_agents(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=0)

        result = json.loads(await reg.execute("list_agents", {}))
        assert result["total_agents"] >= 5  # at least builtins
        assert result["can_spawn"] is True

    @pytest.mark.asyncio
    async def test_view_board_empty(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=0)

        result = json.loads(await reg.execute("view_board", {}))
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_clear_board(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        mgr._project_board["default"] = [{"agent_id": "r", "result": "ok"}]
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=0)

        result = json.loads(await reg.execute("clear_board", {}))
        assert result["cleared"] == 1

    @pytest.mark.asyncio
    async def test_agent_history_empty(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        reg = ToolRegistry()
        from qanot.orchestrator.tools import register_orchestrator_tools
        register_orchestrator_tools(reg, mgr, config, depth=0)

        result = json.loads(await reg.execute("agent_history", {}))
        assert result["total"] == 0


# ═══════════════════════════════════════════════════════
# Monitor
# ═══════════════════════════════════════════════════════


class TestMonitor:
    def test_get_agent_name_main(self):
        from qanot.orchestrator.monitor import _get_agent_name
        config = _make_config()
        assert _get_agent_name(config, "main") == "TestBot"

    def test_get_agent_name_agent(self):
        from qanot.orchestrator.monitor import _get_agent_name
        agent_def = AgentDefinition(id="myagent", name="My Agent")
        config = _make_config(agents=[agent_def])
        assert _get_agent_name(config, "myagent") == "My Agent"

    def test_get_agent_bot_token_fallback(self):
        from qanot.orchestrator.monitor import _get_agent_bot_token
        config = _make_config()
        assert _get_agent_bot_token(config, "researcher") == "main-bot-token"

    def test_get_agent_bot_token_custom(self):
        from qanot.orchestrator.monitor import _get_agent_bot_token
        agent_def = AgentDefinition(id="myagent", bot_token="custom-token")
        config = _make_config(agents=[agent_def])
        assert _get_agent_bot_token(config, "myagent") == "custom-token"

    @pytest.mark.asyncio
    async def test_mirror_noop_without_group(self):
        from qanot.orchestrator.monitor import mirror_to_group
        config = _make_config(monitor_group_id=0)
        # Should not raise
        await mirror_to_group(config, "main", "researcher", "test")

    @pytest.mark.asyncio
    async def test_set_monitor_group_handler(self):
        from qanot.orchestrator.monitor import handle_set_monitor_group
        config = _make_config()
        result = json.loads(await handle_set_monitor_group(config, {"group_id": -123456}))
        assert result["status"] == "configured"
        assert config.monitor_group_id == -123456

    @pytest.mark.asyncio
    async def test_set_monitor_group_invalid(self):
        from qanot.orchestrator.monitor import handle_set_monitor_group
        config = _make_config()
        result = json.loads(await handle_set_monitor_group(config, {"group_id": 0}))
        assert "error" in result

    def test_cleanup_bot_cache(self):
        from qanot.orchestrator.monitor import _group_bot_cache, cleanup_bot_cache
        _group_bot_cache["test"] = "dummy"
        cleanup_bot_cache()
        assert len(_group_bot_cache) == 0


# ═══════════════════════════════════════════════════════
# Loop Detection
# ═══════════════════════════════════════════════════════


class TestLoopDetection:
    def test_no_loop_on_empty(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        result = mgr._check_loop("u1", "researcher", "Do something")
        assert result is None

    def test_detects_too_many_active(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        # Register 3 active runs for same agent
        for _ in range(3):
            run = _make_run(parent_user_id="u1", agent_id="researcher", status=STATUS_RUNNING)
            mgr.registry.register(run)
        result = mgr._check_loop("u1", "researcher", "Do something")
        assert result is not None
        assert "loop detected" in result

    def test_detects_similar_completed_task(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        run = _make_run(
            parent_user_id="u1", agent_id="researcher",
            status=STATUS_COMPLETED, task="Research Python features",
        )
        mgr.registry.register(run)
        result = mgr._check_loop("u1", "researcher", "Research Python features")
        assert result is not None
        assert "similar" in result

    def test_no_loop_different_task(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        run = _make_run(
            parent_user_id="u1", agent_id="researcher",
            status=STATUS_COMPLETED, task="Research JavaScript",
        )
        mgr.registry.register(run)
        result = mgr._check_loop("u1", "researcher", "Analyze database performance")
        assert result is None

    def test_no_loop_different_agent(self):
        config = _make_config()
        mgr = SubagentManager(config, MagicMock(), _make_registry())
        run = _make_run(
            parent_user_id="u1", agent_id="coder",
            status=STATUS_COMPLETED, task="Research Python features",
        )
        mgr.registry.register(run)
        result = mgr._check_loop("u1", "researcher", "Research Python features")
        assert result is None
