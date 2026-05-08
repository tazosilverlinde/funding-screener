"""Tests for the alert state machine + evaluators."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import pytest

from funding_screener.notifications import (
    AlertState,
    evaluate_composite_alerts,
    evaluate_funding_alerts,
)


# ---------------- AlertState ----------------


def test_alert_state_first_fire():
    state = AlertState()
    assert state.should_fire("k", cooldown_seconds=60)
    state.mark_fired("k")
    # Same key while active → don't refire.
    assert not state.should_fire("k", cooldown_seconds=60)


def test_alert_state_cooldown_after_resolve():
    """After resolution, can't refire until cooldown elapses."""
    state = AlertState()
    state.mark_fired("k")
    state.mark_resolved("k")
    # Cooldown still active; should NOT fire.
    assert not state.should_fire("k", cooldown_seconds=300)


def test_alert_state_separate_keys_independent():
    state = AlertState()
    state.mark_fired("a")
    assert state.should_fire("b", cooldown_seconds=60)


# ---------------- evaluate_funding_alerts ----------------


@dataclass
class _FakeRow:
    exchange: str
    symbol: str
    rate_8h_norm_percent: float


def test_funding_alert_fires_above_threshold():
    rows = [_FakeRow("Binance", "ARBUSDT", 2.5)]
    out = evaluate_funding_alerts(rows, threshold_pct=2.0)
    assert len(out) == 1
    key, status, msg = out[0]
    assert "ARBUSDT" in msg
    assert status == "active"
    assert key == "funding:Binance:ARBUSDT"


def test_funding_alert_resolved_below_threshold():
    rows = [_FakeRow("MEXC", "FOO_USDT", 0.5)]
    out = evaluate_funding_alerts(rows, threshold_pct=2.0)
    assert out[0][1] == "resolved"


def test_funding_alert_negative_rate_above_threshold():
    """Magnitude matters, sign doesn't."""
    rows = [_FakeRow("Binance", "WIFUSDT", -3.0)]
    out = evaluate_funding_alerts(rows, threshold_pct=2.0)
    assert out[0][1] == "active"
    assert "negative" in out[0][2]


# ---------------- evaluate_composite_alerts ----------------


@dataclass
class _FakeCombined:
    base_asset: str
    quote_asset: str
    binance_symbol: Optional[str]
    mexc_symbol: Optional[str]
    composite_score: Optional[int]
    composite_emoji: Optional[str]
    composite_short: Optional[str]
    composite_breakdown: Optional[str]


def test_composite_alert_strong_bull():
    row = _FakeCombined("ARB", "USDT", "ARBUSDT", "ARB_USDT", 85, "🚀", "Strong bull", "...")
    out = evaluate_composite_alerts([row], bull_threshold=70, bear_threshold=-70)
    assert out[0][1] == "active"
    assert "ARBUSDT" in out[0][2]


def test_composite_alert_strong_bear():
    row = _FakeCombined("X", "USDT", "XUSDT", None, -90, "💥", "Strong bear", "...")
    out = evaluate_composite_alerts([row], bull_threshold=70, bear_threshold=-70)
    assert out[0][1] == "active"
    assert "XUSDT" in out[0][2]


def test_composite_alert_resolved():
    row = _FakeCombined("X", "USDT", "XUSDT", None, 50, "🟢", "Bullish", "...")
    out = evaluate_composite_alerts([row], bull_threshold=70, bear_threshold=-70)
    assert out[0][1] == "resolved"


def test_composite_alert_skips_none_score():
    row = _FakeCombined("X", "USDT", "XUSDT", None, None, None, None, None)
    out = evaluate_composite_alerts([row], bull_threshold=70, bear_threshold=-70)
    assert out == []


# ---------------- evaluate_score_delta_alerts ----------------


@dataclass
class _FakeRowWithDelta:
    base_asset: str
    quote_asset: str
    binance_symbol: Optional[str]
    mexc_symbol: Optional[str]
    composite_score: Optional[int]
    composite_emoji: Optional[str]
    composite_short: Optional[str]
    composite_score_delta_1h: Optional[int]


def test_score_delta_alert_fires_on_big_jump():
    from funding_screener.notifications import evaluate_score_delta_alerts
    row = _FakeRowWithDelta("ARB", "USDT", "ARBUSDT", None, 60, "🟢", "Bullish", 35)
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    assert out[0][1] == "active"
    assert "surging" in out[0][2]
    assert "ARBUSDT" in out[0][2]


def test_score_delta_alert_negative_direction():
    from funding_screener.notifications import evaluate_score_delta_alerts
    row = _FakeRowWithDelta("PEPE", "USDT", "PEPEUSDT", None, -40, "🔴", "Bearish", -30)
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    assert out[0][1] == "active"
    assert "plunging" in out[0][2]


def test_score_delta_alert_resolved_when_below_threshold():
    from funding_screener.notifications import evaluate_score_delta_alerts
    row = _FakeRowWithDelta("X", "USDT", "XUSDT", None, 50, "🟢", "Bullish", 5)
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    assert out[0][1] == "resolved"


def test_score_delta_alert_skips_none_delta():
    from funding_screener.notifications import evaluate_score_delta_alerts
    row = _FakeRowWithDelta("X", "USDT", "XUSDT", None, 50, "🟢", "Bullish", None)
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    assert out == []
