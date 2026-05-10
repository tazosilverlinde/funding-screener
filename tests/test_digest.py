"""Tests for the daily digest composer (Round 21)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from funding_screener.digest import (
    compose_daily_digest,
    compose_market_overview,
    sector_winners_and_losers,
    top_liquidation_events,
    top_long_candidates,
    top_short_candidates,
    upcoming_unlocks,
)


@dataclass
class _FakeRow:
    base_asset: str
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = None
    mexc_symbol: Optional[str] = None
    composite_score: Optional[int] = None
    composite_emoji: Optional[str] = None
    composite_short: Optional[str] = None
    binance_rate_8h_norm_percent: Optional[float] = None
    mexc_rate_8h_norm_percent: Optional[float] = None


@dataclass
class _FakeUnlock:
    symbol: str
    days_until: int
    date_str: str = "2026-05-15"
    amount_usd: Optional[float] = 50_000_000.0
    pct_of_supply: Optional[float] = 1.5


# ----------------- compose_market_overview -----------------


def test_overview_counts_buckets_correctly():
    rows = [
        _FakeRow("A", composite_score=80),    # strong bull
        _FakeRow("B", composite_score=50),    # bullish
        _FakeRow("C", composite_score=10),    # neutral
        _FakeRow("D", composite_score=-50),   # bearish
        _FakeRow("E", composite_score=-80),   # strong bear
        _FakeRow("F", composite_score=None),  # neutral (defaults to 0)
    ]
    ov = compose_market_overview(rows)
    assert ov["total_symbols"] == 6
    assert ov["bullish"] == 2
    assert ov["bearish"] == 2
    assert ov["neutral"] == 2
    assert ov["strong_bull"] == 1
    assert ov["strong_bear"] == 1


def test_overview_handles_empty_input():
    ov = compose_market_overview([])
    assert ov["total_symbols"] == 0
    assert all(ov[k] == 0 for k in ("bullish", "bearish", "neutral", "strong_bull", "strong_bear"))


# ----------------- top_long_candidates -----------------


def test_top_longs_filters_below_min_score():
    rows = [
        _FakeRow("A", binance_symbol="AUSDT", composite_score=80, composite_emoji="🚀", composite_short="Strong bull"),
        _FakeRow("B", binance_symbol="BUSDT", composite_score=20),  # below default min=30
    ]
    out = top_long_candidates(rows, top_n=5)
    assert len(out) == 1
    assert out[0]["symbol"] == "AUSDT"
    assert out[0]["score"] == 80


def test_top_longs_sorted_descending():
    rows = [
        _FakeRow("A", binance_symbol="AUSDT", composite_score=50),
        _FakeRow("B", binance_symbol="BUSDT", composite_score=80),
        _FakeRow("C", binance_symbol="CUSDT", composite_score=65),
    ]
    out = top_long_candidates(rows, top_n=5)
    assert [r["symbol"] for r in out] == ["BUSDT", "CUSDT", "AUSDT"]


def test_top_longs_respects_top_n_cap():
    rows = [
        _FakeRow(f"X{i}", binance_symbol=f"X{i}USDT", composite_score=80 - i)
        for i in range(10)
    ]
    out = top_long_candidates(rows, top_n=3)
    assert len(out) == 3


def test_top_longs_uses_funding_from_either_side():
    rows = [
        _FakeRow("A", binance_symbol="AUSDT", composite_score=50,
                 binance_rate_8h_norm_percent=-1.5, mexc_rate_8h_norm_percent=None),
        _FakeRow("B", mexc_symbol="B_USDT", composite_score=70,
                 binance_rate_8h_norm_percent=None, mexc_rate_8h_norm_percent=-0.8),
    ]
    out = top_long_candidates(rows, top_n=5)
    a = next(r for r in out if r["symbol"] == "AUSDT")
    b = next(r for r in out if r["symbol"] == "B_USDT")
    assert a["funding_8h_pct"] == -1.5
    assert b["funding_8h_pct"] == -0.8


# ----------------- top_short_candidates -----------------


def test_top_shorts_picks_most_negative():
    rows = [
        _FakeRow("A", binance_symbol="AUSDT", composite_score=-30),
        _FakeRow("B", binance_symbol="BUSDT", composite_score=-90),
        _FakeRow("C", binance_symbol="CUSDT", composite_score=-50),
    ]
    out = top_short_candidates(rows, top_n=5)
    assert [r["symbol"] for r in out] == ["BUSDT", "CUSDT", "AUSDT"]


def test_top_shorts_filters_above_max_score():
    rows = [
        _FakeRow("A", binance_symbol="AUSDT", composite_score=-50),
        _FakeRow("B", binance_symbol="BUSDT", composite_score=-15),  # above default max=-30
    ]
    out = top_short_candidates(rows, top_n=5)
    assert len(out) == 1


# ----------------- top_liquidation_events -----------------


def test_top_squeezes_picks_short_dominant():
    by_symbol = {
        "BTC": {"long_liq_usd": 1_000_000, "short_liq_usd": 10_000_000, "total_usd": 11_000_000, "events_count": 50},
        "ETH": {"long_liq_usd": 5_000_000, "short_liq_usd": 1_000_000, "total_usd": 6_000_000, "events_count": 30},
    }
    out = top_liquidation_events(by_symbol, side="short")
    assert len(out) == 1
    assert out[0]["symbol"] == "BTC"


def test_top_cascades_picks_long_dominant():
    by_symbol = {
        "BTC": {"long_liq_usd": 1_000_000, "short_liq_usd": 10_000_000, "total_usd": 11_000_000, "events_count": 50},
        "ETH": {"long_liq_usd": 5_000_000, "short_liq_usd": 1_000_000, "total_usd": 6_000_000, "events_count": 30},
    }
    out = top_liquidation_events(by_symbol, side="long")
    assert len(out) == 1
    assert out[0]["symbol"] == "ETH"


def test_top_liq_excludes_below_noise_floor():
    by_symbol = {
        "TINY": {"long_liq_usd": 100, "short_liq_usd": 500, "total_usd": 600, "events_count": 2},
    }
    assert top_liquidation_events(by_symbol, side="short", min_total_usd=1_000_000) == []


def test_top_liq_invalid_side_raises():
    with pytest.raises(ValueError):
        top_liquidation_events({}, side="bad")


# ----------------- upcoming_unlocks -----------------


def test_unlocks_filters_window():
    events = [
        _FakeUnlock("A", days_until=3, amount_usd=10_000_000),
        _FakeUnlock("B", days_until=10, amount_usd=20_000_000),  # outside default 7-day window
    ]
    out = upcoming_unlocks(events, days_ahead=7, top_n=5)
    assert [r["symbol"] for r in out] == ["A"]


def test_unlocks_sorted_by_amount():
    events = [
        _FakeUnlock("Small", days_until=3, amount_usd=10_000_000),
        _FakeUnlock("Big", days_until=5, amount_usd=200_000_000),
    ]
    out = upcoming_unlocks(events, days_ahead=7)
    assert out[0]["symbol"] == "Big"


def test_unlocks_skips_past_events():
    events = [_FakeUnlock("Past", days_until=-2, amount_usd=999_000_000)]
    assert upcoming_unlocks(events, days_ahead=7) == []


# ----------------- sector_winners_and_losers -----------------


def test_sector_winners_losers_ordering():
    sectors = [
        {"sector": "AI", "avg_score": 60, "row_count": 5},
        {"sector": "DeFi", "avg_score": -30, "row_count": 12},
        {"sector": "Memes", "avg_score": -50, "row_count": 8},
        {"sector": "L1", "avg_score": 20, "row_count": 4},
    ]
    winners, losers = sector_winners_and_losers(sectors, top_n=2)
    assert [w["sector"] for w in winners] == ["AI", "L1"]
    assert [l["sector"] for l in losers] == ["Memes", "DeFi"]


# ----------------- compose_daily_digest -----------------


def test_compose_handles_empty_inputs():
    """Quiet market — every section computes without crashing."""
    digest = compose_daily_digest(combined_rows=[])
    assert digest["market_overview"]["total_symbols"] == 0
    assert digest["top_longs"] == []
    assert digest["top_shorts"] == []
    assert digest["top_squeezes"] == []
    assert digest["top_cascades"] == []
    assert digest["whale_highlight"] is None
    assert digest["upcoming_unlocks"] == []
    assert digest["macro"] is None


def test_compose_full_set_of_sections():
    rows = [
        _FakeRow("A", binance_symbol="AUSDT", composite_score=80, composite_emoji="🚀", composite_short="Strong bull"),
        _FakeRow("B", binance_symbol="BUSDT", composite_score=-80, composite_emoji="💥", composite_short="Strong bear"),
    ]
    liq_stats = {
        "AUSDT": {"long_liq_usd": 1_000_000, "short_liq_usd": 25_000_000, "total_usd": 26_000_000, "events_count": 100},
    }
    onchain = [{"token": "WIF", "whale_net_usd": 12_000_000, "whale_unique_count": 4}]
    unlocks = [_FakeUnlock("ARB", days_until=3, amount_usd=80_000_000)]
    supply = {"TOTAL": {"now": 200e9, "change_24h_pct": 0.5}}
    sectors = [{"sector": "AI", "avg_score": 50, "row_count": 5}]

    digest = compose_daily_digest(
        combined_rows=rows,
        liq_stats_by_symbol=liq_stats,
        onchain_flows=onchain,
        unlock_events=unlocks,
        stablecoin_supply=supply,
        sector_rows=sectors,
    )
    assert digest["market_overview"]["total_symbols"] == 2
    assert len(digest["top_longs"]) == 1
    assert len(digest["top_shorts"]) == 1
    assert len(digest["top_squeezes"]) == 1
    assert digest["whale_highlight"] is not None
    assert len(digest["upcoming_unlocks"]) == 1
    assert digest["macro"] is not None
    assert "sector_winners" in digest


def test_compose_sector_rows_optional():
    """Pass nothing for sectors → no sector_winners/losers in output."""
    digest = compose_daily_digest(combined_rows=[])
    assert "sector_winners" not in digest
    assert "sector_losers" not in digest


# ---------------- Round 49: watchlist filter ----------------


def test_compose_watchlist_filters_top_picks():
    """When watchlist is set, top_longs/top_shorts only include matching bases."""
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", composite_score=80,
                 composite_emoji="🚀", composite_short="Strong bull"),
        _FakeRow("ETH", binance_symbol="ETHUSDT", composite_score=75,
                 composite_emoji="🚀", composite_short="Strong bull"),
        _FakeRow("WIF", binance_symbol="WIFUSDT", composite_score=70,
                 composite_emoji="🚀", composite_short="Strong bull"),
    ]
    digest = compose_daily_digest(combined_rows=rows, watchlist={"BTC"})
    assert {p["base"] for p in digest["top_longs"]} == {"BTC"}


def test_compose_empty_watchlist_passes_all():
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", composite_score=80,
                 composite_emoji="🚀", composite_short="Strong bull"),
        _FakeRow("ETH", binance_symbol="ETHUSDT", composite_score=75,
                 composite_emoji="🚀", composite_short="Strong bull"),
    ]
    digest = compose_daily_digest(combined_rows=rows, watchlist=set())
    assert {p["base"] for p in digest["top_longs"]} == {"BTC", "ETH"}


def test_compose_none_watchlist_passes_all():
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", composite_score=80,
                 composite_emoji="🚀", composite_short="Strong bull"),
        _FakeRow("ETH", binance_symbol="ETHUSDT", composite_score=75,
                 composite_emoji="🚀", composite_short="Strong bull"),
    ]
    digest = compose_daily_digest(combined_rows=rows, watchlist=None)
    assert len(digest["top_longs"]) == 2


def test_compose_watchlist_market_overview_uses_full_universe():
    """market_overview is always computed across ALL rows, even with watchlist."""
    rows = [
        _FakeRow("BTC", composite_score=80),
        _FakeRow("ETH", composite_score=75),
        _FakeRow("SOL", composite_score=-80),
        _FakeRow("WIF", composite_score=10),
    ]
    digest = compose_daily_digest(combined_rows=rows, watchlist={"BTC"})
    # 4 total, despite watchlist of 1.
    assert digest["market_overview"]["total_symbols"] == 4
    assert digest["market_overview"]["bullish"] == 2  # BTC + ETH
    assert digest["market_overview"]["bearish"] == 1  # SOL


def test_compose_watchlist_filters_liq_squeezes_and_cascades():
    rows = [_FakeRow("BTC", composite_score=80)]
    liq_stats = {
        "BTCUSDT": {
            "long_liq_usd": 1_000_000, "short_liq_usd": 25_000_000,
            "total_usd": 26_000_000, "events_count": 100,
        },
        "ETHUSDT": {
            "long_liq_usd": 1_000_000, "short_liq_usd": 25_000_000,
            "total_usd": 26_000_000, "events_count": 100,
        },
    }
    digest = compose_daily_digest(
        combined_rows=rows, liq_stats_by_symbol=liq_stats, watchlist={"BTC"},
    )
    # ETHUSDT should be filtered out (its base ETH isn't in watchlist).
    assert {r["symbol"] for r in digest["top_squeezes"]} == {"BTCUSDT"}
