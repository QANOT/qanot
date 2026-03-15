"""APScheduler-based cron executor with isolated agent spawner and self-healing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from qanot.tools.jobs_io import load_jobs, save_jobs

if TYPE_CHECKING:
    from qanot.agent import Agent, ToolRegistry
    from qanot.config import Config
    from qanot.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Heartbeat response token — agent returns this when nothing needs attention
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"

# Default heartbeat prompt (reads the real HEARTBEAT.md checklist)
HEARTBEAT_PROMPT = (
    "HEARTBEAT: You are running as an autonomous self-healing agent.\n"
    "Read HEARTBEAT.md and follow every check listed there.\n\n"
    "Rules:\n"
    "- Fix issues silently using your tools (read_file, write_file, etc.)\n"
    "- If you found and fixed issues, write a summary to proactive-outbox.md\n"
    "- If nothing needs attention, respond ONLY with: HEARTBEAT_OK\n"
    "- Do NOT invent tasks. Only act on real issues found in workspace files.\n"
    "- Keep reports concise — what you found, what you fixed, recommendations.\n"
)

# Daily briefing prompt
BRIEFING_PROMPT = (
    "DAILY BRIEFING: Create a concise morning summary for the owner.\n\n"
    "Steps:\n"
    "1. Read yesterday's and today's daily notes in memory/\n"
    "2. Read SESSION-STATE.md for active context\n"
    "3. Read MEMORY.md for important long-term facts\n"
    "4. Check if any cron jobs ran overnight (list_files in the sessions dir)\n\n"
    "Write a briefing to proactive-outbox.md with:\n"
    "- **Pending tasks** from yesterday that weren't completed\n"
    "- **Key events** — what happened in recent conversations\n"
    "- **Reminders** — upcoming scheduled items or deadlines mentioned\n"
    "- **Suggestions** — one actionable recommendation based on patterns\n\n"
    "Keep it SHORT (under 500 chars). Use bullet points.\n"
    "If there's truly nothing to report (no notes, no tasks), respond with: HEARTBEAT_OK\n"
)


class CronScheduler:
    """Manages scheduled cron jobs using APScheduler with self-healing."""

    def __init__(
        self,
        config: "Config",
        provider: "LLMProvider",
        tool_registry: "ToolRegistry",
        main_agent: "Agent | None" = None,
        message_queue: asyncio.Queue | None = None,
    ):
        self.config = config
        self.provider = provider
        self.tool_registry = tool_registry
        self.main_agent = main_agent
        self.message_queue = message_queue or asyncio.Queue()
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self._jobs_path = Path(config.cron_dir) / "jobs.json"
        # Track last user activity to skip heartbeat when user is active
        self._last_user_activity: float = 0.0
        # Idle threshold: skip heartbeat if user was active within this window (seconds)
        self._idle_threshold = 300  # 5 minutes

    def record_user_activity(self) -> None:
        """Record that a user interacted with the bot."""
        self._last_user_activity = asyncio.get_event_loop().time()

    def _is_user_idle(self) -> bool:
        """Check if user has been idle long enough for heartbeat."""
        if self._last_user_activity == 0.0:
            return True  # No activity recorded yet
        elapsed = asyncio.get_event_loop().time() - self._last_user_activity
        return elapsed >= self._idle_threshold

    def _load_jobs(self) -> list[dict]:
        """Load jobs from JSON file."""
        return load_jobs(self._jobs_path)

    def _ensure_builtin_jobs(self, jobs: list[dict]) -> list[dict]:
        """Ensure heartbeat and briefing jobs exist in the job list."""
        changed = False
        existing_names = {j["name"] for j in jobs}

        if "heartbeat" not in existing_names:
            jobs.append({
                "name": "heartbeat",
                "schedule": self.config.heartbeat_interval,
                "mode": "isolated",
                "prompt": HEARTBEAT_PROMPT,
                "enabled": self.config.heartbeat_enabled,
            })
            changed = True

        if "briefing" not in existing_names:
            jobs.append({
                "name": "briefing",
                "schedule": self.config.briefing_schedule,
                "mode": "isolated",
                "prompt": BRIEFING_PROMPT,
                "enabled": self.config.briefing_enabled,
            })
            changed = True

        if changed:
            save_jobs(self._jobs_path, jobs)
        return jobs

    def start(self) -> None:
        """Load jobs and start the scheduler."""
        jobs = self._load_jobs()
        jobs = self._ensure_builtin_jobs(jobs)

        for job in jobs:
            if not job.get("enabled", True):
                continue
            self._add_job(job)

        self.scheduler.start()
        logger.info("Cron scheduler started with %d jobs", len(jobs))

    def _add_job(self, job: dict) -> None:
        """Add a single job to the scheduler."""
        name = job["name"]
        schedule = job.get("schedule", "")
        at = job.get("at", "")
        mode = job.get("mode", "isolated")
        prompt = job["prompt"]
        delete_after_run = job.get("delete_after_run", False)
        tz = job.get("timezone", self.config.timezone)

        try:
            if at:
                # One-shot reminder at specific time
                trigger = DateTrigger(run_date=at, timezone=tz)
            elif schedule:
                # Recurring cron expression (minute hour day month day_of_week)
                parts = schedule.split()
                if len(parts) == 5:
                    trigger = CronTrigger(
                        minute=parts[0],
                        hour=parts[1],
                        day=parts[2],
                        month=parts[3],
                        day_of_week=parts[4],
                        timezone=tz,
                    )
                else:
                    logger.warning("Invalid cron expression for job %s: %s", name, schedule)
                    return
            else:
                logger.warning("Job %s has no schedule or at — skipping", name)
                return

            handler = self._run_isolated if mode == "isolated" else self._run_system_event
            self.scheduler.add_job(
                handler,
                trigger=trigger,
                id=f"cron_{name}",
                name=name,
                kwargs={
                    "job_name": name,
                    "prompt": prompt,
                    "delete_after_run": delete_after_run,
                },
                replace_existing=True,
            )

            sched_desc = at or schedule
            logger.info("Scheduled cron job: %s (%s, mode=%s)", name, sched_desc, mode)
        except Exception as e:
            logger.error("Failed to schedule job %s: %s", name, e)

    def _delete_job(self, job_name: str) -> None:
        """Delete a job from the jobs file after execution (for one-shot reminders)."""
        try:
            jobs = self._load_jobs()
            new_jobs = [j for j in jobs if j["name"] != job_name]
            if len(new_jobs) < len(jobs):
                save_jobs(self._jobs_path, new_jobs)
                logger.info("Auto-deleted one-shot job: %s", job_name)
        except Exception as e:
            logger.warning("Failed to auto-delete job %s: %s", job_name, e)

    async def _run_isolated(self, job_name: str, prompt: str, delete_after_run: bool = False) -> None:
        """Run an isolated agent for a cron job."""
        # Skip heartbeat/briefing if user is currently active (avoid wasting tokens)
        if job_name in ("heartbeat", "briefing") and not self._is_user_idle():
            logger.info("Heartbeat skipped — user is active")
            return

        # Check if HEARTBEAT.md is empty (skip API call to save tokens)
        if job_name == "heartbeat":
            hb_path = Path(self.config.workspace_dir) / "HEARTBEAT.md"
            if hb_path.exists():
                content = hb_path.read_text(encoding="utf-8").strip()
                # Skip if file is empty or contains only comments
                has_actionable = any(
                    not stripped.startswith("#")
                    for line in content.splitlines()
                    if (stripped := line.strip())
                )
                if not has_actionable:
                    logger.info("Heartbeat skipped — HEARTBEAT.md is empty")
                    return

        logger.info("Running isolated cron job: %s", job_name)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        session_id = f"cron-{job_name}-{ts}"

        try:
            from qanot.agent import spawn_isolated_agent

            result = await spawn_isolated_agent(
                config=self.config,
                provider=self.provider,
                tool_registry=self.tool_registry,
                prompt=prompt,
                session_id=session_id,
            )

            # Suppress HEARTBEAT_OK — don't deliver to users
            if job_name == "heartbeat" and result and _is_heartbeat_ok(result):
                logger.info("Heartbeat OK — nothing to report")
                return

            # Check if the agent wrote to proactive-outbox.md
            outbox_path = Path(self.config.workspace_dir) / "proactive-outbox.md"
            if outbox_path.exists():
                content = outbox_path.read_text(encoding="utf-8").strip()
                if content:
                    await self.message_queue.put({
                        "type": "proactive",
                        "text": content,
                        "source": job_name,
                    })
                    # Clear outbox after reading
                    outbox_path.write_text("", encoding="utf-8")
                    logger.info("Proactive outbox delivered from job: %s", job_name)

            logger.info("Isolated cron job completed: %s", job_name)
        except Exception as e:
            logger.error("Isolated cron job failed (%s): %s", job_name, e)
        finally:
            if delete_after_run:
                self._delete_job(job_name)

    async def _run_system_event(self, job_name: str, prompt: str, delete_after_run: bool = False) -> None:
        """Inject a prompt into the main agent's message queue."""
        logger.info("System event cron job: %s", job_name)
        await self.message_queue.put({
            "type": "proactive",
            "text": prompt,
            "source": job_name,
        })
        if delete_after_run:
            self._delete_job(job_name)

    async def reload_jobs(self) -> None:
        """Reload all jobs from disk."""
        # Remove existing jobs
        for job in self.scheduler.get_jobs():
            if job.id.startswith("cron_"):
                job.remove()

        # Re-add from file
        jobs = self._load_jobs()
        jobs = self._ensure_builtin_jobs(jobs)
        for job in jobs:
            if not job.get("enabled", True):
                continue
            self._add_job(job)

        logger.info("Cron jobs reloaded: %d jobs", len(jobs))

    def stop(self) -> None:
        """Stop the scheduler."""
        self.scheduler.shutdown(wait=False)
        logger.info("Cron scheduler stopped")


def _is_heartbeat_ok(text: str) -> bool:
    """Check if the agent response is a HEARTBEAT_OK (nothing to report).

    Handles variations: "HEARTBEAT_OK", "heartbeat_ok", with surrounding text.
    """
    stripped = text.strip().upper()
    # Exact match or the only meaningful content
    if stripped == HEARTBEAT_OK_TOKEN:
        return True
    # Allow minor surrounding text (e.g. "Everything is fine. HEARTBEAT_OK")
    if HEARTBEAT_OK_TOKEN in stripped and len(stripped) < 300:
        return True
    return False
