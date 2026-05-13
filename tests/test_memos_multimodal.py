"""Tests for qanot.memos.multimodal — voice/image → memo persistence."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from qanot.memos import (
    IMAGE_DIR_NAME,
    MemoStore,
    MemoType,
    VOICE_DIR_NAME,
    save_image_memo,
    save_voice_memo,
)


def _run(coro):
    return asyncio.run(coro)


# ─── voice ──────────────────────────────────────────────────────


class TestSaveVoiceMemo:
    def test_basic_save(self, tmp_path):
        # Pretend we just downloaded a voice file.
        src = tmp_path / "tmp_input.ogg"
        src.write_bytes(b"OggS\x00\x02\x00fakefake" * 10)

        result = _run(save_voice_memo(
            audio_src_path=str(src),
            transcript="Salom, bu test ovozli xabar.",
            duration_sec=12,
            workspace_dir=tmp_path,
            user_id="1545",
            thread_id="ish",
        ))
        assert result is not None
        assert result.action == "created"
        # Audio file copied to media/voice/
        voice_dir = tmp_path / VOICE_DIR_NAME
        assert any(voice_dir.iterdir())
        # Memo file exists with multimodal metadata
        memo = MemoStore(tmp_path).load(result.name)
        assert memo is not None
        assert memo.media_type == "voice"
        assert memo.media_path.startswith(VOICE_DIR_NAME)
        assert memo.duration_sec == 12
        assert memo.user_scope == "1545"
        assert memo.thread_scope == "ish"
        assert "Salom, bu test ovozli xabar." in memo.body

    def test_empty_transcript_skipped(self, tmp_path):
        src = tmp_path / "tmp.ogg"
        src.write_bytes(b"fake")
        result = _run(save_voice_memo(
            audio_src_path=str(src),
            transcript="   ",
            duration_sec=5,
            workspace_dir=tmp_path,
        ))
        assert result is None
        # No file copied either.
        voice_dir = tmp_path / VOICE_DIR_NAME
        assert not voice_dir.exists() or not any(voice_dir.iterdir())

    def test_missing_source_returns_none(self, tmp_path):
        result = _run(save_voice_memo(
            audio_src_path=str(tmp_path / "does_not_exist.ogg"),
            transcript="content",
            duration_sec=10,
            workspace_dir=tmp_path,
        ))
        assert result is None

    def test_explicit_summary_used_in_description(self, tmp_path):
        src = tmp_path / "tmp.ogg"
        src.write_bytes(b"audio")
        result = _run(save_voice_memo(
            audio_src_path=str(src),
            transcript="long transcript " * 100,
            duration_sec=60,
            workspace_dir=tmp_path,
            summary="Q2 strategy meeting summary",
        ))
        assert result is not None
        memo = MemoStore(tmp_path).load(result.name)
        assert "Q2 strategy meeting summary" in memo.description

    def test_global_scope_when_no_caller_ids(self, tmp_path):
        src = tmp_path / "tmp.ogg"
        src.write_bytes(b"audio")
        result = _run(save_voice_memo(
            audio_src_path=str(src),
            transcript="hello world",
            duration_sec=3,
            workspace_dir=tmp_path,
        ))
        memo = MemoStore(tmp_path).load(result.name)
        assert memo.user_scope == ""
        assert memo.thread_scope == ""
        assert memo.is_global

    def test_name_format_kebab_case(self, tmp_path):
        # Name must satisfy the strict spec regex; specifically, no
        # uppercase / underscores / consecutive hyphens.
        src = tmp_path / "tmp.ogg"
        src.write_bytes(b"audio")
        result = _run(save_voice_memo(
            audio_src_path=str(src),
            transcript="x",
            duration_sec=1,
            workspace_dir=tmp_path,
        ))
        assert result is not None
        assert result.name.startswith("multimodal-voice-")
        # Kebab-case round trip — store.load implies the file parsed clean.
        assert MemoStore(tmp_path).load(result.name) is not None


# ─── image ──────────────────────────────────────────────────────


class TestSaveImageMemo:
    def test_basic_save(self, tmp_path):
        image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes-here" * 50
        result = _run(save_image_memo(
            image_bytes=image_bytes,
            description_text="A screenshot of XAUUSD M5 chart showing bullish triangle.",
            workspace_dir=tmp_path,
            user_id="1545",
        ))
        assert result is not None
        # Image file persisted
        image_dir = tmp_path / IMAGE_DIR_NAME
        assert any(image_dir.iterdir())
        memo = MemoStore(tmp_path).load(result.name)
        assert memo is not None
        assert memo.media_type == "image"
        assert memo.media_path.startswith(IMAGE_DIR_NAME)
        assert memo.duration_sec == 0
        assert "XAUUSD" in memo.body

    def test_empty_description_skipped(self, tmp_path):
        result = _run(save_image_memo(
            image_bytes=b"jpeg",
            description_text="",
            workspace_dir=tmp_path,
        ))
        assert result is None

    def test_empty_bytes_skipped(self, tmp_path):
        result = _run(save_image_memo(
            image_bytes=b"",
            description_text="some description",
            workspace_dir=tmp_path,
        ))
        assert result is None

    def test_duplicate_image_reuses_file(self, tmp_path):
        image_bytes = b"identical-image-bytes" * 100
        r1 = _run(save_image_memo(
            image_bytes=image_bytes,
            description_text="first description",
            workspace_dir=tmp_path,
            user_id="u",
        ))
        # Second save with the same bytes — should write a new memo
        # (different timestamp) but REUSE the existing image file.
        r2 = _run(save_image_memo(
            image_bytes=image_bytes,
            description_text="second description",
            workspace_dir=tmp_path,
            user_id="u",
        ))
        assert r1 is not None and r2 is not None
        # Same media_path on both memos.
        m1 = MemoStore(tmp_path).load(r1.name)
        m2 = MemoStore(tmp_path).load(r2.name)
        assert m1.media_path == m2.media_path
        # Only ONE image file on disk.
        image_dir = tmp_path / IMAGE_DIR_NAME
        assert sum(1 for _ in image_dir.iterdir()) == 1

    def test_name_format(self, tmp_path):
        result = _run(save_image_memo(
            image_bytes=b"jpegbytes",
            description_text="desc",
            workspace_dir=tmp_path,
        ))
        assert result is not None
        assert result.name.startswith("multimodal-image-")


# ─── integration with router scope ──────────────────────────────


class TestScopeFiltering:
    def test_thread_scoped_voice_filters_correctly(self, tmp_path):
        src = tmp_path / "v.ogg"
        src.write_bytes(b"audio")
        _run(save_voice_memo(
            audio_src_path=str(src),
            transcript="IELTS strategiya",
            duration_sec=30,
            workspace_dir=tmp_path,
            user_id="1545",
            thread_id="ielts",
        ))
        # Pull memos for the IELTS thread only — should include this one.
        store = MemoStore(tmp_path)
        in_scope = store.list_in_scope(user_id="1545", thread_id="ielts")
        assert len(in_scope) == 1
        # Pull for a different thread — empty.
        wrong = store.list_in_scope(user_id="1545", thread_id="other")
        assert wrong == []
