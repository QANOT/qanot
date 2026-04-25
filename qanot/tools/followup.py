"""Task follow-up engine — agent-tracked open items with scheduled re-evaluation.

Pattern (Claude-Code-meets-OpenClaw, distilled):
- Each follow-up is a small JSON record in ``<workspace>/followups.json``.
- The follow-up ALSO creates a one-shot cron job that re-fires the agent
  in ``isolated`` mode at the entry's due time.
- When the cron fires, the agent reads its OWN tracker entry (full original
  context preserved) and decides: still open, resolved, or extend.
- Resolution flows through ``close_followup``, which marks the JSON entry
  done and removes the cron job (a no-op if it already auto-deleted).

What we explicitly do NOT do:
  - Per-job execution history. The single ``last_check`` line is enough
    for personal use; the audit trail lives in session logs anyway.
  - Hard age expiry. Personal follow-ups can be "remind me in 6 weeks";
    the user, not a 7-day timer, decides when to give up.
  - SQLite or any new dependency. JSON is plenty for the volumes involved
    (dozens of open items, not tens of thousands).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qanot.registry import ToolRegistry
from qanot.tools.jobs_io import load_jobs, save_jobs

logger = logging.getLogger(__name__)

FOLLOWUPS_FILENAME = "followups.json"
FOLLOWUPS_SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"^ftk_[0-9a-f]{6,16}$")
CRON_JOB_PREFIX = "followup_"

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

# When the agent re-fires on a follow-up, this prompt template tells it
# what to do. We embed the ID so the isolated agent can find its own
# tracker entry without the calling user re-providing context.
RE_EVAL_PROMPT_TEMPLATE = (
    "Followup {fid} ni qayta baholang.\n\n"
    "1) followups.json fayldan {fid} yozuvini oling. Topic, why, context "
    "maydonlarini o'qing.\n"
    "2) Hozirgi holatni tekshiring — agar Telegram chat bilan bog'liq bo'lsa "
    "tg_get_chat_history yoki tg_scan_unread orqali, agar boshqa joy bo'lsa "
    "tegishli vositalarni ishlating.\n"
    "3) Qaror qabul qiling:\n"
    "   • Hal bo'lgan → close_followup({fid}, resolution=...)\n"
    "   • Hali kutilyapti, lekin xavotirli → proactive-outbox.md ga yozing\n"
    "   • Hali erta, keyinroq tekshirish kerak → track_followup yangi entry "
    "yarating va shu eski {fid} ni close_followup orqali yopib qo'ying "
    "(\"deferred to <new_id>\")\n"
    "Ortiqcha gap qilmang — ish qiling, natijani qisqa yozing."
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_tz(name: str | None) -> Any:
    """Look up an IANA timezone, falling back to UTC if it's unknown."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("followup: unknown timezone %r, using UTC", name)
        return timezone.utc


def _new_id() -> str:
    return f"ftk_{uuid.uuid4().hex[:8]}"


def _load_state(path: Path) -> dict[str, Any]:
    """Load the followups file. Always returns a well-formed dict."""
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": FOLLOWUPS_SCHEMA_VERSION, "items": []}

    if not isinstance(data, dict):
        return {"version": FOLLOWUPS_SCHEMA_VERSION, "items": []}
    items = data.get("items")
    if not isinstance(items, list):
        items = []
    # Preserve unknown future fields by only fixing the bits we care about.
    data["version"] = FOLLOWUPS_SCHEMA_VERSION
    data["items"] = [it for it in items if isinstance(it, dict) and "id" in it]
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write so a crash mid-save can't corrupt the queue."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _find(items: list[dict], fid: str) -> dict | None:
    for it in items:
        if it.get("id") == fid:
            return it
    return None


