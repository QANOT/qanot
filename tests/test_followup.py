"""Unit tests for the follow-up engine."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from qanot.registry import ToolRegistry
from qanot.tools import followup as fu_mod
from qanot.tools.followup import (
    CRON_JOB_PREFIX,
    FOLLOWUPS_FILENAME,
    STATUS_OPEN,
    STATUS_RESOLVED,
    overdue_followups,
    register_followup_tools,
)
from qanot.tools.jobs_io import load_jobs


# ────────── Fixtures ──────────

@pytest.fixture
def workdirs(tmp_path):
    """Two sibling directories — workspace (state) and cron_dir (jobs.json)."""
    ws = tmp_path / "workspace"
    cron = tmp_path / "cron"
    ws.mkdir()
    cron.mkdir()
    return ws, cron


@pytest.fixture
def registry(workdirs):
    ws, cron = workdirs
    reg = ToolRegistry()
    register_followup_tools(
        reg,
        workspace_dir=str(ws),
        cron_dir=str(cron),
        timezone_name="Asia/Tashkent",
        scheduler_ref=None,
    )
    return reg


def _call(reg: ToolRegistry, name: str, params: dict) -> dict:
    """Execute a tool through the registry and parse the JSON result."""
    raw = asyncio.run(reg.execute(name, params))
    return json.loads(raw)


def _future(seconds: int) -> str:
    """ISO 8601 string in Tashkent local, ``seconds`` from now."""
    return (datetime.now(ZoneInfo("Asia/Tashkent"))
            + timedelta(seconds=seconds)).isoformat()


def _state_path(workdirs):
    ws, _ = workdirs
    return ws / FOLLOWUPS_FILENAME


def _jobs_path(workdirs):
    _, cron = workdirs
    return cron / "jobs.json"


# ────────── Tool registration ──────────

def test_three_tools_registered(registry):
    names = registry.tool_names if hasattr(registry, "tool_names") else \
        {t["name"] for t in registry.get_definitions()}
    assert {"track_followup", "list_followups", "close_followup"} <= set(names)


# ────────── track_followup ──────────

def test_track_followup_happy_path(registry, workdirs):
    result = _call(registry, "track_followup", {
        "topic": "ABS server SSH ishlamayapti",
        "due": _future(3600),
        "why": "kritik, @ibragimov_abdugani 03:13 yozdi",
        "context": "ABS-team chat",
    })
    assert result["ok"] is True
    fid = result["id"]
    assert fid.startswith("ftk_")
    assert result["status"] == STATUS_OPEN
    assert result["cron_job"] == f"{CRON_JOB_PREFIX}{fid}"

    # State file written.
    state = json.loads(_state_path(workdirs).read_text())
    assert state["version"] == 1
    assert len(state["items"]) == 1
    item = state["items"][0]
    assert item["id"] == fid
    assert item["topic"] == "ABS server SSH ishlamayapti"
    assert item["status"] == STATUS_OPEN
    assert item["why"].startswith("kritik")

    # Cron job written too — that's the actual scheduling effect.
    jobs = load_jobs(_jobs_path(workdirs))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == f"{CRON_JOB_PREFIX}{fid}"
    assert job["mode"] == "isolated"
    assert job["delete_after_run"] is True
    assert fid in job["prompt"]


def test_track_followup_naive_due_uses_configured_tz(registry, workdirs):
    """A naive ISO string ('2026-04-26T08:00:00') means 08:00 *Tashkent*,
    not 08:00 UTC — that's the operator's mental model.

    Build the naive ISO string from Tashkent-local "now + 2h" so this test
    works under any system timezone (CI runs in UTC; failed previously
    because datetime.now() is naive UTC there, naive_future was already in
    the past once interpreted as Tashkent).
    """
    tashkent = ZoneInfo("Asia/Tashkent")
    naive_future = (
        (datetime.now(tashkent) + timedelta(hours=2))
        .replace(tzinfo=None, microsecond=0)
        .isoformat()
    )
    result = _call(registry, "track_followup", {
        "topic": "naive due",
        "due": naive_future,
    })
    assert result["ok"] is True
    # Stored due should now carry an offset.
    state = json.loads(_state_path(workdirs).read_text())
    due = state["items"][0]["due"]
    assert "+05:00" in due, due


def test_track_followup_rejects_past_due(registry):
    result = _call(registry, "track_followup", {
        "topic": "in the past",
        "due": _future(-3600),  # one hour ago
    })
    assert "error" in result
    assert "past" in result["error"].lower() or "o'tib" in result["error"]


def test_track_followup_rejects_bad_iso(registry):
    result = _call(registry, "track_followup", {
        "topic": "x",
        "due": "tomorrow at lunch",
    })
    assert "error" in result
    assert "ISO" in result["error"]


def test_track_followup_rejects_empty_topic(registry):
    result = _call(registry, "track_followup", {
        "topic": "   ",
        "due": _future(3600),
    })
    assert "error" in result


def test_track_followup_rolls_back_on_cron_failure(
    registry, workdirs, monkeypatch,
):
    """If cron-write blows up, the state file must NOT carry an orphan
    that never re-fires."""
    def _explode(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fu_mod, "_create_cron_job", _explode)

    result = _call(registry, "track_followup", {
        "topic": "would orphan",
        "due": _future(3600),
    })
    assert "error" in result
    state = json.loads(_state_path(workdirs).read_text() or '{"items": []}') \
        if _state_path(workdirs).exists() else {"items": []}
    assert state.get("items", []) == []


# ────────── list_followups ──────────

def test_list_followups_default_only_open(registry):
    _call(registry, "track_followup", {
        "topic": "alpha", "due": _future(3600),
    })
    f2 = _call(registry, "track_followup", {
        "topic": "bravo", "due": _future(7200),
    })
    _call(registry, "close_followup", {
        "id": f2["id"], "resolution": "done",
    })

    result = _call(registry, "list_followups", {})
    assert result["count"] == 1
    assert result["items"][0]["topic"] == "alpha"


def test_list_followups_resolved_filter(registry):
    f1 = _call(registry, "track_followup", {
        "topic": "alpha", "due": _future(3600),
    })
    _call(registry, "close_followup", {
        "id": f1["id"], "resolution": "fixed",
    })
    result = _call(registry, "list_followups", {"status": "resolved"})
    assert result["count"] == 1
    assert result["items"][0]["resolution"] == "fixed"


def test_list_followups_open_sorted_by_due(registry):
    later = _call(registry, "track_followup", {
        "topic": "later", "due": _future(7200),
    })
    sooner = _call(registry, "track_followup", {
        "topic": "sooner", "due": _future(60),
    })
    result = _call(registry, "list_followups", {})
    ids = [it["id"] for it in result["items"]]
    assert ids == [sooner["id"], later["id"]]


def test_list_followups_rejects_bad_status(registry):
    result = _call(registry, "list_followups", {"status": "garbage"})
    assert "error" in result


# ────────── close_followup ──────────

def test_close_followup_happy_path(registry, workdirs):
    f = _call(registry, "track_followup", {
        "topic": "umid bilan uchrashuv",
        "due": _future(3600),
    })
    fid = f["id"]
    # Cron job exists.
    assert any(j["name"] == f"{CRON_JOB_PREFIX}{fid}"
               for j in load_jobs(_jobs_path(workdirs)))

    result = _call(registry, "close_followup", {
        "id": fid, "resolution": "uchrashuv ertaga 15:00 ga ko'chirildi",
    })
    assert result["ok"] is True
    assert result["status"] == STATUS_RESOLVED
    assert result["cron_job_removed"] is True

    # State updated.
    item = json.loads(_state_path(workdirs).read_text())["items"][0]
    assert item["status"] == STATUS_RESOLVED
    assert item["resolution"].startswith("uchrashuv")
    assert item["closed_at"]

    # Cron job is gone.
    assert not any(j["name"] == f"{CRON_JOB_PREFIX}{fid}"
                   for j in load_jobs(_jobs_path(workdirs)))


def test_close_followup_idempotent(registry):
    f = _call(registry, "track_followup", {
        "topic": "x", "due": _future(3600),
    })
    fid = f["id"]
    _call(registry, "close_followup", {"id": fid, "resolution": "first"})
    second = _call(registry, "close_followup", {"id": fid, "resolution": "second"})
    assert second["ok"] is True
    assert second["already_resolved"] is True
    assert second["resolution"] == "first"  # original preserved


def test_close_followup_not_found(registry):
    result = _call(registry, "close_followup", {
        "id": "ftk_deadbeef",
        "resolution": "x",
    })
    assert "error" in result
    assert "topilmadi" in result["error"]


def test_close_followup_bad_id_format(registry):
    result = _call(registry, "close_followup", {
        "id": "not-an-id",
        "resolution": "x",
    })
    assert "error" in result


def test_close_followup_requires_resolution(registry):
    f = _call(registry, "track_followup", {"topic": "x", "due": _future(3600)})
    result = _call(registry, "close_followup", {
        "id": f["id"], "resolution": "",
    })
    assert "error" in result


# ────────── overdue_followups helper ──────────

def test_overdue_followups_returns_only_past_open(registry, workdirs):
    f_due_now = _call(registry, "track_followup", {
        "topic": "soon", "due": _future(60),  # 60s ahead
    })
    f_far = _call(registry, "track_followup", {
        "topic": "far", "due": _future(7200),
    })
    # Stub time so the soon-one is past, the far-one is not.
    later = time.time() + 120
    overdue = overdue_followups(str(workdirs[0]), now=later)
    ids = [it["id"] for it in overdue]
    assert f_due_now["id"] in ids
    assert f_far["id"] not in ids


def test_overdue_skips_resolved(registry, workdirs):
    f = _call(registry, "track_followup", {
        "topic": "x", "due": _future(60),
    })
    _call(registry, "close_followup", {
        "id": f["id"], "resolution": "done",
    })
    overdue = overdue_followups(str(workdirs[0]), now=time.time() + 600)
    assert overdue == []


# ────────── _load_state / _save_state robustness ──────────

def test_load_state_missing_file_returns_empty(tmp_path):
    state = fu_mod._load_state(tmp_path / "nope.json")
    assert state == {"version": 1, "items": []}


def test_load_state_corrupt_json_returns_empty(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json at all")
    state = fu_mod._load_state(p)
    assert state == {"version": 1, "items": []}


def test_load_state_drops_malformed_items(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({
        "version": 1,
        "items": [
            {"id": "ftk_good", "topic": "ok", "status": "open"},
            "not a dict",
            {"no_id_field": True},
        ],
    }))
    state = fu_mod._load_state(p)
    assert len(state["items"]) == 1
    assert state["items"][0]["id"] == "ftk_good"
