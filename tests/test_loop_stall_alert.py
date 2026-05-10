"""Tests for evaluate_loop_stall_alert + DataStore.last_loop_ran_at (Round 55)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from funding_screener.background import DataStore
from funding_screener.notifications import evaluate_loop_stall_alert


# ---------------- DataStore instrumentation ----------------


def test_record_loop_duration_stamps_last_ran_at():
    s = DataStore()
    before = datetime.now(timezone.utc)
    s.record_loop_duration("fast", 0.5)
    after = datetime.now(timezone.utc)
    ts = s.last_loop_ran_at["fast"]
    assert before <= ts <= after


def test_read_last_loop_ran_at_returns_copy():
    s = DataStore()
    s.record_loop_duration("fast", 0.5)
    snapshot = s.read_last_loop_ran_at()
    snapshot["BOGUS"] = datetime.now(timezone.utc)
    assert "BOGUS" not in s.last_loop_ran_at


def test_record_loop_duration_updates_per_loop():
    s = DataStore()
    s.record_loop_duration("fast", 0.1)
    s.record_loop_duration("slow", 1.0)
    s.record_loop_duration("fast", 0.2)  # update fast
    snap = s.read_last_loop_ran_at()
    assert "fast" in snap
    assert "slow" in snap
    # Fast was updated more recently than slow.
    assert snap["fast"] >= snap["slow"]


# ---------------- evaluate_loop_stall_alert ----------------


def _now_minus(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def test_active_when_loop_overdue():
    """fast loop expects every 60s; 5 min ago = 5×60s = 5×expected → stalled."""
    last_ran = {"fast": _now_minus(5)}
    out = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=3.0,
    )
    actives = [m for (_k, s, m) in out if s == "active"]
    assert len(actives) == 1
    assert "fast" in actives[0]
    assert "Loop stalled" in actives[0]


def test_resolved_when_loop_recent():
    last_ran = {"fast": _now_minus(0.5)}  # 30s ago, < 3×60s
    out = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=3.0,
    )
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


def test_not_yet_run_loops_skipped():
    """Loops that have NEVER recorded → can't tell stalled from cold-started."""
    out = evaluate_loop_stall_alert(
        {}, expected_intervals_seconds={"fast": 60, "slow": 300}, stall_multiplier=3.0,
    )
    assert out == []


def test_unrecognized_loops_skipped():
    """Loop in last_ran_at but not in expected_intervals → not checked."""
    last_ran = {"some_unknown": _now_minus(60)}
    out = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=3.0,
    )
    assert out == []  # 'fast' has no entry in last_ran; 'some_unknown' not in expected


def test_per_loop_independent_keys():
    """A stalled fast loop emits 'loop_stall:fast'; a healthy slow loop emits
    'loop_stall:slow' resolved. Each tracked independently.
    """
    last_ran = {
        "fast": _now_minus(10),         # stalled (10min vs 60s × 3)
        "slow": _now_minus(2),          # healthy (2min vs 300s × 3 = 15min threshold)
    }
    out = evaluate_loop_stall_alert(
        last_ran,
        expected_intervals_seconds={"fast": 60, "slow": 300},
        stall_multiplier=3.0,
    )
    by_key = {k: s for (k, s, _m) in out}
    assert by_key["loop_stall:fast"] == "active"
    assert by_key["loop_stall:slow"] == "resolved"


def test_stall_multiplier_tuneable():
    """Same input, different multiplier → different verdict."""
    last_ran = {"fast": _now_minus(2.5)}  # 150s ago, expected 60s
    # multiplier 2.0 → threshold 120s → 150s exceeds → active
    out_strict = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=2.0,
    )
    # multiplier 5.0 → threshold 300s → 150s under → resolved
    out_lenient = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=5.0,
    )
    assert any(s == "active" for (_k, s, _m) in out_strict)
    assert all(s == "resolved" for (_k, s, _m) in out_lenient)


def test_multiple_chains_each_get_own_key():
    """onchain.ethereum and onchain.bsc both registered → independent alerts."""
    last_ran = {
        "onchain.ethereum": _now_minus(60),  # 60min, expected 900s × 3 = 45min → STALLED
        "onchain.bsc": _now_minus(10),       # 10min < 45min → healthy
    }
    out = evaluate_loop_stall_alert(
        last_ran,
        expected_intervals_seconds={"onchain.ethereum": 900, "onchain.bsc": 900},
        stall_multiplier=3.0,
    )
    by_key = {k: s for (k, s, _m) in out}
    assert by_key["loop_stall:onchain.ethereum"] == "active"
    assert by_key["loop_stall:onchain.bsc"] == "resolved"


def test_active_message_contains_elapsed_and_expected():
    last_ran = {"fast": _now_minus(10)}  # 10min, expected 60s
    out = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=3.0,
    )
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "10.0 min" in msg or "9.9" in msg or "10." in msg
    assert "60s" in msg or "60.0s" in msg
    assert "3.0×" in msg or "3×" in msg


def test_handles_empty_inputs():
    assert evaluate_loop_stall_alert({}, {}) == []
    assert evaluate_loop_stall_alert(None, {}) == []  # type: ignore[arg-type]
    assert evaluate_loop_stall_alert({"fast": _now_minus(1)}, None) == []  # type: ignore[arg-type]


def test_handles_malformed_timestamp_gracefully():
    """If a value isn't a datetime (e.g. corrupted state), skip it instead of crashing."""
    last_ran = {"fast": "not-a-datetime"}
    out = evaluate_loop_stall_alert(
        last_ran, expected_intervals_seconds={"fast": 60}, stall_multiplier=3.0,
    )
    assert out == []
