"""Supervisor-triggered graceful restart on config changes.

Some config fields (``model``, ``routing_enabled``, ``rag_enabled``,
plugin list, etc.) bake into objects at startup — the provider wrapper,
the plugin registry, the RAG index. Mutating them in the in-memory
Config doesn't retroactively rebuild those objects, so the live process
keeps behaving like the old config. The fix is not "re-wire every
subsystem on the fly" (lots of edge cases, state corruption risks) but
the cleaner approach openclaw uses: **exit the process cleanly and let
the supervisor restart us** with the new config applied at startup.

For QanotCloud bot containers, the supervisor is Docker with
``RestartPolicy: unless-stopped``; the container dies on exit(0) and
Docker respawns it within ~1 second.
For self-hosted qanot, the supervisor is systemd / launchd / schtasks
(see ``qanot/daemon.py``).
For local dev (``python -m qanot``), there is no supervisor — the
process exits and stays down, which is fine for dev.

The module's job:
  1. Classify whether a config field needs restart (``should_restart``).
  2. Schedule a restart with a short coalesce window so batch edits
     only trigger one restart (``schedule_restart``).
  3. Track in-flight LLM turns and drain them before exiting, so a
     user's half-complete answer isn't cut off.
  4. Refuse new turns during the drain so they aren't started just
     to be aborted (``is_shutting_down``).

This is intentionally minimal and doesn't try to be a generic lifecycle
manager. Other shutdown paths (SIGTERM, health failure) remain the
supervisor's problem.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# ── Classification ───────────────────────────────────────────────

# Fields whose change requires rebuilding objects that were constructed
# once at startup (provider, routing wrapper, RAG engine, plugin
# registry, context tracker, etc). Changing them in-memory does NOT
# take effect until the process restarts.
#
# Everything NOT listed here is treated as hot-reloadable — the code that
# reads the field looks it up on ``self.config`` every time, so the new
# value is picked up naturally on the next turn.
_RESTART_REQUIRED_FIELDS: frozenset[str] = frozenset({
    # Provider wiring
    "provider",
    "api_key",
    "model",
    # Routing wiring (sets up RoutingProvider wrapper at startup)
    "routing_enabled",
    "routing_model",
    "routing_mid_model",
    "routing_threshold",
    # RAG (FastEmbed + sqlite-vec loaded at startup, 300MB+ resident)
    "rag_enabled",
    "rag_mode",
    # Context tracker sized at startup
    "max_context_tokens",
    # Multi-agent / orchestration topology
    "agents_enabled",
    "agents",
    "group_orchestration",
    "orchestration_group_id",
    # Plugin list — plugin constructors run once at startup
    "plugins",
    # Telegram transport (polling vs webhook is decided once)
    "telegram_mode",
    "webhook_url",
    "webhook_port",
})


def should_restart(field: str) -> bool:
    """Return True if a change to ``field`` requires a process restart."""
    return field in _RESTART_REQUIRED_FIELDS


# ── Orchestration state (module-level singletons) ────────────────

# Seconds to wait after the first restart-triggering change before
# actually exiting. Any additional restart-required fields that change
# during this window piggyback on the same restart (batch admin edits).
_COALESCE_WINDOW_SECONDS = 5.0

# How long to wait for in-flight turns to finish before forcing exit.
# openclaw uses 5 minutes because its subagent LLM calls can run long;
# in qanot, a single turn rarely exceeds 60s, so this is plenty.
_INFLIGHT_DRAIN_TIMEOUT_SECONDS = 60.0

# Minimum time between restarts. Protects against an attacker (or bug)
# that rapid-fires config writes — we exit at most every ~30s.
_RESTART_COOLDOWN_SECONDS = 30.0


class _State:
    """All mutable module state, bundled so tests can reset it."""

    def __init__(self) -> None:
        self.pending_restart_task: asyncio.Task | None = None
        self.last_restart_at: float = 0.0
        self.inflight: int = 0
        self.shutting_down: bool = False

    def reset(self) -> None:
        self.pending_restart_task = None
        self.last_restart_at = 0.0
        self.inflight = 0
        self.shutting_down = False


_state = _State()


def _reset_for_tests() -> None:
    """Restore module state — tests only."""
    if _state.pending_restart_task and not _state.pending_restart_task.done():
        _state.pending_restart_task.cancel()
    _state.reset()


# ── In-flight tracking ──────────────────────────────────────────


def bump_inflight() -> None:
    """Mark a new turn as in-progress. Call at turn start."""
    _state.inflight += 1


def drop_inflight() -> None:
    """Mark a turn as finished. Call in turn's finally block."""
    _state.inflight = max(0, _state.inflight - 1)


