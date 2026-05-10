"""Tests for the watchlist filter helpers (Round 48)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.notifications import (
    filter_funding_rows_by_watchlist,
    filter_liq_stats_by_watchlist,
    filter_rows_by_watchlist,
    parse_watchlist,
)


# ---------------- parse_watchlist ----------------


def test_parse_empty_returns_empty_set():
    assert parse_watchlist([]) == set()
    assert parse_watchlist(None) == set()


def test_parse_uppercases_entries():
    assert parse_watchlist(["btc", "Eth", "SOL"]) == {"BTC", "ETH", "SOL"}


def test_parse_strips_whitespace():
    assert parse_watchlist(["  BTC ", "ETH"]) == {"BTC", "ETH"}


def test_parse_skips_non_strings():
    assert parse_watchlist(["BTC", None, 42, "ETH"]) == {"BTC", "ETH"}


def test_parse_skips_empty_strings():
    assert parse_watchlist(["BTC", "", "  ", "ETH"]) == {"BTC", "ETH"}


# ---------------- filter_rows_by_watchlist ----------------


@dataclass
class _Row:
    base_asset: str = ""


def test_empty_watchlist_passes_all_rows():
    rows = [_Row("BTC"), _Row("ETH"), _Row("SOL")]
    assert len(filter_rows_by_watchlist(rows, set())) == 3


def test_filter_keeps_only_matching_bases():
    rows = [_Row("BTC"), _Row("ETH"), _Row("SOL"), _Row("WIF")]
    out = filter_rows_by_watchlist(rows, {"BTC", "WIF"})
    assert {r.base_asset for r in out} == {"BTC", "WIF"}


def test_filter_case_insensitive_match():
    rows = [_Row("btc"), _Row("Eth")]
    out = filter_rows_by_watchlist(rows, {"BTC", "ETH"})
    assert len(out) == 2


def test_filter_handles_none_rows():
    assert filter_rows_by_watchlist(None, {"BTC"}) == []  # type: ignore[arg-type]


def test_filter_handles_missing_base_attr():
    """Rows without base_asset attribute simply don't match (no crash)."""
    class _Headless:
        pass
    rows = [_Headless(), _Row("BTC")]
    out = filter_rows_by_watchlist(rows, {"BTC"})
    assert len(out) == 1
    assert out[0].base_asset == "BTC"


# ---------------- filter_funding_rows_by_watchlist ----------------


@dataclass
class _Funding:
    base_asset: str
    rate_percent: float = 0.0


def test_funding_filter_basic():
    rows = [_Funding("BTC"), _Funding("ETH"), _Funding("WIF")]
    out = filter_funding_rows_by_watchlist(rows, {"BTC", "WIF"})
    assert {r.base_asset for r in out} == {"BTC", "WIF"}


def test_funding_filter_empty_watchlist_passes_all():
    rows = [_Funding("BTC"), _Funding("ETH")]
    assert len(filter_funding_rows_by_watchlist(rows, set())) == 2


# ---------------- filter_liq_stats_by_watchlist ----------------


def test_liq_filter_strips_usdt_suffix():
    stats = {"BTCUSDT": {}, "ETHUSDT": {}, "WIFUSDC": {}}
    out = filter_liq_stats_by_watchlist(stats, {"BTC", "WIF"})
    assert set(out.keys()) == {"BTCUSDT", "WIFUSDC"}


def test_liq_filter_handles_busd_suffix():
    stats = {"BTCBUSD": {}, "ETHUSDT": {}}
    out = filter_liq_stats_by_watchlist(stats, {"BTC"})
    assert set(out.keys()) == {"BTCBUSD"}


def test_liq_filter_drops_unmatched_quote():
    """A symbol whose quote isn't in the (USDT/USDC/BUSD) list is dropped
    (we can't extract a base from it).
    """
    stats = {"BTCEUR": {}}
    out = filter_liq_stats_by_watchlist(stats, {"BTC"})
    assert out == {}


def test_liq_filter_empty_watchlist_passes_all():
    stats = {"BTCUSDT": {"a": 1}, "ETHUSDT": {"b": 2}}
    out = filter_liq_stats_by_watchlist(stats, set())
    assert out == stats


def test_liq_filter_case_insensitive():
    stats = {"btcusdt": {}}
    # base extraction = "btc" lowercase; we uppercase it before comparison.
    out = filter_liq_stats_by_watchlist(stats, {"BTC"})
    assert "btcusdt" in out


def test_liq_filter_none_safe():
    assert filter_liq_stats_by_watchlist(None, {"BTC"}) == {}  # type: ignore[arg-type]
    assert filter_liq_stats_by_watchlist({}, {"BTC"}) == {}
