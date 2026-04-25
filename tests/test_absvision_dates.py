"""Regression test for the absvision plugin's local-date helper.

Plugin produces ``YYYY-MM-DD`` strings used as ``?date=`` query params on
the ABS HR API. The API expects Tashkent business days, so a UTC clock
silently mis-bills the day boundary (19:00–24:00 UTC = 00:00–05:00 next
day Tashkent) — this test pins the conversion."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_FILE = Path(__file__).parent.parent / "plugins" / "absvision" / "plugin.py"


@pytest.fixture(scope="module")
def absvision_mod():
    spec = importlib.util.spec_from_file_location(
        "absvision_plugin_under_test", str(PLUGIN_FILE),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["absvision_plugin_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_today_uses_tashkent_local_calendar(absvision_mod, monkeypatch):
    """At 21:00 UTC it's 02:00 next-day Tashkent — _today() must return
    the next day."""
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 4, 25, 21, 0, 0, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(absvision_mod, "datetime", _FrozenDateTime)
    assert absvision_mod._today() == "2026-04-26"


def test_today_within_business_hours_unchanged(absvision_mod, monkeypatch):
    """At 10:00 UTC == 15:00 Tashkent, same calendar day."""
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(absvision_mod, "datetime", _FrozenDateTime)
    assert absvision_mod._today() == "2026-04-25"
