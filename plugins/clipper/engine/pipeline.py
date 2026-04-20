"""End-to-end clipper pipeline orchestrator.

Input: source video (URL / path / Telegram file)
Output: list of rendered short clips with metadata

Stages:
  1. load_source   → SourceMedia
  2. transcribe    → Transcript (word-level timestamps)
  3. detect_moments → list[Moment] (ranked by virality)
  4. cut + reframe → clip MP4 per moment
  5. burn captions → final MP4 per moment
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from engine.captions import burn_captions, clip_local_words
from engine.cutter import cut_clip, extract_thumbnail
from engine.hook import burn_hook_overlay
from engine.jumpcut import (
    _sentence_boundary_times,
    apply_jumpcut,
    compute_keep_segments,
    remap_words,
    total_kept_duration,
)
from engine.models import (
    Clip,
    ClipperConfig,
    Moment,
    Segment,
    SourceMedia,
    Transcript,
)
from engine.moments import detect_moments, snap_to_sentence_boundary
from engine.source import load_source
from engine.transcribe import transcribe

logger = logging.getLogger(__name__)


class ClipperPipeline:
    """Stateful pipeline runner.

    Holds the source + transcript across stages so users can re-run moment detection
    or caption burning without re-downloading/re-transcribing.
    """

    def __init__(
        self,
        provider: Any,
        config: ClipperConfig,
        elevenlabs_key: str | None = None,
        hf_token: str | None = None,
    ):
        """Args:
            provider: Qanot LLMProvider (e.g. AnthropicProvider) with .chat() method.
            config: ClipperConfig with all pipeline settings.
            elevenlabs_key: Optional ElevenLabs API key for Scribe transcription.
            hf_token: Optional HuggingFace token for pyannote diarization.
        """
        self.provider = provider
        self.config = config
        self.elevenlabs_key = elevenlabs_key
        self.hf_token = hf_token
        self.source: SourceMedia | None = None
        self.transcript: Transcript | None = None
        self.moments: list[Moment] = []
        self.clips: list[Clip] = []

    async def load(self, source: str | Path) -> SourceMedia:
        """Stage 1: download/load source video."""
        t0 = time.monotonic()
        source_dir = self.config.output_dir / "sources"
        self.source = await load_source(source, source_dir)
        logger.info(
            "[1/5] Source loaded: %s (%.0fs, %dx%d, %.1ffps) in %.1fs",
            self.source.path.name,
            self.source.duration_s,
            self.source.width,
            self.source.height,
            self.source.fps,
            time.monotonic() - t0,
        )
        return self.source

    async def transcribe(self) -> Transcript:
        """Stage 2: transcribe audio with word-level timestamps."""
        if self.source is None:
            raise RuntimeError("Call load() first")
        t0 = time.monotonic()
        work_dir = self.config.output_dir / "sources"
        self.transcript = await transcribe(
            self.source,
            provider=self.config.transcribe_provider,
            model_name=self.config.whisper_model,
            compute_type=self.config.whisper_compute_type,
            language=self.config.language,
            align=self.config.align_words,
            elevenlabs_key=self.elevenlabs_key,
            work_dir=work_dir,
        )
        total_words = len(self.transcript.words)
        logger.info(
            "[2/5] Transcribed: %d segments, %d words, language=%s in %.1fs",
            len(self.transcript.segments), total_words, self.transcript.language,
            time.monotonic() - t0,
        )

        # Optional diarization pass
        if self.config.diarize:
            from engine.diarize import diarize, summarize_speakers
            audio_path = self.config.output_dir / "sources" / f"{self.source.path.stem}.wav"
            if audio_path.exists():
                t1 = time.monotonic()
                self.transcript = await diarize(
                    self.transcript, audio_path, hf_token=self.hf_token,
                )
                speakers = summarize_speakers(self.transcript)
                logger.info(
                    "[2b/5] Diarized in %.1fs — speakers: %s",
                    time.monotonic() - t1,
                    {k: f"{v:.0f}s" for k, v in speakers.items()},
                )

        return self.transcript

    async def detect(self) -> list[Moment]:
        """Stage 3: LLM finds viral moments."""
        if self.transcript is None:
            raise RuntimeError("Call transcribe() first")
        t0 = time.monotonic()
        moments = await detect_moments(
            self.transcript,
            self.provider,
            count=self.config.count,
            min_duration_s=self.config.min_duration_s,
            max_duration_s=self.config.max_duration_s,
            language=self.config.language,
            virality_threshold=self.config.virality_threshold,
        )
        # Snap to sentence boundaries to avoid mid-sentence cuts
        self.moments = [snap_to_sentence_boundary(m, self.transcript) for m in moments]
        logger.info(
            "[3/5] Detected %d moments (scores: %s) in %.1fs",
            len(self.moments),
            [m.virality_score for m in self.moments],
            time.monotonic() - t0,
        )
        return self.moments

    async def render(self) -> list[Clip]:
        """Stage 4+5: cut + reframe + burn captions for each moment."""
        if self.source is None or self.transcript is None:
            raise RuntimeError("Call load() and transcribe() first")
        if not self.moments:
            logger.warning("No moments to render")
            return []

        t0 = time.monotonic()
        clips_dir = self.config.output_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        self.clips = []
        for i, moment in enumerate(self.moments, start=1):
            clip_t0 = time.monotonic()
            stem = f"clip_{i:02d}_score{moment.virality_score}"
            cut_path = clips_dir / f"{stem}_raw.mp4"
            jumpcut_path = clips_dir / f"{stem}_jumpcut.mp4"
            hooked_path = clips_dir / f"{stem}_hooked.mp4"
            final_path = clips_dir / f"{stem}.mp4"
            thumb_path = clips_dir / f"{stem}.jpg"

            # Stage 4: cut + reframe
            try:
                await cut_clip(
                    self.source, moment, cut_path,
                    target_width=self.config.target_width,
                    target_height=self.config.target_height,
                    reframe_mode=self.config.reframe_mode,
                )
            except Exception as e:
                logger.error("Failed to cut clip %d: %s", i, e)
                continue

            # Words local to this clip (relative to 0 = moment.start_s).
            local_words = clip_local_words(
                self.transcript.words, moment.start_s, moment.end_s
            )
            clip_duration = max(0.0, moment.end_s - moment.start_s)

            # Stage 4a: jump-cut (silence compression, not deletion).
            # Pauses longer than long_gap_threshold_s are TRIMMED down to a
            # natural target length — not removed entirely. Keeps meaning
            # intact while eliminating dead air.
            stage_input = cut_path
            if self.config.jumpcut and local_words:
                try:
                    # Build sentence-boundary times in clip-local seconds.
                    clip_local_segments = [
                        Segment(
                            text=seg.text,
                            start_s=seg.start_s - moment.start_s,
                            end_s=seg.end_s - moment.start_s,
                            words=[
                                replace(w, start_s=w.start_s - moment.start_s,
                                        end_s=w.end_s - moment.start_s)
                                for w in seg.words
                            ],
                            speaker=seg.speaker,
                        )
                        for seg in self.transcript.segments
                        if seg.end_s > moment.start_s and seg.start_s < moment.end_s
                    ]
                    boundary_times = _sentence_boundary_times(clip_local_segments)

                    keep_segments = compute_keep_segments(
                        local_words,
                        clip_duration,
                        long_gap_threshold_s=self.config.long_gap_threshold_s,
                        target_mid_sentence_gap_s=self.config.target_mid_sentence_gap_s,
                        target_sentence_boundary_gap_s=self.config.target_sentence_boundary_gap_s,
                        sentence_boundary_times=boundary_times,
                    )
                    removed = clip_duration - total_kept_duration(keep_segments)
                    if removed > 0.2 and len(keep_segments) >= 1:
                        await apply_jumpcut(
                            cut_path, jumpcut_path, keep_segments,
                            has_audio=self.source.has_audio,
                        )
                        local_words = remap_words(local_words, keep_segments)
                        try:
                            cut_path.unlink()
                        except OSError:
                            pass
                        stage_input = jumpcut_path
                        logger.info(
                            "Clip %d jumpcut: %d segments kept, %.1fs removed (%.0f%%)",
                            i, len(keep_segments), removed,
                            100.0 * removed / max(clip_duration, 0.001),
                        )
                    else:
                        logger.info(
                            "Clip %d jumpcut: nothing to trim (removed=%.2fs)",
                            i, removed,
                        )
                except Exception as e:
                    logger.warning(
                        "Jumpcut failed for clip %d (keeping raw): %s", i, e,
                    )

            # Stage 4b: hook overlay (optional)
            if self.config.add_hook_overlay and moment.hook:
                try:
                    await burn_hook_overlay(
                        stage_input, moment.hook, hooked_path,
                        canvas_width=self.config.target_width,
                        canvas_height=self.config.target_height,
                    )
                    try:
                        stage_input.unlink()
                    except OSError:
                        pass
                    stage_input = hooked_path
                except Exception as e:
                    logger.warning("Hook overlay failed for clip %d (skipping hook): %s", i, e)

            # Stage 5: burn captions
            if self.config.caption_style != "off" and local_words:
                try:
                    await burn_captions(
                        stage_input, local_words, final_path,
                        style_name=self.config.caption_style,
                        canvas_width=self.config.target_width,
                        canvas_height=self.config.target_height,
                    )
                    try:
                        stage_input.unlink()
                    except OSError:
                        pass
                except Exception as e:
                    logger.warning("Caption burn failed for clip %d (keeping hook/raw): %s", i, e)
                    stage_input.rename(final_path)
            else:
                stage_input.rename(final_path)

            # Thumbnail (midpoint frame from source, not clip, for quality)
            try:
                await extract_thumbnail(self.source, moment, thumb_path)
            except Exception as e:
                logger.debug("Thumbnail extraction failed (non-fatal): %s", e)
                thumb_path = None

            self.clips.append(Clip(
                path=final_path,
                moment=moment,
                words=local_words,
                thumbnail_path=thumb_path,
                metadata={
                    "source": str(self.source.path),
                    "source_url": self.source.original_url,
                    "source_title": self.source.title,
                    "reframe_mode": self.config.reframe_mode,
                    "caption_style": self.config.caption_style,
                },
            ))

            logger.info(
                "[4-5/5] Clip %d/%d rendered: %s (%.1fs)",
                i, len(self.moments), final_path.name, time.monotonic() - clip_t0,
            )

        logger.info("Rendered %d clips in %.1fs total", len(self.clips), time.monotonic() - t0)
        return self.clips

    async def run(self, source: str | Path) -> list[Clip]:
        """Run the full pipeline end-to-end."""
        await self.load(source)
        await self.transcribe()
        await self.detect()
        return await self.render()


async def clip_video(
    source: str | Path,
    provider: Any,
    *,
    count: int = 5,
    min_duration_s: float = 30.0,
    max_duration_s: float = 90.0,
    language: str = "uz",
    caption_style: str = "off",
    reframe_mode: str = "blur_pad",
    add_hook_overlay: bool = False,
    jumpcut: bool = True,
    long_gap_threshold_s: float = 0.5,
    target_mid_sentence_gap_s: float = 0.25,
    target_sentence_boundary_gap_s: float = 0.45,
    diarize: bool = False,
    output_dir: Path | None = None,
    elevenlabs_key: str | None = None,
    hf_token: str | None = None,
    virality_threshold: int = 60,
    transcribe_provider: str = "faster-whisper",
) -> list[Clip]:
    """One-shot API: source → clips."""
    config = ClipperConfig(
        count=count,
        min_duration_s=min_duration_s,
        max_duration_s=max_duration_s,
        language=language,
        caption_style=caption_style,
        reframe_mode=reframe_mode,
        add_hook_overlay=add_hook_overlay,
        jumpcut=jumpcut,
        long_gap_threshold_s=long_gap_threshold_s,
        target_mid_sentence_gap_s=target_mid_sentence_gap_s,
        target_sentence_boundary_gap_s=target_sentence_boundary_gap_s,
        diarize=diarize,
        output_dir=output_dir or (Path(__file__).parent.parent / "output"),
        virality_threshold=virality_threshold,
        transcribe_provider=transcribe_provider,
    )
    pipeline = ClipperPipeline(provider, config, elevenlabs_key=elevenlabs_key, hf_token=hf_token)
    return await pipeline.run(source)
