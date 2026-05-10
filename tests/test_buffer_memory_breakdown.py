"""Tests for estimate_buffer_memory (Round 53)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from collections import defaultdict, deque

from funding_screener.liquidations import LiquidationEvent, LiquidationsBuffer
from funding_screener.notifications import AlertLog
from funding_screener.process_memory import estimate_buffer_memory


def _stub_store(**overrides):
    """Build a minimal stand-in for DataStore that has the attributes
    estimate_buffer_memory reads. Anything not overridden is empty.
    """
    base = {
        "score_history": {},
        "liquidations": LiquidationsBuffer(),
        "binance": SimpleNamespace(klines={}),
        "mexc": SimpleNamespace(klines={}),
        "onchain_flows_by_chain": {},
        "macro_daily_flows_by_chain": {},
        "alert_log": AlertLog(),
        "enrichments": {},
        "market_caps_usd": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------- shape ----------------


def test_returns_one_row_per_known_buffer():
    out = estimate_buffer_memory(_stub_store())
    names = {r["buffer"] for r in out}
    expected = {
        "score_history", "liquidations", "klines", "onchain_flows",
        "macro_daily_flows", "alert_log", "enrichments", "market_caps",
    }
    assert names == expected


def test_empty_store_yields_zero_entries():
    out = estimate_buffer_memory(_stub_store())
    for row in out:
        assert row["entries"] == 0
        assert row["est_mb"] == 0.0


# ---------------- entry counting ----------------


def test_counts_score_history_samples_across_pairs():
    history = {
        ("BTC", "USDT"): [(datetime.now(timezone.utc), 60)] * 100,
        ("ETH", "USDT"): [(datetime.now(timezone.utc), 50)] * 50,
    }
    out = estimate_buffer_memory(_stub_store(score_history=history))
    sh = next(r for r in out if r["buffer"] == "score_history")
    assert sh["entries"] == 150


def test_counts_liquidations_across_symbols():
    buf = LiquidationsBuffer()
    for sym in ("BTCUSDT", "ETHUSDT"):
        for i in range(25):
            buf.add(LiquidationEvent(
                symbol=sym, side_liquidated="long",
                price_usd=1.0, qty=1.0, notional_usd=1.0,
                timestamp=1700000000.0 + i,
            ))
    out = estimate_buffer_memory(_stub_store(liquidations=buf))
    liq = next(r for r in out if r["buffer"] == "liquidations")
    assert liq["entries"] == 50


def test_counts_klines_across_both_exchanges():
    bnb_klines = {"BTCUSDT": list(range(365)), "ETHUSDT": list(range(365))}
    mxc_klines = {"BTC_USDT": list(range(180))}
    bnb = SimpleNamespace(klines=bnb_klines)
    mxc = SimpleNamespace(klines=mxc_klines)
    out = estimate_buffer_memory(_stub_store(binance=bnb, mexc=mxc))
    kl = next(r for r in out if r["buffer"] == "klines")
    assert kl["entries"] == 365 * 2 + 180


def test_counts_onchain_flows_across_chains():
    flows = {
        "ethereum": [{"token": "BTC", "net_usd": 1.0}] * 30,
        "bsc": [{"token": "BTCB", "net_usd": 1.0}] * 20,
    }
    out = estimate_buffer_memory(_stub_store(onchain_flows_by_chain=flows))
    of = next(r for r in out if r["buffer"] == "onchain_flows")
    assert of["entries"] == 50


def test_counts_macro_flows_across_chain_and_token():
    macro = {
        "ethereum": {"USDT": [{"date": "x"}] * 7, "USDC": [{"date": "x"}] * 7},
        "bsc": {"BTCB": [{"date": "x"}] * 7},
    }
    out = estimate_buffer_memory(_stub_store(macro_daily_flows_by_chain=macro))
    mf = next(r for r in out if r["buffer"] == "macro_daily_flows")
    assert mf["entries"] == 21


def test_counts_alert_log_entries():
    log = AlertLog()
    for i in range(40):
        log.record(f"composite:X{i}/USDT", "active", "msg", ())
    out = estimate_buffer_memory(_stub_store(alert_log=log))
    al = next(r for r in out if r["buffer"] == "alert_log")
    assert al["entries"] == 40


def test_counts_enrichments_and_market_caps():
    out = estimate_buffer_memory(_stub_store(
        enrichments={(f"e", f"S{i}"): None for i in range(60)},
        market_caps_usd={f"BASE{i}": 1.0e9 for i in range(120)},
    ))
    enr = next(r for r in out if r["buffer"] == "enrichments")
    mcap = next(r for r in out if r["buffer"] == "market_caps")
    assert enr["entries"] == 60
    assert mcap["entries"] == 120


# ---------------- ordering + estimates ----------------


def test_ordered_by_estimated_mb_descending():
    """The breakdown table should put the biggest buffer first."""
    history = {("BTC", "USDT"): [(datetime.now(timezone.utc), 60)] * 5_000}
    flows = {"ethereum": [{"token": "BTC"}] * 1_000}
    out = estimate_buffer_memory(_stub_store(
        score_history=history,
        onchain_flows_by_chain=flows,
    ))
    # est_mb monotonically non-increasing.
    for i in range(1, len(out)):
        assert out[i - 1]["est_mb"] >= out[i]["est_mb"]


def test_est_mb_scales_with_count():
    small_log = AlertLog()
    for _ in range(10):
        small_log.record("a:b", "active", "m", ())
    big_log = AlertLog()
    for _ in range(100):
        big_log.record("a:b", "active", "m", ())

    small_mb = next(
        r["est_mb"] for r in estimate_buffer_memory(_stub_store(alert_log=small_log))
        if r["buffer"] == "alert_log"
    )
    big_mb = next(
        r["est_mb"] for r in estimate_buffer_memory(_stub_store(alert_log=big_log))
        if r["buffer"] == "alert_log"
    )
    assert big_mb > small_mb
    assert big_mb == round(small_mb * 10, 2) or abs(big_mb - small_mb * 10) < 0.05


# ---------------- defensive ----------------


def test_handles_store_with_missing_attributes():
    """A store-like object missing some attributes (e.g. test stubs) should
    still produce all rows with zero counts — never crash.
    """
    minimal = SimpleNamespace()  # no attributes at all
    out = estimate_buffer_memory(minimal)
    assert len(out) == 8
    for row in out:
        assert row["entries"] == 0


def test_handles_none_attributes():
    """Defensive — None where a dict is expected shouldn't crash."""
    store = SimpleNamespace(
        score_history=None,
        liquidations=None,
        binance=None,
        mexc=None,
        onchain_flows_by_chain=None,
        macro_daily_flows_by_chain=None,
        alert_log=None,
        enrichments=None,
        market_caps_usd=None,
    )
    out = estimate_buffer_memory(store)
    for row in out:
        assert row["entries"] == 0
