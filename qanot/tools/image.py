"""Image generation tool — Nano Banana (Gemini native image generation)."""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from qanot.agent import ToolRegistry

logger = logging.getLogger(__name__)

# Image generation model (Nano Banana 2 = Gemini 3.1 Flash Image)
DEFAULT_IMAGE_MODEL = "gemini-2.0-flash-exp-image-generation"

# Supported models
SUPPORTED_MODELS = {
    "gemini-2.0-flash-exp-image-generation",  # Nano Banana (stable)
    "gemini-3.1-flash-image-preview",  # Nano Banana 2 (latest)
    "gemini-3-pro-image-preview",  # Nano Banana Pro (high quality)
}


def register_image_tools(
    registry: ToolRegistry,
    api_key: str,
    workspace_dir: str,
    *,
    model: str = DEFAULT_IMAGE_MODEL,
    get_user_id: callable = None,
) -> None:
    """Register image generation tool.

    Args:
        registry: Tool registry to register with.
        api_key: Gemini API key.
        workspace_dir: Workspace directory for saving generated images.
        model: Gemini image model to use.
        get_user_id: Callable returning current user ID (for per-user queuing).
    """
    # Lazy import — only needed when tool is actually called
    _client = None

    def _get_client():
        nonlocal _client
        if _client is None:
            from google import genai
            _client = genai.Client(api_key=api_key)
        return _client

    # Directory for generated images
    images_dir = Path(workspace_dir) / "generated"

    async def generate_image(params: dict) -> str:
        """Generate an image from a text prompt using Nano Banana."""
        prompt = params.get("prompt", "").strip()
        if not prompt:
            return json.dumps({"error": "prompt is required"})

        img_model = params.get("model", model)
        if img_model not in SUPPORTED_MODELS:
            return json.dumps({
                "error": f"Unsupported model: {img_model}",
                "supported": list(SUPPORTED_MODELS),
            })

        try:
            from google import genai
            from google.genai import types

            client = _get_client()

            response = client.models.generate_content(
                model=img_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )

            # Extract image from response
            image_data = None
            response_text = ""

            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts:
                    if part.inline_data is not None:
                        image_data = part.inline_data.data
                    elif part.text:
                        response_text = part.text

            if not image_data:
                return json.dumps({
                    "error": "No image generated. The model may have refused the prompt.",
                    "model_response": response_text or "(no text response)",
                })

            # Save image to workspace
            images_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time() * 1000)
            filename = f"img_{timestamp}.png"
            image_path = images_dir / filename

            # image_data is bytes
            if isinstance(image_data, str):
                image_bytes = base64.b64decode(image_data)
            else:
                image_bytes = image_data

            image_path.write_bytes(image_bytes)

            logger.info("Image generated: %s (%d bytes)", image_path, len(image_bytes))

            # Push to agent's pending images queue
            from qanot.agent import Agent
            if get_user_id:
                user_id = get_user_id()
                Agent._push_pending_image(user_id, str(image_path))

            return json.dumps({
                "status": "ok",
                "image_path": str(image_path),
                "model": img_model,
                "description": response_text or prompt,
                "size_bytes": len(image_bytes),
            })

        except ImportError:
            return json.dumps({
                "error": "google-genai package not installed. Run: pip install google-genai",
            })
        except Exception as e:
            logger.error("Image generation failed: %s", e)
            return json.dumps({"error": f"Image generation failed: {e}"})

    registry.register(
        name="generate_image",
        description="Generate an image from a text description using AI (Nano Banana / Gemini). Returns the file path of the generated image.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate. Be specific and detailed for best results.",
                },
                "model": {
                    "type": "string",
                    "description": f"Image model to use. Default: {model}",
                    "enum": list(SUPPORTED_MODELS),
                },
            },
            "required": ["prompt"],
        },
        handler=generate_image,
    )
