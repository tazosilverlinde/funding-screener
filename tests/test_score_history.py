"""Tests for the score-history ring buffer + top-movers computation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.score_history import (
    score_delta,
    top_movers,
    trim_old,
)


def _ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


# ---------------- trim_old ----------------


def test_trim_keeps_recent_drops_old():
    now = datetime.now(timezone.utc)
    samples = [
        (now - timedelta(hours=30), 10),  # too old → dropped
        (now - timedelta(hours=10), 20),
        (now - timedelta(hours=1), 30),
    ]
    out = trim_old(samples, now)
    scores = [s for _, s in out]
    assert scores == [20, 30]


def test_trim_empty_input():
    assert trim_old([], datetime.now(timezone.utc)) == []


# ---------------- score_delta ----------------


def test_delta_returns_none_when_no_samples():
    assert score_delta([], 60) is None


def test_delta_returns_none_when_single_sample():
    assert score_delta([(_ago(5), 50)], 60) is None


def test_delta_basic_calculation():
    samples = [
        (_ago(70), 30),
        (_ago(60), 40),  # 1h ago
        (_ago(5), 70),    # most recent (current)
    ]
    # current = 70, target ≈ -60min (matches sample at -60)
    assert score_delta(samples, 60) == 30


def test_delta_returns_none_when_target_too_far():
    """Looking back 1h, but oldest sample is 5 min ago — drift > 50% of 60 → None."""
    samples = [
        (_ago(5), 30),
        (_ago(0), 40),
    ]
    assert score_delta(samples, 60) is None


def test_delta_picks_closest_sample():
    samples = [
        (_ago(120), 10),   # 2h ago
        (_ago(58), 40),    # ~1h ago — closest to target
        (_ago(0), 75),     # current
    ]
    # current 75 - sample_closest_to_60min(40) = 35
    assert score_delta(samples, 60) == 35


# ---------------- top_movers ----------------


def test_top_movers_separates_risers_and_fallers():
    histories = {
        ("BTC", "USDT"): [(_ago(60), 30), (_ago(0), 70)],   # +40 riser
        ("ETH", "USDT"): [(_ago(60), 20), (_ago(0), 25)],   # +5 small riser
        ("PEPE", "USDT"): [(_ago(60), 50), (_ago(0), 10)],  # -40 faller
        ("DOGE", "USDT"): [(_ago(60), 0), (_ago(0), -30)],  # -30 faller
    }
    risers, fallers = top_movers(histories, minutes_ago=60, limit=5)
    assert [r["base_asset"] for r in risers] == ["BTC", "ETH"]
    assert risers[0]["delta"] == 40
    assert [f["base_asset"] for f in fallers] == ["PEPE", "DOGE"]
    assert fallers[0]["delta"] == -40


def test_top_movers_skips_zero_delta():
    histories = {
        ("BTC", "USDT"): [(_ago(60), 50), (_ago(0), 50)],  # no change
    }
    risers, fallers = top_movers(histories)
    assert risers == [] and fallers == []


def test_top_movers_respects_limit():
    histories = {
        (f"T{i}", "USDT"): [(_ago(60), 0), (_ago(0), i + 1)]
        for i in range(20)
    }
    risers, _ = top_movers(histories, minutes_ago=60, limit=3)
    assert len(risers) == 3
    # Top-3 by delta = symbols T17, T18, T19 (highest current scores).
    assert [r["base_asset"] for r in risers] == ["T19", "T18", "T17"]


def test_top_movers_empty_histories():
    assert top_movers({}) == ([], [])
