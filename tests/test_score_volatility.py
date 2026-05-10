"""Tests for score_volatility (Round 27)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.score_history import score_volatility


def _samples(scores: list[int]) -> list[tuple[datetime, int]]:
    """Build chronologically-ordered samples 10 min apart."""
    base = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    return [(base + timedelta(minutes=10 * i), s) for i, s in enumerate(scores)]


def test_returns_none_for_too_few_samples():
    assert score_volatility(_samples([50, 50, 50])) is None  # default min=4
    assert score_volatility([]) is None


def test_low_vol_for_constant_score():
    """Truly flat scores → std=0 (the lowest-vol case)."""
    out = score_volatility(_samples([50, 50, 50, 50, 50]))
    assert out == pytest.approx(0.0)


def test_higher_vol_for_oscillating_scores():
    """A score swinging between +20 and +80 has much higher std than steady +50."""
    flat = score_volatility(_samples([50, 50, 50, 50, 50]))
    swinging = score_volatility(_samples([20, 80, 20, 80, 20, 80]))
    assert flat is not None and swinging is not None
    assert swinging > flat
    assert swinging > 25  # roughly the half-range


def test_returns_float():
    out = score_volatility(_samples([10, 20, 30, 40]))
    assert out is not None
    assert isinstance(out, float)


def test_custom_min_samples():
    """Allow shorter histories with explicit min."""
    short = _samples([50, 60, 40])
    assert score_volatility(short) is None  # default 4
    assert score_volatility(short, min_samples=3) is not None


def test_realistic_persistent_regime_has_low_vol():
    """A score gently drifting from +60 to +80 should produce low-ish vol."""
    samples = _samples([60, 65, 68, 72, 75, 78, 80])
    out = score_volatility(samples)
    assert out is not None
    assert out < 10  # tight regime


def test_realistic_unstable_regime_has_high_vol():
    """Score flipping ±50 each tick is unstable."""
    samples = _samples([50, -50, 50, -50, 50, -50])
    out = score_volatility(samples)
    assert out is not None
    assert out > 40
