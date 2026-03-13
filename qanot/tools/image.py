"""Image generation & editing — Nano Banana (Gemini native image API)."""

from __future__ import annotations

import base64
import json
import logging
import time
from io import BytesIO
from pathlib import Path

from qanot.agent import ToolRegistry

logger = logging.getLogger(__name__)

# Nano Banana Pro — highest quality
DEFAULT_IMAGE_MODEL = "gemini-3-pro-image-preview"

SUPPORTED_MODELS = {
    "gemini-3-pro-image-preview",      # Nano Banana Pro (highest quality)
    "gemini-3.1-flash-image-preview",  # Nano Banana 2 (fast)
    "gemini-2.5-flash-image",          # Nano Banana (speed optimized)
}


def _save_and_queue(
    image_data: bytes | str,
    images_dir: Path,
    get_user_id: callable | None,
    prefix: str = "img",
) -> tuple[str, int]:
    """Save image bytes to disk and push to agent's pending images queue.

    Returns (image_path, size_bytes).
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    filename = f"{prefix}_{timestamp}.png"
    image_path = images_dir / filename

    if isinstance(image_data, str):
        image_bytes = base64.b64decode(image_data)
    else:
        image_bytes = image_data

    image_path.write_bytes(image_bytes)
    logger.info("Image saved: %s (%d bytes)", image_path, len(image_bytes))

    # Push to agent's pending images queue
    from qanot.agent import Agent
    if get_user_id:
        user_id = get_user_id()
        Agent._push_pending_image(user_id, str(image_path))

    return str(image_path), len(image_bytes)


def _extract_image_from_response(response) -> tuple[bytes | None, str]:
    """Extract image data and text from Gemini response.

    Returns (image_bytes_or_None, response_text).
    """
    image_data = None
    response_text = ""

    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                image_data = part.inline_data.data
            elif part.text:
                response_text = part.text

    return image_data, response_text


def _find_last_image_in_conversation(get_user_id: callable | None) -> bytes | None:
    """Find the last user-sent image from the current conversation.

    Searches backwards through messages for an image content block,
    decodes base64 and returns raw bytes.
    """
    from qanot.agent import Agent
    if not get_user_id or Agent._instance is None:
        return None

    user_id = get_user_id()
    messages = Agent._instance.get_conversation(user_id)

    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                source = block.get("source", {})
                if source.get("type") == "base64" and source.get("data"):
                    return base64.b64decode(source["data"])
    return None


def register_image_tools(
    registry: ToolRegistry,
    api_key: str,
    workspace_dir: str,
    *,
    model: str = DEFAULT_IMAGE_MODEL,
    get_user_id: callable = None,
) -> None:
    """Register image generation and editing tools."""
    _client = None

    def _get_client():
        nonlocal _client
        if _client is None:
            from google import genai
            _client = genai.Client(api_key=api_key)
        return _client

    images_dir = Path(workspace_dir) / "generated"

    def _validate_params(params: dict, prompt_error: str) -> tuple[str | None, str | None, str | None]:
        """Validate prompt and model. Returns (prompt, img_model, error_json)."""
        prompt = params.get("prompt", "").strip()
        if not prompt:
            return None, None, json.dumps({"error": prompt_error})
        img_model = params.get("model", model)
        if img_model not in SUPPORTED_MODELS:
            return None, None, json.dumps({
                "error": f"Unsupported model: {img_model}",
                "supported": list(SUPPORTED_MODELS),
            })
        return prompt, img_model, None

    def _build_success(response, prompt, img_model, prefix, fail_msg):
        """Process Gemini response: extract image, save, queue, return JSON result."""
        image_data_bytes, response_text = _extract_image_from_response(response)
        if not image_data_bytes:
            return json.dumps({
                "error": fail_msg,
                "model_response": response_text or "(no text response)",
            })
        image_path, size_bytes = _save_and_queue(
            image_data_bytes, images_dir, get_user_id, prefix=prefix,
        )
        return json.dumps({
            "status": "ok",
            "image_path": image_path,
            "model": img_model,
            "description": response_text or prompt,
            "size_bytes": size_bytes,
        })

    # ── generate_image ──────────────────────────────────────

    async def generate_image(params: dict) -> str:
        """Generate an image from a text prompt using Nano Banana."""
        prompt, img_model, err = _validate_params(params, "prompt is required")
        if err:
            return err

        try:
            from google.genai import types

            client = _get_client()
            response = client.models.generate_content(
                model=img_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )

            return _build_success(
                response, prompt, img_model, "gen",
                "No image generated. The model may have refused the prompt.",
            )

        except ImportError:
            return json.dumps({
                "error": "google-genai package not installed. Run: pip install google-genai",
            })
        except Exception as e:
            logger.error("Image generation failed: %s", e)
            return json.dumps({"error": f"Image generation failed: {e}"})

    # ── edit_image ──────────────────────────────────────────

    async def edit_image(params: dict) -> str:
        """Edit the user's last sent image based on a text instruction."""
        prompt, img_model, err = _validate_params(
            params, "prompt is required — describe what to change",
        )
        if err:
            return err

        # Find the source image
        source_bytes = _find_last_image_in_conversation(get_user_id)
        if not source_bytes:
            return json.dumps({
                "error": "No image found in conversation. The user must send a photo first.",
            })

        try:
            from google.genai import types
            from PIL import Image

            client = _get_client()

            # Convert bytes → PIL Image (Gemini SDK accepts PIL images)
            pil_image = Image.open(BytesIO(source_bytes))

            response = client.models.generate_content(
                model=img_model,
                contents=[prompt, pil_image],
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )

            return _build_success(
                response, prompt, img_model, "edit",
                "Image editing failed. The model may have refused the request.",
            )

        except ImportError:
            return json.dumps({
                "error": "google-genai or Pillow not installed. Run: pip install google-genai Pillow",
            })
        except Exception as e:
            logger.error("Image editing failed: %s", e)
            return json.dumps({"error": f"Image editing failed: {e}"})

    # ── Register tools ──────────────────────────────────────

    registry.register(
        name="generate_image",
        description="Generate a NEW image from a text description using AI (Nano Banana / Gemini). Use this when the user wants to CREATE an image from scratch.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed text description of the image to generate.",
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

    registry.register(
        name="edit_image",
        description="Edit the user's LAST SENT photo based on a text instruction using AI (Nano Banana / Gemini). Use this when the user sends a photo and asks to modify/change/edit it (e.g. 'make it sunset', 'remove the background', 'add a hat').",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text instruction describing how to edit the image (e.g. 'change background to mountains', 'make it black and white', 'add sunglasses').",
                },
                "model": {
                    "type": "string",
                    "description": f"Image model to use. Default: {model}",
                    "enum": list(SUPPORTED_MODELS),
                },
            },
            "required": ["prompt"],
        },
        handler=edit_image,
    )
