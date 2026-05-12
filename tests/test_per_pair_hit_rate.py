"""Tests for per-pair hit-rate + signal decay curve (Round 72)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.analytics import (
    compute_decay_curve,
    compute_per_pair_hit_rate,
)


def _samples(pairs: list[tuple[int, int]]) -> list[tuple[datetime, int]]:
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    rows = [(now - timedelta(minutes=mins_ago), score) for mins_ago, score in pairs]
    rows.sort(key=lambda x: x[0])
    return rows


# ---------------- compute_per_pair_hit_rate ----------------


def test_empty_input_returns_empty():
    assert compute_per_pair_hit_rate({}) == []


def test_single_pair_with_one_cross():
    pair = _samples([(70, 50), (60, 75), (0, 80)])
    out = compute_per_pair_hit_rate(
        {("BTC", "USDT"): pair},
        threshold=70, follow_up_hours=1.0, min_crosses=1,
    )
    assert len(out) == 1
    assert out[0]["label"] == "BTC/USDT"
    assert out[0]["n_crosses"] == 1
    assert out[0]["n_sustained"] == 1
    assert out[0]["sustain_rate"] == 1.0


def test_min_crosses_filter():
    """Pairs with too few crossings should be dropped."""
    one_cross = _samples([(70, 50), (60, 75)])
    no_cross = _samples([(30, 10), (20, 20)])
    out = compute_per_pair_hit_rate(
        {"a": one_cross, "b": no_cross},
        threshold=70, min_crosses=2,
    )
    assert out == []


def test_sorted_by_sustain_rate_desc():
    """Best-sustaining pair first."""
    perfect = _samples([(70, 50), (60, 80), (0, 85)])  # crossed, sustained
    faded = _samples([(70, 50), (60, 80), (0, 40)])    # crossed, faded
    out = compute_per_pair_hit_rate(
        {"perfect": perfect, "faded": faded},
        threshold=70, follow_up_hours=1.0,
    )
    assert [r["key"] for r in out] == ["perfect", "faded"]
    assert out[0]["sustain_rate"] == 1.0
    assert out[1]["sustain_rate"] == 0.0


def test_tiebreaker_by_n_crosses_when_rates_equal():
    """Within same rate, more crossings come first (more confidence)."""
    # Build a pair with 2 crossings, each with a sample 60 min later inside
    # the threshold region (sustained). Times measured in min-ago:
    #   first cross at 180min ago; follow-up at 120min ago = +75 (sustained)
    #   second cross at 90min ago; follow-up at 30min ago = +80 (sustained)
    many = _samples([
        (200, 50), (180, 80), (120, 75), (100, 40),
        (90, 75), (30, 80), (0, 85),
    ])
    # And a pair with 1 crossing also sustained.
    few = _samples([(70, 50), (60, 80), (0, 85)])
    out = compute_per_pair_hit_rate(
        {"few": few, "many": many},
        threshold=70, follow_up_hours=1.0, tolerance_minutes=15,
    )
    # Both should have rate 1.0; "many" sorts first by tiebreaker (n_crosses).
    rates = {r["key"]: r["sustain_rate"] for r in out}
    assert rates["many"] == 1.0
    assert rates["few"] == 1.0
    assert out[0]["n_crosses"] > out[1]["n_crosses"]
    assert out[0]["key"] == "many"


def test_label_for_tuple_key():
    pair = _samples([(70, 50), (60, 80)])
    out = compute_per_pair_hit_rate({("ETH", "USDT"): pair}, threshold=70)
    assert out[0]["label"] == "ETH/USDT"


def test_label_for_string_key():
    pair = _samples([(70, 50), (60, 80)])
    out = compute_per_pair_hit_rate({"BTCUSDT": pair}, threshold=70)
    assert out[0]["label"] == "BTCUSDT"


def test_avg_score_metrics_populated():
    pair = _samples([(70, 50), (60, 75), (0, 60)])  # crossed +75, faded to +60
    out = compute_per_pair_hit_rate(
        {"x": pair}, threshold=70, follow_up_hours=1.0,
    )
    assert out[0]["avg_score_at_cross"] == pytest.approx(75.0)
    assert out[0]["avg_score_after"] == pytest.approx(60.0)
    assert out[0]["avg_score_delta"] == pytest.approx(-15.0)


def test_pair_with_no_crossings_excluded():
    pair = _samples([(30, 10), (20, 20)])
    out = compute_per_pair_hit_rate({"x": pair}, threshold=70)
    assert out == []


def test_short_pair_skipped():
    """A pair with <2 samples can't have a crossing."""
    out = compute_per_pair_hit_rate({"x": _samples([(10, 80)])}, threshold=70)
    assert out == []


def test_bearish_threshold_path():
    pair = _samples([(70, 0), (60, -75), (0, -80)])
    out = compute_per_pair_hit_rate({"x": pair}, threshold=-70, follow_up_hours=1.0)
    assert out[0]["n_crosses"] == 1
    assert out[0]["n_sustained"] == 1


# ---------------- compute_decay_curve ----------------


def test_decay_curve_default_windows():
    out = compute_decay_curve({}, threshold=70)
    # Default windows: 0.5, 1, 2, 4, 12 hours.
    assert [r["follow_up_hours"] for r in out] == [0.5, 1.0, 2.0, 4.0, 12.0]


def test_decay_curve_custom_windows():
    out = compute_decay_curve({}, threshold=70, follow_up_windows_hours=[1.0, 6.0])
    assert [r["follow_up_hours"] for r in out] == [1.0, 6.0]


def test_decay_curve_sustains_at_short_window_fades_at_long():
    """Score crossed at -90min, sustained at +75 for ~30min then dropped to +30."""
    pair = _samples([
        (90, 50),    # before cross
        (80, 80),    # cross point (samples[1])
        (60, 75),    # +20min later still sustained
        (0, 30),     # +80min later faded
    ])
    out = compute_decay_curve(
        {"x": pair},
        threshold=70,
        follow_up_windows_hours=[0.3, 1.3],   # 18min, 78min
        tolerance_minutes=20,
    )
    # 0.3h window finds a sample around +20min → +75 → sustained.
    # 1.3h window finds a sample around +80min → +30 → faded.
    rates = {r["follow_up_hours"]: r["sustain_rate"] for r in out}
    assert rates[0.3] == 1.0
    assert rates[1.3] == 0.0


def test_decay_curve_handles_empty_input():
    out = compute_decay_curve({}, threshold=70)
    for row in out:
        assert row["n_crosses"] == 0
        assert row["sustain_rate"] is None
