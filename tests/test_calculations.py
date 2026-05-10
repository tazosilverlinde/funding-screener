"""Calculation tests against the pure screener functions — no HTTP, no Streamlit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.models import ContractInfo, FundingRow, Kline
from funding_screener.screener import (
    screen_combined_high_funding,
    screen_high_funding,
    screen_price_rise,
    screen_usdt_usdc_arb,
)


# ---------------- fixtures ----------------


def _funding(
    exchange: str,
    symbol: str,
    base: str,
    quote: str,
    rate: float,
    interval: float = 8.0,
    next_t: datetime | None = None,
) -> FundingRow:
    return FundingRow(
        exchange=exchange,
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        rate_percent=rate,
        rate_8h_norm_percent=rate * 8.0 / interval,
        interval_hours=interval,
        mark_price=100.0,
        index_price=100.0,
        next_funding_time=next_t or datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
    )


def _contract(exchange: str, symbol: str, base: str, quote: str, maker: float = 0.02) -> ContractInfo:
    return ContractInfo(
        exchange=exchange,
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        status="TRADING",
        maker_fee_percent=maker,
        taker_fee_percent=0.05,
    )


def _kline(close: float, days_ago: int) -> Kline:
    t = datetime(2026, 5, 5, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return Kline(open_time=t, open=close, high=close, low=close, close=close, volume=1.0, quote_volume=close)


# ---------------- high funding ----------------


def test_high_funding_threshold():
    rows = [
        _funding("Binance", "AAA", "AAA", "USDT", rate=1.5, interval=8.0),
        _funding("Binance", "BBB", "BBB", "USDT", rate=0.5, interval=4.0),  # = 1.0% / 8h, not >
        _funding("Binance", "CCC", "CCC", "USDT", rate=0.6, interval=4.0),  # = 1.2% / 8h, flagged
        _funding("Binance", "DDD", "DDD", "USDT", rate=-2.0, interval=8.0),
        _funding("Binance", "EEE", "EEE", "USDT", rate=0.05, interval=1.0),  # = 0.4% / 8h, no
    ]
    out = screen_high_funding(rows, threshold_percent=1.0)
    assert {r.symbol for r in out} == {"AAA", "CCC", "DDD"}


def test_high_funding_sort_order():
    rows = [
        _funding("Binance", "AAA", "AAA", "USDT", rate=1.5),
        _funding("Binance", "BBB", "BBB", "USDT", rate=-3.0),
        _funding("Binance", "CCC", "CCC", "USDT", rate=2.0),
    ]
    out = screen_high_funding(rows, threshold_percent=1.0)
    assert [r.symbol for r in out] == ["BBB", "CCC", "AAA"]


# ---------------- combined high funding ----------------


def test_combined_high_funding_both_present():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", "USDT", rate=1.5)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", "USDT", rate=0.8)]
    bnb_c = [_contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02)]
    mxc_c = [_contract("MEXC", "BTC_USDT", "BTC", "USDT", maker=0.01)]
    out = screen_combined_high_funding(bnb, mxc, bnb_c, mxc_c, {}, threshold_percent=1.0)
    assert len(out) == 1
    r = out[0]
    assert r.base_asset == "BTC"
    assert r.binance_rate_percent == pytest.approx(1.5)
    assert r.mexc_rate_percent == pytest.approx(0.8)
    assert r.binance_maker_fee_percent == pytest.approx(0.02)
    assert r.mexc_maker_fee_percent == pytest.approx(0.01)
    assert r.spread_8h_norm_percent == pytest.approx(0.7)
    assert r.max_abs_8h_norm_percent == pytest.approx(1.5)
    # No enrichment supplied → signal still computed from funding alone, streaks empty.
    assert r.binance_funding_streak is None
    assert r.signal_emoji  # any non-empty


def test_combined_high_funding_includes_funding_history_chart():
    """Sparkline data flows from EnrichmentData.prev_funding_rates_percent.
    API returns most-recent first; the row stores oldest→newest for charting.
    """
    from funding_screener.models import EnrichmentData

    bnb = [_funding("Binance", "BTCUSDT", "BTC", "USDT", rate=1.5)]
    mxc: list[FundingRow] = []
    bnb_c = [_contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02)]
    enrich = {
        ("Binance", "BTCUSDT"): EnrichmentData(
            exchange="Binance",
            symbol="BTCUSDT",
            prev_funding_rates_percent=[0.5, 0.4, 0.3, 0.2, 0.1],  # newest-first
            funding_streak_count=3,
            funding_streak_direction="pos",
            mark_index_spread_percent=0.0,
            fetched_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        ),
    }
    out = screen_combined_high_funding(
        bnb, mxc, bnb_c, [], enrich, threshold_percent=1.0,
    )
    assert len(out) == 1
    r = out[0]
    # Chart should be reversed → oldest first.
    assert r.funding_history_chart == [0.1, 0.2, 0.3, 0.4, 0.5]


def test_combined_high_funding_empty_history_when_no_enrichment():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", "USDT", rate=1.5)]
    bnb_c = [_contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02)]
    out = screen_combined_high_funding(
        bnb, [], bnb_c, [], {}, threshold_percent=1.0,
    )
    assert out[0].funding_history_chart == []
    assert out[0].score_history_chart == []


def test_combined_high_funding_score_history_chart_populated():
    """Score history sparkline pulls from the score_histories arg."""
    bnb = [_funding("Binance", "BTCUSDT", "BTC", "USDT", rate=1.5)]
    bnb_c = [_contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02)]
    base_ts = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    histories = {
        ("BTC", "USDT"): [
            (base_ts - timedelta(minutes=30), 20),
            (base_ts - timedelta(minutes=20), 35),
            (base_ts - timedelta(minutes=10), 50),
            (base_ts, 60),
        ],
    }
    out = screen_combined_high_funding(
        bnb, [], bnb_c, [], {}, threshold_percent=1.0,
        score_histories=histories,
    )
    assert out[0].score_history_chart == [20, 35, 50, 60]


def test_combined_high_funding_score_chart_empty_for_single_sample():
    """Score sparkline needs ≥2 samples; one-shot histories produce empty."""
    bnb = [_funding("Binance", "BTCUSDT", "BTC", "USDT", rate=1.5)]
    bnb_c = [_contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02)]
    base_ts = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    histories = {("BTC", "USDT"): [(base_ts, 60)]}
    out = screen_combined_high_funding(
        bnb, [], bnb_c, [], {}, threshold_percent=1.0,
        score_histories=histories,
    )
    assert out[0].score_history_chart == []


def test_combined_high_funding_only_one_side():
    """Base only on Binance: still flagged if Binance rate > threshold."""
    bnb = [_funding("Binance", "FOOUSDT", "FOO", "USDT", rate=2.5)]
    mxc: list[FundingRow] = []
    bnb_c = [_contract("Binance", "FOOUSDT", "FOO", "USDT", maker=0.02)]
    out = screen_combined_high_funding(bnb, mxc, bnb_c, [], {}, threshold_percent=1.0)
    assert len(out) == 1
    r = out[0]
    assert r.binance_symbol == "FOOUSDT"
    assert r.binance_maker_fee_percent == pytest.approx(0.02)
    assert r.mexc_symbol is None
    assert r.mexc_rate_percent is None
    assert r.mexc_maker_fee_percent is None
    assert r.spread_8h_norm_percent is None
    assert r.max_abs_8h_norm_percent == pytest.approx(2.5)


def test_combined_high_funding_filters_below_threshold():
    bnb = [_funding("Binance", "ZZZUSDT", "ZZZ", "USDT", rate=0.3)]
    mxc = [_funding("MEXC", "ZZZ_USDT", "ZZZ", "USDT", rate=0.4)]
    out = screen_combined_high_funding(bnb, mxc, [], [], {}, threshold_percent=1.0)
    assert out == []


def test_combined_high_funding_drops_side_below_volume_floor():
    """Side with volume below floor should be hidden (None columns)."""
    bnb_c = [_contract("Binance", "FOOUSDT", "FOO", "USDT", maker=0.02)]
    mxc_c = [_contract("MEXC", "FOO_USDT", "FOO", "USDT", maker=0.02)]
    bnb = [_funding("Binance", "FOOUSDT", "FOO", "USDT", rate=2.0)]
    mxc = [_funding("MEXC", "FOO_USDT", "FOO", "USDT", rate=2.0)]
    out = screen_combined_high_funding(
        bnb, mxc, bnb_c, mxc_c, {}, threshold_percent=1.0,
        binance_volumes={"FOOUSDT": 50_000_000.0},
        mexc_volumes={"FOO_USDT": 100_000.0},  # below floor
        min_volume_usd_per_side=1_000_000.0,
    )
    assert len(out) == 1
    r = out[0]
    assert r.binance_symbol == "FOOUSDT"
    assert r.mexc_symbol is None
    assert r.mexc_rate_percent is None


def test_combined_high_funding_drops_row_when_both_sides_below_floor():
    bnb_c = [_contract("Binance", "FOOUSDT", "FOO", "USDT", maker=0.02)]
    bnb = [_funding("Binance", "FOOUSDT", "FOO", "USDT", rate=2.0)]
    out = screen_combined_high_funding(
        bnb, [], bnb_c, [], {}, threshold_percent=1.0,
        binance_volumes={"FOOUSDT": 100.0},
        min_volume_usd_per_side=1_000_000.0,
    )
    assert out == []


def test_combined_high_funding_drops_side_with_no_contract():
    """Funding row for a delisted symbol: contract missing → side hidden."""
    bnb_c: list = []  # contract missing (e.g. status was SETTLING and we filtered)
    mxc_c = [_contract("MEXC", "FOO_USDT", "FOO", "USDT", maker=0.02)]
    bnb = [_funding("Binance", "DELISTEDUSDT", "DELISTED", "USDT", rate=2.0)]
    mxc = [_funding("MEXC", "FOO_USDT", "FOO", "USDT", rate=2.0)]
    out = screen_combined_high_funding(bnb, mxc, bnb_c, mxc_c, {}, threshold_percent=1.0)
    # DELISTED's row must NOT appear because it has no contract on Binance.
    bases = [r.base_asset for r in out]
    assert "DELISTED" not in bases
    assert "FOO" in bases


def test_combined_high_funding_includes_usdc():
    """USDC pairs should appear as their own rows alongside USDT pairs."""
    bnb_c = [
        _contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02),
        _contract("Binance", "BTCUSDC", "BTC", "USDC", maker=0.02),
    ]
    bnb = [
        _funding("Binance", "BTCUSDT", "BTC", "USDT", rate=1.5),
        _funding("Binance", "BTCUSDC", "BTC", "USDC", rate=2.0),
    ]
    out = screen_combined_high_funding(bnb, [], bnb_c, [], {}, threshold_percent=1.0)
    quotes = sorted(r.quote_asset for r in out)
    assert quotes == ["USDC", "USDT"]
    # Sorted by max abs — USDC at 2.0 first.
    assert out[0].quote_asset == "USDC"
    assert out[0].binance_symbol == "BTCUSDC"
    assert out[1].quote_asset == "USDT"


def test_binance_maker_fee_override_priority():
    """Per-symbol > per-quote > default."""
    from funding_screener import config as cfg_mod

    original = cfg_mod.fees.cache_info()  # noqa: F841 — we restore via cache_clear below
    try:
        cfg_mod.fees.cache_clear()
        # Monkey-patch the fees() lookup to a deterministic test config.
        def _fake_fees():
            return {
                "binance": {
                    "futures_maker": 0.02,
                    "futures_taker": 0.05,
                    "futures_maker_by_quote": {"USDC": 0.0},
                    "futures_taker_by_quote": {"USDC": 0.015},
                    "futures_maker_by_symbol": {"BTCUSDT": 0.018},
                    "futures_taker_by_symbol": {},
                },
                "mexc": {"futures_maker": 0.0, "futures_taker": 0.02},
            }
        cfg_mod.fees = _fake_fees  # type: ignore[assignment]
        from funding_screener.config import binance_maker_fee_for

        assert binance_maker_fee_for("BTCUSDT", "USDT") == pytest.approx(0.018)  # symbol override
        assert binance_maker_fee_for("ETHUSDC", "USDC") == pytest.approx(0.0)    # quote override
        assert binance_maker_fee_for("ETHUSDT", "USDT") == pytest.approx(0.02)   # default
    finally:
        # Restore the cached helper.
        from importlib import reload
        reload(cfg_mod)


def test_combined_high_funding_ignores_other_quotes():
    """Quotes other than USDT/USDC (e.g. USD, USD1, BUSD) should be ignored."""
    bnb = [_funding("Binance", "BTCUSD", "BTC", "USD", rate=2.5)]
    out = screen_combined_high_funding(bnb, [], [], [], {}, threshold_percent=1.0)
    assert out == []


def test_combined_high_funding_attaches_enrichment():
    """When enrichment supplied, streak + spread surface and shape the signal."""
    from datetime import datetime, timezone
    from funding_screener.models import EnrichmentData

    bnb = [_funding("Binance", "ALTUSDT", "ALT", "USDT", rate=1.5)]
    bnb_c = [_contract("Binance", "ALTUSDT", "ALT", "USDT", maker=0.02)]
    enr = {
        ("Binance", "ALTUSDT"): EnrichmentData(
            exchange="Binance",
            symbol="ALTUSDT",
            prev_funding_rates_percent=[0.8, 0.6, 0.4, 0.2],  # 4 consecutive positive
            funding_streak_count=4,
            funding_streak_direction="pos",
            mark_index_spread_percent=0.05,
            fetched_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
    }
    out = screen_combined_high_funding(bnb, [], bnb_c, [], enr, threshold_percent=1.0)
    assert len(out) == 1
    r = out[0]
    assert r.binance_funding_streak == "4↑"
    assert r.binance_mark_index_spread_percent == pytest.approx(0.05)
    # Streak >= 3 + positive funding → "Persistent bear"
    assert "Persistent" in r.signal_short or "Bear" in r.signal_short
    assert r.signal_emoji in ("📉", "🔴")


# ---------------- USDT/USDC arb ----------------


def test_usdt_usdc_arb_positive_net():
    funding = [
        _funding("Binance", "BTCUSDT", "BTC", "USDT", rate=0.20),
        _funding("Binance", "BTCUSDC", "BTC", "USDC", rate=0.05),
    ]
    contracts = [
        _contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02),
        _contract("Binance", "BTCUSDC", "BTC", "USDC", maker=0.02),
    ]
    out = screen_usdt_usdc_arb(funding, contracts, exchange_name="Binance")
    assert len(out) == 1
    r = out[0]
    assert r.long_leg_symbol == "BTCUSDC"
    assert r.short_leg_symbol == "BTCUSDT"
    assert r.diff_abs_percent == pytest.approx(0.15)
    assert r.fees_total_percent == pytest.approx(0.08)
    assert r.net_percent == pytest.approx(0.07)


def test_usdt_usdc_arb_drops_unprofitable():
    funding = [
        _funding("Binance", "BTCUSDT", "BTC", "USDT", rate=0.10),
        _funding("Binance", "BTCUSDC", "BTC", "USDC", rate=0.07),
    ]
    contracts = [
        _contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02),
        _contract("Binance", "BTCUSDC", "BTC", "USDC", maker=0.02),
    ]
    out = screen_usdt_usdc_arb(funding, contracts, exchange_name="Binance")
    assert out == []


def test_usdt_usdc_arb_handles_negative_rates():
    funding = [
        _funding("Binance", "BTCUSDT", "BTC", "USDT", rate=-0.10),
        _funding("Binance", "BTCUSDC", "BTC", "USDC", rate=-0.30),
    ]
    contracts = [
        _contract("Binance", "BTCUSDT", "BTC", "USDT", maker=0.02),
        _contract("Binance", "BTCUSDC", "BTC", "USDC", maker=0.02),
    ]
    out = screen_usdt_usdc_arb(funding, contracts, exchange_name="Binance")
    assert len(out) == 1
    assert out[0].long_leg_symbol == "BTCUSDC"
    assert out[0].short_leg_symbol == "BTCUSDT"


def test_usdt_usdc_arb_skips_when_only_one_quote():
    funding = [_funding("Binance", "XRPUSDT", "XRP", "USDT", rate=0.5)]
    contracts = [_contract("Binance", "XRPUSDT", "XRP", "USDT")]
    out = screen_usdt_usdc_arb(funding, contracts, exchange_name="Binance")
    assert out == []


# ---------------- price rise ----------------


def _kline_full(high: float, close: float, quote_volume: float, days_ago: int) -> Kline:
    """Kline with explicit high and quote_volume for ATH/volume tests."""
    t = datetime(2026, 5, 5, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return Kline(
        open_time=t,
        open=close,
        high=high,
        low=close,
        close=close,
        volume=1.0,
        quote_volume=quote_volume,
    )


def test_price_rise_close_to_close():
    contracts = [_contract("Binance", "PUMPUSDT", "PUMP", "USDT")]
    funding_rows = [_funding("Binance", "PUMPUSDT", "PUMP", "USDT", rate=0.5, interval=8.0)]
    closes_by_days_ago = {30: 1.0, 7: 10.0, 1: 50.0, 0: 100.0}
    klines = []
    for d in range(30, -1, -1):
        c = closes_by_days_ago.get(d, 1.0 if d > 7 else (10.0 if d > 1 else 50.0))
        klines.append(_kline(close=c, days_ago=d))

    out = screen_price_rise(
        contracts,
        klines_by_symbol={"PUMPUSDT": klines},
        vol_map={"PUMPUSDT": 5_000_000.0},
        funding_rows=funding_rows,
        market_caps_usd={"PUMP": 250_000_000.0},
        threshold_percent=1000.0,
        windows_days=[1, 7, 30],
        min_24h_quote_volume=0.0,
    )
    assert len(out) == 1
    r = out[0]
    assert r.pct_1d == pytest.approx(100.0)
    assert r.pct_7d == pytest.approx(900.0)
    assert r.pct_30d == pytest.approx(9900.0)
    assert r.max_window_days == 30
    assert r.funding_rate_8h_norm_percent == pytest.approx(0.5)
    assert r.quote_volume_24h_millions == pytest.approx(5.0)
    assert r.market_cap_millions == pytest.approx(250.0)


def test_price_rise_ath_and_daily_volumes():
    """ATH = max(high) over kline history; daily volumes pulled from last 3 closes."""
    contracts = [_contract("Binance", "RIPUSDT", "RIP", "USDT")]
    klines = [
        _kline_full(high=42.0, close=1.0, quote_volume=10_000_000.0, days_ago=10),  # ATH here
        _kline_full(high=12.0, close=10.0, quote_volume=8_000_000.0, days_ago=2),   # day before
        _kline_full(high=15.0, close=12.0, quote_volume=4_000_000.0, days_ago=1),   # yesterday
        _kline_full(high=22.0, close=20.0, quote_volume=2_000_000.0, days_ago=0),   # today
    ]
    out = screen_price_rise(
        contracts,
        klines_by_symbol={"RIPUSDT": klines},
        vol_map={"RIPUSDT": 9_500_000.0},
        funding_rows=[],
        market_caps_usd={},  # CoinGecko data missing — should still produce row
        threshold_percent=0.0,  # accept anything positive
        windows_days=[1],
        min_24h_quote_volume=0.0,
    )
    assert len(out) == 1
    r = out[0]
    assert r.ath_price == pytest.approx(42.0)
    assert r.volume_today_millions == pytest.approx(2.0)
    assert r.volume_yesterday_millions == pytest.approx(4.0)
    assert r.volume_day_before_millions == pytest.approx(8.0)
    assert r.quote_volume_24h_millions == pytest.approx(9.5)
    assert r.market_cap_millions is None
    assert r.funding_rate_8h_norm_percent is None


def test_price_rise_filters_low_volume():
    contracts = [_contract("Binance", "LOWVOLUSDT", "LOWVOL", "USDT")]
    out = screen_price_rise(
        contracts,
        klines_by_symbol={"LOWVOLUSDT": [_kline(1.0, 0)]},
        vol_map={"LOWVOLUSDT": 1.0},
        funding_rows=[],
        market_caps_usd={},
        threshold_percent=1000.0,
        windows_days=[1, 7, 30],
        min_24h_quote_volume=100_000.0,
    )
    assert out == []


def test_price_rise_no_flag_when_below_threshold():
    contracts = [_contract("Binance", "OKUSDT", "OK", "USDT")]
    klines = [_kline(close=1.0, days_ago=d) for d in range(30, 0, -1)]
    klines.append(_kline(close=5.0, days_ago=0))
    out = screen_price_rise(
        contracts,
        klines_by_symbol={"OKUSDT": klines},
        vol_map={"OKUSDT": 1_000_000.0},
        funding_rows=[],
        market_caps_usd={},
        threshold_percent=1000.0,
        windows_days=[1, 7, 30],
        min_24h_quote_volume=0.0,
    )
    assert out == []


def test_price_rise_skips_symbol_with_no_cached_klines():
    """If background updater hasn't fetched klines for this symbol yet, skip silently."""
    contracts = [_contract("Binance", "NEWUSDT", "NEW", "USDT")]
    out = screen_price_rise(
        contracts,
        klines_by_symbol={},
        vol_map={"NEWUSDT": 10_000_000.0},
        funding_rows=[],
        market_caps_usd={},
        threshold_percent=1000.0,
        windows_days=[1, 7, 30],
        min_24h_quote_volume=0.0,
    )
    assert out == []
