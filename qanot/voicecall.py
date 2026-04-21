"""Telegram Voice Chat AI Bot — real-time voice conversation via py-tgcalls.

Joins Telegram group voice chats (or P2P calls) as a userbot participant.
Listens to speech, transcribes via STT, processes through the agent loop,
responds with TTS, and plays audio back — all in real-time.

Requires: pip install py-tgcalls pyrogram numpy torch
Config: voicecall_enabled: true + api_id/api_hash/session from my.telegram.org
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from qanot.audio import (
    pcm_to_wav_file,
    resample_48k_stereo_to_16k_mono,
)
from qanot.vad import CHUNK_BYTES, SileroVAD, VADEvent

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)

# Minimum speech bytes to process (250ms at 16kHz mono 16-bit)
MIN_SPEECH_BYTES = 16000 * 2 * 250 // 1000  # 8000 bytes

# Playback frame pacing: 10ms per frame (WebRTC / ntgcalls standard).
# Must match qanot.audio.VC_FRAME_DURATION_MS.
PLAYBACK_INTERVAL = 0.01  # seconds

# Rate limit: minimum seconds between processing speech segments
MIN_TURN_INTERVAL = 2.0

# Characters/patterns that make TTS providers (notably KotibAI) return 400.
# We strip markdown formatting and emoji/symbol ranges that aren't speakable.
_MARKDOWN_RE = re.compile(r"(\*\*|__|\*|_|`+|~~|#+\s|>\s|\[|\]|\(|\)|\|)")
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF]"   # pictographs, emoticons, symbols
    r"|[\U00002600-\U000027BF]"  # misc symbols, dingbats
    r"|[\U0001F1E6-\U0001F1FF]"  # flags
    r"|[←-⇿]"           # arrows
    r"|[☀-⛿]"           # misc symbols
)
_WHITESPACE_RE = re.compile(r"\s+")


async def _write_tts_audio_to_temp(tts_result) -> str | None:
    """Persist TTS output to a WAV temp file ready for ntgcalls playback.

    Runs the provider audio through a single ffmpeg pass that:
    - pads 150ms of silence at the head and 250ms at the tail, so the
      stream-switch click from ntgcalls lands on silence instead of
      speech onset / mid-word tail
    - applies a 30ms fade-in and 80ms fade-out to blend the transitions
    - normalises to 48kHz stereo s16le (ntgcalls' native voice chat
      format — zero resampling cost at playback time)

    Handles both audio_data bytes (Muxlisa/Aisha) and audio_url (Kotib).
    Returns the absolute path or None on failure.
    """
    from qanot.voice import download_audio

    source_path: str | None = None
    source_is_temp = False
    try:
        if tts_result.audio_data:
            fd, source_path = tempfile.mkstemp(prefix="qanot_tts_src_", suffix=".bin")
            with os.fdopen(fd, "wb") as f:
                f.write(tts_result.audio_data)
            source_is_temp = True
        elif tts_result.audio_url:
            source_path = await download_audio(tts_result.audio_url)
            source_is_temp = True
        if not source_path:
            return None

        fd, out_path = tempfile.mkstemp(prefix="qanot_tts_", suffix=".wav")
        os.close(fd)
        # adelay adds 150ms silence at head (per-channel), apad tacks on
        # 250ms at tail, afade ramps the first 30ms smoothly in. The
        # trailing silence keeps the stream-end click on silence.
        filter_chain = (
            "adelay=150|150,"
            "apad=pad_dur=0.25,"
            "afade=t=in:st=0:d=0.03"
        )
        # Let ffmpeg detect the input format (WAV/MP3/etc) automatically.
        result = await asyncio.to_thread(
            __import__("subprocess").run,
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", source_path,
                "-af", filter_chain,
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                out_path,
            ],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning(
                "ffmpeg TTS polish failed: %s",
                result.stderr[-300:].decode("utf-8", "replace"),
            )
            os.unlink(out_path)
            return None
        return out_path
    finally:
        if source_is_temp and source_path:
            try:
                os.unlink(source_path)
            except OSError:
                pass


def sanitize_for_tts(text: str) -> str:
    """Strip markdown syntax, emoji, and collapse whitespace for TTS input.

    Kotib/Muxlisa/Aisha reject or mis-speak raw model output containing
    **bold**, `code`, pipe tables, or non-speakable pictographs. The
    agent thinks it's answering in chat markdown; the TTS needs plain
    speakable text.
    """
    text = _EMOJI_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


@dataclass
class CallSession:
    """State for one active voice call."""

    chat_id: int
    user_id: int
    conv_key: str
    started_at: float = field(default_factory=time.monotonic)
    last_speech_at: float = field(default_factory=time.monotonic)
    is_speaking: bool = False  # True when bot TTS is playing
    _tts_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    _pending_tempfiles: list[str] = field(default_factory=list, repr=False)


class AudioPipeline:
    """Real-time audio processing pipeline for a single call.

    Handles: inbound PCM → VAD → STT → LLM → TTS → outbound PCM.
    Runs entirely within the asyncio event loop.
    """

    def __init__(
        self,
        manager: VoiceCallManager,
        session: CallSession,
        vad: SileroVAD,
        agent: Agent,
        config: Config,
    ) -> None:
        self._manager = manager
        self._session = session
        self._vad = vad
        self._agent = agent
        self._config = config

        # Speech accumulation buffer (16kHz mono PCM)
        self._speech_buffer = bytearray()
        # VAD chunk accumulation buffer (need 512 samples = 1024 bytes)
        self._vad_buffer = bytearray()

        self._last_turn_time: float = 0.0
        self._tempfile_cleanup_task: asyncio.Task | None = None

        # Turn concurrency control. Only one _process_speech runs at a
        # time per call. A new SPEECH_END cancels the previous turn so
        # the user isn't heard twice and playback doesn't collide.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._current_turn_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background tasks for this pipeline."""
        self._tempfile_cleanup_task = asyncio.create_task(
            self._tempfile_cleanup_loop(),
            name=f"vc_tempfile_cleanup_{self._session.chat_id}",
        )

    async def stop(self) -> None:
        """Stop pipeline and cleanup."""
        if self._current_turn_task and not self._current_turn_task.done():
            self._current_turn_task.cancel()
        if self._tempfile_cleanup_task and not self._tempfile_cleanup_task.done():
            self._tempfile_cleanup_task.cancel()
        self.cancel_playback()
        self._vad.reset()
        # Flush any remaining temp files
        for path in list(self._session._pending_tempfiles):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._session._pending_tempfiles.clear()

    def feed_inbound(self, pcm_48k_stereo: bytes) -> None:
        """Feed raw PCM from voice chat. Called from py-tgcalls frame handler.

        Resamples to 16kHz mono, processes through VAD, accumulates speech.
        Must be fast and non-blocking (called from audio thread).
        """
        # Resample 48kHz stereo → 16kHz mono
        pcm_16k = resample_48k_stereo_to_16k_mono(pcm_48k_stereo)
        if not pcm_16k:
            return

        # Accumulate into VAD buffer
        self._vad_buffer.extend(pcm_16k)

        # Diagnostic: log the audio amplitude every ~1s so we can see
        # whether the PCM reaching VAD is actually speech (non-zero)
        # vs silence/noise (~zero). Compute abs-max of int16 samples.
        self._feed_calls = getattr(self, "_feed_calls", 0) + 1
        if self._feed_calls % 50 == 0:
            try:
                import numpy as _np
                samples = _np.frombuffer(pcm_16k, dtype=_np.int16)
                amp = int(_np.abs(samples).max()) if samples.size else 0
                logger.info(
                    "voicecall: feed_inbound %d | input=%dB 16k=%dB amp=%d (int16 max 32767)",
                    self._feed_calls, len(pcm_48k_stereo), len(pcm_16k), amp,
                )
            except Exception:
                pass

        # Process complete VAD chunks (512 samples = 1024 bytes each)
        while len(self._vad_buffer) >= CHUNK_BYTES:
            chunk = bytes(self._vad_buffer[:CHUNK_BYTES])
            del self._vad_buffer[:CHUNK_BYTES]

            event = self._vad.process_chunk(chunk)

            if event == VADEvent.SPEECH_START:
                logger.info(
                    "voicecall: SPEECH_START in chat %d", self._session.chat_id,
                )
                self._session.last_speech_at = time.monotonic()
                # Barge-in: if bot is speaking OR still processing the
                # previous turn, cancel everything so we commit to the
                # new utterance without overlap.
                if self._config.voicecall_barge_in:
                    self._barge_in()
                # Start accumulating speech
                self._speech_buffer = bytearray()

            # Accumulate audio during speech
            if self._vad.is_speech_active:
                self._speech_buffer.extend(chunk)

            if event == VADEvent.SPEECH_END:
                speech_data = bytes(self._speech_buffer)
                self._speech_buffer = bytearray()

                # Check minimum speech length and rate limiting
                now = time.monotonic()
                meets_min = len(speech_data) >= MIN_SPEECH_BYTES
                meets_rate = now - self._last_turn_time >= MIN_TURN_INTERVAL
                logger.info(
                    "voicecall: SPEECH_END in chat %d — %d bytes "
                    "(min=%s, rate_ok=%s)",
                    self._session.chat_id, len(speech_data),
                    meets_min, meets_rate,
                )
                if meets_min and meets_rate:
                    self._last_turn_time = now
                    self._dispatch_turn(speech_data)

    def _barge_in(self) -> None:
        """Cancel any in-flight turn and stop current playback.

        Called from the audio thread when new speech is detected while
        the bot is still speaking or processing. Cooperatively cancels
        the pending STT/LLM/TTS chain and pauses playback immediately.
        """
        self._session._tts_cancel.set()
        task = self._current_turn_task
        if task is not None and not task.done():
            task.cancel()
        if self._session.is_speaking:
            logger.info("Barge-in: pausing playback in chat %d",
                        self._session.chat_id)
            self.cancel_playback()
        self._session.is_speaking = False

    def _dispatch_turn(self, speech_data: bytes) -> None:
        """Schedule a new turn. Replaces any in-flight turn atomically."""
        prev = self._current_turn_task
        if prev is not None and not prev.done():
            prev.cancel()
        self._current_turn_task = asyncio.create_task(
            self._run_turn(speech_data),
            name=f"vc_turn_{self._session.chat_id}",
        )

    async def _run_turn(self, pcm_16k_mono: bytes) -> None:
        """Serialise turns through the turn lock. Waits for any prior
        turn to finish its cancellation teardown before running."""
        try:
            async with self._turn_lock:
                # Clear the cancel flag now that we own the turn — any
                # prior barge-in set() is stale once we've acquired the
                # lock (the prior task is done).
                self._session._tts_cancel.clear()
                await self._process_speech(pcm_16k_mono)
        except asyncio.CancelledError:
            logger.info("Turn cancelled in chat %d (barge-in)",
                        self._session.chat_id)
            raise

    async def _process_speech(self, pcm_16k_mono: bytes) -> None:
        """Full pipeline: speech PCM → STT → LLM → TTS → outbound.

        Runs under _turn_lock via _run_turn; guarantees only one turn
        at a time per call. Cancellable at every await boundary.
        """
        turn_started = time.monotonic()
        stats = self._manager._stats
        wav_path: str | None = None
        stage = "init"
        try:
            # 1. Write PCM to temp WAV for STT provider
            wav_path = pcm_to_wav_file(pcm_16k_mono)
            stage = "stt"

            # 2. STT: transcribe speech
            from qanot.voice import transcribe
            provider = self._config.voice_provider
            api_key = self._config.get_voice_api_key(provider)
            result = await transcribe(
                wav_path, api_key,
                provider=provider,
                language=self._config.voice_language or None,
            )
            text = result.text.strip() if result and result.text else ""

            if not text:
                logger.debug("VC STT returned empty text, skipping")
                return

            logger.info("VC STT [%d]: %s", self._session.chat_id, text[:100])

            # Check if cancelled (barge-in)
            if self._session._tts_cancel.is_set():
                return

            stage = "llm"
            # 3. LLM: process through agent loop
            response = await self._agent.run_turn(
                text,
                user_id=self._session.conv_key,
                chat_id=self._session.chat_id,
            )

            if not response or not response.strip():
                return

            # Check if cancelled (barge-in during LLM)
            if self._session._tts_cancel.is_set():
                return

            logger.info("VC LLM [%d]: %s", self._session.chat_id, response[:100])

            # 4. TTS: convert response to audio. Sanitize first — providers
            #    (Kotib returns HTTP 400) reject markdown + emojis.
            tts_text = sanitize_for_tts(response)[:2000]
            if not tts_text:
                logger.info("VC skipping TTS [%d]: empty after sanitize", self._session.chat_id)
                return
            stage = "tts"
            from qanot.voice import text_to_speech
            logger.info(
                "VC TTS request [%d]: provider=%s len=%d (raw=%d)",
                self._session.chat_id, provider, len(tts_text), len(response),
            )
            tts_result = await text_to_speech(
                tts_text, api_key,
                provider=provider,
                language=self._config.voice_language or "uz",
                voice=self._config.voice_name or None,
            )
            logger.info(
                "VC TTS response [%d]: audio_data=%s audio_url=%s",
                self._session.chat_id,
                (len(tts_result.audio_data) if tts_result and tts_result.audio_data else 0),
                (tts_result.audio_url if tts_result else None),
            )

            if self._session._tts_cancel.is_set():
                return

            # 5. Write TTS audio to a temp file and hand it to py-tgcalls.
            #    Manually resampling + frame-by-frame send_frame produced
            #    slow/glitchy playback; letting ntgcalls' native ffmpeg
            #    pipeline handle pacing + format conversion is the
            #    documented production pattern.
            from pytgcalls.types import MediaStream
            audio_path = await _write_tts_audio_to_temp(tts_result)
            if not audio_path:
                logger.warning("VC TTS conversion failed")
                return
            stage = "play"
            try:
                self._session.is_speaking = True
                logger.info(
                    "VC play_file [%d]: %s", self._session.chat_id, audio_path,
                )
                await self._manager._tgcalls.play(
                    self._session.chat_id,
                    MediaStream(audio_path, video_flags=MediaStream.Flags.IGNORE),
                )
            finally:
                self._session._pending_tempfiles.append(audio_path)

            # Turn succeeded. Record latency + bump counter.
            latency_ms = (time.monotonic() - turn_started) * 1000
            stats["turns_completed"] += 1
            stats["e2e_latency_sum_ms"] += latency_ms
            stats["e2e_latency_count"] += 1

        except asyncio.CancelledError:
            stats["turns_cancelled"] += 1
            logger.debug("VC turn cancelled (barge-in)")
            raise
        except Exception as e:
            stats["turns_failed"] += 1
            if stage == "stt":
                stats["stt_errors"] += 1
            elif stage == "tts":
                stats["tts_errors"] += 1
            logger.error(
                "VC pipeline error [%d] at stage=%s: %s",
                self._session.chat_id, stage, e, exc_info=True,
            )
            error_class = type(e).__name__
            message = str(e)[:300]
            throttle = f"vc:{stage}:{error_class}"
            await self._manager._notify(
                f"⚠️ Ovozli suhbat xatoligi ({stage}): {error_class}\n{message}",
                throttle_key=throttle,
            )
        finally:
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

    async def _tempfile_cleanup_loop(self) -> None:
        """Periodically remove temp TTS files that ntgcalls has finished
        reading. ntgcalls opens the file at play() time and releases it
        when the stream ends or is replaced, so a 60s grace window is
        plenty — keep the last few to cover back-to-back playbacks."""
        while True:
            try:
                await asyncio.sleep(60)
                # Keep the 2 most recent (currently/just-playing); delete older.
                paths = self._session._pending_tempfiles
                if len(paths) > 2:
                    for path in paths[:-2]:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                    del paths[:-2]
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Tempfile cleanup error: %s", e)

    def cancel_playback(self) -> None:
        """Barge-in: interrupt current TTS playback."""
        self._session.is_speaking = False
        tgcalls = self._manager._tgcalls
        if tgcalls is None:
            return
        chat_id = self._session.chat_id

        async def _pause() -> None:
            try:
                await tgcalls.pause(chat_id)
            except Exception as e:
                # Common: "userbot is not in a call" if call already ended.
                logger.debug("pause() during barge-in failed: %r", e)

        asyncio.create_task(_pause(), name=f"vc_barge_pause_{chat_id}")


