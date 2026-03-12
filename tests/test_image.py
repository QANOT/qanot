"""Tests for image generation & editing tools."""

from __future__ import annotations

import base64
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from qanot.agent import ToolRegistry


# --- generate_image tests ---------------------------------------------------

class TestImageToolRegistration:

    def test_register_both_tools(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        assert "generate_image" in registry.tool_names
        assert "edit_image" in registry.tool_names

    def test_tool_schema_has_prompt(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        tool_defs = registry.get_definitions()
        gen_tool = next(t for t in tool_defs if t["name"] == "generate_image")
        assert "prompt" in gen_tool["input_schema"]["properties"]
        assert "prompt" in gen_tool["input_schema"]["required"]

    def test_edit_tool_schema_has_prompt(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        tool_defs = registry.get_definitions()
        edit_tool = next(t for t in tool_defs if t["name"] == "edit_image")
        assert "prompt" in edit_tool["input_schema"]["properties"]
        assert "prompt" in edit_tool["input_schema"]["required"]


class TestGenerateImageHandler:

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
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

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
    async def test_generation_without_sdk(self):
        """Without google-genai installed locally, should return clean error."""
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("generate_image", {"prompt": "a sunset"})
        data = json.loads(result)
        assert "error" in data


# --- edit_image tests --------------------------------------------------------

class TestEditImageHandler:

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("edit_image", {"prompt": ""})
        data = json.loads(result)
        assert "error" in data
        assert "required" in data["error"]

    @pytest.mark.asyncio
    async def test_unsupported_model_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("edit_image", {
            "prompt": "make it sunset",
            "model": "bad-model",
        })
        data = json.loads(result)
        assert "error" in data
        assert "Unsupported" in data["error"]

    @pytest.mark.asyncio
    async def test_no_image_in_conversation_returns_error(self):
        """When no image was sent by user, edit_image should return helpful error."""
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "fake-api-key", "/tmp/workspace")

        result = await registry.execute("edit_image", {"prompt": "make it sunset"})
        data = json.loads(result)
        assert "error" in data
        assert "No image found" in data["error"]


# --- Helper function tests ---------------------------------------------------

class TestFindLastImageInConversation:

    def test_finds_image_in_messages(self):
        from qanot.tools.image import _find_last_image_in_conversation
        from qanot.agent import Agent

        # Create fake image data
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        b64_data = base64.b64encode(fake_png).decode()

        # Mock Agent._instance with conversation containing an image
        mock_agent = MagicMock()
        mock_agent.get_conversation.return_value = [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_data}},
                {"type": "text", "text": "edit this"},
            ]},
        ]

        original_instance = Agent._instance
        Agent._instance = mock_agent
        try:
            result = _find_last_image_in_conversation(lambda: "user1")
            assert result == fake_png
        finally:
            Agent._instance = original_instance

    def test_returns_none_when_no_images(self):
        from qanot.tools.image import _find_last_image_in_conversation
        from qanot.agent import Agent

        mock_agent = MagicMock()
        mock_agent.get_conversation.return_value = [
            {"role": "user", "content": "just text"},
        ]

        original_instance = Agent._instance
        Agent._instance = mock_agent
        try:
            result = _find_last_image_in_conversation(lambda: "user1")
            assert result is None
        finally:
            Agent._instance = original_instance

    def test_returns_none_without_get_user_id(self):
        from qanot.tools.image import _find_last_image_in_conversation
        assert _find_last_image_in_conversation(None) is None

    def test_finds_most_recent_image(self):
        from qanot.tools.image import _find_last_image_in_conversation
        from qanot.agent import Agent

        img1 = base64.b64encode(b"image1").decode()
        img2 = base64.b64encode(b"image2").decode()

        mock_agent = MagicMock()
        mock_agent.get_conversation.return_value = [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img1}},
            ]},
            {"role": "assistant", "content": "nice photo"},
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img2}},
                {"type": "text", "text": "edit this one"},
            ]},
        ]

        original_instance = Agent._instance
        Agent._instance = mock_agent
        try:
            result = _find_last_image_in_conversation(lambda: "user1")
            assert result == b"image2"  # Most recent
        finally:
            Agent._instance = original_instance


class TestSaveAndQueue:

    def test_saves_bytes(self, tmp_path):
        from qanot.tools.image import _save_and_queue
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

        path, size = _save_and_queue(fake_png, tmp_path / "gen", None, prefix="test")
        assert Path(path).exists()
        assert size == len(fake_png)

    def test_saves_base64_string(self, tmp_path):
        from qanot.tools.image import _save_and_queue
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        b64 = base64.b64encode(fake_png).decode()

        path, size = _save_and_queue(b64, tmp_path / "gen", None, prefix="test")
        assert Path(path).exists()
        assert size == len(fake_png)


class TestSupportedModels:

    def test_default_model_in_supported(self):
        from qanot.tools.image import DEFAULT_IMAGE_MODEL, SUPPORTED_MODELS
        assert DEFAULT_IMAGE_MODEL in SUPPORTED_MODELS

    def test_all_models_are_gemini(self):
        from qanot.tools.image import SUPPORTED_MODELS
        for m in SUPPORTED_MODELS:
            assert "gemini" in m


class TestAgentPendingImages:

    def test_push_and_pop(self, tmp_path):
        from qanot.agent import Agent
        from qanot.config import Config
        from qanot.providers.base import LLMProvider, ProviderResponse

        class FakeProvider(LLMProvider):
            model = "test"
            async def chat(self, messages, tools=None, system=None):
                return ProviderResponse()

        config = Config(bot_token="test", sessions_dir=str(tmp_path / "sessions"), workspace_dir=str(tmp_path))
        agent = Agent(config=config, provider=FakeProvider(), tool_registry=ToolRegistry())

        Agent._push_pending_image("user1", "/tmp/img1.png")
        Agent._push_pending_image("user1", "/tmp/img2.png")
        Agent._push_pending_image("user2", "/tmp/img3.png")

        images = agent.pop_pending_images("user1")
        assert len(images) == 2
        assert agent.pop_pending_images("user1") == []

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
