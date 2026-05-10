"""Tests for signal_age_hours (Round 28)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.score_history import signal_age_hours


def _samples(scores_then_minutes_ago: list[tuple[int, int]]) -> list[tuple[datetime, int]]:
    """Build chronologically-ordered samples.

    Input is list of (score, minutes_ago_from_now). Newest sample is the one
    with the smallest minutes_ago. Returned list is sorted oldest → newest.
    """
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    rows = [(now - timedelta(minutes=mins_ago), score)
            for (score, mins_ago) in scores_then_minutes_ago]
    rows.sort(key=lambda x: x[0])
    return rows


def test_returns_none_for_empty_or_short_history():
    assert signal_age_hours([]) is None
    assert signal_age_hours(_samples([(50, 0)])) is None


def test_returns_none_when_latest_below_threshold():
    """Most recent score is +20, threshold is +30 → no current bullish signal."""
    samples = _samples([(50, 30), (40, 20), (20, 10), (15, 0)])
    assert signal_age_hours(samples, threshold=30) is None


def test_returns_age_when_latest_above_threshold():
    """Score crossed +30 about 60 min ago and stayed above."""
    samples = _samples([(20, 90), (35, 60), (45, 30), (50, 0)])
    out = signal_age_hours(samples, threshold=30)
    assert out is not None
    assert 0.5 <= out <= 1.5  # ≈ 1 hour


def test_negative_threshold_for_bearish_signals():
    """Latest score is -50 and was -40 30min ago, -10 60min ago."""
    samples = _samples([(20, 90), (-10, 60), (-40, 30), (-50, 0)])
    out = signal_age_hours(samples, threshold=-30)
    assert out is not None
    assert 0.5 <= out <= 1.0


def test_negative_threshold_returns_none_when_above():
    samples = _samples([(20, 90), (-10, 60), (-40, 30), (10, 0)])
    assert signal_age_hours(samples, threshold=-30) is None


def test_age_uses_most_recent_transition():
    """Bullish 4h ago, dropped, re-entered 1h ago — actionable age = 1h."""
    samples = _samples([
        (50, 240),   # bullish then
        (60, 200),
        (10, 120),   # dropped to neutral
        (15, 80),
        (40, 60),    # re-entered bullish
        (50, 30),
        (55, 0),
    ])
    out = signal_age_hours(samples, threshold=30)
    assert out is not None
    assert 0.8 <= out <= 1.2  # ≈ 1 hour, not 4


def test_age_full_window_when_always_in_region():
    """If every sample is bullish, age == full history span."""
    samples = _samples([(50, 240), (52, 180), (60, 120), (55, 60), (58, 0)])
    out = signal_age_hours(samples, threshold=30)
    assert out is not None
    assert 3.5 <= out <= 4.5  # ≈ 4 hours = full window


def test_age_at_exact_transition_is_zero():
    """Latest sample IS the first in-region observation → age is 0h.

    Anchoring to the in-region sample (rather than its predecessor) gives
    "we just observed the signal" — useful for traders watching for fresh
    setups. A 0h age tells the user "this just lit up; check it now."
    """
    samples = _samples([(20, 30), (35, 0)])  # crossed at the latest sample
    out = signal_age_hours(samples, threshold=30)
    assert out is not None
    assert out == pytest.approx(0.0)


def test_threshold_at_zero_treated_as_positive():
    """Default behavior with threshold=0 → bullish region is score >= 0."""
    samples = _samples([(-10, 60), (5, 30), (10, 0)])
    out = signal_age_hours(samples, threshold=0)
    assert out is not None
    assert out > 0


def test_age_never_negative():
    """Sanity: age is always ≥ 0 even for edge timing cases."""
    samples = _samples([(35, 5), (40, 0)])  # always above
    out = signal_age_hours(samples, threshold=30)
    assert out is not None and out >= 0
