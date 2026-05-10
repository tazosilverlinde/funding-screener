"""Tests for estimate_funding_income (Round 26)."""

from __future__ import annotations

import pytest

from funding_screener.signals import estimate_funding_income


def test_long_with_positive_rate_pays_funding():
    """Long position + positive funding rate → user PAYS → negative result."""
    out = estimate_funding_income(
        rate_8h_norm_pct=0.5, position_usd=10_000, hold_hours=24.0, direction="long",
    )
    # 0.5% of $10K = $50 per 8h × 3 periods = $150 per 24h, paid (negative).
    assert out == pytest.approx(-150.0)


def test_short_with_positive_rate_receives_funding():
    """Short + positive funding → user RECEIVES → positive result."""
    out = estimate_funding_income(
        rate_8h_norm_pct=0.5, position_usd=10_000, hold_hours=24.0, direction="short",
    )
    assert out == pytest.approx(150.0)


def test_long_with_negative_rate_receives_funding():
    """Long + negative funding → user RECEIVES (shorts pay)."""
    out = estimate_funding_income(
        rate_8h_norm_pct=-0.5, position_usd=10_000, hold_hours=24.0, direction="long",
    )
    assert out == pytest.approx(150.0)


def test_short_with_negative_rate_pays_funding():
    out = estimate_funding_income(
        rate_8h_norm_pct=-0.5, position_usd=10_000, hold_hours=24.0, direction="short",
    )
    assert out == pytest.approx(-150.0)


def test_partial_period_scales_proportionally():
    """8h hold = one period; 4h hold = half period."""
    full = estimate_funding_income(0.1, 10_000, hold_hours=8.0, direction="long")
    half = estimate_funding_income(0.1, 10_000, hold_hours=4.0, direction="long")
    assert half == pytest.approx(full / 2.0)


def test_returns_none_when_rate_is_none():
    assert estimate_funding_income(None, 10_000, 24.0) is None


def test_returns_none_for_zero_or_negative_position():
    assert estimate_funding_income(0.5, 0, 24.0) is None
    assert estimate_funding_income(0.5, -100, 24.0) is None


def test_returns_none_for_zero_or_negative_hold_hours():
    assert estimate_funding_income(0.5, 10_000, 0) is None
    assert estimate_funding_income(0.5, 10_000, -1) is None


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        estimate_funding_income(0.5, 10_000, 24.0, direction="sideways")


def test_zero_rate_means_zero_income():
    assert estimate_funding_income(0.0, 10_000, 24.0, "long") == 0.0


def test_scales_linearly_with_position_size():
    small = estimate_funding_income(0.1, 1_000, 24.0, "long")
    big = estimate_funding_income(0.1, 100_000, 24.0, "long")
    assert big == pytest.approx(small * 100)


def test_realistic_high_funding_scenario():
    """0.05%/8h (typical) on $25K, 24h, long: pays $0.05% * $25K * 3 = $37.50."""
    out = estimate_funding_income(0.05, 25_000, 24.0, "long")
    assert out == pytest.approx(-37.5)
