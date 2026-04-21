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
import tempfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qanot.audio import (
    VC_FRAME_BYTES,
    pcm_to_wav_file,
    resample_16k_mono_to_48k_stereo,
    resample_48k_stereo_to_16k_mono,
    split_pcm_frames,
    tts_result_to_vc_pcm,
)
from qanot.vad import CHUNK_BYTES, SileroVAD, VADEvent

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)

# Minimum speech bytes to process (250ms at 16kHz mono 16-bit)
MIN_SPEECH_BYTES = 16000 * 2 * 250 // 1000  # 8000 bytes

# Playback frame pacing: 20ms at 48kHz stereo
PLAYBACK_INTERVAL = 0.02  # seconds

# Rate limit: minimum seconds between processing speech segments
MIN_TURN_INTERVAL = 2.0


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
    _llm_task: asyncio.Task | None = field(default=None, repr=False)


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

        # Outbound audio queue (48kHz stereo PCM frames)
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=300)
        self._playback_task: asyncio.Task | None = None
        self._last_turn_time: float = 0.0

    async def start(self) -> None:
        """Start the playback loop."""
        self._playback_task = asyncio.create_task(
            self._playback_loop(),
            name=f"vc_playback_{self._session.chat_id}",
        )

    async def stop(self) -> None:
        """Stop pipeline and cleanup."""
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
        self.cancel_playback()
        self._vad.reset()

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

        # Process complete VAD chunks (512 samples = 1024 bytes each)
        while len(self._vad_buffer) >= CHUNK_BYTES:
            chunk = bytes(self._vad_buffer[:CHUNK_BYTES])
            del self._vad_buffer[:CHUNK_BYTES]

            event = self._vad.process_chunk(chunk)

            if event == VADEvent.SPEECH_START:
                self._session.last_speech_at = time.monotonic()
                # Barge-in: if bot is speaking, interrupt it
                if self._session.is_speaking and self._config.voicecall_barge_in:
                    logger.info("Barge-in detected in chat %d", self._session.chat_id)
                    self.cancel_playback()
                    self._session.is_speaking = False
                    if self._session._llm_task and not self._session._llm_task.done():
                        self._session._llm_task.cancel()
                    self._session._tts_cancel.set()
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
                if (
                    len(speech_data) >= MIN_SPEECH_BYTES
                    and now - self._last_turn_time >= MIN_TURN_INTERVAL
                ):
                    self._last_turn_time = now
                    # Process in background — don't block audio thread
                    self._session._tts_cancel.clear()
                    self._session._llm_task = asyncio.create_task(
                        self._process_speech(speech_data),
                        name=f"vc_turn_{self._session.chat_id}",
                    )

    async def _process_speech(self, pcm_16k_mono: bytes) -> None:
        """Full pipeline: speech PCM → STT → LLM → TTS → outbound.

        Runs as an asyncio task. Cancellable via barge-in.
        """
        wav_path: str | None = None
        try:
            # 1. Write PCM to temp WAV for STT provider
            wav_path = pcm_to_wav_file(pcm_16k_mono)

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

            # 4. TTS: convert response to audio
            from qanot.voice import text_to_speech
            tts_result = await text_to_speech(
                response[:2000],  # Cap TTS input length
                api_key,
                provider=provider,
                language=self._config.voice_language or "uz",
                voice=self._config.voice_name or None,
            )

            if self._session._tts_cancel.is_set():
                return

            # 5. Convert TTS output to 48kHz stereo PCM
            vc_pcm = await tts_result_to_vc_pcm(tts_result, self._config)
            if not vc_pcm:
                logger.warning("VC TTS conversion failed, sending text fallback")
                return

            # 6. Queue frames for playback
            self._session.is_speaking = True
            frames = split_pcm_frames(vc_pcm)
            for frame in frames:
                if self._session._tts_cancel.is_set():
                    break
                try:
                    self._outbound_queue.put_nowait(frame)
                except asyncio.QueueFull:
                    logger.warning("VC outbound queue full, dropping frame")
                    break

        except asyncio.CancelledError:
            logger.debug("VC turn cancelled (barge-in)")
        except Exception as e:
            logger.error("VC pipeline error [%d]: %s", self._session.chat_id, e, exc_info=True)
        finally:
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

    async def _playback_loop(self) -> None:
        """Continuously dequeue outbound PCM and send to voice chat.

        Paces at real-time rate (20ms per frame) using asyncio.sleep.
        """
        from pytgcalls.types import Device
        from pytgcalls.types.stream.frame import Frame

        tgcalls = self._manager._tgcalls
        chat_id = self._session.chat_id

        while True:
            try:
                frame = await asyncio.wait_for(
                    self._outbound_queue.get(), timeout=1.0,
                )
                # Send frame to voice chat.
                # py-tgcalls 2.x: send_frame(chat_id, device, data, frame_info)
                # — device=MICROPHONE for outbound audio, default Frame.Info
                # is fine for audio (width/height/rotation matter for video only).
                try:
                    await tgcalls.send_frame(
                        chat_id, Device.MICROPHONE, frame, Frame.Info.default,
                    )
                except Exception as e:
                    logger.debug("send_frame failed: %s", e)

                # Pace at real-time rate
                await asyncio.sleep(PLAYBACK_INTERVAL)

                # Mark not speaking when queue is drained
                if self._outbound_queue.empty():
                    self._session.is_speaking = False

            except asyncio.TimeoutError:
                # No audio to play — send silence or just wait
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Playback loop error: %s", e)
                await asyncio.sleep(0.1)

    def cancel_playback(self) -> None:
        """Barge-in: clear outbound queue immediately."""
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._session.is_speaking = False


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

        # Start auto-leave watchdog
        self._auto_leave_task = asyncio.create_task(
            self._auto_leave_loop(),
            name="vc_auto_leave",
        )

        logger.info("VoiceCallManager started (py-tgcalls + Pyrogram)")

    def _register_handlers(self) -> None:
        """Register py-tgcalls event handlers."""
        try:
            from pytgcalls import filters as tg_filters
            from pytgcalls.types import ChatUpdate, StreamFrames, Direction, Device

            @self._tgcalls.on_update(
                tg_filters.stream_frame(Direction.INCOMING, Device.MICROPHONE)
            )
            async def on_audio_frame(_: object, update: StreamFrames) -> None:
                chat_id = update.chat_id
                pipeline = self._pipelines.get(chat_id)
                if pipeline and update.frames:
                    for frame in update.frames:
                        pipeline.feed_inbound(frame.frame)

            @self._tgcalls.on_update(tg_filters.chat_update())
            async def on_chat_update(_: object, update: ChatUpdate) -> None:
                # Handle call ended by remote side
                if hasattr(update, "status"):
                    status = update.status
                    chat_id = update.chat_id
                    if hasattr(status, "name") and "LEFT" in str(status.name):
                        if chat_id in self._active_calls:
                            logger.info("Call ended remotely in chat %d", chat_id)
                            await self._cleanup_call(chat_id)

        except Exception as e:
            logger.warning("Failed to register py-tgcalls handlers: %s", e)

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

            # Join voice chat.
            # py-tgcalls 2.x: play(chat_id, stream=None) joins without any
            # outbound media — we stream our TTS frames manually via
            # send_frame(Device.MICROPHONE, ...) from the playback_loop.
            # This replaces the 1.x MediaStream(audio_path="") pattern,
            # which 2.x rejects with "missing a required argument".
            await self._tgcalls.play(chat_id)

            # Start recording to receive incoming audio frames.
            # py-tgcalls 2.x: record(chat_id) defaults are fine. 1.x's
            # RecordStream(audio=True) arg was removed.
            await self._tgcalls.record(chat_id)

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
