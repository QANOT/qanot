"""Tests for plugin registry: install, remove, search, lock file, 3-tier resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from qanot.plugins.registry import (
    LockEntry,
    PluginEntry,
    _is_git_url,
    _is_valid_plugin,
    find_plugin_3tier,
    install_plugin,
    list_all_plugins,
    plugin_info,
    read_lock,
    remove_plugin,
    search_registry,
    write_lock,
)


# ── Lock file ─────────────────────────────────────────────


class TestLockFile:
    def test_write_and_read(self, tmp_path):
        entries = {
            "foo": LockEntry(
                name="foo", version="1.0.0", source="git",
                source_url="https://github.com/x/foo.git",
                installed_at="2026-03-22T12:00:00", install_dir=str(tmp_path / "foo"),
            )
        }
        write_lock(tmp_path, entries)

        result = read_lock(tmp_path)
        assert "foo" in result
        assert result["foo"].version == "1.0.0"
        assert result["foo"].source == "git"

    def test_read_empty_dir(self, tmp_path):
        assert read_lock(tmp_path) == {}

    def test_read_corrupt_json(self, tmp_path):
        (tmp_path / "plugins.lock.json").write_text("not json")
        assert read_lock(tmp_path) == {}

    def test_write_creates_dir(self, tmp_path):
        target = tmp_path / "sub" / "dir"
        write_lock(target, {})
        assert (target / "plugins.lock.json").exists()

    def test_roundtrip_preserves_data(self, tmp_path):
        entry = LockEntry(
            name="bar", version="2.0.0", source="registry",
            source_url="https://example.com", installed_at="2026-01-01",
            install_dir="/tmp/bar",
        )
        write_lock(tmp_path, {"bar": entry})
        result = read_lock(tmp_path)
        assert result["bar"].name == "bar"
        assert result["bar"].version == "2.0.0"
        assert result["bar"].source_url == "https://example.com"


# ── Git URL detection ─────────────────────────────────────


class TestIsGitUrl:
    def test_https(self):
        assert _is_git_url("https://github.com/user/repo")

    def test_http(self):
        assert _is_git_url("http://github.com/user/repo")

    def test_git_ssh(self):
        assert _is_git_url("git@github.com:user/repo.git")

    def test_dot_git_suffix(self):
        assert _is_git_url("something.git")

    def test_plain_name(self):
        assert not _is_git_url("my-plugin")

    def test_empty(self):
        assert not _is_git_url("")


# ── Plugin validation ─────────────────────────────────────


class TestIsValidPlugin:
    def test_with_plugin_py(self, tmp_path):
        p = tmp_path / "myplugin"
        p.mkdir()
        (p / "plugin.py").write_text("class QanotPlugin: pass")
        assert _is_valid_plugin(p)

    def test_with_init_py(self, tmp_path):
        p = tmp_path / "myplugin"
        p.mkdir()
        (p / "__init__.py").write_text("class QanotPlugin: pass")
        assert _is_valid_plugin(p)

    def test_empty_dir(self, tmp_path):
        p = tmp_path / "myplugin"
        p.mkdir()
        assert not _is_valid_plugin(p)


# ── 3-tier resolution ─────────────────────────────────────


class TestFindPlugin3Tier:
    def _make_plugin(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        (path / "plugin.py").write_text("class QanotPlugin: pass")

    def test_workspace_wins(self, tmp_path):
        ws = tmp_path / "workspace" / "myplugin"
        user = tmp_path / "user" / "myplugin"
        self._make_plugin(ws)
        self._make_plugin(user)

        with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
            result = find_plugin_3tier("myplugin", str(tmp_path / "workspace"))
        assert result == ws

    def test_user_fallback(self, tmp_path):
        user = tmp_path / "user" / "myplugin"
        self._make_plugin(user)

        with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
            result = find_plugin_3tier("myplugin", str(tmp_path / "empty"))
        assert result == user

    def test_not_found(self, tmp_path):
        with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
            with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "builtin"):
                result = find_plugin_3tier("nonexistent", str(tmp_path / "ws"))
        assert result is None


# ── Registry search ───────────────────────────────────────


class TestSearchRegistry:
    def test_search_matches_name(self):
        entries = [
            PluginEntry(name="crm", description="CRM integration"),
            PluginEntry(name="analytics", description="Analytics tools"),
        ]
        with patch("qanot.plugins.registry.fetch_registry", return_value=entries):
            results = search_registry("crm")
        assert len(results) == 1
        assert results[0].name == "crm"

    def test_search_matches_description(self):
        entries = [
            PluginEntry(name="foo", description="Sales CRM connector"),
            PluginEntry(name="bar", description="Image tools"),
        ]
        with patch("qanot.plugins.registry.fetch_registry", return_value=entries):
            results = search_registry("crm")
        assert len(results) == 1
        assert results[0].name == "foo"

    def test_search_matches_tags(self):
        entries = [
            PluginEntry(name="bito", description="POS", tags=["pos", "sales"]),
            PluginEntry(name="sms", description="SMS sender", tags=["notification"]),
        ]
        with patch("qanot.plugins.registry.fetch_registry", return_value=entries):
            results = search_registry("sales")
        assert len(results) == 1
        assert results[0].name == "bito"

    def test_search_case_insensitive(self):
        entries = [PluginEntry(name="CRM", description="CRM Tool")]
        with patch("qanot.plugins.registry.fetch_registry", return_value=entries):
            results = search_registry("crm")
        assert len(results) == 1

    def test_search_no_results(self):
        with patch("qanot.plugins.registry.fetch_registry", return_value=[]):
            results = search_registry("nothing")
        assert results == []


# ── Install ───────────────────────────────────────────────


class TestInstallPlugin:
    def test_install_from_git_success(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        def mock_run(cmd, **kwargs):
            # Simulate git clone
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "plugin.py").write_text("class QanotPlugin: pass")
            (clone_dir / "plugin.json").write_text(json.dumps({
                "name": "mytest", "version": "1.0.0",
            }))
            (clone_dir / ".git").mkdir()
            return MagicMock(returncode=0)

        with patch("qanot.plugins.registry.subprocess.run", side_effect=mock_run):
            ok, msg = install_plugin(
                "https://github.com/user/qanot-plugin-mytest.git",
                plugins_dir,
            )

        assert ok
        assert "mytest" in msg
        # .git should be removed
        assert not (plugins_dir / "mytest" / ".git").exists()
        # Lock file should exist
        lock = read_lock(plugins_dir)
        assert "mytest" in lock

    def test_install_git_clone_fails(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        mock_result = MagicMock(returncode=1, stderr="fatal: repo not found")
        with patch("qanot.plugins.registry.subprocess.run", return_value=mock_result):
            ok, msg = install_plugin("https://github.com/x/y.git", plugins_dir)

        assert not ok
        assert "failed" in msg.lower()

    def test_install_already_exists(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        existing = plugins_dir / "mytest"
        existing.mkdir(parents=True)

        def mock_run(cmd, **kwargs):
            clone_dir = Path(cmd[-1])
            # Dir already exists, simulate real git behavior
            return MagicMock(returncode=1, stderr="already exists")

        with patch("qanot.plugins.registry.subprocess.run", side_effect=mock_run):
            ok, msg = install_plugin("https://github.com/x/mytest.git", plugins_dir)

        assert not ok
        assert "already exists" in msg

    def test_install_unknown_name(self, tmp_path):
        with patch("qanot.plugins.registry.fetch_registry", return_value=[]):
            ok, msg = install_plugin("nonexistent", tmp_path)
        assert not ok
        assert "not found" in msg.lower()

    def test_install_from_registry(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        entries = [PluginEntry(
            name="crm", git_url="https://github.com/x/crm.git",
        )]

        def mock_run(cmd, **kwargs):
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "plugin.py").write_text("class QanotPlugin: pass")
            return MagicMock(returncode=0)

        with patch("qanot.plugins.registry.fetch_registry", return_value=entries):
            with patch("qanot.plugins.registry.subprocess.run", side_effect=mock_run):
                ok, msg = install_plugin("crm", plugins_dir)

        assert ok


# ── Remove ────────────────────────────────────────────────


class TestRemovePlugin:
    def test_remove_workspace_plugin(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugin_path = plugins_dir / "foo"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text("pass")

        # Write lock entry
        write_lock(plugins_dir, {
            "foo": LockEntry(name="foo", version="1.0", source="git"),
        })

        ok, msg = remove_plugin("foo", plugins_dir)
        assert ok
        assert not plugin_path.exists()
        # Lock should be updated
        assert "foo" not in read_lock(plugins_dir)

    def test_remove_nonexistent(self, tmp_path):
        with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "builtin"):
            ok, msg = remove_plugin("ghost", tmp_path)
        assert not ok
        assert "not found" in msg.lower()

    def test_remove_bundled_blocked(self, tmp_path):
        bundled = tmp_path / "bundled" / "core"
        bundled.mkdir(parents=True)

        with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "bundled"):
            ok, msg = remove_plugin("core", tmp_path / "empty")
        assert not ok
        assert "bundled" in msg.lower()


# ── Plugin info ───────────────────────────────────────────


class TestPluginInfo:
    def test_info_with_manifest(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        p = plugins_dir / "foo"
        p.mkdir(parents=True)
        (p / "plugin.py").write_text("class QanotPlugin:\n    @tool(\n    pass")
        (p / "plugin.json").write_text(json.dumps({
            "name": "foo", "version": "2.0.0",
            "description": "Foo plugin", "author": "Test",
            "dependencies": ["aiohttp"],
        }))

        with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
            with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "builtin"):
                info = plugin_info("foo", plugins_dir)

        assert info is not None
        assert info["name"] == "foo"
        assert info["version"] == "2.0.0"
        assert info["tier"] == "workspace"

    def test_info_not_found(self, tmp_path):
        with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
            with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "builtin"):
                info = plugin_info("ghost", tmp_path)
        assert info is None


# ── List all plugins ──────────────────────────────────────


class TestListAllPlugins:
    def test_lists_across_tiers(self, tmp_path):
        # Create bundled
        bundled = tmp_path / "bundled" / "core"
        bundled.mkdir(parents=True)
        (bundled / "plugin.py").write_text("pass")

        # Create workspace
        ws = tmp_path / "workspace" / "custom"
        ws.mkdir(parents=True)
        (ws / "plugin.py").write_text("pass")
        (ws / "plugin.json").write_text(json.dumps({
            "name": "custom", "version": "1.0", "description": "Custom plugin",
        }))

        with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "bundled"):
            with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
                plugins = list_all_plugins(str(tmp_path / "workspace"))

        names = {p["name"] for p in plugins}
        assert "core" in names
        assert "custom" in names

    def test_workspace_overrides_bundled(self, tmp_path):
        # Same name in both tiers
        bundled = tmp_path / "bundled" / "foo"
        bundled.mkdir(parents=True)
        (bundled / "plugin.py").write_text("pass")

        ws = tmp_path / "workspace" / "foo"
        ws.mkdir(parents=True)
        (ws / "plugin.py").write_text("pass")

        with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "bundled"):
            with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
                plugins = list_all_plugins(str(tmp_path / "workspace"))

        foo = next(p for p in plugins if p["name"] == "foo")
        assert foo["tier"] == "workspace"


# ── PluginEntry ───────────────────────────────────────────


class TestPluginEntry:
    def test_from_dict(self):
        data = {
            "name": "test", "version": "1.0.0",
            "description": "Test plugin", "author": "Me",
            "git_url": "https://github.com/me/test",
            "tags": ["crm", "sales"],
        }
        entry = PluginEntry.from_dict(data)
        assert entry.name == "test"
        assert entry.tags == ["crm", "sales"]

    def test_from_dict_defaults(self):
        entry = PluginEntry.from_dict({"name": "minimal"})
        assert entry.version == "0.1.0"
        assert entry.tags == []


# ── 3-tier loader integration ─────────────────────────────


class TestLoaderThreeTier:
    """Test that loader._find_plugin_dir uses 3-tier resolution."""

    def test_workspace_before_bundled(self, tmp_path):
        from qanot.plugins.loader import _find_plugin_dir

        # Create both
        bundled = tmp_path / "bundled" / "foo"
        bundled.mkdir(parents=True)

        ws = tmp_path / "workspace" / "foo"
        ws.mkdir(parents=True)

        with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "bundled"):
            with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
                result = _find_plugin_dir("foo", str(tmp_path / "workspace"))

        assert result == ws

    def test_user_level_discovery(self, tmp_path):
        from qanot.plugins.loader import _find_plugin_dir

        user = tmp_path / "user" / "bar"
        user.mkdir(parents=True)

        with patch("qanot.plugins.loader.BUILTIN_PLUGINS_DIR", tmp_path / "bundled"):
            with patch("qanot.plugins.registry.USER_PLUGINS_DIR", tmp_path / "user"):
                result = _find_plugin_dir("bar", str(tmp_path / "workspace"))

        assert result == user
