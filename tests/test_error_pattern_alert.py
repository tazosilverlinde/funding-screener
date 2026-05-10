"""Tests for evaluate_error_pattern_alert + _error_category (Round 57)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from funding_screener.notifications import (
    _error_category,
    evaluate_error_pattern_alert,
)


# ---------------- _error_category ----------------


def test_category_extracts_source_prefix():
    assert _error_category("fast-loop fatal: TimeoutError: foo") == "fast-loop fatal"


def test_category_handles_multi_colon_message():
    assert _error_category("alerts: composite evaluator failed: bar") == "alerts"


def test_category_handles_short_messages():
    assert _error_category("oops") == "oops"


def test_category_truncates_long_prefix():
    long = "x" * 100 + ": detail"
    assert len(_error_category(long)) == 50


def test_category_empty_or_none():
    assert _error_category("") == "unknown"
    assert _error_category(None) == "unknown"  # type: ignore[arg-type]


# ---------------- evaluate_error_pattern_alert ----------------


def _now_minus(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def test_active_when_pattern_repeats_above_threshold():
    errors = [(_now_minus(5), "onchain[ethereum]: TimeoutError: foo") for _ in range(6)]
    out = evaluate_error_pattern_alert(errors, window_minutes=30, repeat_threshold=5)
    actives = [m for (_k, s, m) in out if s == "active"]
    assert len(actives) == 1
    assert "onchain[ethereum]" in actives[0]
    assert "6 times" in actives[0] or "`6`" in actives[0]


def test_resolved_when_pattern_below_threshold():
    errors = [(_now_minus(5), "fast-loop fatal: blip") for _ in range(2)]
    out = evaluate_error_pattern_alert(errors, window_minutes=30, repeat_threshold=5)
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


def test_active_message_includes_latest_example():
    errors = [
        (_now_minus(10), "onchain: error A"),
        (_now_minus(8), "onchain: error B"),
        (_now_minus(6), "onchain: error C"),
        (_now_minus(4), "onchain: error D"),
        (_now_minus(2), "onchain: latest specific failure"),
    ]
    out = evaluate_error_pattern_alert(errors, window_minutes=30, repeat_threshold=5)
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "latest specific failure" in msg


def test_window_excludes_old_errors():
    """An error from 2h ago shouldn't count when the window is 30 min."""
    errors = [(_now_minus(120), "fast-loop: stale") for _ in range(10)] + [
        (_now_minus(5), "fast-loop: recent")
    ]
    out = evaluate_error_pattern_alert(errors, window_minutes=30, repeat_threshold=5)
    # Only the recent one is in the window — under threshold → resolved.
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


def test_categories_independent():
    """5 onchain errors active doesn't suppress the other category's resolved."""
    errors = (
        [(_now_minus(5), "onchain: foo") for _ in range(6)]
        + [(_now_minus(5), "alerts: bar") for _ in range(2)]
    )
    out = evaluate_error_pattern_alert(errors, window_minutes=30, repeat_threshold=5)
    by_key = {k: s for (k, s, _m) in out}
    assert by_key.get("error_pattern:onchain") == "active"
    assert by_key.get("error_pattern:alerts") == "resolved"


def test_only_categories_seen_in_window_emit():
    """Categories with zero events in the window don't appear at all."""
    errors = [(_now_minus(5), "fast: just one") for _ in range(2)]
    out = evaluate_error_pattern_alert(errors, window_minutes=30, repeat_threshold=5)
    keys = {k for (k, _s, _m) in out}
    # Only 'fast' shows. No 'onchain', 'alerts', etc.
    assert keys == {"error_pattern:fast"}


def test_threshold_tuneable():
    errors = [(_now_minus(5), "x: y") for _ in range(3)]
    # 3 occurrences: under 5 → resolved; under 3 → active.
    strict = [s for (_k, s, _m) in evaluate_error_pattern_alert(errors, 30, 5) if s == "active"]
    relaxed = [s for (_k, s, _m) in evaluate_error_pattern_alert(errors, 30, 3) if s == "active"]
    assert strict == []
    assert relaxed == ["active"]


def test_handles_empty_input():
    assert evaluate_error_pattern_alert([]) == []
    assert evaluate_error_pattern_alert(None) == []  # type: ignore[arg-type]


def test_handles_malformed_timestamp_gracefully():
    """Non-datetime timestamp in tuple → skip without crashing."""
    errors = [
        ("not-a-datetime", "x: y"),
        (_now_minus(5), "x: y"),
    ]
    out = evaluate_error_pattern_alert(errors, 30, 5)
    # Should not raise; the valid one alone is below threshold → resolved.
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


def test_message_includes_window_for_context():
    errors = [(_now_minus(5), "x: y") for _ in range(6)]
    out = evaluate_error_pattern_alert(errors, window_minutes=15, repeat_threshold=5)
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "15 min" in msg
