"""Tests for the recent-errors ring buffer (Round 56)."""

from __future__ import annotations

from datetime import datetime, timezone

from funding_screener.background import DataStore


def test_record_error_appends_to_recent_errors():
    s = DataStore()
    s.record_error("first failure")
    s.record_error("second failure")
    out = s.read_recent_errors()
    assert len(out) == 2
    assert out[0][1] == "first failure"
    assert out[1][1] == "second failure"


def test_record_error_stamps_timestamp():
    s = DataStore()
    before = datetime.now(timezone.utc)
    s.record_error("oops")
    after = datetime.now(timezone.utc)
    ts, _ = s.read_recent_errors()[0]
    assert before <= ts <= after


def test_empty_message_does_not_log():
    """record_error('') is the existing 'clear last_error' signal — must not
    append to the ring (otherwise normal recovery would pollute the buffer).
    """
    s = DataStore()
    s.record_error("real failure")
    s.record_error("")  # used to clear last_error
    s.record_error("")
    out = s.read_recent_errors()
    assert len(out) == 1
    assert out[0][1] == "real failure"


def test_record_error_caps_at_50():
    s = DataStore()
    for i in range(75):
        s.record_error(f"error #{i}")
    out = s.read_recent_errors()
    assert len(out) == 50
    # Oldest 25 dropped; we keep #25 through #74.
    assert out[0][1] == "error #25"
    assert out[-1][1] == "error #74"


def test_read_returns_copy():
    s = DataStore()
    s.record_error("first")
    snapshot = s.read_recent_errors()
    snapshot.append((datetime.now(timezone.utc), "tampered"))
    fresh = s.read_recent_errors()
    assert len(fresh) == 1


def test_last_error_still_set_alongside_ring():
    """Existing last_error semantics unchanged."""
    s = DataStore()
    s.record_error("first")
    s.record_error("second")
    assert s.last_error == "second"


def test_empty_clears_last_error_but_preserves_ring():
    """Clearing last_error to '' means 'no current problem' but the audit
    history still shows what failed earlier.
    """
    s = DataStore()
    s.record_error("transient blip")
    s.record_error("")
    assert s.last_error == ""
    out = s.read_recent_errors()
    assert len(out) == 1
    assert out[0][1] == "transient blip"


def test_read_recent_errors_empty_when_never_logged():
    s = DataStore()
    assert s.read_recent_errors() == []
