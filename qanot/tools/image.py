"""Image generation & editing — multi-provider (Gemini Nano Banana + OpenAI gpt-image).

Model name selects the backend: ``gpt-image-*`` → OpenAI, ``gemini-*`` → Gemini.
Both backends produce raw PNG bytes which flow through the same save/queue path,
so rate-limiting, the pending-image queue, and tool schemas are provider-agnostic.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from qanot.ratelimit import RateLimiter
from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── model catalogue ─────────────────────────────────────────
GEMINI_MODELS = {
    "gemini-3-pro-image-preview",      # Nano Banana Pro (highest quality)
    "gemini-3.1-flash-image-preview",  # Nano Banana 2 (fast)
    "gemini-2.5-flash-image",          # Nano Banana (speed optimized)
}
OPENAI_MODELS = {
    "gpt-image-2",        # #1 ranked (2026), newest
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",   # cheapest
}
SUPPORTED_MODELS = GEMINI_MODELS | OPENAI_MODELS

DEFAULT_GEMINI_MODEL = "gemini-3-pro-image-preview"
DEFAULT_OPENAI_MODEL = "gpt-image-2"
DEFAULT_IMAGE_MODEL = DEFAULT_GEMINI_MODEL  # historical default

# OpenAI gpt-image params
OPENAI_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
OPENAI_QUALITIES = {"low", "medium", "high", "auto"}
DEFAULT_OPENAI_SIZE = "1024x1024"
DEFAULT_OPENAI_QUALITY = "high"

# Image generation is slow (gpt-image-2 high quality can take 60-120s); the
# global 30s tool timeout would kill it. Give these tools generous headroom.
IMAGE_TOOL_TIMEOUT = 180.0


def _provider_for(model: str) -> str:
    """Return the backend provider for a model name."""
    return "openai" if model in OPENAI_MODELS else "gemini"


def _save_and_queue(
    image_data: bytes | str,
    images_dir: Path,
    get_user_id: Callable[[], str | None] | None,
    prefix: str = "img",
) -> tuple[str, int]:
    """Save image bytes to disk and push to agent's pending images queue.

    Returns (image_path, size_bytes).
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    image_path = images_dir / f"{prefix}_{int(time.time() * 1000)}.png"

    image_bytes = base64.b64decode(image_data) if isinstance(image_data, str) else image_data

    image_path.write_bytes(image_bytes)
    logger.info("Image saved: %s (%d bytes)", image_path, len(image_bytes))

    # Push to agent's pending images queue
    if get_user_id:
        from qanot.agent import Agent
        user_id = get_user_id()
        Agent._push_pending_image(user_id, str(image_path))

    return str(image_path), len(image_bytes)


def _extract_image_from_response(response) -> tuple[bytes | None, str]:
    """Extract image data and text from a Gemini response.

    Returns (image_bytes_or_None, response_text).
    """
    image_data = None
    response_text = ""

    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                image_data = part.inline_data.data
            if part.text:
                response_text = part.text

    return image_data, response_text


def _find_last_image_in_conversation(get_user_id: Callable[[], str | None] | None) -> bytes | None:
    """Find the last user-sent image from the current conversation.

    Searches backwards through messages for an image content block,
    decodes base64 and returns raw bytes.
    """
    if not get_user_id:
        return None
    from qanot.agent import Agent
    if Agent._instance is None:
        return None

    user_id = get_user_id()
    messages = Agent._instance.get_conversation(user_id)

    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
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


