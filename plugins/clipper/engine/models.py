"""Data models for the clipper pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Word:
    """A transcribed word with timing."""
    text: str
    start_s: float
    end_s: float
    speaker: str | None = None
    confidence: float = 1.0


@dataclass
class Segment:
    """A transcript segment (usually sentence-level)."""
    text: str
    start_s: float
    end_s: float
    words: list[Word] = field(default_factory=list)
    speaker: str | None = None


@dataclass
class Transcript:
    """Full transcript of a source video."""
    language: str
    duration_s: float
    segments: list[Segment] = field(default_factory=list)

    @property
    def words(self) -> list[Word]:
        """Flatten to word list."""
        out: list[Word] = []
        for s in self.segments:
            out.extend(s.words)
        return out

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def words_in_range(self, start: float, end: float) -> list[Word]:
        """Return words whose midpoint falls in [start, end]."""
        return [
            w for w in self.words
            if start <= (w.start_s + w.end_s) / 2 <= end
        ]


@dataclass
class Moment:
    """A viral-moment candidate."""
    start_s: float
    end_s: float
    hook: str
    virality_score: int  # 0-99
    rationale: str
    title: str = ""
    hashtags: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class Clip:
    """A final rendered clip."""
    path: Path
    moment: Moment
    words: list[Word] = field(default_factory=list)
    thumbnail_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceMedia:
    """A loaded source video."""
    path: Path
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool
    original_url: str | None = None
    title: str | None = None

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width

    @property
    def aspect_ratio(self) -> float:
        return self.width / max(self.height, 1)


@dataclass
class ClipperConfig:
    """Configuration for a clipping run."""
    count: int = 5
    min_duration_s: float = 30.0
    max_duration_s: float = 90.0
    language: str = "uz"  # whisper language code
    caption_style: str = "captions_ai"  # captions_ai | submagic | minimal | off
    reframe_mode: str = "center"  # center | smart | none
    target_width: int = 1080
    target_height: int = 1920  # 9:16
    add_hook_overlay: bool = True
    output_dir: Path = field(default_factory=lambda: Path("output"))
    transcribe_provider: str = "faster-whisper"  # faster-whisper | elevenlabs
    whisper_model: str = "large-v3"
    whisper_compute_type: str = "int8"  # int8 | float16 | float32
    align_words: bool = True  # use whisperx alignment for word-level accuracy
    diarize: bool = False
    virality_threshold: int = 60  # drop clips below this score
