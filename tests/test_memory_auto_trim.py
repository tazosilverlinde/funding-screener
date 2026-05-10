"""Tests for the memory auto-trim helpers (Round 54)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from funding_screener.background import DataStore
from funding_screener.liquidations import LiquidationEvent, LiquidationsBuffer


# ---------------- LiquidationsBuffer.trim_to_window_seconds ----------------


def test_trim_drops_events_older_than_window():
    buf = LiquidationsBuffer()
    now = time.time()
    # Add 5 events from oldest to newest spaced 1 hour apart.
    for hours_ago in (5, 4, 3, 2, 1):
        buf.add(LiquidationEvent(
            symbol="BTCUSDT", side_liquidated="long",
            price_usd=1.0, qty=1.0, notional_usd=1.0,
            timestamp=now - hours_ago * 3600,
        ))
    # Trim to last 2.5 hours → keeps the 2h-ago and 1h-ago events.
    dropped = buf.trim_to_window_seconds(int(2.5 * 3600))
    assert dropped == 3  # 5h, 4h, 3h ago → dropped
    stats = buf.aggregate("BTCUSDT", window_seconds=10 * 3600)
    assert stats["events_count"] == 2


def test_trim_idempotent():
    """Calling trim with the same window twice → second call drops 0."""
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(LiquidationEvent(
        symbol="BTCUSDT", side_liquidated="long",
        price_usd=1.0, qty=1.0, notional_usd=1.0,
        timestamp=now - 5 * 3600,
    ))
    first = buf.trim_to_window_seconds(3600)
    second = buf.trim_to_window_seconds(3600)
    assert first == 1
    assert second == 0


def test_trim_removes_empty_symbols_from_buffer():
    """When all events for a symbol are trimmed away, the symbol key itself
    is dropped from the underlying dict so it doesn't accumulate.
    """
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(LiquidationEvent(
        symbol="OLD", side_liquidated="long",
        price_usd=1.0, qty=1.0, notional_usd=1.0,
        timestamp=now - 99999,
    ))
    buf.add(LiquidationEvent(
        symbol="NEW", side_liquidated="long",
        price_usd=1.0, qty=1.0, notional_usd=1.0,
        timestamp=now - 100,
    ))
    buf.trim_to_window_seconds(3600)
    assert "OLD" not in buf._buf
    assert "NEW" in buf._buf


def test_trim_with_zero_window_keeps_only_now():
    """window_seconds=0 means cutoff == now; only events at or after now stay
    (which is essentially nothing in practice).
    """
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(LiquidationEvent(
        symbol="BTCUSDT", side_liquidated="long",
        price_usd=1.0, qty=1.0, notional_usd=1.0,
        timestamp=now - 1,
    ))
    dropped = buf.trim_to_window_seconds(0)
    assert dropped == 1


def test_trim_negative_window_treated_as_zero():
    """Defensive — bad config doesn't crash."""
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(LiquidationEvent(
        symbol="BTCUSDT", side_liquidated="long",
        price_usd=1.0, qty=1.0, notional_usd=1.0,
        timestamp=now - 1,
    ))
    # window=-100 should be clamped to 0; cutoff = now → drop everything older.
    dropped = buf.trim_to_window_seconds(-100)
    assert dropped == 1


def test_trim_empty_buffer_safe():
    buf = LiquidationsBuffer()
    assert buf.trim_to_window_seconds(3600) == 0


# ---------------- DataStore.trim_score_history_to_hours ----------------


def test_score_history_trim_drops_old_samples():
    s = DataStore()
    now = datetime.now(timezone.utc)
    s.score_history[("BTC", "USDT")] = [
        (now - timedelta(hours=20), 50),
        (now - timedelta(hours=10), 60),
        (now - timedelta(hours=5), 70),
        (now - timedelta(hours=1), 80),
    ]
    dropped = s.trim_score_history_to_hours(8)
    assert dropped == 2  # 20h and 10h dropped; 5h and 1h kept
    samples = s.score_history[("BTC", "USDT")]
    assert len(samples) == 2
    assert all(t >= now - timedelta(hours=8) for (t, _) in samples)


def test_score_history_trim_drops_empty_keys():
    """When all samples for a key are dropped, the key itself is removed."""
    s = DataStore()
    now = datetime.now(timezone.utc)
    s.score_history[("OLD", "USDT")] = [(now - timedelta(hours=20), 50)]
    s.score_history[("NEW", "USDT")] = [(now - timedelta(hours=1), 60)]
    s.trim_score_history_to_hours(2)
    assert ("OLD", "USDT") not in s.score_history
    assert ("NEW", "USDT") in s.score_history


def test_score_history_trim_idempotent():
    s = DataStore()
    now = datetime.now(timezone.utc)
    s.score_history[("BTC", "USDT")] = [
        (now - timedelta(hours=10), 50),
        (now - timedelta(hours=1), 80),
    ]
    first = s.trim_score_history_to_hours(5)
    second = s.trim_score_history_to_hours(5)
    assert first == 1
    assert second == 0


def test_score_history_trim_to_zero_hours_drops_all():
    s = DataStore()
    now = datetime.now(timezone.utc)
    s.score_history[("BTC", "USDT")] = [
        (now - timedelta(seconds=1), 50),
    ]
    # 0h means cutoff = now → ts < cutoff so everything older drops.
    dropped = s.trim_score_history_to_hours(0)
    assert dropped == 1
    assert s.score_history == {}


def test_score_history_trim_empty_safe():
    s = DataStore()
    assert s.trim_score_history_to_hours(12) == 0


def test_score_history_trim_preserves_thread_safety():
    """The trim should hold the store's lock — call it under contention.
    We can't easily test the lock without races, but verify the method
    completes cleanly when called multiple times in quick succession.
    """
    s = DataStore()
    now = datetime.now(timezone.utc)
    s.score_history[("BTC", "USDT")] = [
        (now - timedelta(hours=h), 50) for h in range(1, 25)
    ]
    for _ in range(10):
        s.trim_score_history_to_hours(12)
    # After repeated trim, only samples within last 12h remain.
    samples = s.score_history.get(("BTC", "USDT"), [])
    assert all(t >= now - timedelta(hours=12) for (t, _) in samples)
