"""Tests for the liquidation tape (Round 14)."""

from __future__ import annotations

import time

import pytest

from funding_screener.liquidations import (
    LiquidationEvent,
    LiquidationsBuffer,
    parse_force_order_message,
)


# -------------- parse_force_order_message --------------


def _sample_msg(side: str = "SELL", price: str = "100", qty: str = "1", t_ms: int | None = None) -> dict:
    return {
        "e": "forceOrder",
        "E": t_ms or int(time.time() * 1000),
        "o": {
            "s": "BTCUSDT",
            "S": side,
            "ap": price,
            "q": qty,
            "T": t_ms or int(time.time() * 1000),
        },
    }


def test_parse_sell_means_long_liquidated():
    """SELL side = exchange selling to close a LONG position."""
    ev = parse_force_order_message(_sample_msg(side="SELL", price="9910", qty="0.014"))
    assert ev is not None
    assert ev.side_liquidated == "long"
    assert ev.symbol == "BTCUSDT"
    assert ev.price_usd == pytest.approx(9910.0)
    assert ev.qty == pytest.approx(0.014)
    assert ev.notional_usd == pytest.approx(9910.0 * 0.014)


def test_parse_buy_means_short_liquidated():
    ev = parse_force_order_message(_sample_msg(side="BUY"))
    assert ev is not None
    assert ev.side_liquidated == "short"


def test_parse_returns_none_for_wrong_event_type():
    bad = {"e": "ticker", "o": {"s": "BTCUSDT", "S": "SELL", "ap": "100", "q": "1"}}
    assert parse_force_order_message(bad) is None


def test_parse_returns_none_for_missing_fields():
    assert parse_force_order_message({}) is None
    assert parse_force_order_message({"e": "forceOrder"}) is None
    assert parse_force_order_message({"e": "forceOrder", "o": {}}) is None


def test_parse_returns_none_for_zero_price_or_qty():
    assert parse_force_order_message(_sample_msg(price="0", qty="1")) is None
    assert parse_force_order_message(_sample_msg(price="100", qty="0")) is None


def test_parse_returns_none_for_garbage_payload():
    assert parse_force_order_message("not a dict") is None  # type: ignore[arg-type]
    assert parse_force_order_message(None) is None  # type: ignore[arg-type]


# -------------- LiquidationsBuffer aggregation --------------


def _ev(symbol: str, side: str, notional: float, ts: float) -> LiquidationEvent:
    return LiquidationEvent(
        symbol=symbol, side_liquidated=side,
        price_usd=100.0, qty=notional / 100.0, notional_usd=notional, timestamp=ts,
    )


def test_aggregate_sums_long_and_short_separately():
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("BTCUSDT", "long", 1_000_000, now - 100))
    buf.add(_ev("BTCUSDT", "long", 500_000, now - 50))
    buf.add(_ev("BTCUSDT", "short", 800_000, now - 25))
    s = buf.aggregate("BTCUSDT")
    assert s["long_liq_usd"] == pytest.approx(1_500_000)
    assert s["short_liq_usd"] == pytest.approx(800_000)
    assert s["total_usd"] == pytest.approx(2_300_000)
    assert s["long_count"] == 2
    assert s["short_count"] == 1
    assert s["events_count"] == 3


def test_aggregate_tracks_biggest_single():
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("ETHUSDT", "long", 100_000, now - 50))
    buf.add(_ev("ETHUSDT", "short", 5_000_000, now - 30))   # biggest
    buf.add(_ev("ETHUSDT", "long", 2_000_000, now - 10))
    s = buf.aggregate("ETHUSDT")
    assert s["biggest_single_usd"] == pytest.approx(5_000_000)
    assert s["biggest_single_side"] == "short"


def test_aggregate_excludes_events_outside_window():
    """Window of 1h: an event 2h old should be pruned and not appear in stats."""
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("BTCUSDT", "long", 999_999, now - 7200))  # 2h ago — outside 1h window
    buf.add(_ev("BTCUSDT", "long", 1000, now - 60))       # inside 1h window
    s = buf.aggregate("BTCUSDT", window_seconds=3600)
    assert s["long_liq_usd"] == pytest.approx(1000)
    assert s["events_count"] == 1


def test_aggregate_empty_buffer_returns_zero_stats():
    buf = LiquidationsBuffer()
    s = buf.aggregate("BTCUSDT")
    assert s["events_count"] == 0
    assert s["total_usd"] == 0.0
    assert s["biggest_single_side"] is None


def test_aggregate_all_returns_only_active_symbols():
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("BTCUSDT", "long", 100, now - 10))
    buf.add(_ev("ETHUSDT", "short", 200, now - 10))
    out = buf.aggregate_all()
    assert set(out.keys()) == {"BTCUSDT", "ETHUSDT"}
    assert out["BTCUSDT"]["long_liq_usd"] == pytest.approx(100)
    assert out["ETHUSDT"]["short_liq_usd"] == pytest.approx(200)


def test_aggregate_all_prunes_per_symbol():
    """Old events from one symbol are dropped without affecting others."""
    buf = LiquidationsBuffer()
    now = time.time()
    buf.add(_ev("OLDONE", "long", 999, now - 100_000))  # ancient
    buf.add(_ev("FRESH", "long", 1, now - 10))
    out = buf.aggregate_all(window_seconds=3600)  # 1h
    assert "OLDONE" not in out
    assert "FRESH" in out


def test_lifetime_counter_and_last_event_timestamp():
    buf = LiquidationsBuffer()
    assert buf.total_events_seen() == 0
    assert buf.last_event_at() is None
    now = time.time()
    buf.add(_ev("BTCUSDT", "long", 100, now - 50))
    buf.add(_ev("BTCUSDT", "short", 200, now - 25))
    assert buf.total_events_seen() == 2
    assert buf.last_event_at() == pytest.approx(now - 25)


def test_max_per_symbol_cap_drops_oldest():
    """An overflow-flood doesn't unbounded-grow memory — oldest entries pop."""
    buf = LiquidationsBuffer()
    base_ts = time.time()
    # Add slightly more than the per-symbol cap of 50_000.
    for i in range(50_010):
        buf.add(_ev("BTCUSDT", "long", 1.0, base_ts + i * 0.001))
    s = buf.aggregate("BTCUSDT")
    # Cap drops 10 oldest, so events_count ≤ 50_000.
    assert s["events_count"] <= 50_000
    assert s["events_count"] >= 49_000  # sanity — still mostly there