class VoiceCallManager:
    """Manages Telegram voice call sessions via Pyrogram + py-tgcalls.

    Lifecycle: start() on bot startup, stop() on shutdown.
    Commands: join_call(), leave_call(), is_in_call().
    """

    def __init__(self, config: Config, agent: Agent) -> None:
        self._config = config
        self._agent = agent
        self._client = None  # Pyrogram Client
        self._tgcalls = None  # PyTgCalls instance
        self._active_calls: dict[int, CallSession] = {}
        self._pipelines: dict[int, AudioPipeline] = {}
        self._vad = SileroVAD(
            threshold=config.voicecall_vad_threshold,
            min_silence_ms=config.voicecall_silence_ms,
            min_speech_ms=config.voicecall_min_speech_ms,
        )
        self._started = False
        self._auto_leave_task: asyncio.Task | None = None
        # Optional owner-notification hook (wired by main.py to telegram
        # adapter). Signature: (text, throttle_key) -> Awaitable[None].
        self.notify_owner: Callable[..., Awaitable[None]] | None = None
        # Observability counters. Reset at restart; dashboard reads via
        # stats_snapshot(). Not thread-safe but we only touch these in
        # the asyncio loop.
        self._stats: dict[str, int | float] = {
            "turns_completed": 0,
            "turns_failed": 0,
            "turns_cancelled": 0,
            "stt_errors": 0,
            "tts_errors": 0,
            "e2e_latency_sum_ms": 0.0,
            "e2e_latency_count": 0,
        }

    def stats_snapshot(self) -> dict:
        """Return a point-in-time voice-call metrics dict for the dashboard."""
        latency_avg = (
            self._stats["e2e_latency_sum_ms"] / self._stats["e2e_latency_count"]
            if self._stats["e2e_latency_count"] else 0
        )
        return {
            "enabled": True,
            "started": self._started,
            "active_calls": len(self._active_calls),
            "turns_completed": self._stats["turns_completed"],
            "turns_failed": self._stats["turns_failed"],
            "turns_cancelled": self._stats["turns_cancelled"],
            "stt_errors": self._stats["stt_errors"],
            "tts_errors": self._stats["tts_errors"],
            "e2e_latency_avg_ms": round(latency_avg, 1),
            "e2e_latency_samples": self._stats["e2e_latency_count"],
        }

    async def _notify(self, text: str, throttle_key: str | None = None) -> None:
        if self.notify_owner is None:
            return
        try:
            await self.notify_owner(text, throttle_key=throttle_key)
        except Exception as e:
            logger.debug("notify_owner failed: %r", e)

    async def start(self) -> None:
        """Initialize Pyrogram client + py-tgcalls. Called once at bot startup."""
        try:
            from pyrogram import Client
            from pytgcalls import PyTgCalls
        except ImportError:
            raise RuntimeError(
                "Voice call requires: pip install py-tgcalls pyrogram numpy torch"
            )

        cfg = self._config
        if not cfg.voicecall_api_id or not cfg.voicecall_api_hash:
            raise ValueError("voicecall_api_id and voicecall_api_hash required")

        # Create Pyrogram client with session string (no interactive auth needed)
        client_kwargs = {
            "name": "qanot_voicecall",
            "api_id": cfg.voicecall_api_id,
            "api_hash": cfg.voicecall_api_hash,
            "no_updates": True,  # Don't process regular messages
            "in_memory": True,
        }
        if cfg.voicecall_session:
            client_kwargs["session_string"] = cfg.voicecall_session

        self._client = Client(**client_kwargs)
        self._tgcalls = PyTgCalls(self._client)

        # Register frame handler
        self._register_handlers()

        await self._client.start()
        await self._tgcalls.start()
        self._started = True

        # Warm up VAD *off* the audio hot path. First-call initialisation
        # pulls in torch + onnxruntime + the Silero model graph — easily
        # 1-3s of sync work. Doing it here (in the asyncio loop before
        # any frames arrive) prevents the first audio frame from blocking
        # the stream_frame handler long enough for py-tgcalls to stall
        # subsequent frames (observed symptom: "1 inbound frame received"
        # then silence). Run in an executor so the event loop still ticks.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._warm_up_vad,
            )
        except Exception as e:
            logger.warning("voicecall: VAD warm-up failed (non-fatal): %s", e)

        # Start auto-leave watchdog
        self._auto_leave_task = asyncio.create_task(
            self._auto_leave_loop(),
            name="vc_auto_leave",
        )

        logger.info("VoiceCallManager started (py-tgcalls + Pyrogram)")

    def _warm_up_vad(self) -> None:
        """Load the VAD model and run one inference so subsequent frame
        processing doesn't pay the init cost inline."""
        silence = bytes(CHUNK_BYTES)
        self._vad.process_chunk(silence)
        self._vad.reset()
        logger.info("voicecall: VAD warmed up")

    def _register_handlers(self) -> None:
        """Register py-tgcalls event handlers.

        Each handler is registered in its OWN try/except so a failure on
        one (e.g. chat_update's flag API changed) doesn't silently drop
        the other (audio frames) — which is the failure mode that
        previously left the bot in the call but deaf to anyone speaking.
        """
        from pytgcalls import filters as tg_filters
        from pytgcalls.types import ChatUpdate, Direction, Device, StreamFrames

        # ── Inbound audio frames (CRITICAL: without this, bot can't hear) ──
        # Diagnostic counter: log every N frames so we can see at a
        # glance whether py-tgcalls is delivering audio at all (vs
        # something between stream_frame → on_audio_frame being broken).
        # 50 frames ≈ 1 second at 20ms pacing, so we get ~1 line/sec.
        self._frame_counters: dict[int, int] = {}
        try:
            @self._tgcalls.on_update(
                tg_filters.stream_frame(Direction.INCOMING, Device.MICROPHONE)
            )
            async def on_audio_frame(_: object, update: StreamFrames) -> None:
                chat_id = update.chat_id
                count = self._frame_counters.get(chat_id, 0) + len(update.frames or [])
                self._frame_counters[chat_id] = count
                if count == 1 or count % 50 == 0:
                    logger.info(
                        "voicecall: %d inbound frames received in chat %d "
                        "(pipelines=%s)",
                        count, chat_id, list(self._pipelines.keys()),
                    )
                pipeline = self._pipelines.get(chat_id)
                if pipeline and update.frames:
                    for frame in update.frames:
                        pipeline.feed_inbound(frame.frame)
                elif count == 1:
                    # Log once if pipeline isn't registered for this chat.
                    logger.warning(
                        "voicecall: no pipeline for chat %d (registered: %s)",
                        chat_id, list(self._pipelines.keys()),
                    )

            logger.info("voicecall: stream_frame handler registered")
        except Exception as e:
            logger.error(
                "voicecall: FAILED to register stream_frame handler "
                "(bot won't hear anyone): %s", e,
            )

        # ── Chat updates (so we clean up when the call ends remotely) ──
        # py-tgcalls 2.x: chat_update() requires a `flags` argument — an
        # OR'd ChatUpdate.Status mask. We care about leave/close events.
        try:
            leave_flags = (
                ChatUpdate.Status.LEFT_GROUP
                | ChatUpdate.Status.CLOSED_VOICE_CHAT
                | ChatUpdate.Status.KICKED
                | ChatUpdate.Status.DISCARDED_CALL
            )

            @self._tgcalls.on_update(tg_filters.chat_update(leave_flags))
            async def on_chat_update(_: object, update: ChatUpdate) -> None:
                chat_id = update.chat_id
                if chat_id in self._active_calls:
                    logger.info("Call ended remotely in chat %d", chat_id)
                    await self._cleanup_call(chat_id)

            logger.info("voicecall: chat_update handler registered")
        except Exception as e:
            # Non-fatal: cleanup just happens on /leavecall or auto-leave
            # instead of remote-end detection.
            logger.warning(
                "voicecall: chat_update handler registration skipped: %s", e,
            )

    async def stop(self) -> None:
        """Leave all calls and shutdown. Called at bot shutdown."""
        if self._auto_leave_task and not self._auto_leave_task.done():
            self._auto_leave_task.cancel()

        # Leave all active calls
        for chat_id in list(self._active_calls):
            await self._cleanup_call(chat_id)

        if self._tgcalls and self._started:
            try:
                await self._tgcalls.stop()
            except Exception as e:
                logger.debug("py-tgcalls stop error: %s", e)

        if self._client:
            try:
                await self._client.stop()
            except Exception as e:
                logger.debug("Pyrogram stop error: %s", e)

        self._started = False
        logger.info("VoiceCallManager stopped")

    async def join_call(self, chat_id: int, user_id: int) -> str:
        """Join a group voice chat. Returns status message."""
        if not self._started:
            return "Voice call tizimi ishga tushmagan."

        if chat_id in self._active_calls:
            return "Allaqachon bu suhbatdaman."

        if len(self._active_calls) >= self._config.voicecall_max_calls:
            return f"Maksimal qo'ng'iroq limiti ({self._config.voicecall_max_calls}) ga yetildi."

        try:
            # Create session
            conv_key = f"vc_{chat_id}"
            session = CallSession(chat_id=chat_id, user_id=user_id, conv_key=conv_key)
            self._active_calls[chat_id] = session

            # Create and start audio pipeline
            pipeline = AudioPipeline(
                manager=self,
                session=session,
                vad=SileroVAD(
                    threshold=self._config.voicecall_vad_threshold,
                    min_silence_ms=self._config.voicecall_silence_ms,
                    min_speech_ms=self._config.voicecall_min_speech_ms,
                ),
                agent=self._agent,
                config=self._config,
            )
            self._pipelines[chat_id] = pipeline
            await pipeline.start()

            # Warm up this call's VAD off the audio thread. See start()
            # for the full rationale — silero+torch first-call init can
            # block ~1-2s, which stalls py-tgcalls frame delivery.
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: (pipeline._vad.process_chunk(bytes(CHUNK_BYTES)),
                               pipeline._vad.reset()),
            )

            from pytgcalls.types import RecordStream

            # Join voice chat.
            # py-tgcalls 2.x: to stream TTS frames manually via
            # send_frame(Device.MICROPHONE, ...), we must declare an
            # external audio source with MediaStream(ExternalMedia.AUDIO).
            # Calling play(chat_id) without a stream joins the call but
            # leaves the outbound microphone uninitialised, causing every
            # send_frame to fail with 'External source not initialized'.
            from pytgcalls.types import MediaStream
            from pytgcalls.types.stream.external_media import ExternalMedia
            await self._tgcalls.play(chat_id, MediaStream(ExternalMedia.AUDIO))

            # Enable inbound audio frame delivery.
            # RecordStream(audio=True) sets media_source=MediaSource.EXTERNAL,
            # which tells ntgcalls to route incoming mic audio to our
            # on_update(stream_frame) handler as raw PCM. Without this,
            # ntgcalls silently discards the audio and no frames ever reach
            # Python. I mistakenly removed this when porting to 2.x — the
            # class is still valid in 2.x; only some param names changed.
            await self._tgcalls.record(chat_id, RecordStream(audio=True))

            logger.info("Joined voice chat in %d (user %d)", chat_id, user_id)
            return "Ovozli suhbatga qo'shildim! Gapiring — men tinglayman."

        except Exception as e:
            # Cleanup on failure
            await self._cleanup_call(chat_id)
            logger.error("Failed to join voice chat %d: %s", chat_id, e, exc_info=True)
            return f"Qo'shila olmadim: {e}"

    async def leave_call(self, chat_id: int) -> str:
        """Leave a voice chat. Returns status message."""
        if chat_id not in self._active_calls:
            return "Bu suhbatda emasman."

        await self._cleanup_call(chat_id)
        return "Ovozli suhbatdan chiqdim."

    def is_in_call(self, chat_id: int) -> bool:
        """Check if currently in a voice chat."""
        return chat_id in self._active_calls

    async def _cleanup_call(self, chat_id: int) -> None:
        """Stop pipeline, leave call, remove session."""
        # Stop pipeline
        pipeline = self._pipelines.pop(chat_id, None)
        if pipeline:
            await pipeline.stop()

        # Leave call
        if self._tgcalls:
            try:
                await self._tgcalls.leave_call(chat_id)
            except Exception as e:
                logger.debug("leave_call error for %d: %s", chat_id, e)

        # Remove session
        self._active_calls.pop(chat_id, None)
        logger.info("Cleaned up voice call for chat %d", chat_id)

    async def _auto_leave_loop(self) -> None:
        """Watchdog: auto-leave calls that have been idle too long."""
        timeout = self._config.voicecall_auto_leave_minutes * 60
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                now = time.monotonic()
                for chat_id, session in list(self._active_calls.items()):
                    idle = now - session.last_speech_at
                    if idle > timeout:
                        logger.info(
                            "Auto-leaving VC %d after %d min idle",
                            chat_id, int(idle // 60),
                        )
                        await self._cleanup_call(chat_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Auto-leave loop error: %s", e)
