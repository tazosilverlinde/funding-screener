"""Tests for liquidation-skew contribution to compute_composite_score (Round 17)."""

from __future__ import annotations

import pytest

from funding_screener.signals import compute_composite_score


def test_no_liq_inputs_score_unchanged():
    """Backwards compatibility — old calls without liq kwargs still work."""
    score = compute_composite_score(funding_8h_norm_pct=-1.0)
    assert score.score > 0  # negative funding → positive score
    # No liq breakdown line
    assert not any("liq " in line for line in score.breakdown)


def test_short_squeeze_adds_positive_contribution():
    """Heavy short liquidations + neutral funding → bullish bias from liq alone."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=1_000_000,
        liq_short_usd_24h=49_000_000,  # total $50M, skew strongly +
    )
    assert score.score > 5  # at least the liq contribution shows up
    assert any("shorts squeezed" in line for line in score.breakdown)


def test_long_cascade_adds_negative_contribution():
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=49_000_000,
        liq_short_usd_24h=1_000_000,
    )
    assert score.score < -5
    assert any("longs cascaded" in line for line in score.breakdown)


def test_balanced_liq_no_contribution():
    """Same long and short → skew = 0 → no contribution."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=25_000_000,
        liq_short_usd_24h=25_000_000,
    )
    assert score.score == 0
    assert not any("liq " in line for line in score.breakdown)


def test_below_noise_floor_no_contribution():
    """Tiny totals (<$1M) shouldn't be treated as signal."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=10_000,
        liq_short_usd_24h=200_000,
    )
    assert score.score == 0
    assert not any("liq " in line for line in score.breakdown)


def test_magnitude_factor_dampens_small_totals():
    """Same skew (+1.0) but $5M total vs $50M total → smaller contribution."""
    small = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=0,
        liq_short_usd_24h=5_000_000,  # total $5M, full short skew
    )
    big = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=0,
        liq_short_usd_24h=50_000_000,  # total $50M, same skew
    )
    # Big should contribute the full +10; small only ~10% of that.
    big_liq = next(int(round(float(line.split("→")[-1].replace("σ", "").strip())))
                   for line in big.breakdown if "liq " in line)
    assert big_liq == 10
    # Small should be present but tiny (sub-1pt items are filtered out, so it
    # may not even register — that's the magnitude floor working).
    assert big.score > small.score


def test_caps_at_plus_or_minus_10():
    """No matter how big the liq, the contribution is capped at ±10."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=0,
        liq_short_usd_24h=10_000_000_000,  # $10B
    )
    liq_lines = [line for line in score.breakdown if "liq " in line]
    assert len(liq_lines) == 1
    delta = float(liq_lines[0].split("→")[-1].strip())
    assert delta == 10.0


def test_liq_combines_with_other_signals_correctly():
    """Strong liq + strong funding + streak compound, but stay clamped at ±100."""
    score = compute_composite_score(
        funding_8h_norm_pct=-2.0,           # +30 from funding
        streak_count=5, streak_direction="neg",  # +15 from streak
        liq_long_usd_24h=0,
        liq_short_usd_24h=50_000_000,        # +10 from liq
    )
    # Total before clamp: 30 + 15 + 10 = 55. Score should be 55, not clamped.
    assert 50 <= score.score <= 60


def test_liq_score_dampened_by_mark_index_risk():
    """Mark/Idx > 0.5% multiplies total by 0.5 — including liq contribution."""
    no_risk = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=0,
        liq_short_usd_24h=50_000_000,
    )
    with_risk = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=0,
        liq_short_usd_24h=50_000_000,
        mark_index_spread_pct=1.0,  # > 0.5% triggers ×0.5 damp
    )
    assert with_risk.score == round(no_risk.score / 2)


def test_only_long_zero_short_treated_as_skew_minus1():
    """Pure long-side cascade — full bearish contribution."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=50_000_000,
        liq_short_usd_24h=0.0,
    )
    assert score.score == -10


def test_one_side_none_skips_contribution():
    """If only one side known (e.g. partial data), don't compute skew."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=50_000_000,
        liq_short_usd_24h=None,
    )
    # Both required → no liq contribution.
    assert score.score == 0


def test_breakdown_includes_skew_value_for_debugging():
    """The breakdown line should mention the actual skew so it's verifiable."""
    score = compute_composite_score(
        funding_8h_norm_pct=0.0,
        liq_long_usd_24h=10_000_000,
        liq_short_usd_24h=40_000_000,  # skew ~ +0.6
    )
    line = next(l for l in score.breakdown if "liq " in l)
    assert "skew +0.6" in line.lower() or "skew +0.60" in line.lower()
