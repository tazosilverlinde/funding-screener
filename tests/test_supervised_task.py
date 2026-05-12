"""Tests for the _supervised task supervisor (Round 64)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from funding_screener.background import _supervised


# ---------------- helper: minimal store stub ----------------


def _stub_store():
    """Just the attributes _supervised reaches for."""
    return SimpleNamespace(
        task_restart_counts={},
        recent_errors=[],
        last_error="",
        record_error=lambda msg: None,  # default no-op
    )


# ---------------- happy path ----------------


@pytest.mark.asyncio
async def test_clean_return_does_not_restart(caplog):
    """A task that returns normally is not restarted — _supervised treats it
    as completed work and exits.
    """
    store = _stub_store()
    call_count = [0]

    async def one_shot():
        call_count[0] += 1

    # We don't need to bound iterations because one_shot returns.
    await asyncio.wait_for(
        _supervised(lambda: one_shot(), "test", store), timeout=2.0,
    )
    assert call_count[0] == 1


# ---------------- crash + restart ----------------


@pytest.mark.asyncio
async def test_crash_triggers_restart_with_counter():
    """A task that raises gets caught, counter increments, factory re-invoked."""
    store = _stub_store()
    call_count = [0]

    async def crashy():
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated crash")
        # Second invocation succeeds → supervisor exits.

    # Patch sleep so the backoff doesn't actually wait.
    real_sleep = asyncio.sleep

    async def _fast_sleep(secs):
        await real_sleep(0)

    asyncio.sleep_orig = asyncio.sleep
    asyncio.sleep = _fast_sleep
    try:
        await asyncio.wait_for(
            _supervised(lambda: crashy(), "test", store), timeout=2.0,
        )
    finally:
        asyncio.sleep = asyncio.sleep_orig
    assert call_count[0] == 2
    assert store.task_restart_counts["test"] == 1


@pytest.mark.asyncio
async def test_record_error_called_on_crash():
    """The supervisor logs the crash via store.record_error."""
    store = _stub_store()
    recorded = []
    store.record_error = lambda msg: recorded.append(msg)
    call_count = [0]

    async def crashy():
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("boom")

    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s: real_sleep(0)
    try:
        await asyncio.wait_for(_supervised(lambda: crashy(), "fast", store), timeout=2.0)
    finally:
        asyncio.sleep = real_sleep
    assert any("fast" in m and "crashed" in m for m in recorded)


# ---------------- multiple restarts + backoff state ----------------


@pytest.mark.asyncio
async def test_multiple_crashes_accumulate_counter():
    store = _stub_store()
    call_count = [0]

    async def crashy():
        call_count[0] += 1
        if call_count[0] < 4:
            raise RuntimeError("crash")
        # 4th try succeeds.

    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s: real_sleep(0)
    try:
        await asyncio.wait_for(_supervised(lambda: crashy(), "x", store), timeout=2.0)
    finally:
        asyncio.sleep = real_sleep
    assert store.task_restart_counts["x"] == 3


# ---------------- cancellation respected ----------------


@pytest.mark.asyncio
async def test_cancelled_error_re_raised():
    """asyncio.CancelledError must propagate so graceful shutdown works."""
    store = _stub_store()

    async def waiter():
        await asyncio.sleep(10)  # will be cancelled

    task = asyncio.create_task(_supervised(lambda: waiter(), "wait", store))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Counter should NOT have been incremented for a cancellation.
    assert store.task_restart_counts.get("wait", 0) == 0


# ---------------- counter scoping ----------------


@pytest.mark.asyncio
async def test_counters_per_task_name():
    """Two tasks with different names have independent counters."""
    store = _stub_store()
    call_a = [0]
    call_b = [0]

    async def crashy_a():
        call_a[0] += 1
        if call_a[0] == 1:
            raise RuntimeError("a")

    async def crashy_b():
        call_b[0] += 1
        if call_b[0] < 3:
            raise RuntimeError("b")

    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s: real_sleep(0)
    try:
        await asyncio.wait_for(_supervised(lambda: crashy_a(), "A", store), timeout=2.0)
        await asyncio.wait_for(_supervised(lambda: crashy_b(), "B", store), timeout=2.0)
    finally:
        asyncio.sleep = real_sleep
    assert store.task_restart_counts == {"A": 1, "B": 2}