def _latest_upload_bytes(uploads_dir: Path) -> bytes | None:
    """Most recently uploaded USER image from ``<workspace>/uploads/``.

    Robust fallback for ``edit_image``: the base64 vision block is stripped
    from the conversation by context management to save tokens, so a few turns
    after a photo arrives the in-context copy is gone — but the file on disk
    (saved by ``save_photo_to_uploads``) survives. Excludes our own generated
    outputs (``gen_*`` / ``edit_*``) so we never "edit" a prior generation.
    """
    try:
        if not uploads_dir.is_dir():
            return None
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        candidates = [
            p for p in uploads_dir.iterdir()
            if p.is_file() and p.suffix.lower() in exts
            and not p.name.startswith(("gen_", "edit_"))
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        return latest.read_bytes()
    except Exception:  # noqa: BLE001
        return None


def register_image_tools(
    registry: ToolRegistry,
    workspace_dir: str,
    *,
    gemini_api_key: str | None = None,
    openai_api_key: str | None = None,
    model: str = DEFAULT_IMAGE_MODEL,
    get_user_id: Callable[[], str | None] | None = None,
    per_user_hourly: int = 0,
) -> None:
    """Register image generation and editing tools (multi-provider).

    At least one of ``gemini_api_key`` / ``openai_api_key`` must be set, else
    this is a no-op (caller is expected to gate, but we double-guard here).

    Args:
        per_user_hourly: Max ``generate_image`` calls per user per hour.
            0 disables rate limiting. Only applied to ``generate_image`` —
            editing is gated by the user sending a fresh photo so its
            bill-leak risk is naturally bounded.
    """
    if not gemini_api_key and not openai_api_key:
        logger.warning("register_image_tools called with no provider key — skipping")
        return

    # Which models are actually usable given the keys we have.
    available_models: set[str] = set()
    if gemini_api_key:
        available_models |= GEMINI_MODELS
    if openai_api_key:
        available_models |= OPENAI_MODELS

    # Effective default: honor the configured model if its provider key exists,
    # otherwise fall back to a sensible default for whichever key we have.
    if model in available_models:
        default_model = model
    elif openai_api_key:
        default_model = DEFAULT_OPENAI_MODEL
    elif gemini_api_key:
        default_model = DEFAULT_GEMINI_MODEL
    else:  # unreachable (guarded above)
        default_model = model

    _gemini_client = None

    def _get_gemini_client():
        nonlocal _gemini_client
        if _gemini_client is None:
            from google import genai
            _gemini_client = genai.Client(api_key=gemini_api_key)
        return _gemini_client

    _openai_client = None

    def _get_openai_client():
        nonlocal _openai_client
        if _openai_client is None:
            from openai import AsyncOpenAI
            _openai_client = AsyncOpenAI(api_key=openai_api_key)
        return _openai_client

    images_dir = Path(workspace_dir) / "generated"

    # Per-user-per-tool sliding window. Window == lockout == 3600s so the
    # retry-after we expose to the LLM matches the configured cap exactly.
    image_gen_limiter: RateLimiter | None = None
    if per_user_hourly > 0:
        image_gen_limiter = RateLimiter(
            max_requests=per_user_hourly,
            window_seconds=3600,
            lockout_seconds=3600,
        )

    def _check_image_rate_limit() -> str | None:
        """Return rate-limit error JSON if user is over cap, else None.

        Fails OPEN when user_id is unavailable (system callers / tests).
        """
        if image_gen_limiter is None or get_user_id is None:
            return None
        user_id = get_user_id()
        if user_id is None:
            return None
        user_id = str(user_id)
        allowed, _reason = image_gen_limiter.check(user_id)
        if not allowed:
            retry = image_gen_limiter.retry_after(user_id)
            logger.warning(
                "generate_image rate-limited for user %s (cap %d/hour)",
                user_id, per_user_hourly,
            )
            return json.dumps({
                "error": "rate_limited",
                "reason": (
                    f"generate_image hourly limit ({per_user_hourly}) reached"
                ),
                "retry_after_seconds": retry,
            })
        image_gen_limiter.record(user_id)
        return None

    def _validate_params(params: dict, prompt_error: str) -> tuple[str | None, str | None, str | None]:
        """Validate prompt and model. Returns (prompt, img_model, error_json)."""
        prompt = params.get("prompt", "").strip()
        if not prompt:
            return None, None, json.dumps({"error": prompt_error})
        img_model = params.get("model", default_model)
        if img_model not in available_models:
            return None, None, json.dumps({
                "error": f"Unsupported or unavailable model: {img_model}",
                "available": sorted(available_models),
                "hint": "The provider key for this model is not configured.",
            })
        return prompt, img_model, None

    def _finalize(image_bytes: bytes | None, response_text: str, prompt: str,
                  img_model: str, prefix: str, fail_msg: str) -> str:
        """Save raw image bytes, queue them, and return the JSON result."""
        if not image_bytes:
            return json.dumps({
                "error": fail_msg,
                "model_response": response_text or "(no text response)",
            })
        image_path, size_bytes = _save_and_queue(
            image_bytes, images_dir, get_user_id, prefix=prefix,
        )
        return json.dumps({
            "status": "ok",
            "image_path": image_path,
            "model": img_model,
            "description": response_text or prompt,
            "size_bytes": size_bytes,
        })

    # ── OpenAI backend ──────────────────────────────────────
    def _openai_opts(params: dict) -> tuple[str, str]:
        size = params.get("size", DEFAULT_OPENAI_SIZE)
        if size not in OPENAI_SIZES:
            size = DEFAULT_OPENAI_SIZE
        quality = params.get("quality", DEFAULT_OPENAI_QUALITY)
        if quality not in OPENAI_QUALITIES:
            quality = DEFAULT_OPENAI_QUALITY
        return size, quality

    async def _openai_generate(prompt: str, img_model: str, params: dict) -> bytes:
        size, quality = _openai_opts(params)
        client = _get_openai_client()
        resp = await client.images.generate(
            model=img_model, prompt=prompt, size=size, quality=quality, n=1,
        )
        return base64.b64decode(resp.data[0].b64_json)

    async def _openai_edit(prompt: str, img_model: str, source_bytes: bytes, params: dict) -> bytes:
        size, _quality = _openai_opts(params)
        client = _get_openai_client()
        buf = BytesIO(source_bytes)
        buf.name = "source.png"
        # gpt-image edits don't take a quality arg the same way; pass size only.
        resp = await client.images.edit(
            model=img_model, image=buf, prompt=prompt, size=size, n=1,
        )
        return base64.b64decode(resp.data[0].b64_json)

    # ── generate_image ──────────────────────────────────────

    async def generate_image(params: dict) -> str:
        """Generate an image from a text prompt (Gemini Nano Banana or OpenAI gpt-image)."""
        prompt, img_model, err = _validate_params(params, "prompt is required")
        if err:
            return err

        # Per-user hourly cap (bill-leak protection). Run before any network
        # call — image gen is ~$0.02-0.17/image and the loop runs up to 25
        # iterations per turn.
        if (rl_err := _check_image_rate_limit()) is not None:
            return rl_err

        try:
            if _provider_for(img_model) == "openai":
                image_bytes = await _openai_generate(prompt, img_model, params)
                return _finalize(
                    image_bytes, "", prompt, img_model, "gen",
                    "No image generated. The model may have refused the prompt.",
                )

            # Gemini
            from google.genai import types
            client = _get_gemini_client()
            response = await client.aio.models.generate_content(
                model=img_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )
            image_bytes, response_text = _extract_image_from_response(response)
            return _finalize(
                image_bytes, response_text, prompt, img_model, "gen",
                "No image generated. The model may have refused the prompt.",
            )

        except ImportError as e:
            pkg = "openai" if _provider_for(img_model) == "openai" else "google-genai"
            return json.dumps({"error": f"{pkg} package not installed ({e}). Run: pip install {pkg}"})
        except Exception as e:
            logger.error("Image generation failed (%s): %s", img_model, e)
            return json.dumps({"error": f"Image generation failed: {e}"})

    # ── edit_image ──────────────────────────────────────────

    async def edit_image(params: dict) -> str:
        """Edit the user's last sent image based on a text instruction."""
        prompt, img_model, err = _validate_params(
            params, "prompt is required — describe what to change",
        )
        if err:
            return err

        # Resolve the source image.
        #   source="avatar" → the FROZEN character (set once via set_avatar).
        #     Every slide reuses the SAME high-quality reference, so there's no
        #     generation-loss from re-transforming and the character stays
        #     consistent. This is the right way to make a multi-slide carousel.
        #   source="last" (default) → the user's most recent photo (in-context
        #     base64 block, else the last file in uploads/).
        avatar_path = images_dir.parent / "avatar.jpg"
        src = (params.get("source") or "last").lower()
        if src == "avatar":
            if not avatar_path.exists():
                return json.dumps({
                    "error": "No saved avatar yet. Generate the character once, then call "
                             "set_avatar (with its image_path) to freeze it — after that, "
                             "source='avatar' reuses it on every slide.",
                })
            source_bytes = avatar_path.read_bytes()
        else:
            source_bytes = _find_last_image_in_conversation(get_user_id)
            if not source_bytes:
                source_bytes = _latest_upload_bytes(images_dir.parent / "uploads")
            if not source_bytes:
                return json.dumps({
                    "error": "No image found. Ask the user to send the photo again.",
                })

        try:
            if _provider_for(img_model) == "openai":
                image_bytes = await _openai_edit(prompt, img_model, source_bytes, params)
                return _finalize(
                    image_bytes, "", prompt, img_model, "edit",
                    "Image editing failed. The model may have refused the request.",
                )

            # Gemini
            from google.genai import types
            from PIL import Image
            client = _get_gemini_client()
            pil_image = Image.open(BytesIO(source_bytes))
            response = await client.aio.models.generate_content(
                model=img_model,
                contents=[prompt, pil_image],
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )
            image_bytes, response_text = _extract_image_from_response(response)
            return _finalize(
                image_bytes, response_text, prompt, img_model, "edit",
                "Image editing failed. The model may have refused the request.",
            )

        except ImportError as e:
            pkg = "openai" if _provider_for(img_model) == "openai" else "google-genai or Pillow"
            return json.dumps({"error": f"{pkg} not installed ({e})."})
        except Exception as e:
            logger.error("Image editing failed (%s): %s", img_model, e)
            return json.dumps({"error": f"Image editing failed: {e}"})

    # ── set_avatar (freeze a reusable character) ────────────

    async def set_avatar(params: dict) -> str:
        """Freeze an image as the persistent character avatar for reuse.

        The whole point of a fixed avatar: create the character ONCE, then
        transform that single high-quality reference for each slide. Reusing
        one frozen source avoids both (a) re-cartoonifying the raw selfie every
        time (slight drift) and (b) chaining output→output (real quality loss).
        """
        avatar_path = images_dir.parent / "avatar.jpg"
        data: bytes | None = None
        path = params.get("image_path")
        if path:
            p = Path(path)
            if not p.is_absolute():
                p = images_dir.parent / path
            if p.exists() and p.is_file():
                data = p.read_bytes()
        if data is None:
            # No explicit path → freeze the user's most recent photo.
            data = (_find_last_image_in_conversation(get_user_id)
                    or _latest_upload_bytes(images_dir.parent / "uploads"))
        if not data:
            return json.dumps({
                "error": "No image to save. Pass image_path of the generated character, "
                         "or have the user send a photo first.",
            })
        try:
            avatar_path.parent.mkdir(parents=True, exist_ok=True)
            avatar_path.write_bytes(data)
            logger.info("Avatar frozen: %s (%d bytes)", avatar_path, len(data))
            return json.dumps({
                "status": "ok",
                "avatar_path": str(avatar_path),
                "size_bytes": len(data),
                "note": "Saved. Use edit_image with source='avatar' to reuse this character "
                        "on every slide — consistent and no quality loss.",
            })
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"Failed to save avatar: {e}"})

    # ── Register tools ──────────────────────────────────────

    # Build provider-aware parameter schema. Only expose models we can serve;
    # surface size/quality only when OpenAI is available (Gemini ignores them).
    _gen_props: dict = {
        "prompt": {
            "type": "string",
            "description": "Detailed text description of the image to generate.",
        },
        "model": {
            "type": "string",
            "description": f"Image model. Default: {default_model}.",
            "enum": sorted(available_models),
        },
    }
    if openai_api_key:
        _gen_props["size"] = {
            "type": "string",
            "description": "OpenAI gpt-image only. Default 1024x1024.",
            "enum": sorted(OPENAI_SIZES),
        }
        _gen_props["quality"] = {
            "type": "string",
            "description": "OpenAI gpt-image only. Default high.",
            "enum": sorted(OPENAI_QUALITIES),
        }

    _provider_note = []
    if openai_api_key:
        _provider_note.append("OpenAI gpt-image-2")
    if gemini_api_key:
        _provider_note.append("Gemini Nano Banana")
    _providers = " / ".join(_provider_note)

    registry.register(
        name="generate_image",
        description=(
            f"Generate a NEW image from a text description using AI ({_providers}). "
            "Use this when the user wants to CREATE an image from scratch "
            "(illustrations, mascots, posters, carousel slides, logos, etc)."
        ),
        parameters={
            "type": "object",
            "properties": _gen_props,
            "required": ["prompt"],
        },
        handler=generate_image,
        category="image",
        timeout=IMAGE_TOOL_TIMEOUT,
    )

    _edit_props: dict = {
        "prompt": {
            "type": "string",
            "description": "Text instruction describing how to edit the image (e.g. 'change background to mountains', 'make it black and white', 'add sunglasses').",
        },
        "model": {
            "type": "string",
            "description": f"Image model. Default: {default_model}.",
            "enum": sorted(available_models),
        },
        "source": {
            "type": "string",
            "description": (
                "Which image to transform. 'last' (default) = the user's most "
                "recent photo. 'avatar' = the frozen character saved via "
                "set_avatar — use this for every carousel slide so the same "
                "character is reused (consistent, no quality loss)."
            ),
            "enum": ["last", "avatar"],
        },
    }
    if openai_api_key:
        _edit_props["size"] = {
            "type": "string",
            "description": "OpenAI gpt-image only. Output size. Default 1024x1024.",
            "enum": sorted(OPENAI_SIZES),
        }

    registry.register(
        name="edit_image",
        description=(
            f"Edit the user's LAST SENT photo based on a text instruction using AI ({_providers}). "
            "Use this when the user sends a photo and asks to modify/change/edit it "
            "(e.g. 'make it sunset', 'remove the background', 'add a hat')."
        ),
        parameters={
            "type": "object",
            "properties": _edit_props,
            "required": ["prompt"],
        },
        handler=edit_image,
        category="image",
        timeout=IMAGE_TOOL_TIMEOUT,
    )

    registry.register(
        name="set_avatar",
        description=(
            "Freeze a reference character/avatar to reuse across many images. "
            "Workflow: generate the user's character ONCE (e.g. edit_image on their "
            "selfie → a cartoon avatar), then call set_avatar with that result's "
            "image_path. After that, use edit_image with source='avatar' for every "
            "carousel slide — the SAME character is reused with no quality loss. "
            "Without image_path it freezes the user's most recent photo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Path of the character image to freeze (from a prior "
                                   "generate_image/edit_image result). Omit to use the "
                                   "user's last sent photo.",
                },
            },
        },
        handler=set_avatar,
        category="image",
    )

    logger.info(
        "Image tools registered (providers: %s, default: %s)",
        _providers or "none", default_model,
    )
