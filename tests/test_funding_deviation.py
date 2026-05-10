"""Tests for compute_funding_deviation (Round 12)."""

from __future__ import annotations

import pytest

from funding_screener.signals import (
    FundingDeviation,
    compute_funding_deviation,
)


# ---------------- happy paths ----------------


def test_normal_rate_in_band():
    """Current rate near the mean → classification 'normal'."""
    history = [0.01, 0.012, 0.008, 0.011, 0.009, 0.010, 0.013, 0.007, 0.011, 0.009, 0.010, 0.012]
    out = compute_funding_deviation(0.011, history)
    assert out is not None
    assert out.classification == "normal"
    assert -1.5 <= out.z_score <= 1.5
    assert "persistent regime" in out.comment


def test_overshoot_z_near_2():
    """Current rate ~2σ above mean → 'overshoot'."""
    history = [0.01] * 12 + [0.02] * 4 + [0.005] * 4
    # mean≈0.011, std modest. Choose current that's ~2σ above.
    import statistics
    mean = statistics.mean(history)
    std = statistics.stdev(history)
    current = mean + 2.0 * std
    out = compute_funding_deviation(current, history)
    assert out is not None
    assert out.classification == "overshoot"
    assert out.z_score >= 1.5
    assert "running hot" in out.comment


def test_extreme_overshoot_above_2_5_sigma():
    history = [0.005, 0.006, 0.005, 0.007, 0.006, 0.005, 0.006, 0.007, 0.006, 0.005, 0.006, 0.007]
    out = compute_funding_deviation(0.05, history)  # way above the mean
    assert out is not None
    assert out.classification == "extreme_overshoot"
    assert out.z_score >= 2.5
    assert "🔥" in out.emoji
    assert "mean-revert" in out.comment


def test_undershoot_negative_z():
    """Current rate ~2σ below mean → 'undershoot'."""
    history = [0.01] * 12 + [0.02] * 4 + [0.005] * 4
    import statistics
    mean = statistics.mean(history)
    std = statistics.stdev(history)
    current = mean - 2.0 * std
    out = compute_funding_deviation(current, history)
    assert out is not None
    assert out.classification == "undershoot"
    assert out.z_score <= -1.5
    assert "running cool" in out.comment


def test_extreme_undershoot_below_minus_2_5_sigma():
    history = [0.005, 0.006, 0.005, 0.007, 0.006, 0.005, 0.006, 0.007, 0.006, 0.005, 0.006, 0.007]
    out = compute_funding_deviation(-0.05, history)
    assert out is not None
    assert out.classification == "extreme_undershoot"
    assert out.z_score <= -2.5
    assert "❄" in out.emoji


# ---------------- guard rails ----------------


def test_returns_none_when_current_is_missing():
    history = [0.01] * 20
    assert compute_funding_deviation(None, history) is None


def test_returns_none_for_too_few_samples():
    """Default min_samples=10 — anything fewer should bail out rather than mislead."""
    history = [0.01, 0.02, 0.03, 0.01, 0.02]
    assert compute_funding_deviation(0.01, history) is None


def test_respects_custom_min_samples():
    history = [0.01, 0.012, 0.008, 0.011, 0.009]
    # With min_samples=5 it should compute now.
    out = compute_funding_deviation(0.010, history, min_samples=5)
    assert out is not None


def test_returns_none_for_empty_history():
    assert compute_funding_deviation(0.01, []) is None


def test_returns_none_when_std_is_zero():
    """Degenerate input — every historical rate identical → divide by zero."""
    history = [0.01] * 20
    assert compute_funding_deviation(0.05, history) is None


# ---------------- output shape ----------------


def test_output_has_all_fields_populated():
    history = [0.01, 0.012, 0.008, 0.011, 0.009, 0.010, 0.013, 0.007, 0.011, 0.009, 0.010, 0.012]
    out = compute_funding_deviation(0.015, history)
    assert isinstance(out, FundingDeviation)
    assert out.current_pct == 0.015
    assert out.mean_pct == pytest.approx(sum(history) / len(history), rel=1e-6)
    assert out.std_pct > 0
    assert out.classification in {
        "extreme_overshoot", "overshoot", "normal", "undershoot", "extreme_undershoot"
    }
    assert out.emoji
    assert out.comment


def test_z_score_sign_matches_current_vs_mean():
    history = [0.01] * 6 + [0.02] * 6
    # Mean is 0.015. Current above → positive z.
    above = compute_funding_deviation(0.025, history)
    below = compute_funding_deviation(0.005, history)
    assert above is not None and below is not None
    assert above.z_score > 0
    assert below.z_score < 0
    # And they should be roughly symmetric for symmetric history.
    assert abs(above.z_score - abs(below.z_score)) < 0.01


def test_handles_negative_funding_history():
    """Most over-the-counter mean-revert setups happen with negative funding."""
    history = [-0.02, -0.025, -0.022, -0.018, -0.024, -0.020, -0.023, -0.019, -0.021, -0.022, -0.020, -0.024]
    # Current rate spikes positive — would be huge z.
    out = compute_funding_deviation(0.05, history)
    assert out is not None
    assert out.z_score > 2.5
    assert out.classification == "extreme_overshoot"
