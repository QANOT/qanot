# Clipper Plugin Tools

## clip_video

Turn a long-form video into 5-10 viral short clips optimized for TikTok, Instagram Reels, and YouTube Shorts.

**When to use:**
- User sends a YouTube URL and asks for shorts: "shu videodan shorts qil"
- User uploads a long video (podcast, interview, lecture) to Telegram
- User wants to repurpose existing long-form content
- User mentions words like "shorts", "reels", "clips", "viral", "qisqa video"

**What it does:**
1. Downloads source (if URL) or loads local file
2. Transcribes audio with word-level timestamps (faster-whisper or ElevenLabs Scribe)
3. LLM finds 5-10 viral moments ranked 0-99 on hook strength, emotion, value, completeness
4. Cuts each moment, reframes to 9:16 (center-crop or smart face-tracking)
5. Burns word-by-word animated captions (Captions.ai or Submagic style)
6. Returns clip paths + metadata

**Parameters:**
- `source` (required): YouTube URL, any https video URL, or local file path
- `count` (default 5, max 10): number of clips to extract
- `min_duration` (default 30): minimum clip length in seconds
- `max_duration` (default 90): maximum clip length in seconds
- `language` (default "uz"): source language — uz, ru, en, tr
- `caption_style` (default "captions_ai"): captions_ai | submagic | minimal | off
- `reframe_mode` (default "center"): center | smart | none
- `virality_threshold` (default 60): minimum virality score to include (0-99)

**Typical usage:**
```
User: "Shu YouTube video-dan 5ta shorts qil: https://youtu.be/abc123"
→ clip_video(source="https://youtu.be/abc123", count=5, language="uz")

User: "podcast.mp4 fayldan eng qiziq 3 ta qisqa video kerak"
→ clip_video(source="/path/to/podcast.mp4", count=3)
```

**After calling:**
Send each clip as a Telegram document using the `send_file` tool, with the
hook text as caption. Include virality_score in the message so the user
understands ranking. Suggest hashtags in Uzbek where appropriate.
