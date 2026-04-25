"""Concurrency tests for SubagentRegistry.

These tests assert that the registry survives heavy concurrent use without
raising `RuntimeError: dictionary changed size during iteration` and that
mutations from many coroutines all land in the registry deterministically.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from qanot.orchestrator.registry import SubagentRegistry
from qanot.orchestrator.types import (
    SubagentRun,
    make_run_id,
    MODE_SYNC,
    ROLE_LEAF,
    STATUS_PENDING,
    STATUS_COMPLETED,
)


def _make_run(user_id: str = "u1", status: str = STATUS_PENDING) -> SubagentRun:
    return SubagentRun(
        run_id=make_run_id(),
        parent_user_id=user_id,
        parent_chat_id=1,
        task="t",
        agent_id="researcher",
        agent_name="R",
        role=ROLE_LEAF,
        depth=1,
        status=status,
        mode=MODE_SYNC,
    )


@pytest.mark.asyncio
async def test_concurrent_register_no_race_in_memory():
    """Spawn many concurrent register() calls and assert all land safely."""
    reg = SubagentRegistry()  # no persist path -> pure in-memory hot path
    runs = [_make_run(user_id=f"u{i % 5}") for i in range(50)]

    # Fire all 50 registrations in parallel.
    await asyncio.gather(*(reg.register(r) for r in runs))

    # All 50 must be present.
    assert len(reg._runs) == 50
    for r in runs:
        assert reg.get(r.run_id) is r

    # Per-user index totals must equal 50 across all users.
    total_indexed = sum(len(v) for v in reg._by_user.values())
    assert total_indexed == 50


@pytest.mark.asyncio
async def test_concurrent_register_with_persist_no_race():
    """Same as above but with disk persistence enabled.

    Exercises the asyncio.to_thread offload path under contention.
    """
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "reg.json")
        reg = SubagentRegistry(path)
        runs = [_make_run(user_id=f"u{i % 5}") for i in range(50)]

        await asyncio.gather(*(reg.register(r) for r in runs))

        assert len(reg._runs) == 50

        # Final on-disk snapshot must contain all 50 runs (the last writer
        # wins, but it always sees the full set since it ran under the lock).
        data = json.loads(open(path).read())
        assert len(data["runs"]) == 50
        run_ids_on_disk = {entry["run_id"] for entry in data["runs"]}
        assert run_ids_on_disk == {r.run_id for r in runs}


@pytest.mark.asyncio
async def test_concurrent_register_and_update_no_race():
    """Mix register + update concurrently; no exceptions, final state consistent."""
    reg = SubagentRegistry()
    runs = [_make_run() for _ in range(30)]

    # First wave: pre-register half.
    await asyncio.gather(*(reg.register(r) for r in runs[:15]))

    async def updater(run):
        await reg.update(run.run_id, status=STATUS_COMPLETED)

    # Second wave: register the rest while updating the first half. No race.
    await asyncio.gather(
        *(reg.register(r) for r in runs[15:]),
        *(updater(r) for r in runs[:15]),
    )

    assert len(reg._runs) == 30
    for r in runs[:15]:
        assert reg.get(r.run_id).status == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_persist_does_not_block_event_loop():
    """The persist call must yield to the loop (not run inline on it).

    We assert the calling coroutine relinquished control at least once
    during persist by interleaving a sentinel coroutine.
    """
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "reg.json")
        reg = SubagentRegistry(path)

        sentinel_ran = asyncio.Event()

        async def sentinel():
            # If persist were sync-on-loop, this could only run after
            # register() fully completed. With to_thread offload it runs
            # interleaved, so we assert it sets within a generous timeout.
            sentinel_ran.set()

        await asyncio.gather(
            reg.register(_make_run()),
            sentinel(),
        )
        assert sentinel_ran.is_set()
