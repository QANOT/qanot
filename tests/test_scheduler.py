"""Tests for CronScheduler: heartbeat, idle detection, job loading, proactive outbox."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.config import Config
from qanot.scheduler import CronScheduler, _is_heartbeat_ok


# ── Helpers ──────────────────────────────────────────────────


def make_config(tmp_path, **overrides) -> Config:
    kwargs = dict(
        workspace_dir=str(tmp_path / "workspace"),
        sessions_dir=str(tmp_path / "sessions"),
        cron_dir=str(tmp_path / "cron"),
        plugins_dir=str(tmp_path / "plugins"),
        bot_token="123:FAKE",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


# ── Heartbeat OK Detection ──────────────────────────────────


class TestHeartbeatOkDetection:
    def test_exact_match(self):
        assert _is_heartbeat_ok("HEARTBEAT_OK") is True

    def test_case_insensitive(self):
        assert _is_heartbeat_ok("heartbeat_ok") is True

    def test_with_whitespace(self):
        assert _is_heartbeat_ok("  HEARTBEAT_OK  \n") is True

    def test_with_surrounding_text(self):
        assert _is_heartbeat_ok("Everything fine. HEARTBEAT_OK") is True

    def test_long_text_not_ok(self):
        # Over 300 chars should not be treated as HEARTBEAT_OK
        long_text = "A" * 301 + " HEARTBEAT_OK"
        assert _is_heartbeat_ok(long_text) is False

    def test_no_token_present(self):
        assert _is_heartbeat_ok("All systems nominal") is False

    def test_empty_string(self):
        assert _is_heartbeat_ok("") is False


# ── Idle Detection ───────────────────────────────────────────


class TestIdleDetection:
    def test_no_activity_is_idle(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        # No activity recorded yet -> should be idle
        assert sched._is_user_idle() is True

    def test_recent_activity_not_idle(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )

        loop = asyncio.new_event_loop()
        try:
            # Record activity at current time
            sched._last_user_activity = loop.time()
            # Monkey-patch _is_user_idle to use the same loop
            with patch("asyncio.get_event_loop", return_value=loop):
                assert sched._is_user_idle() is False
        finally:
            loop.close()

    def test_old_activity_is_idle(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )

        loop = asyncio.new_event_loop()
        try:
            # Activity was 10 minutes ago (well past 5-minute threshold)
            sched._last_user_activity = loop.time() - 600
            with patch("asyncio.get_event_loop", return_value=loop):
                assert sched._is_user_idle() is True
        finally:
            loop.close()

    def test_record_user_activity(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        assert sched._last_user_activity == 0.0
        loop = asyncio.new_event_loop()
        try:
            with patch("asyncio.get_event_loop", return_value=loop):
                sched.record_user_activity()
                assert sched._last_user_activity > 0
        finally:
            loop.close()


# ── Heartbeat Skip Conditions ────────────────────────────────


class TestHeartbeatSkipConditions:
    @pytest.mark.asyncio
    async def test_skip_when_user_active(self, tmp_path):
        """Heartbeat should skip if user is currently active."""
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        # Simulate recent activity
        sched._is_user_idle = MagicMock(return_value=False)

        # This should return without calling spawn_isolated_agent
        with patch("qanot.agent.spawn_isolated_agent") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_heartbeat_md_empty(self, tmp_path):
        """Heartbeat should skip if HEARTBEAT.md has no actionable content."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        hb_path = ws / "HEARTBEAT.md"
        hb_path.write_text("# Heartbeat Checklist\n\n# Just comments\n")

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_when_heartbeat_md_has_content(self, tmp_path):
        """Heartbeat should run when HEARTBEAT.md has actionable items."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        hb_path = ws / "HEARTBEAT.md"
        hb_path.write_text("# Checklist\n\n- Check disk space\n- Verify backups\n")

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="HEARTBEAT_OK") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_ok_suppressed(self, tmp_path):
        """HEARTBEAT_OK responses should not be delivered to users."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        hb_path = ws / "HEARTBEAT.md"
        hb_path.write_text("- Check logs\n")

        config = make_config(tmp_path)
        queue = asyncio.Queue()
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
            message_queue=queue,
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="HEARTBEAT_OK"):
            await sched._run_isolated(job_name="heartbeat", prompt="test")

        # Queue should remain empty (HEARTBEAT_OK suppressed)
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_non_heartbeat_job_always_runs(self, tmp_path):
        """Non-heartbeat jobs should not check idle status."""
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=False)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="done") as mock_spawn:
            await sched._run_isolated(job_name="daily_report", prompt="generate report")
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_no_file_runs_normally(self, tmp_path):
        """If HEARTBEAT.md does not exist, heartbeat should run."""
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)
        # workspace exists but no HEARTBEAT.md
        (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="HEARTBEAT_OK"):
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            # Should have called spawn since file doesn't exist


