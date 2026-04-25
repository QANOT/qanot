"""Concurrency tests for SessionWriter — no torn JSONL lines under load.

These guard the cross-platform locking contract. Before this fix Windows had
no locking at all (fcntl was simply absent → the `if fcntl is not None` branch
silently skipped flock), and Linux/macOS held the lock only for the bare
`f.write()` window which can still tear under contention without flush+fsync.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from qanot.session import SessionWriter


@pytest.mark.asyncio
async def test_async_concurrent_appends_produce_valid_jsonl(tmp_path):
    """50 concurrent _append coroutines must yield 50 well-formed JSONL lines."""
    writer = SessionWriter(str(tmp_path))
    n = 50

    # Pre-build entries so timing measures the I/O path, not entry construction.
    entries = [
        {"type": "message", "id": f"msg_{i:06d}", "payload": "x" * 200, "i": i}
        for i in range(n)
    ]

    await asyncio.gather(*(writer._append(e) for e in entries))

    with open(writer.session_path, encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == n, f"expected {n} lines, got {len(lines)}"
    seen_ids: set[str] = set()
    for idx, line in enumerate(lines):
        # Every line must end with exactly one newline (no torn write artefacts).
        assert line.endswith("\n"), f"line {idx} missing newline: {line!r}"
        # And every line must parse as valid JSON.
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            pytest.fail(f"line {idx} is not valid JSON: {e}\n{line!r}")
        assert obj["i"] in range(n)
        seen_ids.add(obj["id"])

    # Every entry must appear exactly once — no drops, no duplicates.
    assert seen_ids == {f"msg_{i:06d}" for i in range(n)}


@pytest.mark.asyncio
async def test_async_log_helpers_concurrent(tmp_path):
    """Concurrent log_user_message_async / log_assistant_message_async are safe."""
    writer = SessionWriter(str(tmp_path))

    async def one_turn(i: int) -> None:
        await writer.log_user_message_async(f"hello {i}", user_id=f"u{i % 5}")
        await writer.log_assistant_message_async(f"hi {i}", user_id=f"u{i % 5}")

    await asyncio.gather(*(one_turn(i) for i in range(25)))

    with open(writer.session_path, encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 50
    for line in lines:
        json.loads(line)  # raises if torn


def test_sync_threaded_appends_produce_valid_jsonl(tmp_path):
    """Sync log_* methods called from many threads must not tear lines either."""
    writer = SessionWriter(str(tmp_path))
    n = 50

    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()  # maximise contention by releasing all threads at once
        writer.log_user_message(f"msg {i}" + ("y" * 300), user_id=f"u{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(writer.session_path, encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == n
    for line in lines:
        json.loads(line)  # raises if torn
