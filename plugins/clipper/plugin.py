"""Clipper Plugin — long-form video → viral shorts.

Registers `clip_video` tool on the Qanot agent.

Usage from Telegram:
  User: "shu YouTube video-dan 5ta shorts qil: https://youtu.be/XYZ"
  Agent: calls clip_video(source="https://youtu.be/XYZ", count=5)
         returns list of rendered clips with virality scores, sends them as documents
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
# Fallback output dir if no workspace_dir is set
_FALLBACK_OUTPUT = PLUGIN_DIR / "output"

# The plugin loader removes our plugin dir from sys.path after setup().
# Re-add it permanently at module load so engine.X imports keep working
# when tool handlers run later.
_plugin_dir_str = str(PLUGIN_DIR)
if _plugin_dir_str not in sys.path:
    sys.path.insert(0, _plugin_dir_str)


class ClipperPlugin(Plugin):
    """Long-form → short-form video clipper plugin."""

    name = "clipper"
    description = "Clip long videos (podcasts, interviews) into viral shorts"
    version = "1.0.0"

    def __init__(self):
        self._provider: Any = None  # LLMProvider, set via setup
        self._elevenlabs_key: str | None = None
        self._config: dict = {}
        self._output_dir: Path = _FALLBACK_OUTPUT

    async def setup(self, config: dict) -> None:
        """Store config. Agent provider is resolved lazily at tool-call time
        because plugins load BEFORE the Agent instance is created.

        Expected config keys:
          - elevenlabs_key: optional, for Scribe transcription
          - output_dir: optional override for clip storage
          - workspace_dir: injected by qanot; clips go under {workspace_dir}/clipper/
          - public_url_base: for Meta Graph publishing
          - meta_graph_access_token, meta_ig_user_id, meta_fb_page_id: auto-post
        """
        self._config = config
        self._elevenlabs_key = config.get("elevenlabs_key")

        # Resolve output dir: explicit config > workspace_dir/clipper > plugin dir fallback
        out = config.get("output_dir")
        if not out:
            workspace = config.get("workspace_dir")
            if workspace:
                out = str(Path(workspace) / "clipper")
        if out:
            self._output_dir = Path(out)
        else:
            self._output_dir = _FALLBACK_OUTPUT
        self._output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Clipper plugin ready (elevenlabs=%s, output=%s)",
            bool(self._elevenlabs_key),
            self._output_dir,
        )

    def _get_provider(self):
        """Lazily resolve the Agent's LLM provider (Claude, etc.).

        We can't store this at setup() because plugins load before the Agent
        instance is created. Agent._instance is the main agent singleton.
        """
        if self._provider is not None:
            return self._provider
        try:
            from qanot.agent import Agent
            agent = getattr(Agent, "_instance", None)
            if agent is not None:
                self._provider = getattr(agent, "provider", None)
        except Exception as e:
            logger.warning("Failed to resolve provider: %s", e)
        return self._provider

    def get_tools(self) -> list[ToolDef]:
        return self._collect_tools()

    @tool(
        name="clip_video",
        description=(
            "Turn a long-form video (YouTube/TikTok/local/Telegram file) into viral short clips "
            "for Instagram Reels, TikTok, and YouTube Shorts. Uses AI to find the most engaging "
            "moments (hooks, punchlines, insights), then cuts and captions them automatically. "
            "Default language is Uzbek. Returns clip file paths + virality scores."
        ),
        parameters={
            "type": "object",
            "required": ["source"],
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Video source: YouTube URL, any http(s) video URL, or local file path",
                },
                "count": {
                    "type": "integer",
                    "description": "How many clips to extract (default 5, max 10)",
                },
                "min_duration": {
                    "type": "number",
                    "description": "Minimum clip length in seconds (default 30)",
                },
                "max_duration": {
                    "type": "number",
                    "description": "Maximum clip length in seconds (default 90)",
                },
                "language": {
                    "type": "string",
                    "description": "Source language (uz, ru, en, tr). Default: uz",
                },
                "caption_style": {
                    "type": "string",
                    "description": "Caption style: captions_ai | submagic | minimal | off. Default: captions_ai",
                },
                "reframe_mode": {
                    "type": "string",
                    "description": "9:16 reframe: center | smart | none. Default: center (smart needs MediaPipe)",
                },
                "virality_threshold": {
                    "type": "integer",
                    "description": "Drop clips scoring below this (0-99). Default: 60",
                },
            },
        },
    )
    async def clip_video(self, params: dict) -> str:
        """Handler for the clip_video tool."""
        from engine.pipeline import clip_video as run_pipeline

        provider = self._get_provider()
        if provider is None:
            return json.dumps({
                "error": "Clipper plugin not initialized — LLM provider unavailable",
            })

        source = (params.get("source") or "").strip()
        if not source:
            return json.dumps({"error": "source is required"})

        try:
            count = max(1, min(10, int(params.get("count", 5))))
        except (TypeError, ValueError):
            count = 5

        try:
            min_duration = float(params.get("min_duration", 30.0))
        except (TypeError, ValueError):
            min_duration = 30.0

        try:
            max_duration = float(params.get("max_duration", 90.0))
        except (TypeError, ValueError):
            max_duration = 90.0

        try:
            virality_threshold = int(params.get("virality_threshold", 60))
        except (TypeError, ValueError):
            virality_threshold = 60

        language = (params.get("language") or "uz").strip().lower()
        caption_style = (params.get("caption_style") or "captions_ai").strip().lower()
        reframe_mode = (params.get("reframe_mode") or "center").strip().lower()

        try:
            clips = await run_pipeline(
                source=source,
                provider=provider,
                count=count,
                min_duration_s=min_duration,
                max_duration_s=max_duration,
                language=language,
                caption_style=caption_style,
                reframe_mode=reframe_mode,
                output_dir=self._output_dir,
                elevenlabs_key=self._elevenlabs_key,
                virality_threshold=virality_threshold,
            )
        except Exception as e:
            logger.error("clip_video failed: %s", e, exc_info=True)
            return json.dumps({"error": f"Clipping failed: {e}"})

        if not clips:
            return json.dumps({
                "status": "no_clips",
                "message": "No viral moments found above threshold. Try lowering virality_threshold or check source content.",
            })

        result = {
            "status": "ok",
            "count": len(clips),
            "clips": [
                {
                    "path": str(c.path),
                    "thumbnail": str(c.thumbnail_path) if c.thumbnail_path else None,
                    "duration_s": round(c.moment.duration_s, 1),
                    "start_s": round(c.moment.start_s, 1),
                    "end_s": round(c.moment.end_s, 1),
                    "hook": c.moment.hook,
                    "title": c.moment.title,
                    "virality_score": c.moment.virality_score,
                    "rationale": c.moment.rationale,
                    "hashtags": c.moment.hashtags,
                }
                for c in clips
            ],
        }
        return json.dumps(result, ensure_ascii=False)

    @tool(
        name="clipper_health",
        description=(
            "Diagnose the clipper pipeline: checks yt-dlp version, PO-token provider reachability, "
            "cookies.txt presence, ffmpeg availability, and whether YouTube downloads currently work. "
            "Call this when clip_video fails to understand why."
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def clipper_health(self, params: dict) -> str:
        """Quick health probe of all external dependencies used by clip_video."""
        import shutil as _shutil
        import subprocess as _sub
        from engine.source import _probe_bgutil_provider, _find_cookies_file

        report: dict = {"ok": True, "checks": {}}

        # yt-dlp
        ytdlp = _shutil.which("yt-dlp")
        if ytdlp:
            try:
                ver = _sub.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
                report["checks"]["yt_dlp"] = {"ok": True, "version": ver}
            except Exception as e:
                report["checks"]["yt_dlp"] = {"ok": False, "error": str(e)}
                report["ok"] = False
        else:
            report["checks"]["yt_dlp"] = {"ok": False, "error": "binary not found"}
            report["ok"] = False

        # ffmpeg
        ffmpeg = _shutil.which("ffmpeg")
        report["checks"]["ffmpeg"] = {"ok": bool(ffmpeg), "path": ffmpeg or None}
        if not ffmpeg:
            report["ok"] = False

        # bgutil PO token sidecar
        pot_ok = await _probe_bgutil_provider()
        report["checks"]["po_token_provider"] = {
            "ok": pot_ok,
            "url": "http://127.0.0.1:4416",
            "hint": None if pot_ok else "Start: docker run -d --name bgutil-pot --restart unless-stopped --network host brainicism/bgutil-ytdlp-pot-provider",
        }

        # cookies.txt presence
        sources_dir = self._output_dir / "sources"
        cookies = _find_cookies_file(sources_dir)
        report["checks"]["cookies_txt"] = {
            "present": cookies is not None,
            "path": str(cookies) if cookies else None,
        }

        # End-to-end YouTube probe (small public video)
        try:
            from engine.source import _try_probe
            rc, meta, err = await _try_probe(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # classic public video
                ["--extractor-args", "youtube:player_client=default,-tv_simply"] if pot_ok
                else ["--extractor-args", "youtube:player_client=android"],
                timeout=30.0,
            )
            report["checks"]["youtube_probe"] = {
                "ok": meta is not None,
                "error": err[:200] if meta is None else None,
            }
            if meta is None:
                report["ok"] = False
        except Exception as e:
            report["checks"]["youtube_probe"] = {"ok": False, "error": str(e)}
            report["ok"] = False

        return json.dumps(report, ensure_ascii=False)

    @tool(
        name="publish_clip_to_meta",
        description=(
            "Publish a clip file to Instagram Reels and/or Facebook Reels via Meta Graph API. "
            "Requires META_GRAPH_ACCESS_TOKEN + META_IG_USER_ID (or META_FB_PAGE_ID) in env. "
            "The clip must be reachable via a public HTTPS URL — provide `public_url_base` "
            "or ensure an upload callback is configured. Returns per-platform success/failure."
        ),
        parameters={
            "type": "object",
            "required": ["clip_path", "caption"],
            "properties": {
                "clip_path": {
                    "type": "string",
                    "description": "Local file path to the MP4 clip (output of clip_video)",
                },
                "caption": {
                    "type": "string",
                    "description": "Caption with hashtags. Max 2200 chars for IG.",
                },
                "hashtags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional hashtags (no # prefix) — appended to caption",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["instagram", "facebook"]},
                    "description": "Target platforms. Default: [instagram]",
                },
                "public_url_base": {
                    "type": "string",
                    "description": "Public HTTPS base URL where clips are hosted (e.g. https://cdn.example.com/clips)",
                },
            },
        },
    )
    async def publish_clip_to_meta(self, params: dict) -> str:
        """Handler for the publish_clip_to_meta tool."""
        from engine.publisher import publish_clip, build_caption

        clip_path_str = (params.get("clip_path") or "").strip()
        if not clip_path_str:
            return json.dumps({"error": "clip_path is required"})
        clip_path = Path(clip_path_str)
        if not clip_path.exists():
            return json.dumps({"error": f"Clip file not found: {clip_path}"})

        caption = params.get("caption", "") or ""
        hashtags = params.get("hashtags") or []
        if not isinstance(hashtags, list):
            hashtags = []
        platforms = params.get("platforms") or ["instagram"]
        if not isinstance(platforms, list):
            platforms = ["instagram"]
        platforms_tuple = tuple(p for p in platforms if p in ("instagram", "facebook"))
        if not platforms_tuple:
            return json.dumps({"error": "platforms must be non-empty subset of [instagram, facebook]"})

        public_url_base = (
            params.get("public_url_base")
            or self._config.get("public_url_base")
        )

        try:
            results = await publish_clip(
                clip_path=clip_path,
                moment_hook=caption,
                moment_title="",
                hashtags=hashtags,
                public_url_base=public_url_base,
                access_token=self._config.get("meta_graph_access_token"),
                ig_user_id=self._config.get("meta_ig_user_id"),
                fb_page_id=self._config.get("meta_fb_page_id"),
                platforms=platforms_tuple,
            )
        except Exception as e:
            logger.error("publish_clip_to_meta failed: %s", e, exc_info=True)
            return json.dumps({"error": f"Publish failed: {e}"})

        return json.dumps({
            "status": "ok" if any(r.ok for r in results.values()) else "failed",
            "results": {
                platform: {
                    "ok": r.ok,
                    "media_id": r.media_id,
                    "permalink": r.permalink,
                    "error": r.error,
                }
                for platform, r in results.items()
            },
        }, ensure_ascii=False)
