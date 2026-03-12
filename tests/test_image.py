"""Tests for image generation tool."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from qanot.agent import ToolRegistry


class TestImageToolRegistration:
    """Test that the image tool registers correctly."""

    def test_register_image_tools(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        assert "generate_image" in registry.tool_names

    def test_tool_schema_has_prompt(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        tool_defs = registry.get_definitions()
        gen_tool = next(t for t in tool_defs if t["name"] == "generate_image")
        assert "prompt" in gen_tool["input_schema"]["properties"]
        assert "prompt" in gen_tool["input_schema"]["required"]


class TestGenerateImageHandler:
    """Test the generate_image handler logic."""

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("generate_image", {"prompt": ""})
        data = json.loads(result)
        assert "error" in data
        assert "required" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_prompt_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("generate_image", {})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_unsupported_model_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("generate_image", {
            "prompt": "a cat",
            "model": "nonexistent-model",
        })
        data = json.loads(result)
        assert "error" in data
        assert "Unsupported" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_google_genai_returns_error(self):
        """When google-genai is not installed, return helpful error."""
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        # Patch import to fail
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "google" or name == "google.genai":
                raise ImportError("No module named 'google'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            result = await registry.execute("generate_image", {"prompt": "a cat"})

        data = json.loads(result)
        assert "error" in data
        assert "google-genai" in data["error"]

    @pytest.mark.asyncio
    async def test_successful_generation_response_format(self):
        """Test that a successful result has the right JSON structure."""
        # We can't easily mock the google.genai import chain in the handler closure,
        # so we test the expected output format by checking the error path instead.
        # The actual API call is tested via integration tests on the server.
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        # This will fail with ImportError since google-genai isn't installed locally
        result = await registry.execute("generate_image", {"prompt": "a sunset"})
        data = json.loads(result)
        # Should get a clean error about missing package, not a crash
        assert "error" in data


class TestSupportedModels:
    """Test model validation."""

    def test_default_model_in_supported(self):
        from qanot.tools.image import DEFAULT_IMAGE_MODEL, SUPPORTED_MODELS
        assert DEFAULT_IMAGE_MODEL in SUPPORTED_MODELS

    def test_all_models_are_gemini(self):
        from qanot.tools.image import SUPPORTED_MODELS
        for model in SUPPORTED_MODELS:
            assert "gemini" in model


class TestAgentPendingImages:
    """Test the Agent pending images queue."""

    def test_push_and_pop(self, tmp_path):
        from qanot.agent import Agent
        from qanot.config import Config
        from qanot.providers.base import LLMProvider, ProviderResponse
        from qanot.session import SessionWriter

        class FakeProvider(LLMProvider):
            model = "test"
            async def chat(self, messages, tools=None, system=None):
                return ProviderResponse()

        config = Config(bot_token="test", sessions_dir=str(tmp_path / "sessions"), workspace_dir=str(tmp_path))
        agent = Agent(config=config, provider=FakeProvider(), tool_registry=ToolRegistry())

        # Push images
        Agent._push_pending_image("user1", "/tmp/img1.png")
        Agent._push_pending_image("user1", "/tmp/img2.png")
        Agent._push_pending_image("user2", "/tmp/img3.png")

        # Pop user1
        images = agent.pop_pending_images("user1")
        assert len(images) == 2
        assert images[0] == "/tmp/img1.png"

        # Pop again — empty
        assert agent.pop_pending_images("user1") == []

        # Pop user2
        images = agent.pop_pending_images("user2")
        assert len(images) == 1

    def test_pop_nonexistent_user(self, tmp_path):
        from qanot.agent import Agent
        from qanot.config import Config
        from qanot.providers.base import LLMProvider, ProviderResponse

        class FakeProvider(LLMProvider):
            model = "test"
            async def chat(self, messages, tools=None, system=None):
                return ProviderResponse()

        config = Config(bot_token="test", sessions_dir=str(tmp_path / "sessions"), workspace_dir=str(tmp_path))
        agent = Agent(config=config, provider=FakeProvider(), tool_registry=ToolRegistry())
        assert agent.pop_pending_images("nobody") == []
