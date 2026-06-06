"""Tests for image generation & editing tools."""

from __future__ import annotations

import base64
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from qanot.registry import ToolRegistry


# --- generate_image tests ---------------------------------------------------

class TestImageToolRegistration:

    def test_register_both_tools(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        assert "generate_image" in registry.tool_names
        assert "edit_image" in registry.tool_names

    def test_tool_schema_has_prompt(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        tool_defs = registry.get_definitions()
        gen_tool = next(t for t in tool_defs if t["name"] == "generate_image")
        assert "prompt" in gen_tool["input_schema"]["properties"]
        assert "prompt" in gen_tool["input_schema"]["required"]

    def test_edit_tool_schema_has_prompt(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        tool_defs = registry.get_definitions()
        edit_tool = next(t for t in tool_defs if t["name"] == "edit_image")
        assert "prompt" in edit_tool["input_schema"]["properties"]
        assert "prompt" in edit_tool["input_schema"]["required"]


class TestGenerateImageHandler:

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        result = await registry.execute("generate_image", {"prompt": ""})
        data = json.loads(result)
        assert "error" in data
        assert "required" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_prompt_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        result = await registry.execute("generate_image", {})
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_unsupported_model_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

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
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

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
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        result = await registry.execute("generate_image", {"prompt": "a sunset"})
        data = json.loads(result)
        assert "error" in data


# --- edit_image tests --------------------------------------------------------

class TestEditImageHandler:

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

        result = await registry.execute("edit_image", {"prompt": ""})
        data = json.loads(result)
        assert "error" in data
        assert "required" in data["error"]

    @pytest.mark.asyncio
    async def test_unsupported_model_returns_error(self):
        registry = ToolRegistry()
        from qanot.tools.image import register_image_tools
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

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
        register_image_tools(registry, "/tmp/workspace", gemini_api_key="fake-api-key")

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

    def test_models_split_by_provider(self):
        from qanot.tools.image import (
            GEMINI_MODELS, OPENAI_MODELS, SUPPORTED_MODELS, _provider_for,
        )
        assert SUPPORTED_MODELS == GEMINI_MODELS | OPENAI_MODELS
        assert GEMINI_MODELS and OPENAI_MODELS
        assert not (GEMINI_MODELS & OPENAI_MODELS)  # disjoint
        for m in GEMINI_MODELS:
            assert "gemini" in m and _provider_for(m) == "gemini"
        for m in OPENAI_MODELS:
            assert m.startswith("gpt-image") and _provider_for(m) == "openai"


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


# --- OpenAI gpt-image backend ----------------------------------------------

class TestOpenAIBackend:

    def _registry_with_openai(self, tmp_path):
        from qanot.tools.image import register_image_tools
        registry = ToolRegistry()
        register_image_tools(registry, str(tmp_path), openai_api_key="sk-test")
        return registry

    def test_openai_only_default_and_models(self, tmp_path):
        from qanot.tools.image import OPENAI_MODELS
        registry = self._registry_with_openai(tmp_path)
        schema = registry.get_definitions()
        gen = next(t for t in schema if t["name"] == "generate_image")
        enum = set(gen["input_schema"]["properties"]["model"]["enum"])
        assert enum == OPENAI_MODELS  # gemini models NOT offered without a gemini key
        assert "size" in gen["input_schema"]["properties"]
        assert "quality" in gen["input_schema"]["properties"]

    def test_register_requires_a_key(self, tmp_path):
        from qanot.tools.image import register_image_tools
        registry = ToolRegistry()
        register_image_tools(registry, str(tmp_path))  # no keys
        assert "generate_image" not in registry.tool_names

    @pytest.mark.asyncio
    async def test_generate_calls_openai_and_saves(self, tmp_path):
        from qanot.tools.image import register_image_tools

        png = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEDATA").decode()

        captured = {}

        class _Images:
            async def generate(self, **kw):
                captured.update(kw)
                return MagicMock(data=[MagicMock(b64_json=png)])

        fake_client = MagicMock(images=_Images())

        registry = ToolRegistry()
        register_image_tools(registry, str(tmp_path), openai_api_key="sk-test")
        handler = registry.get_handler("generate_image")

        with patch("openai.AsyncOpenAI", return_value=fake_client):
            out = json.loads(await handler({"prompt": "a robot mascot", "quality": "low"}))

        assert out["status"] == "ok"
        assert out["model"] == "gpt-image-2"          # default OpenAI model
        assert Path(out["image_path"]).exists()
        assert captured["model"] == "gpt-image-2"
        assert captured["quality"] == "low"
        assert captured["size"] == "1024x1024"        # default size

    @pytest.mark.asyncio
    async def test_unavailable_model_rejected(self, tmp_path):
        # gemini model requested but only an OpenAI key is configured
        from qanot.tools.image import register_image_tools
        registry = ToolRegistry()
        register_image_tools(registry, str(tmp_path), openai_api_key="sk-test")
        handler = registry.get_handler("generate_image")
        out = json.loads(await handler({"prompt": "x", "model": "gemini-3-pro-image-preview"}))
        assert "error" in out and "available" in out


# --- edit_image uploads/ fallback (no base64 block in context) -------------

class TestEditUploadsFallback:

    def test_latest_upload_picks_newest_user_image(self, tmp_path):
        import os, time
        from qanot.tools.image import _latest_upload_bytes
        up = tmp_path / "uploads"; up.mkdir()
        (up / "old.jpg").write_bytes(b"OLD")
        (up / "gen_999.png").write_bytes(b"GENERATED")   # our output, must be ignored
        time.sleep(0.02)
        (up / "new.jpg").write_bytes(b"NEW")
        os.utime(up / "gen_999.png", None)               # make generated newest by mtime
        assert _latest_upload_bytes(up) == b"NEW"         # still picks user image

    def test_latest_upload_none_when_empty(self, tmp_path):
        from qanot.tools.image import _latest_upload_bytes
        (tmp_path / "uploads").mkdir()
        assert _latest_upload_bytes(tmp_path / "uploads") is None
        assert _latest_upload_bytes(tmp_path / "nope") is None


# --- set_avatar + edit_image source=avatar (no API) ------------------------

class TestAvatarFreeze:

    def _reg(self, tmp_path):
        from qanot.tools.image import register_image_tools
        r = ToolRegistry()
        register_image_tools(r, str(tmp_path), openai_api_key="sk-test")
        return r

    @pytest.mark.asyncio
    async def test_set_avatar_from_image_path(self, tmp_path):
        # a generated file the agent would pass back
        gen = tmp_path / "generated"; gen.mkdir()
        (gen / "edit_1.png").write_bytes(b"CHARACTER")
        r = self._reg(tmp_path)
        out = json.loads(await r.get_handler("set_avatar")({"image_path": "generated/edit_1.png"}))
        assert out["status"] == "ok"
        assert (tmp_path / "avatar.jpg").read_bytes() == b"CHARACTER"

    @pytest.mark.asyncio
    async def test_set_avatar_falls_back_to_last_upload(self, tmp_path):
        up = tmp_path / "uploads"; up.mkdir()
        (up / "selfie.jpg").write_bytes(b"SELFIE")
        r = self._reg(tmp_path)
        out = json.loads(await r.get_handler("set_avatar")({}))
        assert out["status"] == "ok"
        assert (tmp_path / "avatar.jpg").read_bytes() == b"SELFIE"

    @pytest.mark.asyncio
    async def test_edit_source_avatar_missing_errors(self, tmp_path):
        r = self._reg(tmp_path)
        out = json.loads(await r.get_handler("edit_image")({"prompt": "x", "source": "avatar"}))
        assert "error" in out and "set_avatar" in out["error"]

    @pytest.mark.asyncio
    async def test_edit_source_avatar_uses_frozen(self, tmp_path):
        (tmp_path / "avatar.jpg").write_bytes(b"\x89PNG\r\n\x1a\nAVATAR")
        captured = {}

        class _Images:
            async def edit(self, **kw):
                captured["called"] = True
                # image arg is a BytesIO of the avatar bytes
                captured["src"] = kw["image"].getvalue()
                captured["quality"] = kw.get("quality")
                captured["fidelity"] = kw.get("input_fidelity")
                return MagicMock(data=[MagicMock(b64_json=base64.b64encode(b"OUT").decode())])

        r = self._reg(tmp_path)
        with patch("openai.AsyncOpenAI", return_value=MagicMock(images=_Images())):
            out = json.loads(await r.get_handler("edit_image")({"prompt": "make a CTA slide", "source": "avatar"}))
        assert out["status"] == "ok"
        assert captured["called"] and captured["src"] == b"\x89PNG\r\n\x1a\nAVATAR"
        # default model is gpt-image-2 → high quality, NO input_fidelity (it 400s on it)
        assert captured["quality"] == "high" and captured["fidelity"] is None

    @pytest.mark.asyncio
    async def test_edit_image_15_gets_input_fidelity(self, tmp_path):
        (tmp_path / "avatar.jpg").write_bytes(b"AV")
        captured = {}

        class _Images:
            async def edit(self, **kw):
                captured["model"] = kw.get("model")
                captured["fidelity"] = kw.get("input_fidelity")
                return MagicMock(data=[MagicMock(b64_json=base64.b64encode(b"O").decode())])

        r = self._reg(tmp_path)
        with patch("openai.AsyncOpenAI", return_value=MagicMock(images=_Images())):
            out = json.loads(await r.get_handler("edit_image")(
                {"prompt": "x", "source": "avatar", "model": "gpt-image-1.5"}))
        assert out["status"] == "ok"
        assert captured["model"] == "gpt-image-1.5" and captured["fidelity"] == "high"
