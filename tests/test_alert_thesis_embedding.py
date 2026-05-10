"""Tests for thesis embedding in score-delta, funding-deviation, and OI-surge
alerts (Round 39).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.notifications import (
    evaluate_funding_deviation_alerts,
    evaluate_oi_surge_alerts,
    evaluate_score_delta_alerts,
)


@dataclass
class _RichRow:
    base_asset: str
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = "BTCUSDT"
    mexc_symbol: Optional[str] = None
    composite_score: Optional[int] = None
    composite_emoji: Optional[str] = None
    composite_short: Optional[str] = None
    composite_score_delta_1h: Optional[int] = None
    funding_deviation_z: Optional[float] = None
    binance_oi_change_24h_pct: Optional[float] = None
    binance_rate_8h_norm_percent: Optional[float] = None
    binance_mark_index_spread_percent: Optional[float] = None
    binance_ls_ratio_global: Optional[float] = None
    binance_ls_ratio_top: Optional[float] = None
    signal_age_hours: Optional[float] = None
    composite_score_stddev_24h: Optional[float] = None
    setup_quality_label: Optional[str] = None


# ---------------- score-delta evaluator ----------------


def test_score_delta_active_includes_thesis():
    row = _RichRow(
        base_asset="BTC",
        composite_score=70,
        composite_emoji="🚀",
        composite_short="Strong bull",
        composite_score_delta_1h=40,
        binance_rate_8h_norm_percent=-0.8,  # → bullish reason
    )
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    msg = next(m for (k, s, m) in out if s == "active")
    assert "Bullish reasons" in msg
    assert "Funding deeply negative" in msg


def test_score_delta_active_no_thesis_when_no_data():
    row = _RichRow(
        base_asset="X",
        composite_score=70,
        composite_emoji="🚀",
        composite_short="Strong bull",
        composite_score_delta_1h=40,
    )
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    msg = next(m for (k, s, m) in out if s == "active")
    # Header still present.
    assert "Composite score Δ" in msg or "score Δ" in msg
    # No reasons because no inputs fired any rule.
    assert "Bullish reasons" not in msg
    assert "Bearish reasons" not in msg


def test_score_delta_resolved_format_unchanged():
    row = _RichRow(base_asset="X", composite_score_delta_1h=5)
    out = evaluate_score_delta_alerts([row], abs_threshold=25)
    assert out[0][1] == "resolved"
    assert "back below threshold" in out[0][2]


# ---------------- funding-deviation evaluator ----------------


def test_funding_deviation_active_overshoot_includes_thesis():
    row = _RichRow(
        base_asset="ETH",
        composite_score=-60,
        funding_deviation_z=3.0,            # extreme overshoot → active
        binance_rate_8h_norm_percent=1.2,   # bearish reason: deeply positive
    )
    out = evaluate_funding_deviation_alerts([row], z_threshold=2.5)
    actives = [m for (_k, s, m) in out if s == "active"]
    assert len(actives) == 1
    msg = actives[0]
    assert "extreme overshoot" in msg
    # Thesis content should be appended.
    assert "Bearish reasons" in msg
    assert "Funding deeply positive" in msg


def test_funding_deviation_active_undershoot_includes_thesis():
    row = _RichRow(
        base_asset="X",
        composite_score=70,
        funding_deviation_z=-3.0,
        binance_rate_8h_norm_percent=-1.0,
    )
    out = evaluate_funding_deviation_alerts([row], z_threshold=2.5)
    actives = [m for (_k, s, m) in out if s == "active"]
    assert any("extreme undershoot" in m for m in actives)
    assert any("Bullish reasons" in m for m in actives)


def test_funding_deviation_resolved_format_unchanged():
    row = _RichRow(base_asset="X", funding_deviation_z=0.5)
    out = evaluate_funding_deviation_alerts([row], z_threshold=2.5)
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


# ---------------- OI-surge evaluator ----------------


def test_oi_surge_active_includes_thesis():
    row = _RichRow(
        base_asset="BTC",
        composite_score=80,
        binance_oi_change_24h_pct=60.0,
        binance_rate_8h_norm_percent=-1.0,  # bullish reason
    )
    out = evaluate_oi_surge_alerts([row], threshold_pct_24h=50.0)
    msg = next(m for (k, s, m) in out if s == "active")
    assert "OI Δ24h" in msg
    assert "Fresh leverage" in msg
    # Thesis appended.
    assert "Bullish reasons" in msg
    assert "Funding deeply negative" in msg


def test_oi_surge_active_no_thesis_when_no_inputs():
    """Header is present even when no inputs fired any thesis rule."""
    row = _RichRow(base_asset="X", binance_oi_change_24h_pct=60.0)
    out = evaluate_oi_surge_alerts([row], threshold_pct_24h=50.0)
    msg = next(m for (k, s, m) in out if s == "active")
    assert "OI Δ24h" in msg
    assert "Bullish reasons" not in msg


def test_oi_surge_resolved_format_unchanged():
    row = _RichRow(base_asset="X", binance_oi_change_24h_pct=10.0)
    out = evaluate_oi_surge_alerts([row], threshold_pct_24h=50.0)
    assert out[0][1] == "resolved"
    assert "OI 24h Δ back to" in out[0][2]


# ---------------- shared invariants ----------------


def test_thesis_helper_works_with_minimal_row():
    """A row with only base_asset (no optional fields) must not crash any
    evaluator. getattr fallback to None is the safety net.
    """
    minimal = _RichRow(
        base_asset="X",
        composite_score=80,
        composite_emoji="🚀",
        composite_short="Strong bull",
        composite_score_delta_1h=40,
        funding_deviation_z=3.0,
        binance_oi_change_24h_pct=60.0,
    )
    # All three should produce active without error.
    assert any(s == "active" for (_k, s, _m) in evaluate_score_delta_alerts([minimal], abs_threshold=25))
    assert any(s == "active" for (_k, s, _m) in evaluate_funding_deviation_alerts([minimal], z_threshold=2.5))
    assert any(s == "active" for (_k, s, _m) in evaluate_oi_surge_alerts([minimal], threshold_pct_24h=50.0))
