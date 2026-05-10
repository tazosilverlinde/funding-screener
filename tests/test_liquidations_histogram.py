"""Tests for LiquidationsBuffer.histogram (Round 18)."""

from __future__ import annotations

import time

import pytest

from funding_screener.liquidations import LiquidationEvent, LiquidationsBuffer


def _ev(symbol: str, side: str, notional: float, ts: float) -> LiquidationEvent:
    return LiquidationEvent(
        symbol=symbol, side_liquidated=side,
        price_usd=100.0, qty=notional / 100.0, notional_usd=notional, timestamp=ts,
    )


def test_histogram_returns_n_bins_for_window():
    """24h window with 1h bins → 24 bins."""
    buf = LiquidationsBuffer()
    out = buf.histogram("BTCUSDT", bin_seconds=3600, window_seconds=24 * 3600)
    assert len(out) == 24


def test_empty_buffer_returns_zeroed_bins_with_consistent_x_axis():
    buf = LiquidationsBuffer()
    out = buf.histogram("EMPTYUSDT", bin_seconds=3600, window_seconds=12 * 3600)
    assert len(out) == 12
    for b in out:
        assert b["long_liq_usd"] == 0.0
        assert b["short_liq_usd"] == 0.0
        assert b["count"] == 0
        assert b["ts"] > 0


def test_events_land_in_correct_bin():
    """Place each event at a deterministic offset from the bin floor so the
    test isn't flaky on bin boundaries. We use now_floor as the reference
    point — the same floor histogram() uses internally — so events are
    placed mid-bin regardless of wallclock when the test runs.
    """
    buf = LiquidationsBuffer()
    now = time.time()
    bin_size = 3600
    now_floor = (int(now) // bin_size) * bin_size
    # Mid-current-bin: 30 min into the current hour.
    in_current_bin_ts = now_floor + bin_size // 2
    # Mid-previous-bin: 30 min into the previous hour.
    in_prev_bin_ts = now_floor - bin_size + bin_size // 2
    # Future timestamps relative to now_floor will skip the cutoff prune; use
    # the past variant if needed. now_floor + 30min may be > now (causing
    # prune drop) so always subtract back into the past.
    if in_current_bin_ts > now:
        in_current_bin_ts = now - 1  # safely in current bin
    buf.add(_ev("BTCUSDT", "long", 5_000_000, in_current_bin_ts))
    buf.add(_ev("BTCUSDT", "short", 3_000_000, in_prev_bin_ts))
    out = buf.histogram("BTCUSDT", bin_seconds=bin_size, window_seconds=24 * 3600)
    # Last bin = current hour = +5M long.
    assert out[-1]["long_liq_usd"] == pytest.approx(5_000_000)
    assert out[-1]["short_liq_usd"] == 0.0
    # Second-to-last = previous hour = +3M short.
    assert out[-2]["short_liq_usd"] == pytest.approx(3_000_000)
    assert out[-2]["long_liq_usd"] == 0.0


def test_events_outside_window_excluded():
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("BTCUSDT", "long", 999_999, now - 48 * 3600))  # 2 days old
    buf.add(_ev("BTCUSDT", "long", 1_000, now - 60))           # fresh
    out = buf.histogram("BTCUSDT", bin_seconds=3600, window_seconds=24 * 3600)
    long_total = sum(b["long_liq_usd"] for b in out)
    assert long_total == pytest.approx(1_000)


def test_histogram_long_and_short_separated():
    """Same bin, both sides — they should NOT collapse together."""
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("BTCUSDT", "long", 1_000_000, now - 600))
    buf.add(_ev("BTCUSDT", "short", 2_000_000, now - 600))
    out = buf.histogram("BTCUSDT", bin_seconds=3600)
    last = out[-1]
    assert last["long_liq_usd"] == pytest.approx(1_000_000)
    assert last["short_liq_usd"] == pytest.approx(2_000_000)
    assert last["count"] == 2


def test_count_field_aggregates_event_count():
    buf = LiquidationsBuffer()
    now = time.time()
    for i in range(5):
        buf.add(_ev("BTCUSDT", "long", 100, now - 100 - i))
    out = buf.histogram("BTCUSDT", bin_seconds=3600)
    last = out[-1]
    assert last["count"] == 5


def test_smaller_bin_seconds_makes_more_bins():
    """30-min bins over 24h → 48 bins."""
    buf = LiquidationsBuffer()
    out = buf.histogram("BTCUSDT", bin_seconds=1800, window_seconds=24 * 3600)
    assert len(out) == 48


def test_bins_are_chronological():
    """Output must be sorted ascending by ts (oldest first)."""
    buf = LiquidationsBuffer()
    out = buf.histogram("BTCUSDT", bin_seconds=3600, window_seconds=12 * 3600)
    for i in range(1, len(out)):
        assert out[i]["ts"] > out[i - 1]["ts"]
