"""Unit tests for the clipper plugin — pure-Python logic only (no ffmpeg/whisper)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent / "plugins" / "clipper"


def _load_engine_module(name: str):
    """Load a clipper engine module in isolation (matches plugin loader behavior).

    Subpackage was renamed from ``engine`` → ``cl_engine`` to avoid a
    sys.modules collision: other plugins (tgchannel, sheets, notion)
    previously used a generic ``engine`` subpackage too, so whichever
    loaded first would hijack later imports and silently break setup.
    """
    plugin_str = str(PLUGIN_DIR)
    if plugin_str not in sys.path:
        sys.path.insert(0, plugin_str)
    return importlib.import_module(f"cl_engine.{name}")


@pytest.fixture(scope="module")
def models():
    return _load_engine_module("models")


@pytest.fixture(scope="module")
def moments_mod():
    _load_engine_module("models")  # side-effect: add to path
    return _load_engine_module("moments")


@pytest.fixture(scope="module")
def captions_mod():
    _load_engine_module("models")
    return _load_engine_module("captions")


@pytest.fixture(scope="module")
def source_mod():
    return _load_engine_module("source")


# ────────── Models ──────────

def test_transcript_words_flatten(models):
    seg1 = models.Segment(text="Salom", start_s=0, end_s=1, words=[
        models.Word(text="Salom", start_s=0, end_s=1),
    ])
    seg2 = models.Segment(text="dunyo", start_s=1, end_s=2, words=[
        models.Word(text="dunyo", start_s=1, end_s=2),
    ])
    t = models.Transcript(language="uz", duration_s=2, segments=[seg1, seg2])
    assert len(t.words) == 2
    assert t.words[0].text == "Salom"


def test_transcript_words_in_range(models):
    t = models.Transcript(language="uz", duration_s=10, segments=[
        models.Segment(text="a b c", start_s=0, end_s=3, words=[
            models.Word(text="a", start_s=0, end_s=1),
            models.Word(text="b", start_s=1, end_s=2),
            models.Word(text="c", start_s=2, end_s=3),
        ]),
    ])
    # Midpoints: a=0.5, b=1.5, c=2.5. Range [1.0, 2.0] → only b (midpoint 1.5)
    in_range = t.words_in_range(1.0, 2.0)
    assert [w.text for w in in_range] == ["b"]


def test_source_media_aspect(models):
    s = models.SourceMedia(
        path=Path("/tmp/test.mp4"),
        duration_s=100, width=1920, height=1080, fps=30, has_audio=True,
    )
    assert s.is_vertical is False
    assert abs(s.aspect_ratio - 16 / 9) < 0.01

    s2 = models.SourceMedia(
        path=Path("/tmp/test.mp4"),
        duration_s=100, width=1080, height=1920, fps=30, has_audio=True,
    )
    assert s2.is_vertical is True


# ────────── Source helpers ──────────

def test_is_url(source_mod):
    assert source_mod.is_url("https://youtu.be/abc") is True
    assert source_mod.is_url("http://example.com/video.mp4") is True
    assert source_mod.is_url("/local/file.mp4") is False
    assert source_mod.is_url("file.mp4") is False
    assert source_mod.is_url("") is False
    assert source_mod.is_url("ftp://example.com") is False


def test_parse_fps(source_mod):
    assert source_mod._parse_fps("30/1") == 30.0
    assert abs(source_mod._parse_fps("30000/1001") - 29.97) < 0.1
    assert source_mod._parse_fps("24") == 24.0
    assert source_mod._parse_fps("invalid") == 0.0
    assert source_mod._parse_fps("1/0") == 0.0  # avoid div by zero


# ────────── Moments: JSON extraction ──────────

def test_extract_json_block_plain(moments_mod):
    text = '{"moments": [{"start_s": 1, "end_s": 2}]}'
    assert moments_mod._extract_json_block(text) == text


def test_extract_json_block_markdown(moments_mod):
    text = '```json\n{"moments": []}\n```'
    assert moments_mod._extract_json_block(text) == '{"moments": []}'


def test_extract_json_block_with_preamble(moments_mod):
    text = 'Here are the moments:\n\n{"moments": []}\n\nHope this helps!'
    assert moments_mod._extract_json_block(text) == '{"moments": []}'


# ────────── Moments: schema validation ──────────

def test_moment_candidate_end_after_start(moments_mod):
    with pytest.raises(Exception):
        moments_mod._MomentCandidate(
            start_s=100, end_s=50,  # end before start
            hook="test", title="test",
            virality_score=80,
        )


def test_moment_candidate_virality_range(moments_mod):
    with pytest.raises(Exception):
        moments_mod._MomentCandidate(
            start_s=0, end_s=30,
            hook="test", title="test",
            virality_score=150,  # > 99
        )


def test_moment_candidate_strips_hashtag_prefix(moments_mod):
    c = moments_mod._MomentCandidate(
        start_s=0, end_s=30,
        hook="test", title="test",
        virality_score=80,
        hashtags=["#biznes", "tadbirkor", "#uz"],
    )
    assert c.hashtags == ["biznes", "tadbirkor", "uz"]


# ────────── Moments: snap to boundary ──────────

def test_snap_to_sentence_boundary(moments_mod, models):
    transcript = models.Transcript(language="uz", duration_s=60, segments=[
        models.Segment(text="a", start_s=10.0, end_s=15.0),
        models.Segment(text="b", start_s=15.5, end_s=20.0),
        models.Segment(text="c", start_s=21.0, end_s=30.0),
    ])
    moment = models.Moment(
        start_s=11.2, end_s=29.1,
        hook="test", virality_score=80, rationale="", title="test",
    )
    snapped = moments_mod.snap_to_sentence_boundary(moment, transcript, max_shift_s=3.0)
    assert snapped.start_s == 10.0  # closest segment start within 3s
    assert snapped.end_s == 30.0  # closest segment end within 3s


def test_snap_does_not_shift_too_far(moments_mod, models):
    transcript = models.Transcript(language="uz", duration_s=60, segments=[
        models.Segment(text="a", start_s=0.0, end_s=5.0),
    ])
    moment = models.Moment(
        start_s=30.0, end_s=40.0,  # far from any segment
        hook="test", virality_score=80, rationale="", title="test",
    )
    snapped = moments_mod.snap_to_sentence_boundary(moment, transcript, max_shift_s=3.0)
    # No segment within 3s → keep original timestamps
    assert snapped.start_s == 30.0
    assert snapped.end_s == 40.0


# ────────── Captions: clip-local translation ──────────

def test_clip_local_words(captions_mod, models):
    words = [
        models.Word(text="a", start_s=10.0, end_s=11.0),
        models.Word(text="b", start_s=11.0, end_s=12.0),
        models.Word(text="c", start_s=12.0, end_s=13.0),
        models.Word(text="d", start_s=13.0, end_s=14.0),
    ]
    local = captions_mod.clip_local_words(words, clip_start_s=11.0, clip_end_s=13.5)
    # Midpoints: a=10.5, b=11.5, c=12.5, d=13.5
    # Keep b, c, d (midpoints in [11.0, 13.5])
    assert [w.text for w in local] == ["b", "c", "d"]
    # b's local time: 0.0 - 1.0
    assert abs(local[0].start_s - 0.0) < 0.01
    assert abs(local[0].end_s - 1.0) < 0.01


def test_caption_styles_exist(captions_mod):
    assert "captions_ai" in captions_mod.STYLES
    assert "submagic" in captions_mod.STYLES
    assert "minimal" in captions_mod.STYLES


def test_build_pages(captions_mod, models):
    words = [models.Word(text=f"w{i}", start_s=i, end_s=i + 1) for i in range(10)]
    pages = captions_mod._build_pages(words, words_per_page=4)
    assert len(pages) == 3  # 4+4+2
    assert len(pages[0]) == 4
    assert len(pages[2]) == 2


# ────────── Hook text wrapping ──────────

def test_hook_wrap():
    hook_mod = _load_engine_module("hook")
    lines = hook_mod._wrap_text("Bu juda uzun hook matni bo'ladi albatta", max_chars=18)
    assert len(lines) >= 2
    for line in lines:
        assert len(line) <= 20  # slight flex for word boundaries
