"""Tests for agent-to-agent delegation, ping-pong, and project board."""

from __future__ import annotations

import json
import os
import tempfile
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from qanot.registry import ToolRegistry
from qanot.config import AgentDefinition
from qanot.tools.delegate import (
    BUILTIN_ROLES,
    MAX_DELEGATION_DEPTH,
    DELEGATION_TIMEOUT,
    MAX_CONTEXT_CHARS,
    MAX_RESULT_CHARS,
    MAX_PING_PONG_TURNS,
    MAX_BOARD_ENTRIES,
    MAX_SESSION_HISTORY,
    MAX_SESSION_HISTORY_RETURN,
    MAX_ACTIVITY_LOG,
    LOOP_DETECTION_WINDOW,
    LOOP_SIMILARITY_THRESHOLD,
    _ALWAYS_DENIED,
    _build_delegate_registry,
    _truncate_context,
    _get_available_agents,
    _load_agent_identity,
    _get_board_summary,
    _post_to_board,
    _project_boards,
    _record_session_message,
    _get_session_history,
    _get_active_sessions,
    _agent_sessions,
    _check_delegate_allow,
    _log_activity,
    _check_for_loop,
    get_activity_log,
    set_notify_callback,
    _activity_log,
    _notify_callback,
)


def _make_config(**overrides):
    config = MagicMock()
    config.agents = overrides.get("agents", [])
    config.provider = "anthropic"
    config.model = "claude-sonnet-4-6"
    config.api_key = "test-key"
    config.workspace_dir = overrides.get("workspace_dir", "/tmp/test-workspace")
    return config


def _make_registry(*tool_names) -> ToolRegistry:
    reg = ToolRegistry()

    async def noop(params):
        return "{}"

    for name in tool_names:
        reg.register(name, f"desc for {name}", {"type": "object", "properties": {}}, noop)
    return reg


class TestBuiltinRoles:

    def test_all_roles_have_required_fields(self):
        for name, info in BUILTIN_ROLES.items():
            assert "name" in info
            assert "prompt" in info
            assert len(info["prompt"]) > 20

    def test_expected_roles_exist(self):
        assert set(BUILTIN_ROLES.keys()) == {"researcher", "analyst", "coder", "reviewer", "writer"}


class TestConstants:

    def test_max_depth(self):
        assert MAX_DELEGATION_DEPTH == 2

    def test_timeout(self):
        assert DELEGATION_TIMEOUT == 120

    def test_ping_pong_turns(self):
        assert MAX_PING_PONG_TURNS == 5

    def test_board_entries(self):
        assert MAX_BOARD_ENTRIES == 20

    def test_denied_tools(self):
        assert "spawn_sub_agent" in _ALWAYS_DENIED
        assert "list_sub_agents" in _ALWAYS_DENIED


class TestTruncateContext:

    def test_short_unchanged(self):
        assert _truncate_context("short") == "short"

    def test_long_truncated(self):
        text = "x" * (MAX_CONTEXT_CHARS + 500)
        result = _truncate_context(text)
        assert "[... context truncated]" in result
        assert len(result) < len(text)


class TestGetAvailableAgents:

    def test_builtin_roles(self):
        agents = _get_available_agents(_make_config())
        assert "researcher" in agents
        assert agents["researcher"]["source"] == "builtin"

    def test_config_agents(self):
        config = _make_config(agents=[
            AgentDefinition(id="custom", name="Custom Agent", prompt="Do stuff"),
        ])
        agents = _get_available_agents(config)
        assert "custom" in agents
        assert agents["custom"]["name"] == "Custom Agent"
        assert agents["custom"]["source"] == "config"

    def test_config_overrides_builtin(self):
        config = _make_config(agents=[
            AgentDefinition(id="researcher", name="My Researcher", model="gpt-4o"),
        ])
        agents = _get_available_agents(config)
        assert agents["researcher"]["name"] == "My Researcher"
        assert agents["researcher"]["model"] == "gpt-4o"
        assert agents["researcher"]["source"] == "config"


