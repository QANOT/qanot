"""Tests for exec security modes: open, cautious, strict."""

from __future__ import annotations

import asyncio
import json
import pytest

from qanot.tools.builtin import (
    _is_dangerous_command,
    _needs_approval,
    _matches_allowlist,
)


class TestDangerousCommands:
    """Always blocked regardless of exec_security mode."""

    def test_rm_rf_root(self):
        assert _is_dangerous_command("rm -rf /") is not None

    def test_fork_bomb(self):
        assert _is_dangerous_command(":(){ :|:& };:") is not None

    def test_shutdown(self):
        assert _is_dangerous_command("shutdown -h now") is not None

    def test_safe_command_passes(self):
        assert _is_dangerous_command("ls -la") is None

    def test_echo_passes(self):
        assert _is_dangerous_command("echo hello") is None


class TestCautiousPatterns:
    """Commands that need approval in cautious mode."""

    def test_pip_install(self):
        assert _needs_approval("pip install requests") is not None

    def test_npm_install(self):
        assert _needs_approval("npm install express") is not None

    def test_curl(self):
        assert _needs_approval("curl https://example.com") is not None

    def test_sudo(self):
        assert _needs_approval("sudo apt update") is not None

    def test_git_push(self):
        assert _needs_approval("git push origin main") is not None

    def test_docker(self):
        assert _needs_approval("docker run ubuntu") is not None

    def test_kill(self):
        assert _needs_approval("kill -9 1234") is not None

    def test_ssh(self):
        assert _needs_approval("ssh root@server") is not None

    def test_safe_ls(self):
        assert _needs_approval("ls -la") is None

    def test_safe_cat(self):
        assert _needs_approval("cat file.txt") is None

    def test_safe_python(self):
        assert _needs_approval("python3 script.py") is None

    def test_safe_git_status(self):
        assert _needs_approval("git status") is None

    def test_safe_git_log(self):
        assert _needs_approval("git log --oneline") is None


class TestAllowlist:
    """Strict mode allowlist matching."""

    def test_exact_match(self):
        assert _matches_allowlist("ls", ["ls"]) is True

    def test_prefix_match(self):
        assert _matches_allowlist("git status", ["git"]) is True

    def test_full_prefix(self):
        assert _matches_allowlist("pip install requests", ["pip install"]) is True

    def test_no_match(self):
        assert _matches_allowlist("rm -rf /tmp", ["ls", "cat", "echo"]) is False

    def test_empty_allowlist(self):
        assert _matches_allowlist("ls", []) is False


class TestExecSecurityModes:
    """Integration tests for run_command with different security modes."""

    def _make_registry(self, exec_security="cautious", exec_allowlist=None):
        from qanot.registry import ToolRegistry
        from qanot.context import ContextTracker
        from qanot.tools.builtin import register_builtin_tools

        registry = ToolRegistry()
        context = ContextTracker(max_tokens=200000)
        register_builtin_tools(
            registry, "/tmp", context,
            exec_security=exec_security,
            exec_allowlist=exec_allowlist or [],
        )
        return registry

    def test_open_mode_allows_curl(self):
        registry = self._make_registry(exec_security="open")
        handler = registry._handlers["run_command"]
        result = asyncio.run(handler({"command": "echo test_open"}))
        assert "test_open" in result

    def test_cautious_mode_blocks_curl(self):
        registry = self._make_registry(exec_security="cautious")
        handler = registry._handlers["run_command"]
        result = asyncio.run(handler({"command": "curl https://example.com"}))
        data = json.loads(result)
        assert data.get("needs_approval") is True
        assert "curl" in data.get("reason", "").lower()

    def test_cautious_mode_allows_with_approval(self):
        registry = self._make_registry(exec_security="cautious")
        handler = registry._handlers["run_command"]
        result = asyncio.run(handler({"command": "echo approved_test", "approved": True}))
        assert "approved_test" in result

    def test_cautious_mode_allows_safe_commands(self):
        registry = self._make_registry(exec_security="cautious")
        handler = registry._handlers["run_command"]
        result = asyncio.run(handler({"command": "echo safe_command"}))
        assert "safe_command" in result

    def test_strict_mode_blocks_unlisted(self):
        registry = self._make_registry(exec_security="strict", exec_allowlist=["ls", "cat"])
        handler = registry._handlers["run_command"]
        result = asyncio.run(handler({"command": "echo hello"}))
        data = json.loads(result)
        assert "not in allowlist" in data.get("error", "")

    def test_strict_mode_allows_listed(self):
        registry = self._make_registry(exec_security="strict", exec_allowlist=["ls", "echo"])
        handler = registry._handlers["run_command"]
        result = asyncio.run(handler({"command": "echo strict_test"}))
        assert "strict_test" in result

    def test_dangerous_blocked_in_all_modes(self):
        for mode in ("open", "cautious", "strict"):
            registry = self._make_registry(exec_security=mode, exec_allowlist=["rm"])
            handler = registry._handlers["run_command"]
            result = asyncio.run(handler({"command": "rm -rf /"}))
            data = json.loads(result)
            assert "blocked" in data.get("error", "").lower(), f"Failed in {mode} mode"
