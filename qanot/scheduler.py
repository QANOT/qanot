"""APScheduler-based cron executor with isolated agent spawner."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from qanot.agent import Agent, ToolRegistry
    from qanot.config import Config
    from qanot.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Default heartbeat cron
HEARTBEAT_JOB = {
    "name": "heartbeat",
    "schedule": "0 */4 * * *",  # Every 4 hours
    "mode": "isolated",
    "prompt": (
        "HEARTBEAT: Read HEARTBEAT.md and perform self-improvement checks:\n"
        "1. Check proactive-tracker.md — overdue behaviors?\n"
        "2. Pattern check — repeated requests to automate?\n"
        "3. Outcome check — decisions >7 days old to follow up?\n"
        "4. Memory — context %, update MEMORY.md with distilled learnings\n"
        "5. Proactive surprise — anything to delight human?\n"
        "If you have a message for the human, write it to /data/workspace/proactive-outbox.md"
    ),
    "enabled": True,
}


class CronScheduler:
    """Manages scheduled cron jobs using APScheduler."""

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

    def _load_jobs(self) -> list[dict]:
        """Load jobs from JSON file."""
        if self._jobs_path.exists():
            try:
                jobs = json.loads(self._jobs_path.read_text(encoding="utf-8"))
                return jobs
            except json.JSONDecodeError:
                return []
        return []

    def _ensure_heartbeat(self, jobs: list[dict]) -> list[dict]:
        """Ensure heartbeat job exists in the job list."""
        if not any(j["name"] == "heartbeat" for j in jobs):
            jobs.append(HEARTBEAT_JOB.copy())
            # Save back
            self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
            self._jobs_path.write_text(
                json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return jobs

    def start(self) -> None:
        """Load jobs and start the scheduler."""
        jobs = self._load_jobs()
        jobs = self._ensure_heartbeat(jobs)

        for job in jobs:
            if not job.get("enabled", True):
                continue
            self._add_job(job)

        self.scheduler.start()
        logger.info("Cron scheduler started with %d jobs", len(jobs))

    def _add_job(self, job: dict) -> None:
        """Add a single job to the scheduler."""
        name = job["name"]
        schedule = job["schedule"]
        mode = job.get("mode", "isolated")
        prompt = job["prompt"]

        try:
            # Parse cron expression (minute hour day month day_of_week)
            parts = schedule.split()
            if len(parts) == 5:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                    timezone=self.config.timezone,
                )
            else:
                logger.warning("Invalid cron expression for job %s: %s", name, schedule)
                return

            if mode == "isolated":
                self.scheduler.add_job(
                    self._run_isolated,
                    trigger=trigger,
                    id=f"cron_{name}",
                    name=name,
                    kwargs={"job_name": name, "prompt": prompt},
                    replace_existing=True,
                )
            else:  # systemEvent
                self.scheduler.add_job(
                    self._run_system_event,
                    trigger=trigger,
                    id=f"cron_{name}",
                    name=name,
                    kwargs={"job_name": name, "prompt": prompt},
                    replace_existing=True,
                )

            logger.info("Scheduled cron job: %s (%s, mode=%s)", name, schedule, mode)
        except Exception as e:
            logger.error("Failed to schedule job %s: %s", name, e)

    async def _run_isolated(self, job_name: str, prompt: str) -> None:
        """Run an isolated agent for a cron job."""
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
                    # Clear outbox
                    outbox_path.write_text("", encoding="utf-8")

            logger.info("Isolated cron job completed: %s", job_name)
        except Exception as e:
            logger.error("Isolated cron job failed (%s): %s", job_name, e)

    async def _run_system_event(self, job_name: str, prompt: str) -> None:
        """Inject a prompt into the main agent's message queue."""
        logger.info("System event cron job: %s", job_name)
        await self.message_queue.put({
            "type": "system_event",
            "text": prompt,
            "source": job_name,
        })

    async def reload_jobs(self) -> None:
        """Reload all jobs from disk."""
        # Remove existing jobs
        for job in self.scheduler.get_jobs():
            if job.id.startswith("cron_"):
                job.remove()

        # Re-add from file
        jobs = self._load_jobs()
        jobs = self._ensure_heartbeat(jobs)
        for job in jobs:
            if not job.get("enabled", True):
                continue
            self._add_job(job)

        logger.info("Cron jobs reloaded: %d jobs", len(jobs))

    def stop(self) -> None:
        """Stop the scheduler."""
        self.scheduler.shutdown(wait=False)
        logger.info("Cron scheduler stopped")
