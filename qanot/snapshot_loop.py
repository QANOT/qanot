"""Periodic conversation snapshot to disk.

The graceful-shutdown save in ``main.py`` covers ``docker stop``-style
exits. It does NOT cover: OOM kills, kernel segfaults, host reboots,
forced ``kill -9``, or any code path that bypasses the lifespan exit
handlers. In those cases the in-memory ``_conversations`` dict is lost
and the next turn falls back to JSONL session restore — which trims
to ``history_limit`` and drops anything older.

A periodic snapshot every ``SNAPSHOT_INTERVAL_SECONDS`` (default 300)
gives a worst-case loss bound of ~5 minutes of conversation per crash.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How often we dump the in-memory conversations dict to disk. 300s
# trades disk I/O for crash-resilience. Single JSON write per cycle is
# cheap (~100KB for 10 active conversations).
SNAPSHOT_INTERVAL_SECONDS = 300


async def periodic_snapshot_loop(agent) -> None:
    """Forever-loop: save the conversation snapshot every 5 minutes.

    Cancellation-safe (caught on shutdown), exception-resilient (failed
    writes log and continue). Save calls are idempotent — overwriting
    the previous snapshot atomically via tmp + rename.
    """
    while True:
        try:
            await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
            saved = agent.save_snapshot()
            if saved:
                logger.debug(
                    "periodic snapshot: saved %d conversations", saved,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            # Don't crash the loop on transient disk errors — try again
            # next tick.
            logger.warning("periodic snapshot error: %s", e)
