"""Cron tool definitions — agent-facing tools for scheduled jobs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from qanot.agent import ToolRegistry
from qanot.tools.jobs_io import load_jobs as _load_jobs_from_file, save_jobs as _save_jobs_to_file

logger = logging.getLogger(__name__)


def register_cron_tools(
    registry: ToolRegistry,
    cron_dir: str,
    scheduler_ref: object | None = None,
) -> None:
    """Register cron management tools."""

    jobs_path = Path(cron_dir) / "jobs.json"

    def _load_jobs() -> list[dict]:
        return _load_jobs_from_file(jobs_path)

    def _save_jobs(jobs: list[dict]) -> None:
        _save_jobs_to_file(jobs_path, jobs)

    async def _reload_scheduler() -> None:
        if scheduler_ref and hasattr(scheduler_ref, "reload_jobs"):
            await scheduler_ref.reload_jobs()

    # ── cron_create ──
    async def cron_create(params: dict) -> str:
        name = str(params.get("name", "")).strip()
        schedule = str(params.get("schedule", "")).strip()
        at = str(params.get("at", "")).strip()  # ISO 8601 for one-shot reminders
        mode = str(params.get("mode", "isolated")).strip()
        prompt = str(params.get("prompt", "")).strip()
        delete_after_run = params.get("delete_after_run", False)
        tz = str(params.get("timezone", "")).strip()

        if not name or not prompt:
            return json.dumps({"error": "name and prompt are required"})

        if len(name) > 200:
            return json.dumps({"error": "name must be at most 200 characters"})
        if len(prompt) > 10000:
            return json.dumps({"error": "prompt must be at most 10000 characters"})
        if schedule and len(schedule) > 200:
            return json.dumps({"error": "schedule must be at most 200 characters"})
        if at and len(at) > 100:
            return json.dumps({"error": "at must be at most 100 characters"})
        if tz and len(tz) > 100:
            return json.dumps({"error": "timezone must be at most 100 characters"})
        if not isinstance(delete_after_run, bool):
            return json.dumps({"error": "delete_after_run must be a boolean"})

        if not schedule and not at:
            return json.dumps({"error": "Either 'schedule' (cron expression) or 'at' (ISO timestamp) is required"})

        if mode not in ("systemEvent", "isolated"):
            return json.dumps({"error": "mode must be 'systemEvent' or 'isolated'"})

        jobs = _load_jobs()

        # Check for duplicate
        if any(j["name"] == name for j in jobs):
            return json.dumps({"error": f"Job '{name}' already exists. Use cron_update to modify."})

        job = {
            "name": name,
            "mode": mode,
            "prompt": prompt,
            "enabled": True,
        }

        if at:
            # One-shot reminder at specific time
            job["at"] = at
            job["delete_after_run"] = True  # Always auto-delete one-shot jobs
        else:
            job["schedule"] = schedule

        if delete_after_run:
            job["delete_after_run"] = True
        if tz:
            job["timezone"] = tz

        jobs.append(job)
        _save_jobs(jobs)

        # Notify scheduler to reload
        await _reload_scheduler()

        logger.info("Cron job created: %s (%s)", name, at or schedule)
        return json.dumps({"success": True, "job": job})

    registry.register(
        name="cron_create",
        description=(
            "Create a scheduled job or one-shot reminder. "
            "For reminders: use 'at' with ISO 8601 timestamp (e.g. '2026-03-12T17:00:00+05:00'). "
            "For recurring jobs: use 'schedule' with cron expression (e.g. '0 */4 * * *'). "
            "mode: 'systemEvent' for simple text delivery, 'isolated' for tasks needing agent tools. "
            "Write the prompt as the reminder text the user will see when it fires."
        ),
        parameters={
            "type": "object",
            "required": ["name", "prompt"],
            "properties": {
                "name": {"type": "string", "description": "Unique job name"},
                "schedule": {"type": "string", "description": "Cron expression (e.g. '0 9 * * *' = every day at 9am)"},
                "at": {"type": "string", "description": "ISO 8601 timestamp for one-shot reminder (e.g. '2026-03-12T17:00:00+05:00')"},
                "mode": {"type": "string", "description": "'isolated' (full agent) or 'systemEvent' (text only)", "default": "systemEvent"},
                "prompt": {"type": "string", "description": "Reminder text or task prompt"},
                "delete_after_run": {"type": "boolean", "description": "Auto-delete after execution (default true for 'at' reminders)"},
                "timezone": {"type": "string", "description": "IANA timezone (e.g. 'Asia/Tashkent')"},
            },
        },
        handler=cron_create,
        category="cron",
    )

    # ── cron_list ──
    async def cron_list(params: dict) -> str:
        jobs = _load_jobs()
        if not jobs:
            return json.dumps({"message": "Hech qanday rejali ish yo'q"})
        return json.dumps(jobs, indent=2, ensure_ascii=False)

    registry.register(
        name="cron_list",
        description="Barcha rejali ishlar ro'yxati.",
        parameters={"type": "object", "properties": {}},
        handler=cron_list,
        category="cron",
    )

    # ── cron_delete ──
    async def cron_delete(params: dict) -> str:
        name = params.get("name", "").strip()
        if not name:
            return json.dumps({"error": "name is required"})

        jobs = _load_jobs()
        new_jobs = [j for j in jobs if j["name"] != name]

        if len(new_jobs) == len(jobs):
            return json.dumps({"error": f"Job '{name}' not found"})

        _save_jobs(new_jobs)

        await _reload_scheduler()

        return json.dumps({"success": True, "deleted": name})

    registry.register(
        name="cron_delete",
        description="Rejali ishni o'chirish.",
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "O'chiriladigan ish nomi"},
            },
        },
        handler=cron_delete,
        category="cron",
    )

    # ── cron_update ──
    async def cron_update(params: dict) -> str:
        name = params.get("name", "").strip()
        if not name:
            return json.dumps({"error": "name is required"})

        jobs = _load_jobs()
        found = next((j for j in jobs if j["name"] == name), None)

        if not found:
            return json.dumps({"error": f"Job '{name}' not found"})

        if "schedule" in params:
            sched = str(params["schedule"]).strip()
            if not sched or len(sched) > 200:
                return json.dumps({"error": "schedule must be a non-empty string (max 200 chars)"})
            found["schedule"] = sched
        if "mode" in params:
            mode_val = str(params["mode"]).strip()
            if mode_val not in ("systemEvent", "isolated"):
                return json.dumps({"error": "mode must be 'systemEvent' or 'isolated'"})
            found["mode"] = mode_val
        if "prompt" in params:
            prompt_val = str(params["prompt"]).strip()
            if not prompt_val or len(prompt_val) > 10000:
                return json.dumps({"error": "prompt must be a non-empty string (max 10000 chars)"})
            found["prompt"] = prompt_val
        if "enabled" in params:
            enabled_val = params["enabled"]
            if not isinstance(enabled_val, bool):
                return json.dumps({"error": "enabled must be a boolean"})
            found["enabled"] = enabled_val

        _save_jobs(jobs)

        await _reload_scheduler()

        return json.dumps({"success": True, "job": found})

    registry.register(
        name="cron_update",
        description="Rejali ishni yangilash.",
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Ish nomi"},
                "schedule": {"type": "string", "description": "Yangi cron ifodasi"},
                "mode": {"type": "string", "description": "Yangi bajarish turi"},
                "prompt": {"type": "string", "description": "Yangi ko'rsatma"},
                "enabled": {"type": "boolean", "description": "Yoqish/o'chirish"},
            },
        },
        handler=cron_update,
        category="cron",
    )