def inflight_count() -> int:
    return _state.inflight


def is_shutting_down() -> bool:
    """Turn entry should call this and refuse new work if True."""
    return _state.shutting_down


# ── Scheduling ──────────────────────────────────────────────────


NotifyCallback = Callable[[], Awaitable[None]]


def schedule_restart(
    *,
    reason: str = "config_change",
    notify: NotifyCallback | None = None,
    _exit_fn: Callable[[int], None] | None = None,
) -> bool:
    """Schedule a graceful restart. Dedupes concurrent requests.

    Parameters:
        reason: human-readable reason for the restart (logged for audit).
        notify: optional async callable run right before exit — useful
            for sending the user a "restarting now" Telegram message.
        _exit_fn: test injection. Defaults to ``os._exit``.

    Returns True if a restart was scheduled (or was already pending);
    False if denied by cooldown.
    """
    if _state.shutting_down:
        logger.debug("schedule_restart(%s) ignored: already shutting down", reason)
        return True

    now = time.time()
    cooldown_remaining = _RESTART_COOLDOWN_SECONDS - (now - _state.last_restart_at)
    if _state.last_restart_at > 0 and cooldown_remaining > 0:
        logger.info(
            "schedule_restart(%s) denied: cooldown active (%.1fs remaining)",
            reason, cooldown_remaining,
        )
        return False

    if _state.pending_restart_task and not _state.pending_restart_task.done():
        logger.debug(
            "schedule_restart(%s) piggy-backing on already-pending restart",
            reason,
        )
        return True

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        logger.warning(
            "schedule_restart(%s) called with no running event loop", reason,
        )
        return False

    _state.pending_restart_task = loop.create_task(
        _do_graceful_restart(
            reason=reason,
            notify=notify,
            exit_fn=_exit_fn or os._exit,
        ),
    )
    return True


async def _do_graceful_restart(
    *,
    reason: str,
    notify: NotifyCallback | None,
    exit_fn: Callable[[int], None],
) -> None:
    """Internal: coalesce → notify → drain → exit."""
    # 1) Coalesce — give rapid-fire config edits a chance to batch.
    await asyncio.sleep(_COALESCE_WINDOW_SECONDS)

    _state.shutting_down = True
    _state.last_restart_at = time.time()
    logger.info("Graceful restart beginning — reason=%s", reason)

    # 2) Notify user (best-effort, bounded).
    if notify is not None:
        try:
            await asyncio.wait_for(notify(), timeout=5.0)
        except Exception as e:
            logger.debug("Pre-restart notify failed (non-fatal): %s", e)

    # 3) Drain in-flight turns.
    drain_started = time.time()
    while _state.inflight > 0:
        elapsed = time.time() - drain_started
        if elapsed >= _INFLIGHT_DRAIN_TIMEOUT_SECONDS:
            logger.warning(
                "Drain timeout (%.0fs) hit with %d in-flight — forcing exit",
                _INFLIGHT_DRAIN_TIMEOUT_SECONDS, _state.inflight,
            )
            break
        logger.info(
            "Draining — %d turn(s) still in-flight (%.0fs elapsed)",
            _state.inflight, elapsed,
        )
        await asyncio.sleep(1.0)
    else:
        if _state.inflight == 0:
            logger.info("Drain complete — no active turns")

    # 4) Exit. Supervisor will restart us; os._exit bypasses atexit
    #    handlers that might deadlock waiting on pending asyncio tasks.
    logger.info("Exiting(0) for supervisor-triggered restart")
    exit_fn(0)
