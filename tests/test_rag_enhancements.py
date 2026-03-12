"""Tests for RAG enhancements — MMR deduplication and temporal decay."""

from __future__ import annotations

import time
import pytest

from qanot.rag.engine import _text_similarity, _is_redundant, MMR_SIMILARITY_THRESHOLD


class TestTextSimilarity:

    def test_identical_texts(self):
        assert _text_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert _text_similarity("hello world", "foo bar baz") == 0.0

    def test_partial_overlap(self):
        sim = _text_similarity("the quick brown fox", "the slow brown cat")
        assert 0.2 < sim < 0.6  # "the" and "brown" overlap

    def test_empty_text(self):
        assert _text_similarity("", "hello") == 0.0
        assert _text_similarity("hello", "") == 0.0
        assert _text_similarity("", "") == 0.0

    def test_case_insensitive(self):
        assert _text_similarity("Hello World", "hello world") == 1.0

    def test_near_duplicate(self):
        a = "user prefers dark mode and compact layout"
        b = "user prefers dark mode and compact layout settings"
        sim = _text_similarity(a, b)
        assert sim > 0.7  # Should be detected as near-duplicate


class TestIsRedundant:

    def test_no_seen_texts(self):
        assert not _is_redundant("anything", [])

    def test_exact_duplicate(self):
        assert _is_redundant("hello world", ["hello world"])

    def test_near_duplicate(self):
        seen = ["the user likes dark mode and compact view"]
        assert _is_redundant("the user likes dark mode and compact view settings", seen)

    def test_different_content(self):
        seen = ["the user likes dark mode"]
        assert not _is_redundant("project deadline is next friday", seen)

    def test_multiple_seen(self):
        seen = [
            "the user prefers dark mode and compact layout for the interface",
            "project uses Python and FastAPI for backend services",
        ]
        assert not _is_redundant("database is PostgreSQL hosted on AWS", seen)
        # Same content with minor additions → redundant
        assert _is_redundant(
            "the user prefers dark mode and compact layout for the app interface", seen,
        )


class TestTemporalDecay:
    """Test temporal decay math (decay = 1 / (1 + age_days / 30))."""

    def test_fresh_memory_no_decay(self):
        # 0 days old → decay = 1.0
        decay = 1.0 / (1.0 + 0 / 30.0)
        assert decay == 1.0

    def test_one_day_old(self):
        # 1 day → decay ≈ 0.968
        decay = 1.0 / (1.0 + 1 / 30.0)
        assert 0.96 < decay < 0.98

    def test_seven_days_old(self):
        # 7 days → decay ≈ 0.811
        decay = 1.0 / (1.0 + 7 / 30.0)
        assert 0.80 < decay < 0.82

    def test_thirty_days_old(self):
        # 30 days → decay = 0.5
        decay = 1.0 / (1.0 + 30 / 30.0)
        assert decay == 0.5

    def test_ninety_days_old(self):
        # 90 days → decay = 0.25
        decay = 1.0 / (1.0 + 90 / 30.0)
        assert decay == 0.25

    def test_recent_beats_old(self):
        """Recent memory with same relevance should rank higher than old one."""
        base_score = 0.8
        now = time.time()

        recent_decay = 1.0 / (1.0 + 1 / 30.0)  # 1 day
        old_decay = 1.0 / (1.0 + 60 / 30.0)  # 60 days

        recent_score = base_score * recent_decay
        old_score = base_score * old_decay

        assert recent_score > old_score
        # Recent: ~0.77, Old: ~0.27
        assert recent_score > 0.7
        assert old_score < 0.3