class TestHeartbeatFollowupSweep:
    """The heartbeat is the safety net for follow-ups whose one-shot cron
    job got dropped (server downtime, manual jobs.json edit, etc.)."""

    def _seed_overdue(self, tmp_path, ids):
        """Write a followups.json with the given ids, all due in the past."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        items = []
        for fid in ids:
            items.append({
                "id": fid,
                "status": "open",
                "topic": f"topic for {fid}",
                "due": "2020-01-01T00:00:00+00:00",  # well in the past
                "created": "2020-01-01T00:00:00+00:00",
            })
        (ws / "followups.json").write_text(
            json.dumps({"version": 1, "items": items}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_runs_when_only_overdue_followups_present(self, tmp_path):
        """Empty HEARTBEAT.md must NOT short-circuit when there are
        overdue follow-ups — that's the whole point of the sweep."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        (ws / "HEARTBEAT.md").write_text("# only comments\n")
        self._seed_overdue(tmp_path, ["ftk_aaaa1111"])

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch(
            "qanot.agent.spawn_isolated_agent",
            new_callable=AsyncMock, return_value="HEARTBEAT_OK",
        ) as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="HB_BASE")
            mock_spawn.assert_called_once()
            sent_prompt = mock_spawn.call_args.kwargs.get("prompt") or \
                mock_spawn.call_args.args[0] if mock_spawn.call_args.args else \
                mock_spawn.call_args.kwargs["prompt"]
            assert "ftk_aaaa1111" in sent_prompt
            assert "HB_BASE" in sent_prompt

    @pytest.mark.asyncio
    async def test_skip_when_no_actionable_and_no_overdue(self, tmp_path):
        """Both gates must agree to skip."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        (ws / "HEARTBEAT.md").write_text("# only comments\n")
        # No followups.json at all — nothing overdue.

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="x")
            mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_overdue_capped_at_three(self, tmp_path):
        """A backlog must never blow up the heartbeat token budget."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        (ws / "HEARTBEAT.md").write_text("- check logs\n")
        self._seed_overdue(tmp_path, [
            "ftk_111", "ftk_222", "ftk_333", "ftk_444", "ftk_555",
        ])

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        ids = sched._overdue_followup_ids()
        assert len(ids) == 3

    def test_helper_returns_empty_when_disabled(self, tmp_path):
        self._seed_overdue(tmp_path, ["ftk_aaa"])
        config = make_config(tmp_path, followup_enabled=False)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        assert sched._overdue_followup_ids() == []


# ── Scheduler Load Jobs ──────────────────────────────────────


class TestCronSchedulerJobs:
    def test_load_jobs_empty_file(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("[]")

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._load_jobs()
        assert jobs == []

    def test_load_jobs_missing_file(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._load_jobs()
        assert jobs == []

    def test_load_jobs_invalid_json(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("not json")

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._load_jobs()
        assert jobs == []

    def test_ensure_builtin_jobs_adds_if_missing(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("[]")

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._ensure_builtin_jobs([])
        assert any(j["name"] == "heartbeat" for j in jobs)
        assert any(j["name"] == "briefing" for j in jobs)

    def test_ensure_builtin_jobs_no_duplicate(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)

        existing = [
            {"name": "heartbeat", "schedule": "*/30 * * * *", "mode": "isolated", "prompt": "test", "enabled": True},
            {"name": "briefing", "schedule": "0 8 * * *", "mode": "isolated", "prompt": "test", "enabled": True},
        ]
        (cron_dir / "jobs.json").write_text(json.dumps(existing))

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._ensure_builtin_jobs(existing)
        assert len([j for j in jobs if j["name"] == "heartbeat"]) == 1
        assert len([j for j in jobs if j["name"] == "briefing"]) == 1


# ── Proactive Outbox ────────────────────────────────────────


class TestProactiveOutbox:
    @pytest.mark.asyncio
    async def test_outbox_content_queued(self, tmp_path):
        """Proactive outbox content should be put into the message queue."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        outbox = ws / "proactive-outbox.md"
        outbox.write_text("Found disk usage at 95%. Cleaned temp files.")

        # Also create HEARTBEAT.md with content so it doesn't skip
        (ws / "HEARTBEAT.md").write_text("- Check disk space\n")

        config = make_config(tmp_path)
        queue = asyncio.Queue()
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
            message_queue=queue,
        )
        sched._is_user_idle = MagicMock(return_value=True)

        # Agent returns a non-HEARTBEAT_OK result (indicating work was done)
        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="Fixed disk issue"):
            await sched._run_isolated(job_name="heartbeat", prompt="check")

        assert not queue.empty()
        msg = await queue.get()
        assert msg["type"] == "proactive"
        assert "95%" in msg["text"]
        assert msg["source"] == "heartbeat"

        # Outbox should be cleared after reading
        assert outbox.read_text() == ""

    @pytest.mark.asyncio
    async def test_empty_outbox_not_queued(self, tmp_path):
        """Empty proactive outbox should not enqueue anything."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        outbox = ws / "proactive-outbox.md"
        outbox.write_text("")

        (ws / "HEARTBEAT.md").write_text("- Check logs\n")

        config = make_config(tmp_path)
        queue = asyncio.Queue()
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
            message_queue=queue,
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="Fixed something"):
            await sched._run_isolated(job_name="heartbeat", prompt="check")

        assert queue.empty()
