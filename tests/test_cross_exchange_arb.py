"""Tests for the cross-exchange arbitrage screener (Round 23)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.models import ContractInfo, FundingRow
from funding_screener.screener import screen_cross_exchange_arb


def _funding(
    exchange: str, symbol: str, base: str, quote: str = "USDT",
    rate_pct: float = 0.0, interval_h: float = 8.0,
    next_funding_min_from_now: float = 60.0,
) -> FundingRow:
    """Build a FundingRow with rate_8h_norm derived from rate_pct/interval."""
    return FundingRow(
        exchange=exchange, symbol=symbol,
        base_asset=base, quote_asset=quote,
        rate_percent=rate_pct,
        rate_8h_norm_percent=rate_pct * 8.0 / interval_h,
        interval_hours=interval_h,
        mark_price=100.0, index_price=100.0,
        next_funding_time=datetime.now(timezone.utc) + timedelta(minutes=next_funding_min_from_now),
    )


def _contract(symbol: str, base: str, quote: str = "USDT", maker_fee: float = 0.02,
              status: str = "TRADING") -> ContractInfo:
    return ContractInfo(
        exchange="X", symbol=symbol, base_asset=base, quote_asset=quote,
        status=status,
        maker_fee_percent=maker_fee, taker_fee_percent=maker_fee * 2,
    )


# ---------------- positive cases ----------------


def test_emits_row_when_spread_exceeds_fees():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=1.0)]   # 8h norm = +1%
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-0.5)]   # 8h norm = -0.5%
    # Spread = 1.5%; fees @ 0.02% × 4 = 0.08%; net = 1.42%
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9},
        mexc_volumes={"BTC_USDT": 1e9},
        min_volume_usd_per_side=1_000_000,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.diff_8h_norm_percent == pytest.approx(1.5)
    assert r.fees_total_percent == pytest.approx(0.08)
    assert r.net_8h_norm_percent == pytest.approx(1.42)
    # Long the lower-funding side: MEXC is at -0.5, Binance at +1.0 → long MEXC
    assert r.long_exchange == "MEXC"
    assert r.long_symbol == "BTC_USDT"
    assert r.short_exchange == "Binance"


def test_apr_estimate_scales_with_net():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=0.1)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-0.1)]
    # Net = 0.2 - 0.08 = 0.12% per 8h. APR = 0.12 × 3 × 365 = 131.4%
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDT": 1e9},
    )
    assert rows[0].apr_estimate_percent == pytest.approx(131.4, rel=0.001)


def test_skew_minutes_computed_from_next_funding_times():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=1.0, next_funding_min_from_now=60)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-1.0, next_funding_min_from_now=180)]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDT": 1e9},
    )
    assert rows[0].funding_time_skew_minutes == pytest.approx(120, abs=1)


# ---------------- negative cases / filters ----------------


def test_excludes_when_spread_below_fees():
    """Tiny spread that doesn't cover fees → row dropped."""
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=0.05)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=0.04)]  # 0.01% diff < 0.08% fees
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDT": 1e9},
    )
    assert rows == []


def test_excludes_when_only_one_side_listed():
    """Base trades on Binance only → no cross-exchange opportunity."""
    bnb = [_funding("Binance", "FOOUSDT", "FOO", rate_pct=2.0)]
    rows = screen_cross_exchange_arb(
        bnb, [],
        [_contract("FOOUSDT", "FOO")], [],
    )
    assert rows == []


def test_excludes_when_one_side_below_volume_floor():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=1.0)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-0.5)]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9},
        mexc_volumes={"BTC_USDT": 100},  # way below default $1M floor
        min_volume_usd_per_side=1_000_000,
    )
    assert rows == []


def test_zero_volume_floor_disables_filter():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=1.0)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-0.5)]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={}, mexc_volumes={},
        min_volume_usd_per_side=0,
    )
    assert len(rows) == 1


def test_excludes_non_trading_status():
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=1.0)]
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-0.5)]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC", status="HALTED")],
        [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDT": 1e9},
    )
    assert rows == []


def test_excludes_cross_quote_pairs():
    """USDT on one side, USDC on the other → no row (FX risk)."""
    bnb = [_funding("Binance", "BTCUSDT", "BTC", "USDT", rate_pct=1.0)]
    mxc = [_funding("MEXC", "BTC_USDC", "BTC", "USDC", rate_pct=-1.0)]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC", "USDT")], [_contract("BTC_USDC", "BTC", "USDC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDC": 1e9},
    )
    assert rows == []


# ---------------- ordering ----------------


def test_rows_sorted_by_net_descending():
    bnb = [
        _funding("Binance", "BTCUSDT", "BTC", rate_pct=0.5),    # smaller spread
        _funding("Binance", "ETHUSDT", "ETH", rate_pct=2.0),    # bigger spread
    ]
    mxc = [
        _funding("MEXC", "BTC_USDT", "BTC", rate_pct=-0.2),
        _funding("MEXC", "ETH_USDT", "ETH", rate_pct=-1.0),
    ]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC"), _contract("ETHUSDT", "ETH")],
        [_contract("BTC_USDT", "BTC"), _contract("ETH_USDT", "ETH")],
        binance_volumes={"BTCUSDT": 1e9, "ETHUSDT": 1e9},
        mexc_volumes={"BTC_USDT": 1e9, "ETH_USDT": 1e9},
    )
    assert len(rows) == 2
    assert rows[0].base_asset == "ETH"  # bigger net first
    assert rows[1].base_asset == "BTC"


def test_long_short_attribution_swaps_with_sign():
    """If MEXC side is HIGHER funding, long Binance instead."""
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=-1.0)]  # 8h norm = -1%
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=2.0)]    # 8h norm = +2%
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDT": 1e9},
    )
    assert rows[0].long_exchange == "Binance"  # lower funding side
    assert rows[0].short_exchange == "MEXC"


def test_uses_8h_norm_not_raw_rate():
    """Different funding intervals — math must use 8h-normalized rates."""
    # Binance pays 1% / 8h = 1% per 8h
    bnb = [_funding("Binance", "BTCUSDT", "BTC", rate_pct=1.0, interval_h=8.0)]
    # MEXC pays -1% / 4h = -2% per 8h
    mxc = [_funding("MEXC", "BTC_USDT", "BTC", rate_pct=-1.0, interval_h=4.0)]
    rows = screen_cross_exchange_arb(
        bnb, mxc,
        [_contract("BTCUSDT", "BTC")], [_contract("BTC_USDT", "BTC")],
        binance_volumes={"BTCUSDT": 1e9}, mexc_volumes={"BTC_USDT": 1e9},
    )
    # Diff should be |1 - (-2)| = 3, not |1 - (-1)| = 2.
    assert rows[0].diff_8h_norm_percent == pytest.approx(3.0)
