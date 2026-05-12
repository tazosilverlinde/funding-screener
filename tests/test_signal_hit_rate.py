"""Tests for signal hit-rate analytics (Round 71)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.analytics import (
    compute_signal_hit_rate,
    find_threshold_crossings,
)


def _samples(pairs: list[tuple[int, int]]) -> list[tuple[datetime, int]]:
    """Build chronological samples from a list of (minutes_ago, score).

    minutes_ago=0 means right now; higher = older. We reverse so the list
    is oldest-first as the analytics layer expects.
    """
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    rows = [(now - timedelta(minutes=mins_ago), score) for mins_ago, score in pairs]
    rows.sort(key=lambda x: x[0])
    return rows


# ---------------- find_threshold_crossings ----------------


def test_no_crossings_when_all_below():
    samples = _samples([(60, 10), (50, 20), (40, 30), (30, 25), (20, 15), (10, 5)])
    assert find_threshold_crossings(samples, threshold=70) == []


def test_single_upward_crossing():
    samples = _samples([(60, 10), (50, 65), (40, 72), (30, 85)])
    # Crossings into [70, ...] region:
    # i=1: 10 → 65, no (65 < 70)
    # i=2: 65 → 72, YES (crossing into >= 70)
    # i=3: 72 → 85, no (already in region)
    out = find_threshold_crossings(samples, threshold=70)
    assert out == [2]


def test_multiple_upward_crossings():
    """Score dips below then re-enters → counts as a fresh crossing."""
    samples = _samples([(60, 65), (50, 75), (40, 50), (30, 80)])
    # i=1: 65 → 75 → crosses
    # i=2: 75 → 50 → no (going down)
    # i=3: 50 → 80 → crosses again
    out = find_threshold_crossings(samples, threshold=70)
    assert out == [1, 3]


def test_downward_crossing_for_negative_threshold():
    samples = _samples([(40, 0), (30, -50), (20, -75)])
    # threshold = -70: crossing when going below -70.
    # i=1: 0 → -50, no (-50 > -70)
    # i=2: -50 → -75, YES
    out = find_threshold_crossings(samples, threshold=-70)
    assert out == [2]


def test_at_exact_threshold_counts_as_inside():
    """Half-open semantics: >= threshold is INSIDE."""
    samples = _samples([(30, 69), (20, 70)])
    assert find_threshold_crossings(samples, threshold=70) == [1]


def test_empty_or_single_sample_returns_empty():
    assert find_threshold_crossings([], threshold=70) == []
    assert find_threshold_crossings(_samples([(10, 80)]), threshold=70) == []


# ---------------- compute_signal_hit_rate ----------------


def test_empty_input_returns_zero_structure():
    out = compute_signal_hit_rate({})
    assert out["n_crosses"] == 0
    assert out["sustain_rate"] is None
    assert out["n_sustained"] == 0


def test_none_input_returns_zero_structure():
    out = compute_signal_hit_rate(None)  # type: ignore[arg-type]
    assert out["n_crosses"] == 0


def test_single_pair_single_cross_sustained():
    # Crossed +70 at -60min, still +75 at -0min (1h later).
    pair_samples = _samples([(70, 50), (60, 75), (10, 75), (0, 80)])
    out = compute_signal_hit_rate(
        {("BTC", "USDT"): pair_samples},
        threshold=70, follow_up_hours=1.0, tolerance_minutes=15.0,
    )
    assert out["n_crosses"] == 1
    assert out["n_sustained"] == 1
    assert out["sustain_rate"] == 1.0


def test_single_cross_faded():
    # Crossed +70 at -60min, faded to +40 at -0min.
    pair_samples = _samples([(70, 50), (60, 75), (10, 40), (0, 40)])
    out = compute_signal_hit_rate(
        {("X", "USDT"): pair_samples},
        threshold=70, follow_up_hours=1.0, tolerance_minutes=15.0,
    )
    assert out["n_crosses"] == 1
    assert out["n_sustained"] == 0
    assert out["sustain_rate"] == 0.0


def test_multi_pair_aggregates_correctly():
    a = _samples([(70, 30), (60, 80), (0, 85)])  # crossed and sustained
    b = _samples([(70, 30), (60, 80), (0, 40)])  # crossed and faded
    out = compute_signal_hit_rate(
        {("A", "USDT"): a, ("B", "USDT"): b},
        threshold=70, follow_up_hours=1.0,
    )
    assert out["n_crosses"] == 2
    assert out["n_sustained"] == 1
    assert out["sustain_rate"] == 0.5


def test_sustain_threshold_relaxed_below_cross_threshold():
    """User can ask 'sustained at +50 after crossing +70?'"""
    pair = _samples([(70, 50), (60, 75), (0, 55)])  # crossed +70, now +55
    # Default: sustain_threshold = threshold (+70). 55 < 70 → not sustained.
    out_strict = compute_signal_hit_rate({"x": pair}, threshold=70)
    assert out_strict["n_sustained"] == 0
    # Relaxed: sustain_threshold = +50. 55 >= 50 → sustained.
    out_relaxed = compute_signal_hit_rate(
        {"x": pair}, threshold=70, sustain_threshold=50,
    )
    assert out_relaxed["n_sustained"] == 1


def test_bearish_threshold_path():
    pair = _samples([(70, 0), (60, -75), (0, -80)])
    out = compute_signal_hit_rate(
        {("X", "USDT"): pair}, threshold=-70, follow_up_hours=1.0,
    )
    assert out["n_crosses"] == 1
    assert out["n_sustained"] == 1


def test_missing_follow_up_counted_separately():
    """When no sample is within tolerance of target follow-up time, the
    crossing IS counted but flagged as missing.
    """
    # Cross at -90min; want follow-up at -30min. No sample near -30 (the
    # closest is -90 and -0).
    pair = _samples([(120, 30), (90, 80), (0, 85)])
    out = compute_signal_hit_rate(
        {"x": pair}, threshold=70, follow_up_hours=1.0, tolerance_minutes=10.0,
    )
    assert out["n_crosses"] == 1
    assert out["n_followup_missing"] == 1
    # n_sustained doesn't increment when follow-up is missing.
    assert out["n_sustained"] == 0


def test_avg_score_at_cross():
    a = _samples([(30, 50), (20, 75)])  # cross at +75
    b = _samples([(30, 50), (20, 85)])  # cross at +85
    out = compute_signal_hit_rate(
        {"a": a, "b": b}, threshold=70, follow_up_hours=0.5,
    )
    assert out["n_crosses"] == 2
    assert out["avg_score_at_cross"] == pytest.approx(80.0)


def test_no_crossings_yields_no_rate():
    pair = _samples([(30, 10), (20, 20), (10, 30)])
    out = compute_signal_hit_rate({"x": pair}, threshold=70)
    assert out["n_crosses"] == 0
    assert out["sustain_rate"] is None


def test_pair_with_too_few_samples_skipped():
    """A pair with <2 samples can't have a crossing — silently skipped."""
    out = compute_signal_hit_rate(
        {"x": _samples([(10, 80)])},
        threshold=70,
    )
    assert out["n_crosses"] == 0