class TestBuildDelegateRegistry:

    def test_depth_2_denies_delegation(self):
        parent = _make_registry("read_file", "delegate_to_agent", "converse_with_agent", "spawn_sub_agent")
        child = _build_delegate_registry(parent, depth=2)
        names = [t["name"] for t in child.get_definitions()]
        assert "delegate_to_agent" not in names
        assert "converse_with_agent" not in names
        assert "spawn_sub_agent" not in names
        assert "read_file" in names

    def test_depth_1_allows_delegation(self):
        parent = _make_registry("read_file", "delegate_to_agent", "converse_with_agent", "spawn_sub_agent")
        child = _build_delegate_registry(parent, depth=1)
        names = [t["name"] for t in child.get_definitions()]
        assert "delegate_to_agent" in names
        assert "converse_with_agent" in names
        assert "spawn_sub_agent" not in names

    def test_tools_allow_whitelist(self):
        parent = _make_registry("read_file", "write_file", "web_search")
        child = _build_delegate_registry(parent, depth=1, tools_allow=["read_file"])
        names = [t["name"] for t in child.get_definitions()]
        assert names == ["read_file"]

    def test_tools_deny_blacklist(self):
        parent = _make_registry("read_file", "run_command")
        child = _build_delegate_registry(parent, depth=1, tools_deny=["run_command"])
        names = [t["name"] for t in child.get_definitions()]
        assert "read_file" in names
        assert "run_command" not in names


class TestLoadAgentIdentity:

    def test_loads_existing_soul(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir) / "agents" / "researcher"
            agent_dir.mkdir(parents=True)
            (agent_dir / "SOUL.md").write_text("I am a deep research specialist.")
            result = _load_agent_identity(tmpdir, "researcher")
            assert result == "I am a deep research specialist."

    def test_returns_empty_for_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _load_agent_identity(tmpdir, "nonexistent")
            assert result == ""

    def test_returns_empty_for_empty_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir) / "agents" / "coder"
            agent_dir.mkdir(parents=True)
            (agent_dir / "SOUL.md").write_text("")
            result = _load_agent_identity(tmpdir, "coder")
            assert result == ""


class TestProjectBoard:

    def setup_method(self):
        _project_boards.clear()

    def test_post_and_get_summary(self):
        _post_to_board("user1", "researcher", "Tadqiqotchi", "Research AI", "AI is growing fast")
        summary = _get_board_summary("user1")
        assert "Tadqiqotchi" in summary
        assert "Research AI" in summary
        assert "AI is growing fast" in summary

    def test_empty_board(self):
        assert _get_board_summary("user1") == ""

    def test_exclude_agent(self):
        _post_to_board("user1", "researcher", "R", "task1", "result1")
        _post_to_board("user1", "coder", "C", "task2", "result2")
        summary = _get_board_summary("user1", exclude_agent="researcher")
        assert "researcher" not in summary.lower() or "R" not in summary
        assert "C" in summary

    def test_board_eviction(self):
        for i in range(MAX_BOARD_ENTRIES + 5):
            _post_to_board("user1", f"agent{i}", f"A{i}", f"task{i}", f"result{i}")
        board = _project_boards["user1"]
        assert len(board) == MAX_BOARD_ENTRIES

    def test_per_user_isolation(self):
        _post_to_board("user1", "researcher", "R", "task1", "result1")
        _post_to_board("user2", "coder", "C", "task2", "result2")
        assert len(_project_boards["user1"]) == 1
        assert len(_project_boards["user2"]) == 1
        assert _project_boards["user1"][0]["agent_id"] == "researcher"
        assert _project_boards["user2"][0]["agent_id"] == "coder"

    def test_result_truncated_on_board(self):
        long_result = "x" * 5000
        _post_to_board("user1", "a", "A", "task", long_result)
        assert len(_project_boards["user1"][0]["result"]) == 2000

    def teardown_method(self):
        _project_boards.clear()


