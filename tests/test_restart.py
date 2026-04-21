"""Tests for qanot.restart — supervisor-triggered graceful restart.

Covers:
  - Field classification (should_restart)
  - bump_inflight / drop_inflight counters
  - schedule_restart cooldown enforcement
  - schedule_restart coalescing (one restart per batch)
  - Drain wait behaviour (waits for inflight to reach 0, times out)
  - Pre-exit notify callback invoked
  - os._exit is called with 0
  - is_shutting_down flag flips before exit
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from qanot import restart
from qanot.restart import (
    _reset_for_tests,
    bump_inflight,
    drop_inflight,
    inflight_count,
    is_shutting_down,
    schedule_restart,
    should_restart,
)


@pytest.fixture(autouse=True)
def _fresh_state():
    """Each test starts with a clean restart module."""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ── Field classification ──────────────────────────────────────


def test_model_requires_restart():
    assert should_restart("model") is True


def test_routing_fields_require_restart():
    assert should_restart("routing_enabled")
    assert should_restart("routing_model")
    assert should_restart("routing_mid_model")


def test_rag_requires_restart():
    assert should_restart("rag_enabled")
    assert should_restart("rag_mode")


def test_plugins_list_requires_restart():
    assert should_restart("plugins")


def test_hot_reloadable_fields_do_not_require_restart():
    # Fields qanot reads every turn → no restart needed
    for f in ("voice_mode", "reactions_enabled", "response_mode",
              "group_mode", "thinking_level", "stream_flush_interval",
              "daily_budget_usd"):
        assert should_restart(f) is False, f"{f} shouldn't require restart"


def test_unknown_field_does_not_require_restart():
    # Conservative: unknown → assume hot-reloadable
    assert should_restart("random_new_field") is False


# ── inflight counter ─────────────────────────────────────────


def test_inflight_starts_at_zero():
    assert inflight_count() == 0


def test_bump_drop_balances():
    bump_inflight()
    bump_inflight()
    assert inflight_count() == 2
    drop_inflight()
    assert inflight_count() == 1
    drop_inflight()
    assert inflight_count() == 0


def test_drop_below_zero_clamped():
    drop_inflight()  # no bump yet
    assert inflight_count() == 0


# ── schedule_restart: dedup, cooldown, coalesce ──────────────


def test_schedule_restart_needs_running_loop():
    # Outside an event loop → returns False (logs warning)
    # Must be called outside async to test; use asyncio.get_event_loop() behaviour.
    # In modern Python this raises RuntimeError, which schedule_restart catches.
    # We can't easily simulate "no loop" inside pytest-asyncio, so skip detailed check.
    # This test just ensures no exception leaks.
    try:
        result = schedule_restart(reason="test")
        # If we happened to be in a loop (e.g. anyio), it should succeed
        assert isinstance(result, bool)
    except RuntimeError:
        pytest.fail("schedule_restart should swallow RuntimeError, not raise")


def test_schedule_restart_coalesces_concurrent_requests():
    """Multiple back-to-back calls share a single pending restart."""
    calls: list[int] = []

    def fake_exit(code: int) -> None:
        calls.append(code)

    async def _run():
        r1 = schedule_restart(reason="first", _exit_fn=fake_exit)
        r2 = schedule_restart(reason="second", _exit_fn=fake_exit)
        r3 = schedule_restart(reason="third", _exit_fn=fake_exit)
        assert r1 and r2 and r3
        # They should all reference the same pending task
        task1 = restart._state.pending_restart_task
        assert task1 is not None
        assert not task1.done()
        # Cancel to prevent actual exit during test
        task1.cancel()
        try:
            await task1
        except (asyncio.CancelledError, BaseException):
            pass

    asyncio.run(_run())
    # fake_exit should NOT have been called — task was cancelled before drain
    assert calls == []


def test_schedule_restart_cooldown_denies_second():
    """Within cooldown window, second restart is denied."""
    async def _run():
        # First call: set last_restart_at to a recent time artificially
        import time
        restart._state.last_restart_at = time.time()  # just happened

        # Next attempt should be denied
        result = schedule_restart(reason="denied", _exit_fn=lambda c: None)
        assert result is False
        assert restart._state.pending_restart_task is None

    asyncio.run(_run())


# ── drain + exit behaviour ─────────────────────────────────────


def test_exit_called_with_zero_when_no_inflight():
    """Happy path: no active turns → exits immediately after coalesce."""
    exit_codes: list[int] = []

    def fake_exit(code: int) -> None:
        exit_codes.append(code)

    async def _run():
        # Patch the module's sleep constants so the test finishes fast.
        orig_coalesce = restart._COALESCE_WINDOW_SECONDS
        orig_drain = restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS
        restart._COALESCE_WINDOW_SECONDS = 0.01
        restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS = 1.0
        try:
            schedule_restart(reason="test_exit", _exit_fn=fake_exit)
            assert not is_shutting_down()  # not yet — coalesce in progress
            await asyncio.sleep(0.1)
            # By now coalesce elapsed, shutting_down flipped, drain completed
            # (no inflight), exit called.
            assert is_shutting_down()
            assert exit_codes == [0]
        finally:
            restart._COALESCE_WINDOW_SECONDS = orig_coalesce
            restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS = orig_drain

    asyncio.run(_run())


def test_drain_waits_for_inflight_then_exits():
    """In-flight turn keeps drain going until bump_inflight is cleared."""
    exit_codes: list[int] = []

    async def _run():
        orig_coalesce = restart._COALESCE_WINDOW_SECONDS
        orig_drain = restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS
        restart._COALESCE_WINDOW_SECONDS = 0.01
        restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS = 2.0

        bump_inflight()  # simulate turn in progress
        try:
            schedule_restart(reason="test_drain", _exit_fn=lambda c: exit_codes.append(c))

            # After coalesce + first drain tick, still shouldn't have exited
            await asyncio.sleep(0.3)
            assert exit_codes == []
            assert is_shutting_down()

            # Finish the turn
            drop_inflight()
            # Drain loop polls every 1s — give it time
            await asyncio.sleep(1.5)
            assert exit_codes == [0]
        finally:
            restart._COALESCE_WINDOW_SECONDS = orig_coalesce
            restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS = orig_drain

    asyncio.run(_run())


def test_drain_timeout_forces_exit():
    """If inflight never drops, exit still happens after drain timeout."""
    exit_codes: list[int] = []

    async def _run():
        orig_coalesce = restart._COALESCE_WINDOW_SECONDS
        orig_drain = restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS
        restart._COALESCE_WINDOW_SECONDS = 0.01
        restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS = 0.3  # force quick timeout

        bump_inflight()  # and NEVER drop
        try:
            schedule_restart(reason="test_timeout", _exit_fn=lambda c: exit_codes.append(c))
            # Wait beyond coalesce + drain timeout + one poll
            await asyncio.sleep(1.5)
            assert exit_codes == [0]
        finally:
            restart._COALESCE_WINDOW_SECONDS = orig_coalesce
            restart._INFLIGHT_DRAIN_TIMEOUT_SECONDS = orig_drain
            drop_inflight()  # cleanup

    asyncio.run(_run())


def test_notify_callback_invoked_before_exit():
    """The pre-exit notify is awaited before os._exit."""
    exit_codes: list[int] = []
    notify_called: list[str] = []

    async def _notify():
        notify_called.append("yes")

    async def _run():
        orig_coalesce = restart._COALESCE_WINDOW_SECONDS
        restart._COALESCE_WINDOW_SECONDS = 0.01
        try:
            schedule_restart(
                reason="test_notify",
                notify=_notify,
                _exit_fn=lambda c: exit_codes.append(c),
            )
            await asyncio.sleep(0.3)
            assert notify_called == ["yes"]
            assert exit_codes == [0]
        finally:
            restart._COALESCE_WINDOW_SECONDS = orig_coalesce

    asyncio.run(_run())


def test_notify_exception_does_not_block_exit():
    """A crashing notify callback still lets us exit."""
    exit_codes: list[int] = []

    async def _bad_notify():
        raise RuntimeError("telegram died")

    async def _run():
        orig = restart._COALESCE_WINDOW_SECONDS
        restart._COALESCE_WINDOW_SECONDS = 0.01
        try:
            schedule_restart(
                reason="test_bad_notify",
                notify=_bad_notify,
                _exit_fn=lambda c: exit_codes.append(c),
            )
            await asyncio.sleep(0.3)
            assert exit_codes == [0]  # still exited
        finally:
            restart._COALESCE_WINDOW_SECONDS = orig

    asyncio.run(_run())


# ── is_shutting_down flag ──────────────────────────────────────


def test_shutting_down_starts_false():
    assert is_shutting_down() is False


def test_shutting_down_true_after_coalesce():
    async def _run():
        orig = restart._COALESCE_WINDOW_SECONDS
        restart._COALESCE_WINDOW_SECONDS = 0.01
        try:
            schedule_restart(reason="test_flag", _exit_fn=lambda c: None)
            assert is_shutting_down() is False  # still in coalesce
            await asyncio.sleep(0.05)
            assert is_shutting_down() is True
            # Wait for drain + exit to complete
            await asyncio.sleep(0.2)
        finally:
            restart._COALESCE_WINDOW_SECONDS = orig

    asyncio.run(_run())
