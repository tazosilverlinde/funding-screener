"""Tests for the per-loop performance instrumentation."""

from __future__ import annotations

import pytest

from funding_screener.background import DataStore


def test_record_and_read_single_loop():
    s = DataStore()
    s.record_loop_duration("fast", 1.5)
    stats = s.read_loop_stats()
    assert "fast" in stats
    f = stats["fast"]
    assert f["samples"] == 1
    assert f["avg_s"] == pytest.approx(1.5)
    assert f["p50_s"] == pytest.approx(1.5)
    assert f["p95_s"] == pytest.approx(1.5)
    assert f["last_s"] == pytest.approx(1.5)


def test_aggregates_multiple_samples():
    s = DataStore()
    for d in [1.0, 2.0, 3.0, 4.0, 5.0]:
        s.record_loop_duration("slow", d)
    stats = s.read_loop_stats()["slow"]
    assert stats["samples"] == 5
    assert stats["avg_s"] == pytest.approx(3.0)
    assert stats["p50_s"] == pytest.approx(3.0)
    # P95 of 5 samples → index 4 → 5.0
    assert stats["p95_s"] == pytest.approx(5.0)
    assert stats["last_s"] == pytest.approx(5.0)


def test_keeps_only_last_50_samples():
    s = DataStore()
    for i in range(60):
        s.record_loop_duration("test", float(i))
    stats = s.read_loop_stats()["test"]
    assert stats["samples"] == 50
    # Oldest sample retained should be 10 (60 - 50). Avg of [10..59] = 34.5
    assert stats["avg_s"] == pytest.approx(34.5)
    assert stats["last_s"] == pytest.approx(59.0)


def test_separate_loops_independent():
    s = DataStore()
    s.record_loop_duration("a", 1.0)
    s.record_loop_duration("b", 100.0)
    stats = s.read_loop_stats()
    assert stats["a"]["last_s"] == pytest.approx(1.0)
    assert stats["b"]["last_s"] == pytest.approx(100.0)


def test_empty_loop_returns_no_stats_entry():
    s = DataStore()
    assert s.read_loop_stats() == {}


def test_p95_handles_small_samples():
    """With 3 samples the p95 index = int(3*0.95)=2 → highest sample."""
    s = DataStore()
    for d in [1.0, 2.0, 10.0]:
        s.record_loop_duration("x", d)
    assert s.read_loop_stats()["x"]["p95_s"] == pytest.approx(10.0)
