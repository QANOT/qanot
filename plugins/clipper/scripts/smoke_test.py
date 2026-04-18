"""Manual smoke test for the clipper pipeline.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python3 plugins/clipper/scripts/smoke_test.py SOURCE [--count 3]

Where SOURCE is a YouTube URL or local MP4 path.

Requires the optional deps:
    pip install yt-dlp faster-whisper ffmpeg-python pillow pydantic anthropic
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def _setup_paths() -> None:
    """Add repo root + plugin dir to sys.path for imports."""
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    plugin_dir = repo_root / "plugins" / "clipper"
    for p in (str(repo_root), str(plugin_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)


_setup_paths()


async def main():
    parser = argparse.ArgumentParser(description="Clipper smoke test")
    parser.add_argument("source", help="YouTube URL or local video path")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--min-duration", type=float, default=30.0)
    parser.add_argument("--max-duration", type=float, default=90.0)
    parser.add_argument("--language", default="uz")
    parser.add_argument("--caption-style", default="captions_ai")
    parser.add_argument("--reframe", default="center", choices=["center", "smart", "none"])
    parser.add_argument("--no-hook", action="store_true")
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--virality-threshold", type=int, default=50)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Build Claude provider from env
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    from qanot.providers.anthropic import AnthropicProvider
    provider = AnthropicProvider(api_key=api_key, model="claude-sonnet-4-6")

    from engine.pipeline import clip_video  # noqa: E402

    print(f"\n=== Clipper smoke test ===")
    print(f"Source:     {args.source}")
    print(f"Count:      {args.count}")
    print(f"Duration:   {args.min_duration}-{args.max_duration}s")
    print(f"Language:   {args.language}")
    print(f"Captions:   {args.caption_style}")
    print(f"Reframe:    {args.reframe}")
    print(f"Hook:       {'on' if not args.no_hook else 'off'}")
    print(f"Diarize:    {'on' if args.diarize else 'off'}")
    print(f"Threshold:  {args.virality_threshold}")
    print()

    clips = await clip_video(
        source=args.source,
        provider=provider,
        count=args.count,
        min_duration_s=args.min_duration,
        max_duration_s=args.max_duration,
        language=args.language,
        caption_style=args.caption_style,
        reframe_mode=args.reframe,
        add_hook_overlay=not args.no_hook,
        diarize=args.diarize,
        output_dir=args.output_dir,
        virality_threshold=args.virality_threshold,
        elevenlabs_key=os.environ.get("ELEVENLABS_API_KEY"),
        hf_token=os.environ.get("HUGGINGFACE_TOKEN"),
    )

    print(f"\n=== Results: {len(clips)} clips ===")
    for i, c in enumerate(clips, 1):
        print(f"\n[{i}] Score {c.moment.virality_score} | {c.moment.duration_s:.0f}s")
        print(f"    Hook: {c.moment.hook}")
        print(f"    Title: {c.moment.title}")
        if c.moment.hashtags:
            print(f"    Tags: {' '.join(f'#{t}' for t in c.moment.hashtags)}")
        print(f"    Time: {c.moment.start_s:.0f}s - {c.moment.end_s:.0f}s")
        print(f"    File: {c.path}")


if __name__ == "__main__":
    asyncio.run(main())
