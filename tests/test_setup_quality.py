"""Tests for classify_setup_quality (Round 34)."""

from __future__ import annotations

import pytest

from funding_screener.signals import SetupQuality, classify_setup_quality


# ---------------- neutral / no-data fallbacks ----------------


def test_neutral_score_returns_dash():
    out = classify_setup_quality(score=10, age_hours=2.0, score_delta_1h=5,
                                 score_stddev_24h=10.0)
    assert out.label == "—"


def test_score_at_threshold_qualifies():
    """+30 is the bullish boundary — a +30 score should classify, not be neutral."""
    out = classify_setup_quality(score=30, age_hours=0.5, score_delta_1h=10,
                                 score_stddev_24h=15.0)
    assert out.label != "—"


def test_none_score_returns_dash():
    out = classify_setup_quality(score=None, age_hours=2.0, score_delta_1h=5,
                                 score_stddev_24h=10.0)
    assert out.label == "—"


# ---------------- noisy short-circuits ----------------


def test_high_sigma_is_noisy_regardless_of_score():
    """σ > 30 trumps directional buckets — don't trust the signal."""
    out = classify_setup_quality(score=80, age_hours=0.5, score_delta_1h=20,
                                 score_stddev_24h=45.0)
    assert out.label == "Noisy"
    assert out.emoji == "⚠️"


def test_sigma_at_threshold_is_not_noisy():
    """σ exactly 30 should still classify by direction (strict > 30 for noisy)."""
    out = classify_setup_quality(score=80, age_hours=0.5, score_delta_1h=20,
                                 score_stddev_24h=30.0)
    assert out.label != "Noisy"


# ---------------- fresh bucket ----------------


def test_fresh_bull_under_one_hour():
    out = classify_setup_quality(score=80, age_hours=0.5, score_delta_1h=10,
                                 score_stddev_24h=10.0)
    assert out.emoji == "🚀"
    assert out.label == "Fresh bull"


def test_fresh_bear_under_one_hour():
    out = classify_setup_quality(score=-80, age_hours=0.3, score_delta_1h=-10,
                                 score_stddev_24h=10.0)
    assert out.emoji == "💥"
    assert out.label == "Fresh bear"


# ---------------- building bucket ----------------


def test_building_bull_when_age_1_to_4h_and_delta_positive():
    out = classify_setup_quality(score=60, age_hours=2.5, score_delta_1h=15,
                                 score_stddev_24h=15.0)
    assert out.emoji == "📈"
    assert out.label == "Building bull"


def test_building_bear_when_age_1_to_4h_and_delta_negative():
    out = classify_setup_quality(score=-60, age_hours=2.5, score_delta_1h=-15,
                                 score_stddev_24h=15.0)
    assert out.emoji == "📉"
    assert out.label == "Building bear"


def test_building_window_with_flat_delta_is_mature():
    """Same age window, but delta == 0 → score has stopped advancing → mature."""
    out = classify_setup_quality(score=60, age_hours=2.5, score_delta_1h=0,
                                 score_stddev_24h=15.0)
    assert out.label == "Mature bull"


# ---------------- mature bucket ----------------


def test_mature_bull_at_4_to_12h():
    out = classify_setup_quality(score=70, age_hours=8.0, score_delta_1h=2,
                                 score_stddev_24h=15.0)
    assert out.label == "Mature bull"
    assert out.emoji == "🎯"


# ---------------- late bucket ----------------


def test_late_bull_over_12h():
    out = classify_setup_quality(score=70, age_hours=18.0, score_delta_1h=0,
                                 score_stddev_24h=15.0)
    assert out.label == "Late bull"
    assert out.emoji == "⏰"


def test_late_bear_over_12h():
    out = classify_setup_quality(score=-50, age_hours=15.0, score_delta_1h=0,
                                 score_stddev_24h=10.0)
    assert out.label == "Late bear"


# ---------------- missing-input fallbacks ----------------


def test_missing_age_falls_back_to_directional():
    """Score qualifies but no age data → directional but not bucketed."""
    out = classify_setup_quality(score=50, age_hours=None, score_delta_1h=5,
                                 score_stddev_24h=10.0)
    assert out.label == "Bullish"
    assert out.emoji == "🟢"


def test_missing_delta_in_building_window_treats_as_mature():
    """No delta info during the building window → can't confirm momentum."""
    out = classify_setup_quality(score=50, age_hours=2.0, score_delta_1h=None,
                                 score_stddev_24h=15.0)
    assert out.label == "Mature bull"


def test_missing_sigma_does_not_force_noisy():
    """No sigma → just skip the noisy check."""
    out = classify_setup_quality(score=70, age_hours=0.5, score_delta_1h=10,
                                 score_stddev_24h=None)
    assert out.label == "Fresh bull"