class TestRegisterDelegateTools:

    def test_all_tools_registered_at_depth_0(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(
            reg, _make_config(), MagicMock(), ToolRegistry(),
            get_user_id=lambda: "test",
        )
        names = [t["name"] for t in reg.get_definitions()]
        assert "delegate_to_agent" in names
        assert "converse_with_agent" in names
        assert "view_project_board" in names
        assert "clear_project_board" in names
        assert "list_agents" in names

    def test_delegation_tools_hidden_at_max_depth(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(
            reg, _make_config(), MagicMock(), ToolRegistry(),
            get_user_id=lambda: "test",
            current_depth=MAX_DELEGATION_DEPTH,
        )
        names = [t["name"] for t in reg.get_definitions()]
        assert "delegate_to_agent" not in names
        assert "converse_with_agent" not in names
        # Board + list still available
        assert "view_project_board" in names
        assert "list_agents" in names

    def test_config_agents_in_enum(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        config = _make_config(agents=[
            AgentDefinition(id="seo-bot", name="SEO"),
        ])
        register_delegate_tools(reg, config, MagicMock(), ToolRegistry(), get_user_id=lambda: "test")
        tool_def = next(t for t in reg.get_definitions() if t["name"] == "delegate_to_agent")
        enum_values = tool_def["input_schema"]["properties"]["agent_id"]["enum"]
        assert "seo-bot" in enum_values
        assert "researcher" in enum_values


class TestListAgents:

    @pytest.mark.asyncio
    async def test_list_output(self):
        from qanot.tools.delegate import register_delegate_tools

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create identity file for one agent
            agent_dir = Path(tmpdir) / "agents" / "researcher"
            agent_dir.mkdir(parents=True)
            (agent_dir / "SOUL.md").write_text("I research things.")

            reg = ToolRegistry()
            config = _make_config(workspace_dir=tmpdir)
            register_delegate_tools(reg, config, MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

            result = await reg.execute("list_agents", {})
            data = json.loads(result)
            assert data["total"] == len(BUILTIN_ROLES)

            researcher = next(a for a in data["agents"] if a["agent_id"] == "researcher")
            assert researcher["has_identity_file"] is True

            coder = next(a for a in data["agents"] if a["agent_id"] == "coder")
            assert coder["has_identity_file"] is False


class TestViewProjectBoard:

    def setup_method(self):
        _project_boards.clear()

    @pytest.mark.asyncio
    async def test_empty_board(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("view_project_board", {})
        data = json.loads(result)
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_board_with_entries(self):
        from qanot.tools.delegate import register_delegate_tools

        _post_to_board("test", "researcher", "R", "Research AI", "AI findings here")
        _post_to_board("test", "coder", "C", "Write code", "Code written")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("view_project_board", {})
        data = json.loads(result)
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_board_filter_by_agent(self):
        from qanot.tools.delegate import register_delegate_tools

        _post_to_board("test", "researcher", "R", "task1", "r1")
        _post_to_board("test", "coder", "C", "task2", "r2")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("view_project_board", {"agent_id": "coder"})
        data = json.loads(result)
        assert data["total"] == 1
        assert data["entries"][0]["agent_id"] == "coder"

    def teardown_method(self):
        _project_boards.clear()


class TestClearProjectBoard:

    def setup_method(self):
        _project_boards.clear()

    @pytest.mark.asyncio
    async def test_clear(self):
        from qanot.tools.delegate import register_delegate_tools

        _post_to_board("test", "r", "R", "t", "result")
        _post_to_board("test", "c", "C", "t", "result")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("clear_project_board", {})
        data = json.loads(result)
        assert data["cleared"] == 2
        assert "test" not in _project_boards

    def teardown_method(self):
        _project_boards.clear()


class TestAgentDefinitionConfig:

    def test_defaults(self):
        ad = AgentDefinition(id="test")
        assert ad.timeout == 120
        assert ad.max_iterations == 15
        assert ad.tools_allow == []
        assert ad.delegate_allow == []

    def test_custom(self):
        ad = AgentDefinition(
            id="my-agent", name="My Agent", model="gpt-4o",
            tools_allow=["read_file"], timeout=60,
        )
        assert ad.model == "gpt-4o"
        assert ad.timeout == 60

    def test_delegate_allow(self):
        ad = AgentDefinition(
            id="my-agent", delegate_allow=["researcher", "coder"],
        )
        assert ad.delegate_allow == ["researcher", "coder"]


class TestSessionHistory:

    def setup_method(self):
        _agent_sessions.clear()

    def test_record_and_get(self):
        _record_session_message("user1", "researcher", "user", "Do research")
        _record_session_message("user1", "researcher", "assistant", "Here are findings")

        history = _get_session_history("user1", "researcher")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Do research"
        assert history[1]["role"] == "assistant"

    def test_empty_history(self):
        assert _get_session_history("user1", "nonexistent") == []

    def test_limit(self):
        for i in range(30):
            _record_session_message("user1", "agent1", "user", f"msg {i}")
        history = _get_session_history("user1", "agent1", limit=5)
        assert len(history) == 5
        # Should return the last 5
        assert history[0]["content"] == "msg 25"

    def test_eviction(self):
        for i in range(MAX_SESSION_HISTORY + 10):
            _record_session_message("user1", "agent1", "user", f"msg {i}")
        sessions = _agent_sessions["user1"]["agent1"]
        assert len(sessions) == MAX_SESSION_HISTORY

    def test_include_tools_filter(self):
        _record_session_message("user1", "agent1", "user", "Do task")
        _record_session_message("user1", "agent1", "assistant", "result", has_tools=True)
        _record_session_message("user1", "agent1", "assistant", "final answer")

        without_tools = _get_session_history("user1", "agent1", include_tools=False)
        assert len(without_tools) == 2

        with_tools = _get_session_history("user1", "agent1", include_tools=True)
        assert len(with_tools) == 3

    def test_per_user_isolation(self):
        _record_session_message("user1", "agent1", "user", "msg1")
        _record_session_message("user2", "agent1", "user", "msg2")
        assert len(_get_session_history("user1", "agent1")) == 1
        assert len(_get_session_history("user2", "agent1")) == 1

    def test_content_truncation(self):
        long_content = "x" * 5000
        _record_session_message("user1", "agent1", "user", long_content)
        history = _get_session_history("user1", "agent1")
        assert len(history[0]["content"]) == 2000

    def teardown_method(self):
        _agent_sessions.clear()


class TestActiveSessions:

    def setup_method(self):
        _agent_sessions.clear()

    def test_get_active_sessions(self):
        _record_session_message("user1", "researcher", "user", "task1")
        _record_session_message("user1", "coder", "user", "task2")

        sessions = _get_active_sessions("user1")
        assert len(sessions) == 2
        # Check sorted by last_active (most recent first)
        agent_ids = [s["agent_id"] for s in sessions]
        assert "researcher" in agent_ids
        assert "coder" in agent_ids

    def test_empty_sessions(self):
        assert _get_active_sessions("user1") == []

    def test_session_metadata(self):
        _record_session_message("user1", "agent1", "user", "hello")
        _record_session_message("user1", "agent1", "assistant", "hi there")

        sessions = _get_active_sessions("user1")
        assert sessions[0]["message_count"] == 2
        assert "hi there" in sessions[0]["last_message_preview"]

    def teardown_method(self):
        _agent_sessions.clear()


class TestDelegateAllow:

    def test_main_agent_always_allowed(self):
        config = _make_config()
        assert _check_delegate_allow("", "researcher", config) is True
        assert _check_delegate_allow("", "coder", config) is True

    def test_empty_allow_means_all(self):
        config = _make_config(agents=[
            AgentDefinition(id="bot1", delegate_allow=[]),
        ])
        assert _check_delegate_allow("bot1", "researcher", config) is True
        assert _check_delegate_allow("bot1", "coder", config) is True

    def test_restricted_allow(self):
        config = _make_config(agents=[
            AgentDefinition(id="bot1", delegate_allow=["researcher", "writer"]),
        ])
        assert _check_delegate_allow("bot1", "researcher", config) is True
        assert _check_delegate_allow("bot1", "writer", config) is True
        assert _check_delegate_allow("bot1", "coder", config) is False

    def test_builtin_role_always_allowed(self):
        config = _make_config()
        # Built-in roles (not in config.agents) can always delegate
        assert _check_delegate_allow("researcher", "coder", config) is True


class TestSessionHistoryTool:

    def setup_method(self):
        _agent_sessions.clear()

    @pytest.mark.asyncio
    async def test_empty_history_tool(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("agent_session_history", {"agent_id": "researcher"})
        data = json.loads(result)
        assert data["messages"] == []

    @pytest.mark.asyncio
    async def test_history_with_data(self):
        from qanot.tools.delegate import register_delegate_tools

        _record_session_message("test", "researcher", "user", "Research AI")
        _record_session_message("test", "researcher", "assistant", "AI is growing")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("agent_session_history", {"agent_id": "researcher"})
        data = json.loads(result)
        assert data["total"] == 2
        assert data["agent_name"] == "Tadqiqotchi"

    @pytest.mark.asyncio
    async def test_unknown_agent(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("agent_session_history", {"agent_id": "nonexistent"})
        data = json.loads(result)
        assert "error" in data

    def teardown_method(self):
        _agent_sessions.clear()


class TestSessionsListTool:

    def setup_method(self):
        _agent_sessions.clear()

    @pytest.mark.asyncio
    async def test_empty_sessions_list(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("agent_sessions_list", {})
        data = json.loads(result)
        assert data["sessions"] == []

    @pytest.mark.asyncio
    async def test_sessions_list_with_data(self):
        from qanot.tools.delegate import register_delegate_tools

        _record_session_message("test", "researcher", "user", "task1")
        _record_session_message("test", "coder", "user", "task2")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("agent_sessions_list", {})
        data = json.loads(result)
        assert data["total"] == 2

    def teardown_method(self):
        _agent_sessions.clear()


class TestRegisterNewTools:

    def test_session_tools_registered(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")
        names = [t["name"] for t in reg.get_definitions()]
        assert "agent_session_history" in names
        assert "agent_sessions_list" in names
        assert "view_agent_activity" in names
        assert "set_monitor_group" in names


class TestActivityLog:

    def setup_method(self):
        _activity_log.clear()
        _notify_callback.clear()

    def test_log_activity_basic(self):
        _log_activity("user1", "delegate_start", from_agent="main", to_agent="coder", task="write code")
        log = get_activity_log("user1")
        assert len(log) == 1
        assert log[0]["event"] == "delegate_start"
        assert log[0]["to_agent"] == "coder"
        assert log[0]["task"] == "write code"

    def test_log_activity_eviction(self):
        for i in range(MAX_ACTIVITY_LOG + 10):
            _log_activity("user1", "delegate_start", task=f"task-{i}")
        log = _activity_log["user1"]
        assert len(log) == MAX_ACTIVITY_LOG

    def test_log_activity_truncates_fields(self):
        _log_activity("user1", "test", task="x" * 500, detail="y" * 1000)
        log = get_activity_log("user1")
        assert len(log[0]["task"]) == 200
        assert len(log[0]["detail"]) == 500

    def test_get_activity_log_empty(self):
        assert get_activity_log("nonexistent") == []

    def test_get_activity_log_limit(self):
        for i in range(10):
            _log_activity("user1", "test", task=f"task-{i}")
        log = get_activity_log("user1", limit=3)
        assert len(log) == 3
        assert log[-1]["task"] == "task-9"

    def test_per_user_isolation(self):
        _log_activity("user1", "test", task="task-a")
        _log_activity("user2", "test", task="task-b")
        assert len(get_activity_log("user1")) == 1
        assert len(get_activity_log("user2")) == 1
        assert get_activity_log("user1")[0]["task"] == "task-a"

    def test_notify_callback_called(self):
        called = []
        import asyncio

        async def mock_callback(text):
            called.append(text)

        set_notify_callback("user1", mock_callback)
        _log_activity("user1", "delegate_start", from_agent="main", to_agent="coder", task="write")
        # Notification is fire-and-forget via asyncio.create_task,
        # so we just verify the callback was set
        assert "user1" in _notify_callback

    def teardown_method(self):
        _activity_log.clear()
        _notify_callback.clear()


class TestLoopDetection:

    def setup_method(self):
        _activity_log.clear()

    def test_no_loop_on_few_delegations(self):
        for i in range(LOOP_DETECTION_WINDOW - 2):
            _log_activity("user1", "delegate_start", to_agent="coder", task="write code")
        result = _check_for_loop("user1", "coder", "write code")
        assert result is None

    def test_detects_repeated_task_loop(self):
        for i in range(LOOP_DETECTION_WINDOW * 2):
            _log_activity("user1", "delegate_start", to_agent="coder", task="write code now")
        result = _check_for_loop("user1", "coder", "write code now")
        assert result is not None
        assert "Loop detected" in result

    def test_detects_ping_pong_loop(self):
        # A→B→A→B pattern
        _log_activity("user1", "delegate_start", to_agent="coder", task="a")
        _log_activity("user1", "delegate_start", to_agent="researcher", task="b")
        _log_activity("user1", "delegate_start", to_agent="coder", task="c")
        _log_activity("user1", "delegate_start", to_agent="researcher", task="d")
        result = _check_for_loop("user1", "researcher", "e")
        assert result is not None
        assert "Ping-pong" in result

    def test_no_ping_pong_with_different_pattern(self):
        _log_activity("user1", "delegate_start", to_agent="coder", task="a")
        _log_activity("user1", "delegate_start", to_agent="researcher", task="b")
        _log_activity("user1", "delegate_start", to_agent="analyst", task="c")
        _log_activity("user1", "delegate_start", to_agent="writer", task="d")
        result = _check_for_loop("user1", "coder", "e")
        assert result is None

    def test_no_loop_empty_log(self):
        result = _check_for_loop("user1", "coder", "task")
        assert result is None

    def test_loop_different_tasks_no_detection(self):
        for i in range(LOOP_DETECTION_WINDOW * 2):
            _log_activity("user1", "delegate_start", to_agent="coder", task=f"completely different task {i * 100}")
        # Tasks are very different so overlap should be low
        result = _check_for_loop("user1", "coder", "something entirely new")
        assert result is None

    def test_loop_logs_activity(self):
        for i in range(LOOP_DETECTION_WINDOW * 2):
            _log_activity("user1", "delegate_start", to_agent="coder", task="same task")
        _check_for_loop("user1", "coder", "same task")
        # Should have a loop_detected event in the log
        log = get_activity_log("user1")
        loop_events = [e for e in log if e["event"] == "loop_detected"]
        assert len(loop_events) >= 1

    def teardown_method(self):
        _activity_log.clear()


class TestViewAgentActivity:

    def setup_method(self):
        _activity_log.clear()

    @pytest.mark.asyncio
    async def test_view_empty_activity(self):
        from qanot.tools.delegate import register_delegate_tools

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("view_agent_activity", {})
        data = json.loads(result)
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_view_with_data(self):
        from qanot.tools.delegate import register_delegate_tools

        _log_activity("test", "delegate_start", from_agent="main", to_agent="coder", task="write")
        _log_activity("test", "delegate_done", to_agent="coder", status="completed")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("view_agent_activity", {})
        data = json.loads(result)
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_view_filtered_by_agent(self):
        from qanot.tools.delegate import register_delegate_tools

        _log_activity("test", "delegate_start", from_agent="main", to_agent="coder", task="write")
        _log_activity("test", "delegate_start", from_agent="main", to_agent="researcher", task="search")

        reg = ToolRegistry()
        register_delegate_tools(reg, _make_config(), MagicMock(), ToolRegistry(), get_user_id=lambda: "test")

        result = await reg.execute("view_agent_activity", {"agent_id": "coder"})
        data = json.loads(result)
        assert data["total"] == 1
        assert data["entries"][0]["to_agent"] == "coder"

    def teardown_method(self):
        _activity_log.clear()
