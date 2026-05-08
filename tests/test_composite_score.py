"""Tests for the composite signal score."""

from __future__ import annotations

import pytest

from funding_screener.signals import compute_composite_score


def test_neutral_when_no_inputs():
    s = compute_composite_score()
    assert s.score == 0
    assert s.short == "Neutral"
    assert s.emoji == "🟡"


def test_negative_funding_alone_is_bullish():
    s = compute_composite_score(funding_8h_norm_pct=-1.5)
    assert s.score > 0
    assert s.color == "green"


def test_positive_funding_alone_is_bearish():
    s = compute_composite_score(funding_8h_norm_pct=2.0)
    assert s.score < 0
    assert s.color == "red"


def test_streak_amplifies_funding_signal():
    base = compute_composite_score(funding_8h_norm_pct=-0.5).score
    with_streak = compute_composite_score(
        funding_8h_norm_pct=-0.5, streak_count=4, streak_direction="neg"
    ).score
    assert with_streak > base


def test_oi_rising_with_negative_funding_is_strong_bull():
    """OI rising + negative funding (shorts paying) → very bullish."""
    s = compute_composite_score(
        funding_8h_norm_pct=-1.0,
        streak_count=3, streak_direction="neg",
        oi_change_24h_pct=30.0,
    )
    assert s.score >= 30
    assert s.color == "green"


def test_oi_rising_with_positive_funding_is_bearish():
    """OI rising + positive funding (longs paying) = leveraged longs piling in."""
    base = compute_composite_score(funding_8h_norm_pct=1.0).score
    with_oi = compute_composite_score(funding_8h_norm_pct=1.0, oi_change_24h_pct=30.0).score
    assert with_oi < base


def test_extreme_long_ls_is_contrarian_bearish():
    s = compute_composite_score(ls_ratio_global=4.5)
    assert s.score < 0


def test_extreme_short_ls_is_contrarian_bullish():
    s = compute_composite_score(ls_ratio_global=0.3)
    assert s.score > 0


def test_smart_money_against_retail_adds_signal():
    """L/S retail high (long) + L/S top low (short) → smart money is short → -5."""
    base = compute_composite_score(ls_ratio_global=1.8).score
    with_div = compute_composite_score(ls_ratio_global=1.8, ls_ratio_top=0.7).score
    assert with_div < base


def test_onchain_withdrawals_are_bullish():
    s = compute_composite_score(onchain_net_usd=20_000_000)
    assert s.score > 0


def test_onchain_deposits_are_bearish():
    s = compute_composite_score(onchain_net_usd=-30_000_000)
    assert s.score < 0


def test_mark_index_divergence_damps_score():
    """Big mark/index spread halves the absolute score regardless of direction."""
    base = compute_composite_score(funding_8h_norm_pct=-1.5).score
    damped = compute_composite_score(funding_8h_norm_pct=-1.5, mark_index_spread_pct=0.8).score
    assert abs(damped) < abs(base)


def test_score_clamped_to_range():
    """Pile on every bullish input — score still <= 100."""
    s = compute_composite_score(
        funding_8h_norm_pct=-2.0,
        streak_count=10, streak_direction="neg",
        oi_change_24h_pct=50.0,
        ls_ratio_global=0.2,
        ls_ratio_top=2.0,
        onchain_net_usd=50_000_000,
    )
    assert s.score <= 100
    assert s.score >= 70  # qualitatively very bullish


def test_score_label_thresholds():
    """Boundaries map cleanly to labels."""
    assert compute_composite_score(funding_8h_norm_pct=-2.0).short in ("Bullish", "Strong bull")  # ~ +30
    assert compute_composite_score(funding_8h_norm_pct=2.0).short in ("Bearish", "Strong bear")