def _normalise_due(due_raw: str, default_tz: Any) -> tuple[str, datetime] | tuple[None, None]:
    """Accept ISO-8601 with or without offset; coerce to a tz-aware ISO string.

    Returns ``(iso_string, parsed_dt)`` on success, ``(None, None)`` on failure.
    Naive timestamps are interpreted in ``default_tz`` (i.e. the operator's
    configured local timezone), since "tomorrow at 14:00" is what users mean."""
    s = (due_raw or "").strip()
    if not s:
        return None, None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.isoformat(), dt


def _delete_cron_job(jobs_path: Path, job_name: str) -> bool:
    """Remove the named job from jobs.json. Returns True if removed."""
    jobs = load_jobs(jobs_path)
    new_jobs = [j for j in jobs if j.get("name") != job_name]
    if len(new_jobs) == len(jobs):
        return False
    save_jobs(jobs_path, new_jobs)
    return True


def _create_cron_job(
    *,
    jobs_path: Path,
    job_name: str,
    fid: str,
    due_iso: str,
    tz_name: str | None,
) -> None:
    """Write a one-shot ``isolated`` cron job that fires the re-eval prompt.

    Mirrors the shape produced by :mod:`qanot.tools.cron`'s ``cron_create``,
    so the existing scheduler picks it up with no extra wiring."""
    jobs = load_jobs(jobs_path)
    # Replace any prior job with the same name (e.g. on idempotent re-track).
    jobs = [j for j in jobs if j.get("name") != job_name]
    job: dict[str, Any] = {
        "name": job_name,
        "mode": "isolated",
        "prompt": RE_EVAL_PROMPT_TEMPLATE.format(fid=fid),
        "enabled": True,
        "at": due_iso,
        "delete_after_run": True,
    }
    if tz_name:
        job["timezone"] = tz_name
    jobs.append(job)
    save_jobs(jobs_path, jobs)


def _render_human(item: dict) -> str:
    """Markdown for a single item — what the user actually reads in Telegram."""
    lines = [f"**{item['id']}** · {item.get('status', '?')}"]
    lines.append(f"  · {item.get('topic', '')}")
    if item.get("status") == STATUS_OPEN:
        lines.append(f"  · due: {item.get('due', '?')}")
    why = item.get("why")
    if why:
        lines.append(f"  · why: {why}")
    last = item.get("last_check")
    if last:
        lines.append(f"  · last check: {last}")
    if item.get("status") == STATUS_RESOLVED:
        res = item.get("resolution") or "—"
        lines.append(f"  · resolution: {res}")
    return "\n".join(lines)


