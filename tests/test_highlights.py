"""Tests for the highlights aggregator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest

from funding_screener.highlights import (
    all_highlights,
    best_long_candidate,
    best_short_candidate,
    biggest_unlock,
    hottest_funding,
    macro_summary,
    whale_spotlight,
)


@dataclass
class _FakeRow:
    base_asset: str
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = None
    mexc_symbol: Optional[str] = None
    composite_score: Optional[int] = None
    composite_short: Optional[str] = None
    composite_breakdown: Optional[str] = None
    binance_rate_8h_norm_percent: Optional[float] = None
    mexc_rate_8h_norm_percent: Optional[float] = None
    max_abs_8h_norm_percent: float = 0.0


@dataclass
class _FakeUnlock:
    symbol: str
    days_until: int
    date_str: str
    amount_usd: Optional[float]
    pct_of_supply: Optional[float]
    notes: str = ""


# ---------------- best_long_candidate ----------------


def test_best_long_picks_top_score():
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", composite_score=50, composite_short="Bullish"),
        _FakeRow("ETH", binance_symbol="ETHUSDT", composite_score=80, composite_short="Strong bull"),
        _FakeRow("SOL", binance_symbol="SOLUSDT", composite_score=20, composite_short="Mild bull"),
    ]
    out = best_long_candidate(rows)
    assert out is not None
    assert "ETHUSDT" in out["headline"]
    assert "+80" in out["headline"]


def test_best_long_returns_none_when_top_below_threshold():
    rows = [_FakeRow("X", binance_symbol="XUSDT", composite_score=15)]
    assert best_long_candidate(rows) is None


def test_best_long_returns_none_when_no_rows():
    assert best_long_candidate([]) is None


def test_best_long_skips_none_scores():
    rows = [
        _FakeRow("X", binance_symbol="XUSDT", composite_score=None),
        _FakeRow("Y", binance_symbol="YUSDT", composite_score=40, composite_short="Bullish"),
    ]
    out = best_long_candidate(rows)
    assert "YUSDT" in out["headline"]


# ---------------- best_short_candidate ----------------


def test_best_short_picks_most_negative():
    rows = [
        _FakeRow("X", binance_symbol="XUSDT", composite_score=-50, composite_short="Bearish"),
        _FakeRow("Y", binance_symbol="YUSDT", composite_score=-90, composite_short="Strong bear"),
    ]
    out = best_short_candidate(rows)
    assert "YUSDT" in out["headline"]
    assert "-90" in out["headline"]


def test_best_short_returns_none_when_above_threshold():
    rows = [_FakeRow("X", binance_symbol="XUSDT", composite_score=-15)]
    assert best_short_candidate(rows) is None


# ---------------- hottest_funding ----------------


def test_hottest_funding_picks_largest_abs():
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", binance_rate_8h_norm_percent=0.5, max_abs_8h_norm_percent=0.5),
        _FakeRow("PEPE", binance_symbol="PEPEUSDT", binance_rate_8h_norm_percent=2.5, max_abs_8h_norm_percent=2.5),
        _FakeRow("WIF", binance_symbol="WIFUSDT", binance_rate_8h_norm_percent=-3.0, max_abs_8h_norm_percent=3.0),
    ]
    out = hottest_funding(rows)
    assert out is not None
    assert "WIFUSDT" in out["headline"]
    assert "shorts paying longs" in out["headline"]


def test_hottest_funding_returns_none_below_threshold():
    rows = [_FakeRow("BTC", binance_symbol="BTCUSDT", binance_rate_8h_norm_percent=0.3, max_abs_8h_norm_percent=0.3)]
    assert hottest_funding(rows) is None


# ---------------- biggest_unlock ----------------


def test_biggest_unlock_picks_largest_usd():
    today = date(2026, 5, 8)
    events = [
        _FakeUnlock("ARB", days_until=10, date_str="2026-05-18", amount_usd=50_000_000, pct_of_supply=1.5),
        _FakeUnlock("APT", days_until=5, date_str="2026-05-13", amount_usd=200_000_000, pct_of_supply=2.0),
    ]
    out = biggest_unlock(events)
    assert "APT" in out["headline"]
    assert "200" in out["headline"]


def test_biggest_unlock_filters_by_window():
    events = [
        _FakeUnlock("X", days_until=60, date_str="...", amount_usd=500_000_000, pct_of_supply=5.0),
    ]
    assert biggest_unlock(events, days_ahead=30) is None


def test_biggest_unlock_skips_none_amount():
    events = [_FakeUnlock("X", days_until=5, date_str="...", amount_usd=None, pct_of_supply=1.0)]
    assert biggest_unlock(events) is None


# ---------------- whale_spotlight ----------------


def test_whale_spotlight_picks_largest_abs_with_min_whales():
    flows = [
        {"token": "BTC", "whale_net_usd": 1_000_000, "whale_unique_count": 1},  # below min
        {"token": "ETH", "whale_net_usd": 5_000_000, "whale_unique_count": 3},
        {"token": "ARB", "whale_net_usd": -50_000_000, "whale_unique_count": 5},
    ]
    out = whale_spotlight(flows, min_unique_whales=2)
    assert "ARB" in out["headline"]
    assert "deposited" in out["headline"]


def test_whale_spotlight_returns_none_when_no_qualifying():
    flows = [{"token": "BTC", "whale_net_usd": 1_000_000, "whale_unique_count": 1}]
    assert whale_spotlight(flows, min_unique_whales=2) is None


# ---------------- macro_summary ----------------


def test_macro_summary_bullish():
    supply = {"TOTAL": {"now": 200e9, "change_24h_pct": 0.5}}
    out = macro_summary(supply)
    assert "🟢" in out["emoji"]
    assert "expanding" in out["headline"]


def test_macro_summary_bearish():
    supply = {"TOTAL": {"now": 200e9, "change_24h_pct": -0.5}}
    out = macro_summary(supply)
    assert "🔴" in out["emoji"]
    assert "contracting" in out["headline"]


def test_macro_summary_neutral():
    supply = {"TOTAL": {"now": 200e9, "change_24h_pct": 0.05}}
    out = macro_summary(supply)
    assert "🟡" in out["emoji"]


def test_macro_summary_handles_missing_data():
    assert macro_summary({}) is None
    assert macro_summary({"TOTAL": {}}) is None


# ---------------- all_highlights ----------------


def test_all_highlights_collects_non_none():
    rows = [_FakeRow("BTC", binance_symbol="BTCUSDT", composite_score=80, composite_short="Strong bull",
                     binance_rate_8h_norm_percent=2.0, max_abs_8h_norm_percent=2.0)]
    unlocks = [_FakeUnlock("ARB", days_until=5, date_str="2026-05-13", amount_usd=100e6, pct_of_supply=1.5)]
    flows = [{"token": "ETH", "whale_net_usd": 20e6, "whale_unique_count": 4}]
    supply = {"TOTAL": {"now": 200e9, "change_24h_pct": 0.5}}
    out = all_highlights(rows, unlocks, flows, supply)
    labels = [h["label"] for h in out]
    assert "Best long" in labels
    assert "Hot funding" in labels
    assert "Biggest unlock ahead" in labels
    assert "Whale spotlight" in labels
    assert "Macro" in labels


def test_all_highlights_empty_inputs_return_empty():
    assert all_highlights([], [], [], {}) == []
