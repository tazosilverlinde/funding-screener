"""Tests for per-pair composite-score threshold overrides (Round 69)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.notifications import evaluate_composite_alerts


@dataclass
class _Row:
    base_asset: str
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = None
    mexc_symbol: Optional[str] = None
    composite_score: Optional[int] = None
    composite_emoji: Optional[str] = "🚀"
    composite_short: Optional[str] = "Strong bull"
    composite_breakdown: Optional[str] = None


# ---------------- basic per-pair override behavior ----------------


def test_per_pair_lowers_threshold():
    """BTC scores +60 — under global +70 (resolved) but over per-pair +50 (active)."""
    row = _Row(base_asset="BTC", binance_symbol="BTCUSDT", composite_score=60)
    per_pair = {"BTCUSDT": {"bullish_threshold": 50, "bearish_threshold": -50}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"


def test_per_pair_raises_threshold():
    """WIF scores +75 — over global +70 (would be active) but under per-pair +85."""
    row = _Row(base_asset="WIF", binance_symbol="WIFUSDT", composite_score=75)
    per_pair = {"WIFUSDT": {"bullish_threshold": 85, "bearish_threshold": -85}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "resolved"


def test_no_override_falls_back_to_global():
    """Symbols not in per_pair use the global defaults."""
    row = _Row(base_asset="ETH", binance_symbol="ETHUSDT", composite_score=75)
    per_pair = {"WIFUSDT": {"bullish_threshold": 85}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"  # ETHUSDT uses global +70 → active


def test_none_per_pair_uses_global():
    row = _Row(base_asset="BTC", binance_symbol="BTCUSDT", composite_score=75)
    out = evaluate_composite_alerts([row], 70, -70, per_pair=None)
    assert out[0][1] == "active"


def test_empty_per_pair_uses_global():
    row = _Row(base_asset="BTC", binance_symbol="BTCUSDT", composite_score=75)
    out = evaluate_composite_alerts([row], 70, -70, per_pair={})
    assert out[0][1] == "active"


# ---------------- per-pair on the bearish side ----------------


def test_per_pair_bearish_threshold_tighter():
    """A pair tuned with bear=-50 fires at -55, where global -70 would not."""
    row = _Row(base_asset="X", binance_symbol="XUSDT", composite_score=-55)
    per_pair = {"XUSDT": {"bullish_threshold": 50, "bearish_threshold": -50}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"


def test_per_pair_bearish_threshold_looser():
    row = _Row(base_asset="X", binance_symbol="XUSDT", composite_score=-75)
    per_pair = {"XUSDT": {"bullish_threshold": 90, "bearish_threshold": -90}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "resolved"  # -75 doesn't reach -90


# ---------------- partial overrides (only one direction set) ----------------


def test_partial_override_uses_global_for_missing_direction():
    """Only bull is overridden — bear falls back to global -70."""
    row = _Row(base_asset="X", binance_symbol="XUSDT", composite_score=-75)
    per_pair = {"XUSDT": {"bullish_threshold": 100}}  # no bear key
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"  # global -70 still applies


# ---------------- lookup ordering: binance → mexc → base ----------------


def test_lookup_uses_binance_symbol_first():
    row = _Row(base_asset="BTC", binance_symbol="BTCUSDT", mexc_symbol="BTC_USDT",
               composite_score=60)
    per_pair = {
        "BTCUSDT": {"bullish_threshold": 50},   # match — fires
        "BTC_USDT": {"bullish_threshold": 100}, # also matches but tested second
        "BTC": {"bullish_threshold": 100},
    }
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"  # BTCUSDT match wins; threshold=50; 60≥50


def test_lookup_falls_back_to_mexc_symbol():
    row = _Row(base_asset="X", binance_symbol=None, mexc_symbol="X_USDT",
               composite_score=60)
    per_pair = {"X_USDT": {"bullish_threshold": 50}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"


def test_lookup_falls_back_to_base_ticker():
    """No symbol fields populated → look up by base."""
    row = _Row(base_asset="X", binance_symbol=None, mexc_symbol=None,
               composite_score=60)
    per_pair = {"X": {"bullish_threshold": 50}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    assert out[0][1] == "active"


# ---------------- multiple rows + isolation ----------------


def test_overrides_dont_leak_across_rows():
    """One pair with override shouldn't affect another pair's evaluation."""
    rows = [
        _Row(base_asset="BTC", binance_symbol="BTCUSDT", composite_score=55),  # bull at 50
        _Row(base_asset="ETH", binance_symbol="ETHUSDT", composite_score=55),  # bull at 70 (global)
    ]
    per_pair = {"BTCUSDT": {"bullish_threshold": 50}}
    out = evaluate_composite_alerts(rows, 70, -70, per_pair=per_pair)
    by_key = {k: s for (k, s, _m) in out}
    assert by_key["composite:BTC/USDT"] == "active"
    assert by_key["composite:ETH/USDT"] == "resolved"


# ---------------- message content honors override ----------------


def test_active_message_includes_symbol():
    row = _Row(base_asset="BTC", binance_symbol="BTCUSDT", composite_score=60)
    per_pair = {"BTCUSDT": {"bullish_threshold": 50}}
    out = evaluate_composite_alerts([row], 70, -70, per_pair=per_pair)
    msg = out[0][2]
    assert "BTCUSDT" in msg
    assert "+60" in msg