def register_followup_tools(
    registry: ToolRegistry,
    workspace_dir: str,
    cron_dir: str,
    *,
    timezone_name: str | None = None,
    scheduler_ref: object | None = None,
) -> None:
    """Register the three follow-up tools.

    ``timezone_name`` is the operator's IANA tz (``Config.timezone``). It's
    only used to resolve naive due-times the agent passes in.
    """
    state_path = Path(workspace_dir) / FOLLOWUPS_FILENAME
    jobs_path = Path(cron_dir) / "jobs.json"
    default_tz = _resolve_tz(timezone_name)

    async def _reload_scheduler() -> None:
        if scheduler_ref and hasattr(scheduler_ref, "reload_jobs"):
            try:
                await scheduler_ref.reload_jobs()  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning("followup: scheduler reload failed: %s", e)

    # ── track_followup ──
    async def track_followup(params: dict) -> str:
        topic = str(params.get("topic", "")).strip()
        due_raw = str(params.get("due", "")).strip()
        why = str(params.get("why", "")).strip()
        context = str(params.get("context", "")).strip()

        if not topic:
            return json.dumps(
                {"error": "topic kerak — qisqa, izlanadigan ifoda"},
                ensure_ascii=False,
            )
        if len(topic) > 500:
            return json.dumps({"error": "topic 500 belgidan oshmasin"})
        if len(why) > 2000:
            return json.dumps({"error": "why 2000 belgidan oshmasin"})
        if len(context) > 5000:
            return json.dumps({"error": "context 5000 belgidan oshmasin"})

        due_iso, due_dt = _normalise_due(due_raw, default_tz)
        if due_iso is None:
            return json.dumps(
                {"error": "due ISO 8601 bo'lishi kerak (e.g. '2026-04-26T08:00:00+05:00')"},
                ensure_ascii=False,
            )
        # Reject already-passed due times by more than 60s — the agent
        # almost certainly meant a future moment, and a past one would
        # fire instantly which is rarely what we want.
        now = datetime.now(timezone.utc)
        if (due_dt - now).total_seconds() < -60:
            return json.dumps(
                {"error": f"due o'tib ketgan: {due_iso}. Kelajakdagi vaqt bering."},
                ensure_ascii=False,
            )

        state = _load_state(state_path)
        fid = _new_id()
        # uuid collisions are astronomically unlikely but the loop is
        # cheap insurance and makes the test deterministic.
        while _find(state["items"], fid):
            fid = _new_id()

        item = {
            "id": fid,
            "status": STATUS_OPEN,
            "topic": topic,
            "due": due_iso,
            "why": why or None,
            "context": context or None,
            "created": _utc_iso(),
            "last_check": None,
            "resolution": None,
            "closed_at": None,
        }
        state["items"].append(item)
        _save_state(state_path, state)

        job_name = f"{CRON_JOB_PREFIX}{fid}"
        try:
            _create_cron_job(
                jobs_path=jobs_path,
                job_name=job_name,
                fid=fid,
                due_iso=due_iso,
                tz_name=timezone_name,
            )
        except Exception as e:
            # Roll back the state entry rather than leave an orphan that
            # never re-fires.
            state = _load_state(state_path)
            state["items"] = [x for x in state["items"] if x["id"] != fid]
            _save_state(state_path, state)
            logger.exception("followup: cron job creation failed for %s", fid)
            return json.dumps(
                {"error": f"cron job yaratib bo'lmadi: {e}"},
                ensure_ascii=False,
            )

        await _reload_scheduler()
        logger.info("followup tracked: %s due=%s topic=%r", fid, due_iso, topic)
        return json.dumps(
            {
                "ok": True,
                "id": fid,
                "due": due_iso,
                "status": STATUS_OPEN,
                "cron_job": job_name,
            },
            ensure_ascii=False,
        )

    registry.register(
        name="track_followup",
        description=(
            "Track an open question, decision, or promise that needs a "
            "scheduled re-check. Use this whenever a topic isn't resolved "
            "in this turn but should be revisited at a known future time. "
            "Creates an entry in followups.json AND a one-shot cron job "
            "that re-fires the agent at the due time to re-evaluate."
        ),
        parameters={
            "type": "object",
            "required": ["topic", "due"],
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "Short, searchable phrase describing what's open. "
                        "E.g. 'ABS server SSH ishlamayapti'."
                    ),
                },
                "due": {
                    "type": "string",
                    "description": (
                        "ISO 8601 timestamp of when to re-check. Naive "
                        "timestamps (no offset) are interpreted in the "
                        "operator's configured timezone. Must be future."
                    ),
                },
                "why": {
                    "type": "string",
                    "description": (
                        "Why this matters — context preserved across the "
                        "re-fire. Include who's involved and what's at stake."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional extra context the future-self needs: "
                        "chat ids, recipient_id tokens, links, prior reasoning."
                    ),
                },
            },
        },
        handler=track_followup,
        category="cron",
    )

    # ── list_followups ──
    async def list_followups(params: dict) -> str:
        status_filter = str(params.get("status", "open")).strip().lower()
        if status_filter not in ("open", "resolved", "all"):
            return json.dumps({"error": "status: 'open' | 'resolved' | 'all'"})

        state = _load_state(state_path)
        items = state["items"]
        if status_filter != "all":
            items = [it for it in items if it.get("status") == status_filter]

        # Open items: due-soon first. Resolved: most-recent close first.
        if status_filter == STATUS_RESOLVED:
            items = sorted(items, key=lambda x: x.get("closed_at") or "", reverse=True)
        else:
            items = sorted(items, key=lambda x: x.get("due") or "9999")

        return json.dumps(
            {
                "ok": True,
                "count": len(items),
                "status_filter": status_filter,
                "items": items,
            },
            ensure_ascii=False,
        )

    registry.register(
        name="list_followups",
        description=(
            "List tracked follow-ups. Default returns only open items "
            "sorted by due time (soonest first). Pass status='resolved' "
            "for a closeout history, or status='all' for everything."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "'open' (default), 'resolved', or 'all'.",
                },
            },
        },
        handler=list_followups,
        category="cron",
    )

    # ── close_followup ──
    async def close_followup(params: dict) -> str:
        fid = str(params.get("id", "")).strip()
        resolution = str(params.get("resolution", "")).strip()

        if not fid or not ID_PATTERN.match(fid):
            return json.dumps({"error": "id 'ftk_<hex>' shaklida bo'lsin"})
        if not resolution:
            return json.dumps(
                {"error": "resolution majburiy — qisqa natija/qaror"},
                ensure_ascii=False,
            )
        if len(resolution) > 5000:
            return json.dumps({"error": "resolution 5000 belgidan oshmasin"})

        state = _load_state(state_path)
        item = _find(state["items"], fid)
        if item is None:
            return json.dumps(
                {"error": f"{fid} topilmadi. list_followups bilan tekshiring."},
                ensure_ascii=False,
            )
        if item.get("status") == STATUS_RESOLVED:
            return json.dumps(
                {
                    "ok": True,
                    "already_resolved": True,
                    "id": fid,
                    "resolution": item.get("resolution"),
                },
                ensure_ascii=False,
            )

        item["status"] = STATUS_RESOLVED
        item["resolution"] = resolution
        item["closed_at"] = _utc_iso()
        _save_state(state_path, state)

        job_removed = False
        try:
            job_removed = _delete_cron_job(
                jobs_path=jobs_path,
                job_name=f"{CRON_JOB_PREFIX}{fid}",
            )
        except Exception as e:
            # Tracker is closed regardless. The cron job, if it still
            # exists, will fire and harmlessly find a resolved entry.
            logger.warning("followup: cron cleanup failed for %s: %s", fid, e)

        if job_removed:
            await _reload_scheduler()

        logger.info("followup closed: %s resolution=%r", fid, resolution[:80])
        return json.dumps(
            {
                "ok": True,
                "id": fid,
                "status": STATUS_RESOLVED,
                "cron_job_removed": job_removed,
            },
            ensure_ascii=False,
        )

    registry.register(
        name="close_followup",
        description=(
            "Mark a tracked follow-up as resolved. Pass the id (ftk_…) and "
            "a short resolution note. Removes the scheduled re-check if "
            "it hasn't fired yet."
        ),
        parameters={
            "type": "object",
            "required": ["id", "resolution"],
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Follow-up id, e.g. 'ftk_a3f1c2d4'.",
                },
                "resolution": {
                    "type": "string",
                    "description": "What happened — short, concrete outcome.",
                },
            },
        },
        handler=close_followup,
        category="cron",
    )

    logger.info("followup tools registered (state=%s)", state_path)


# ── Sweep helper (heartbeat / scheduler integration, optional) ──

def overdue_followups(workspace_dir: str, now: float | None = None) -> list[dict]:
    """Return open follow-ups whose due time has passed.

    Used by the heartbeat sweep as a safety net for cron jobs that got
    lost (e.g. server downtime spanning the due time). Cheap pure read,
    no side effects."""
    state = _load_state(Path(workspace_dir) / FOLLOWUPS_FILENAME)
    if not state["items"]:
        return []
    now_ts = time.time() if now is None else now
    out: list[dict] = []
    for it in state["items"]:
        if it.get("status") != STATUS_OPEN:
            continue
        due = it.get("due")
        if not due:
            continue
        try:
            due_dt = datetime.fromisoformat(due)
        except ValueError:
            continue
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=timezone.utc)
        if due_dt.timestamp() <= now_ts:
            out.append(it)
    return out
