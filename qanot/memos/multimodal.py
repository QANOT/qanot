"""Multimodal memory — voice notes and images as first-class retrievable memos.

The bug class this closes: today, when a user sends an audio note, the
transcription text gets folded into the conversation history and then
lost on compaction. Two weeks later, "o'sha IELTS strategiya haqida
ovozli xabar nima edi?" has no answer — the transcript is gone, the
original .ogg file long since deleted from /tmp.

This module routes voice + image events into the existing memo subsystem
so they show up in the router's semantic search and stay around for
months. The original media file is persisted under
``<workspace>/media/{voice,images}/`` and the memo carries a pointer
back to it, so the agent can replay the original via tg_send_voice /
tg_send_photo when the user asks.

We deliberately keep this thin — the heavy lifting (transcription,
Haiku extraction) already lives in ``qanot/voice.py`` and
``qanot/extraction.py``. This module is a *router*: it takes the
output of those modules, persists the raw media, and emits a memo.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from .spec import MemoType
from .store import MemoStore, WriteResult

logger = logging.getLogger(__name__)


# Where original media files live, relative to workspace_dir. The router
# does not read these files — it only embeds the transcript / description
# the agent generated. The path stays in metadata so the agent can attach
# the original when replying.
VOICE_DIR_NAME = "media/voice"
IMAGE_DIR_NAME = "media/images"


# ─── voice memo ──────────────────────────────────────────────────


async def save_voice_memo(
    *,
    audio_src_path: str,
    transcript: str,
    duration_sec: int,
    workspace_dir: str | Path,
    user_id: str = "",
    thread_id: str = "",
    summary: str | None = None,
    audio_suffix: str = ".ogg",
) -> WriteResult | None:
    """Persist a voice note as a memo + keep the audio file around.

    Returns the WriteResult on success or ``None`` when the input is
    too thin to be worth saving (empty transcript, missing file).

    ``audio_src_path`` is the path the bot just downloaded the audio to.
    We COPY it to the workspace's media dir so the source-side temp file
    can be cleaned up by the caller (which is what transcribe_voice does
    today). Same atomic-rename-as-store-write pattern; on failure the
    memo write is skipped so we don't end up with an orphan record.

    ``summary`` is what the router will embed for relevance. If the
    caller didn't pre-compute one (most common), we use the first 200
    chars of the transcript as a placeholder; the curator can refine
    later.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        logger.debug("save_voice_memo: empty transcript, skipping")
        return None

    src = Path(audio_src_path)
    if not src.is_file():
        logger.warning("save_voice_memo: source audio missing at %s", src)
        return None

    workspace = Path(workspace_dir)
    voice_dir = workspace / VOICE_DIR_NAME
    voice_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    digest = _short_hash(src)
    filename = f"{stamp}_{digest}{audio_suffix}"
    dest = voice_dir / filename
    media_rel = f"{VOICE_DIR_NAME}/{filename}"

    try:
        await asyncio.to_thread(shutil.copy2, str(src), str(dest))
    except OSError as exc:
        logger.warning(
            "save_voice_memo: copy %s → %s failed: %s", src, dest, exc,
        )
        return None

    name = _slug("voice", stamp, digest)
    description = (summary or _excerpt(transcript, 180)).replace("\n", " ")

    body = (
        f"[VOICE TRANSCRIPT — {duration_sec}s]\n"
        f"{transcript}\n\n"
        f"original: {media_rel}"
    )

    store = MemoStore(workspace)
    try:
        result = store.upsert(
            name=name,
            description=description,
            memo_type=MemoType.REFERENCE,
            body=body,
            user_scope=user_id,
            thread_scope=thread_id,
            media_type="voice",
            media_path=media_rel,
            duration_sec=int(duration_sec or 0),
            why=(
                "Auto-captured voice note. The audio is on disk so the agent "
                "can replay it via tg_send_voice when the user asks."
            ),
            how_to_apply=(
                "Surface the transcript when the user references this voice "
                "memo by topic. Use tg_send_voice with media_path to replay."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — never let memo write break the turn
        logger.warning("save_voice_memo: store.upsert failed: %s", exc)
        return None

    logger.info(
        "voice memo saved: %s (%ds, %s)",
        result.name, duration_sec, media_rel,
    )
    return result


# ─── image memo ──────────────────────────────────────────────────


async def save_image_memo(
    *,
    image_bytes: bytes,
    description_text: str,
    workspace_dir: str | Path,
    user_id: str = "",
    thread_id: str = "",
    image_suffix: str = ".jpg",
    summary: str | None = None,
) -> WriteResult | None:
    """Persist an image as a memo + keep the raw bytes.

    The caller is expected to have already run the image through
    ``qanot/extraction.py`` (or any equivalent) so ``description_text``
    contains the structured pull. We don't re-extract here — that's a
    different responsibility owned by the extraction module.

    Image bytes are written to ``<workspace>/media/images/<stamp>_<sha1>.<ext>``.
    SHA-1 short prefix in the filename dedupes silently if the same
    image is re-sent (the existing extraction module already does this
    via image_hash, so most callers won't re-trigger).
    """
    description_text = (description_text or "").strip()
    if not description_text:
        logger.debug("save_image_memo: empty extraction, skipping")
        return None
    if not image_bytes:
        logger.debug("save_image_memo: empty bytes, skipping")
        return None

    workspace = Path(workspace_dir)
    image_dir = workspace / IMAGE_DIR_NAME
    image_dir.mkdir(parents=True, exist_ok=True)

    digest = sha1(image_bytes).hexdigest()[:10]
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"{stamp}_{digest}{image_suffix}"
    dest = image_dir / filename
    media_rel = f"{IMAGE_DIR_NAME}/{filename}"

    # Dedupe: if this exact image is already on disk under any timestamp,
    # reuse it rather than write a duplicate. Keeps the media dir from
    # blowing up when the user re-sends the same screenshot.
    for existing in image_dir.glob(f"*_{digest}{image_suffix}"):
        media_rel = f"{IMAGE_DIR_NAME}/{existing.name}"
        break
    else:
        try:
            await asyncio.to_thread(dest.write_bytes, image_bytes)
        except OSError as exc:
            logger.warning("save_image_memo: write %s failed: %s", dest, exc)
            return None

    name = _slug("image", stamp, digest)
    summary = (summary or _excerpt(description_text, 180)).replace("\n", " ")

    body = (
        f"[IMAGE DESCRIPTION]\n"
        f"{description_text}\n\n"
        f"original: {media_rel}"
    )

    store = MemoStore(workspace)
    try:
        result = store.upsert(
            name=name,
            description=summary,
            memo_type=MemoType.REFERENCE,
            body=body,
            user_scope=user_id,
            thread_scope=thread_id,
            media_type="image",
            media_path=media_rel,
            duration_sec=0,
            why=(
                "Auto-captured image. The raw file is on disk so the agent "
                "can re-send it via tg_send_photo when the user asks."
            ),
            how_to_apply=(
                "Surface the description when the user references this image "
                "by topic. Use tg_send_photo with media_path to replay."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_image_memo: store.upsert failed: %s", exc)
        return None

    logger.info("image memo saved: %s (%s)", result.name, media_rel)
    return result


# ─── helpers ─────────────────────────────────────────────────────


def _slug(prefix: str, stamp: str, digest: str) -> str:
    """Memo name. Kebab-case, deterministic, ≤64 chars.

    The format is ``multimodal-<kind>-<YYYYMMDDtHHMMSSz>-<10-char-hash>``.
    All lower-case so it matches the spec's name regex.
    """
    stamp_lc = stamp.lower().replace("z", "z")  # already lowercase, defensive
    return f"multimodal-{prefix}-{stamp_lc}-{digest}"


def _excerpt(text: str, n: int) -> str:
    """First N chars of ``text`` with whitespace squashed; ellipsis on cut."""
    flat = " ".join(text.split())
    if len(flat) <= n:
        return flat
    return flat[: n - 1] + "…"


def _short_hash(path: Path) -> str:
    """SHA-1 hex prefix of ``path``'s contents. Used in filenames so
    repeated identical media doesn't pile up under different timestamps.
    Reads in chunks to keep memory flat for long voice notes.
    """
    hasher = sha1()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
    except OSError:
        # Fall back to a timestamp-only digest — caller still gets a
        # unique filename, just no content-based dedupe.
        return datetime.now(timezone.utc).strftime("%f")
    return hasher.hexdigest()[:10]
