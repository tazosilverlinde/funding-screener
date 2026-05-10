"""Tests for evaluate_fresh_setup_alerts (Round 41)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.notifications import evaluate_fresh_setup_alerts


@dataclass
class _FakeRow:
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = "BTCUSDT"
    mexc_symbol: Optional[str] = None
    composite_score: Optional[int] = None
    setup_quality_label: Optional[str] = None
    binance_rate_8h_norm_percent: Optional[float] = None


# ---------------- happy path ----------------


def test_fires_for_fresh_bull_above_threshold():
    row = _FakeRow(composite_score=80, setup_quality_label="🚀 Fresh bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    actives = [(k, m) for (k, s, m) in out if s == "active"]
    assert len(actives) == 1
    key, msg = actives[0]
    assert key == "fresh:BTC/USDT"
    assert "fresh long setup" in msg
    assert "🚀" in msg


def test_fires_for_fresh_bear_below_negative_threshold():
    row = _FakeRow(composite_score=-85, setup_quality_label="💥 Fresh bear")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    actives = [m for (_k, s, m) in out if s == "active"]
    assert len(actives) == 1
    assert "fresh short setup" in actives[0]
    assert "💥" in actives[0]


# ---------------- filters that should NOT fire ----------------


def test_does_not_fire_for_building_quality():
    row = _FakeRow(composite_score=80, setup_quality_label="📈 Building bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


def test_does_not_fire_for_mature_quality():
    row = _FakeRow(composite_score=80, setup_quality_label="🎯 Mature bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    assert all(s == "resolved" for (_k, s, _m) in out)


def test_does_not_fire_for_late_quality():
    row = _FakeRow(composite_score=80, setup_quality_label="⏰ Late bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    assert all(s == "resolved" for (_k, s, _m) in out)


def test_does_not_fire_for_noisy_quality():
    row = _FakeRow(composite_score=80, setup_quality_label="⚠️ Noisy")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    assert all(s == "resolved" for (_k, s, _m) in out)


def test_does_not_fire_when_score_below_threshold():
    row = _FakeRow(composite_score=50, setup_quality_label="🚀 Fresh bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    assert all(s == "resolved" for (_k, s, _m) in out)


def test_does_not_fire_when_score_is_none():
    row = _FakeRow(composite_score=None, setup_quality_label="🚀 Fresh bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    assert out == []


def test_does_not_fire_when_label_missing():
    row = _FakeRow(composite_score=80, setup_quality_label=None)
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    assert out == []


# ---------------- threshold tunability ----------------


def test_threshold_tuneable_lower():
    """At default 70, 50 doesn't qualify; at threshold 30, it does."""
    row = _FakeRow(composite_score=50, setup_quality_label="🚀 Fresh bull")
    default = [s for (_k, s, _m) in evaluate_fresh_setup_alerts([row], 70) if s == "active"]
    assert default == []
    relaxed = [s for (_k, s, _m) in evaluate_fresh_setup_alerts([row], 30) if s == "active"]
    assert relaxed == ["active"]


# ---------------- thesis embedding ----------------


def test_active_message_includes_thesis_when_inputs_present():
    row = _FakeRow(
        composite_score=80,
        setup_quality_label="🚀 Fresh bull",
        binance_rate_8h_norm_percent=-1.0,  # bullish reason
    )
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "Bullish reasons" in msg
    assert "Funding deeply negative" in msg


def test_active_message_no_thesis_when_no_inputs():
    """Active fires even with a minimal row; thesis section just won't appear."""
    row = _FakeRow(composite_score=80, setup_quality_label="🚀 Fresh bull")
    out = evaluate_fresh_setup_alerts([row], min_abs_score=70)
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "fresh long setup" in msg
    assert "Bullish reasons" not in msg


# ---------------- multiple rows ----------------


def test_multiple_rows_independent():
    rows = [
        _FakeRow(base_asset="BTC", composite_score=80, setup_quality_label="🚀 Fresh bull"),
        _FakeRow(base_asset="ETH", composite_score=80, setup_quality_label="🎯 Mature bull"),
        _FakeRow(base_asset="SOL", composite_score=-85, setup_quality_label="💥 Fresh bear"),
    ]
    out = evaluate_fresh_setup_alerts(rows, min_abs_score=70)
    actives = {k for (k, s, _m) in out if s == "active"}
    assert "fresh:BTC/USDT" in actives
    assert "fresh:SOL/USDT" in actives
    assert "fresh:ETH/USDT" not in actives  # Mature, not Fresh
