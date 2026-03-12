"""Cron tool definitions — agent-facing tools for scheduled jobs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from qanot.agent import ToolRegistry

logger = logging.getLogger(__name__)


def register_cron_tools(
    registry: ToolRegistry,
    cron_dir: str,
    scheduler_ref: object | None = None,
) -> None:
    """Register cron management tools."""

    jobs_path = Path(cron_dir) / "jobs.json"

    def _load_jobs() -> list[dict]:
        if jobs_path.exists():
            try:
                return json.loads(jobs_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    def _save_jobs(jobs: list[dict]) -> None:
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── cron_create ──
    async def cron_create(params: dict) -> str:
        name = params.get("name", "").strip()
        schedule = params.get("schedule", "").strip()
        mode = params.get("mode", "isolated")
        prompt = params.get("prompt", "").strip()

        if not name or not schedule or not prompt:
            return json.dumps({"error": "name, schedule, and prompt are required"})

        if mode not in ("systemEvent", "isolated"):
            return json.dumps({"error": "mode must be 'systemEvent' or 'isolated'"})

        jobs = _load_jobs()

        # Check for duplicate
        if any(j["name"] == name for j in jobs):
            return json.dumps({"error": f"Job '{name}' already exists. Use cron_update to modify."})

        job = {
            "name": name,
            "schedule": schedule,
            "mode": mode,
            "prompt": prompt,
            "enabled": True,
        }
        jobs.append(job)
        _save_jobs(jobs)

        # Notify scheduler to reload
        if scheduler_ref and hasattr(scheduler_ref, "reload_jobs"):
            await scheduler_ref.reload_jobs()

        logger.info("Cron job created: %s (%s)", name, schedule)
        return json.dumps({"success": True, "job": job})

    registry.register(
        name="cron_create",
        description="Rejali ish yaratish. mode: 'isolated' (mustaqil agent) yoki 'systemEvent' (asosiy sessiyaga yuborish).",
        parameters={
            "type": "object",
            "required": ["name", "schedule", "prompt"],
            "properties": {
                "name": {"type": "string", "description": "Ish nomi (unikal)"},
                "schedule": {"type": "string", "description": "Cron ifodasi (masalan: '0 */4 * * *' = har 4 soatda)"},
                "mode": {"type": "string", "description": "Bajarish turi: 'isolated' yoki 'systemEvent'", "default": "isolated"},
                "prompt": {"type": "string", "description": "Agent uchun vazifa/ko'rsatma"},
            },
        },
        handler=cron_create,
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

        if scheduler_ref and hasattr(scheduler_ref, "reload_jobs"):
            await scheduler_ref.reload_jobs()

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
    )

    # ── cron_update ──
    async def cron_update(params: dict) -> str:
        name = params.get("name", "").strip()
        if not name:
            return json.dumps({"error": "name is required"})

        jobs = _load_jobs()
        found = None
        for j in jobs:
            if j["name"] == name:
                found = j
                break

        if not found:
            return json.dumps({"error": f"Job '{name}' not found"})

        if "schedule" in params:
            found["schedule"] = params["schedule"]
        if "mode" in params:
            found["mode"] = params["mode"]
        if "prompt" in params:
            found["prompt"] = params["prompt"]
        if "enabled" in params:
            found["enabled"] = params["enabled"]

        _save_jobs(jobs)

        if scheduler_ref and hasattr(scheduler_ref, "reload_jobs"):
            await scheduler_ref.reload_jobs()

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
    )
