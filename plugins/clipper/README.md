# Qanot Clipper Plugin

Long-form video → short-form viral clips. Takes YouTube videos, podcasts, interviews, or any long-form content and automatically produces platform-ready shorts for TikTok, Instagram Reels, and YouTube Shorts.

## Features

- **Source flexibility**: YouTube URLs, any http(s) video, local files, Telegram uploads
- **Word-level transcription**: faster-whisper large-v3 (local) or ElevenLabs Scribe (paid)
- **LLM viral detection**: Claude analyzes transcript, ranks moments 0-99 on hook/emotion/value/completeness
- **Sentence-boundary snapping**: never cuts mid-sentence
- **Smart 9:16 reframe**: MediaPipe face tracking + YOLOv8 fallback (Phase 2)
- **Animated captions**: Captions.ai or Submagic style word-by-word highlighting
- **Multilingual**: Uzbek, Russian, English, Turkish out of the box

## Installation

Core deps (required):
```bash
pip install yt-dlp faster-whisper ffmpeg-python pillow pydantic
```

Optional — smart reframe (Phase 2):
```bash
pip install mediapipe opencv-python ultralytics
```

Optional — ElevenLabs Scribe (higher accuracy for Uzbek):
```bash
pip install aiohttp
# Set ELEVENLABS_API_KEY env var or pass via config
```

System deps:
- `ffmpeg` (for all video operations)
- `yt-dlp` binary (for URL downloads)

## Usage

### Via agent conversation

```
User: "Shu YouTube video-dan 5ta qisqa video qil: https://youtu.be/abc"
Bot:  [calls clip_video tool → returns clips with virality scores]
      [sends each clip as document via send_file]
```

### Direct API

```python
from plugins.clipper.engine.pipeline import clip_video
from qanot.providers.anthropic import AnthropicProvider

provider = AnthropicProvider(api_key="...", model="claude-sonnet-4-6")
clips = await clip_video(
    source="https://youtu.be/abc123",
    provider=provider,
    count=5,
    language="uz",
    caption_style="captions_ai",
    reframe_mode="center",
)
for c in clips:
    print(f"Score {c.moment.virality_score}: {c.moment.hook}")
    print(f"  → {c.path}")
```

### Stateful pipeline (re-run stages)

```python
from plugins.clipper.engine.pipeline import ClipperPipeline
from plugins.clipper.engine.models import ClipperConfig

pipeline = ClipperPipeline(provider, ClipperConfig(count=3))
await pipeline.load("video.mp4")
await pipeline.transcribe()
await pipeline.detect()
# At this point you can inspect pipeline.moments and tweak before rendering
await pipeline.render()
```

## Pipeline stages

1. **Source loader** (`source.py`): yt-dlp for URLs, ffprobe for metadata, audio extraction
2. **Transcribe** (`transcribe.py`): faster-whisper with word timestamps, optional WhisperX alignment
3. **Moment detection** (`moments.py`): LLM with Pydantic schema, sentence-boundary snapping
4. **Cutter** (`cutter.py`): ffmpeg accurate seek + 9:16 reframe
5. **Captions** (`captions.py`): Pillow word-by-word PNG overlays + ffmpeg overlay filter
6. **Orchestrator** (`pipeline.py`): stitches it all together

## Design decisions

**Why plugin, not core?**
- Heavy deps (faster-whisper ~1GB, MediaPipe, YOLOv8) shouldn't bloat base qanot
- Users who don't clip videos shouldn't pay the dependency cost
- Matches existing plugin pattern (`plugins/reels/`, `plugins/cloud_reporter/`)

**Why faster-whisper over OpenAI Whisper API?**
- No per-minute API cost (matters at scale)
- Local GPU/CPU inference
- Same accuracy as upstream Whisper with 4x speed

**Why Pillow over Remotion?**
- No Node dependency — pure Python toolchain
- Cyrillic/Uzbek text rendering works out of the box with Montserrat/DejaVu

**Why overlay filter over MoviePy?**
- MoviePy loads entire video into memory — fails on long sources
- ffmpeg overlay streams — handles multi-hour sources

## Roadmap

- [x] Phase 1: MVP end-to-end (source → transcribe → detect → cut → caption)
- [ ] Phase 2a: MediaPipe face tracking for smart reframe
- [ ] Phase 2b: YOLOv8 fallback for no-face frames
- [ ] Phase 2c: Pyannote speaker diarization for podcast mode
- [ ] Phase 2d: Hook overlay text on first 3 seconds
- [ ] Phase 3: Meta Graph auto-post integration
- [ ] Phase 4: Batch mode + cron-driven nightly clipping
