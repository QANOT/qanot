"""Tests for the skill curator — age passes and the LLM review gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qanot.curator.loop import (
    ARCHIVE_AFTER_DAYS,
    REVIEW_INTERVAL_DAYS,
    REVIEW_MIN_AGENT_SKILLS,
    STALE_AFTER_DAYS,
    record_review_run,
    run_age_pass,
    should_run_review,
)
from qanot.skills import ARCHIVE_DIR_NAME, SkillStore, UsageStore


def _seed(skills_root: Path, name: str, *, idle_days: float,
          agent_created: bool = True, pinned: bool = False,
          status: str = "active") -> SkillStore:
    """Helper: create a skill and backdate its last_used_at."""
    store = SkillStore(skills_root)
    if not (skills_root / name).exists():
        store.create(name, f"{name} skill", "body", agent_created=agent_created)
    if not agent_created or pinned or status != "active":
        rec = store.usage.get(name) or store.usage.load().get(name)
        # Re-register to set flags, then patch the row.
        records = store.usage.load()
        rec = records[name]
        rec.agent_created = agent_created
        rec.pinned = pinned
        rec.status = status
        records[name] = rec
        store.usage.save(records)
    # Backdate last_used_at.
    if idle_days > 0:
        records = store.usage.load()
        rec = records[name]
        old = datetime.now(timezone.utc) - timedelta(days=idle_days)
        rec.last_used_at = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Use record_use first so last_used_at exists, then overwrite.
        records[name] = rec
        store.usage.save(records)
    return store


# ─── age pass ─────────────────────────────────────────────────────


class TestAgePass:
    def test_fresh_skill_left_alone(self, tmp_path):
        _seed(tmp_path, "fresh", idle_days=1)
        report = run_age_pass(tmp_path)
        assert report.marked_stale == []
        assert report.archived == []

    def test_stale_threshold(self, tmp_path):
        _seed(tmp_path, "rusty", idle_days=STALE_AFTER_DAYS + 1)
        report = run_age_pass(tmp_path)
        assert "rusty" in report.marked_stale
        # Status flipped in the store.
        usage = UsageStore(tmp_path)
        assert usage.get("rusty").status == "stale"

    def test_archive_threshold(self, tmp_path):
        store = _seed(tmp_path, "ancient", idle_days=ARCHIVE_AFTER_DAYS + 1)
        report = run_age_pass(tmp_path)
        assert "ancient" in report.archived
        # Directory was actually moved.
        assert not (tmp_path / "ancient").exists()
        assert (tmp_path / ARCHIVE_DIR_NAME / "ancient").exists()
        # Usage status reflects archival.
        assert store.usage.get("ancient").status == "archived"

    def test_pinned_exempt(self, tmp_path):
        _seed(tmp_path, "pinned-old", idle_days=ARCHIVE_AFTER_DAYS + 10,
              pinned=True)
        report = run_age_pass(tmp_path)
        assert "pinned-old" not in report.archived
        assert "pinned-old" in report.skipped_pinned

    def test_user_authored_exempt(self, tmp_path):
        _seed(tmp_path, "user-skill", idle_days=ARCHIVE_AFTER_DAYS + 10,
              agent_created=False)
        report = run_age_pass(tmp_path)
        assert "user-skill" not in report.archived
        assert "user-skill" in report.skipped_user_authored

    def test_reactivate_recently_used(self, tmp_path):
        # Seeded as stale but with a recent last_used_at.
        store = _seed(tmp_path, "warmed", idle_days=1, status="stale")
        report = run_age_pass(tmp_path)
        assert "warmed" in report.unmarked_active
        assert store.usage.get("warmed").status == "active"


# ─── review gate ──────────────────────────────────────────────────


class TestReviewGate:
    def test_blocks_when_user_active(self, tmp_path):
        for i in range(REVIEW_MIN_AGENT_SKILLS):
            _seed(tmp_path, f"s{i}", idle_days=1)
        active_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok, reason = should_run_review(tmp_path, last_user_activity_iso=active_iso)
        assert not ok
        assert "idle" in reason

    def test_blocks_when_recent_review(self, tmp_path):
        for i in range(REVIEW_MIN_AGENT_SKILLS):
            _seed(tmp_path, f"s{i}", idle_days=1)
        record_review_run(tmp_path)
        ok, reason = should_run_review(tmp_path)
        assert not ok
        assert "last review" in reason

    def test_blocks_when_too_few_skills(self, tmp_path):
        # Only 1 agent-created skill — below the min.
        _seed(tmp_path, "lonely", idle_days=1)
        ok, reason = should_run_review(tmp_path)
        assert not ok
        assert "agent-created" in reason

    def test_runs_when_all_gates_pass(self, tmp_path):
        for i in range(REVIEW_MIN_AGENT_SKILLS):
            _seed(tmp_path, f"s{i}", idle_days=1)
        # No recent review, no recent user activity.
        ok, reason = should_run_review(tmp_path)
        assert ok
        assert reason == "ready"

    def test_record_review_persists(self, tmp_path):
        for i in range(REVIEW_MIN_AGENT_SKILLS):
            _seed(tmp_path, f"s{i}", idle_days=1)
        record_review_run(tmp_path)
        # State file should exist.
        assert (tmp_path / ".curator_state.json").is_file()
        # And the gate should reject within REVIEW_INTERVAL_DAYS.
        ok, _ = should_run_review(tmp_path)
        assert not ok
