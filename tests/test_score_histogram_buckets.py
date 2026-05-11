"""Tests for bucket_scores_for_histogram (Round 59)."""

from __future__ import annotations

from funding_screener.signals import (
    SCORE_HISTOGRAM_BUCKETS,
    bucket_scores_for_histogram,
)


# ---------------- bucket assignment ----------------


def test_buckets_in_canonical_order():
    """Bucket order is bear → bull, left-to-right on the chart."""
    out = bucket_scores_for_histogram([])
    labels = [label for (label, _count) in out]
    assert labels == [
        "💥 Strong bear", "🔴 Bearish", "↘ Mild bear", "🟡 Neutral",
        "↗ Mild bull", "🟢 Bullish", "🚀 Strong bull",
    ]


def test_score_in_strong_bear_bucket():
    out = bucket_scores_for_histogram([-90])
    counts = dict(out)
    assert counts["💥 Strong bear"] == 1
    assert sum(counts.values()) == 1


def test_score_in_strong_bull_bucket():
    out = bucket_scores_for_histogram([95])
    counts = dict(out)
    assert counts["🚀 Strong bull"] == 1


def test_score_at_exact_boundary_falls_into_upper_bucket():
    """Half-open [lo, hi): a score of -70 lands in 🔴 Bearish (not Strong bear)."""
    counts = dict(bucket_scores_for_histogram([-70]))
    assert counts["🔴 Bearish"] == 1
    assert counts["💥 Strong bear"] == 0


def test_score_at_lower_boundary_strong_bear():
    counts = dict(bucket_scores_for_histogram([-100]))
    assert counts["💥 Strong bear"] == 1


def test_max_score_lands_in_strong_bull():
    """+100 (top of clamped range) should fall into Strong bull, not off-chart."""
    counts = dict(bucket_scores_for_histogram([100]))
    assert counts["🚀 Strong bull"] == 1


def test_zero_score_in_neutral_bucket():
    counts = dict(bucket_scores_for_histogram([0]))
    assert counts["🟡 Neutral"] == 1


# ---------------- aggregation ----------------


def test_multiple_scores_aggregate():
    scores = [-80, -50, -20, 0, 5, 20, 50, 80]
    counts = dict(bucket_scores_for_histogram(scores))
    assert counts["💥 Strong bear"] == 1
    assert counts["🔴 Bearish"] == 1
    assert counts["↘ Mild bear"] == 1
    assert counts["🟡 Neutral"] == 2  # 0 and 5
    assert counts["↗ Mild bull"] == 1
    assert counts["🟢 Bullish"] == 1
    assert counts["🚀 Strong bull"] == 1


def test_counts_sum_to_input_size():
    scores = [-95, -60, -20, 0, 25, 55, 85, 100]
    out = bucket_scores_for_histogram(scores)
    assert sum(c for _l, c in out) == len(scores)


# ---------------- guards ----------------


def test_empty_input_returns_all_zero_buckets():
    out = bucket_scores_for_histogram([])
    assert len(out) == len(SCORE_HISTOGRAM_BUCKETS)
    assert all(c == 0 for _l, c in out)


def test_none_input_returns_all_zero_buckets():
    out = bucket_scores_for_histogram(None)  # type: ignore[arg-type]
    assert all(c == 0 for _l, c in out)


def test_none_score_in_list_is_skipped():
    """Defensive: a None mixed into the list shouldn't crash or count."""
    counts = dict(bucket_scores_for_histogram([50, None, 80]))  # type: ignore[list-item]
    assert counts["🟢 Bullish"] == 1
    assert counts["🚀 Strong bull"] == 1
    assert sum(counts.values()) == 2


def test_out_of_range_score_dropped_silently():
    """Score > 100 or < -100 shouldn't pollute the top/bottom bucket — drop."""
    counts = dict(bucket_scores_for_histogram([150, -150, 50]))
    # Only the 50 (Bullish) counts.
    assert counts["🟢 Bullish"] == 1
    assert sum(counts.values()) == 1


def test_custom_buckets_override():
    """Caller can pass custom buckets for different chart layouts."""
    custom = [("A", -100, 0), ("B", 0, 100)]
    out = bucket_scores_for_histogram([-50, 50], buckets=custom)
    assert dict(out) == {"A": 1, "B": 1}
