"""Tests for plugin system: manifest, loader, tool decorator, conflict detection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from qanot.plugins.base import PluginManifest, Plugin, ToolDef, validate_tool_params, tool
from qanot.plugins.loader import PluginManager, _check_required_config


# ── Plugin Manifest ──────────────────────────────────────────


class TestPluginManifest:
    def test_from_file_full(self, tmp_path):
        manifest_data = {
            "name": "mysql_query",
            "version": "1.2.0",
            "description": "MySQL query tool",
            "author": "Sardor",
            "dependencies": ["pymysql"],
            "plugin_deps": ["auth"],
            "required_config": ["db_host", "db_password"],
            "min_qanot_version": "1.0.0",
            "homepage": "https://github.com/example",
            "license": "Apache-2.0",
        }
        manifest_path = tmp_path / "plugin.json"
        manifest_path.write_text(json.dumps(manifest_data))

        result = PluginManifest.from_file(manifest_path)

        assert result.name == "mysql_query"
        assert result.version == "1.2.0"
        assert result.description == "MySQL query tool"
        assert result.author == "Sardor"
        assert result.dependencies == ["pymysql"]
        assert result.plugin_deps == ["auth"]
        assert result.required_config == ["db_host", "db_password"]
        assert result.min_qanot_version == "1.0.0"
        assert result.homepage == "https://github.com/example"
        assert result.license == "Apache-2.0"

    def test_from_file_minimal(self, tmp_path):
        manifest_path = tmp_path / "plugin.json"
        manifest_path.write_text('{"name": "simple"}')

        result = PluginManifest.from_file(manifest_path)

        assert result.name == "simple"
        assert result.version == "0.1.0"
        assert result.dependencies == []
        assert result.plugin_deps == []
        assert result.license == "MIT"

    def test_from_file_missing_name_uses_parent_dir(self, tmp_path):
        plugin_dir = tmp_path / "my_plugin"
        plugin_dir.mkdir()
        manifest_path = plugin_dir / "plugin.json"
        manifest_path.write_text("{}")

        result = PluginManifest.from_file(manifest_path)

        assert result.name == "my_plugin"

    def test_from_file_invalid_json(self, tmp_path):
        manifest_path = tmp_path / "plugin.json"
        manifest_path.write_text("not valid json {{{")

        result = PluginManifest.from_file(manifest_path)

        # Falls back to default with parent dir name
        assert result.name == tmp_path.name

    def test_default_manifest(self):
        result = PluginManifest.default("test_plugin")

        assert result.name == "test_plugin"
        assert result.version == "0.1.0"
        assert result.description == ""
        assert result.dependencies == []
        assert result.plugin_deps == []
        assert result.required_config == []
        assert result.license == "MIT"


# ── Validate Tool Params ─────────────────────────────────────


class TestValidateToolParams:
    def test_valid_params(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        }
        errors = validate_tool_params({"query": "SELECT 1", "limit": 10}, schema)
        assert errors == []

    def test_missing_required(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        }
        errors = validate_tool_params({}, schema)
        assert len(errors) == 1
        assert "Missing required parameter: query" in errors[0]

    def test_wrong_type(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
        }
        errors = validate_tool_params({"count": "not_a_number"}, schema)
        assert len(errors) == 1
        assert "count" in errors[0]
        assert "integer" in errors[0]

    def test_multiple_errors(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        errors = validate_tool_params({"age": "twenty"}, schema)
        assert len(errors) == 2  # missing 'name' + wrong type 'age'

    def test_extra_params_ignored(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
        }
        errors = validate_tool_params({"name": "test", "extra": 42}, schema)
        assert errors == []

    def test_number_accepts_int_and_float(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
            },
        }
        assert validate_tool_params({"value": 42}, schema) == []
        assert validate_tool_params({"value": 3.14}, schema) == []
        assert len(validate_tool_params({"value": "nope"}, schema)) == 1

    def test_boolean_type(self):
        schema = {
            "type": "object",
            "properties": {
                "flag": {"type": "boolean"},
            },
        }
        assert validate_tool_params({"flag": True}, schema) == []
        assert len(validate_tool_params({"flag": "yes"}, schema)) == 1

    def test_array_type(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array"},
            },
        }
        assert validate_tool_params({"items": [1, 2, 3]}, schema) == []
        assert len(validate_tool_params({"items": "not_array"}, schema)) == 1

    def test_object_type(self):
        schema = {
            "type": "object",
            "properties": {
                "data": {"type": "object"},
            },
        }
        assert validate_tool_params({"data": {"key": "val"}}, schema) == []
        assert len(validate_tool_params({"data": [1]}, schema)) == 1

    def test_unknown_type_passes(self):
        schema = {
            "type": "object",
            "properties": {
                "x": {"type": "custom_type"},
            },
        }
        errors = validate_tool_params({"x": "anything"}, schema)
        assert errors == []

    def test_empty_schema(self):
        errors = validate_tool_params({"key": "value"}, {})
        assert errors == []


# ── Plugin Manager ───────────────────────────────────────────


class TestPluginManager:
    def test_initial_state(self):
        mgr = PluginManager()
        assert mgr.loaded_plugins == {}
        assert mgr.get_plugin("anything") is None
        assert mgr.get_manifest("anything") is None

    @pytest.mark.asyncio
    async def test_shutdown_clears_plugins(self):
        mgr = PluginManager()

        # Manually inject a fake plugin
        plugin = MagicMock(spec=Plugin)
        plugin.teardown = AsyncMock()
        manifest = PluginManifest.default("test")

        mgr._plugins["test"] = plugin
        mgr._manifests["test"] = manifest

        assert mgr.get_plugin("test") is not None

        await mgr.shutdown_all()

        assert mgr.loaded_plugins == {}
        assert mgr.get_plugin("test") is None
        assert mgr.get_manifest("test") is None
        plugin.teardown.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_teardown_error(self):
        mgr = PluginManager()

        plugin = MagicMock(spec=Plugin)
        plugin.teardown = AsyncMock(side_effect=RuntimeError("cleanup failed"))
        mgr._plugins["bad"] = plugin
        mgr._manifests["bad"] = PluginManifest.default("bad")

        # Should not raise
        await mgr.shutdown_all()

        assert mgr.loaded_plugins == {}

    def test_check_plugin_deps_all_present(self):
        mgr = PluginManager()
        mgr._plugins["auth"] = MagicMock()
        mgr._plugins["db"] = MagicMock()

        manifest = PluginManifest(name="test", plugin_deps=["auth", "db"])
        missing = mgr._check_plugin_deps(manifest)
        assert missing == []

    def test_check_plugin_deps_some_missing(self):
        mgr = PluginManager()
        mgr._plugins["auth"] = MagicMock()

        manifest = PluginManifest(name="test", plugin_deps=["auth", "db", "cache"])
        missing = mgr._check_plugin_deps(manifest)
        assert missing == ["db", "cache"]

    def test_check_plugin_deps_none_required(self):
        mgr = PluginManager()
        manifest = PluginManifest(name="test", plugin_deps=[])
        missing = mgr._check_plugin_deps(manifest)
        assert missing == []


# ── Check Required Config ────────────────────────────────────


class TestCheckRequiredConfig:
    def test_all_present(self):
        manifest = PluginManifest(name="test", required_config=["db_host", "db_port"])
        config = {"db_host": "localhost", "db_port": "5432"}
        missing = _check_required_config(manifest, config)
        assert missing == []

    def test_some_missing(self):
        manifest = PluginManifest(name="test", required_config=["db_host", "db_port", "db_password"])
        config = {"db_host": "localhost"}
        missing = _check_required_config(manifest, config)
        assert "db_port" in missing
        assert "db_password" in missing

    def test_empty_value_counts_as_missing(self):
        manifest = PluginManifest(name="test", required_config=["api_key"])
        config = {"api_key": ""}
        missing = _check_required_config(manifest, config)
        assert "api_key" in missing

    def test_no_required_config(self):
        manifest = PluginManifest(name="test", required_config=[])
        missing = _check_required_config(manifest, {})
        assert missing == []


# ── Plugin Conflict Detection ────────────────────────────────


class TestPluginConflictDetection:
    @pytest.mark.asyncio
    async def test_tool_name_conflict_logged(self, tmp_path):
        """When a plugin registers a tool that already exists, a warning should be logged."""
        from qanot.registry import ToolRegistry

        registry = ToolRegistry()

        async def noop(_):
            return "{}"

        registry.register("read_file", "Read a file", {"type": "object"}, noop)

        mgr = PluginManager()

        # Create a fake plugin with a conflicting tool
        class FakePlugin(Plugin):
            name = "conflict_test"

            def get_tools(self):
                return [ToolDef(
                    name="read_file",
                    description="Conflicting read_file",
                    parameters={"type": "object"},
                    handler=noop,
                )]

        plugin = FakePlugin()
        tools = plugin.get_tools()
        conflicts = [t.name for t in tools if t.name in registry.tool_names]
        assert conflicts == ["read_file"]


# ── Tool Decorator ───────────────────────────────────────────


class TestToolDecorator:
    def test_tool_decorator_attaches_metadata(self):
        class MyPlugin(Plugin):
            name = "test"

            @tool("greet", "Say hello", {"type": "object", "properties": {"name": {"type": "string"}}})
            async def greet(self, params):
                return json.dumps({"greeting": f"Hello {params['name']}"})

            def get_tools(self):
                return self._collect_tools()

        plugin = MyPlugin()
        tools = plugin.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "greet"
        assert tools[0].description == "Say hello"
        assert "name" in tools[0].parameters["properties"]

    def test_tool_decorator_default_params(self):
        class MyPlugin(Plugin):
            name = "test"

            @tool("noop", "Does nothing")
            async def noop(self, params):
                return "{}"

            def get_tools(self):
                return self._collect_tools()

        plugin = MyPlugin()
        tools = plugin.get_tools()
        assert tools[0].parameters == {"type": "object", "properties": {}}
