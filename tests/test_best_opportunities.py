"""Tests for pick_best_opportunities (Round 35)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.highlights import pick_best_opportunities


@dataclass
class _FakeRow:
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = "BTCUSDT"
    mexc_symbol: Optional[str] = None
    composite_score: Optional[int] = None
    composite_short: Optional[str] = None
    setup_quality_label: Optional[str] = None
    binance_rate_8h_norm_percent: Optional[float] = None
    mexc_rate_8h_norm_percent: Optional[float] = None
    signal_age_hours: Optional[float] = None


def test_picks_fresh_and_building_excludes_mature():
    rows = [
        _FakeRow(base_asset="A", composite_score=80, setup_quality_label="🚀 Fresh bull"),
        _FakeRow(base_asset="B", composite_score=70, setup_quality_label="🎯 Mature bull"),
        _FakeRow(base_asset="C", composite_score=60, setup_quality_label="📈 Building bull"),
    ]
    picks = pick_best_opportunities(rows, top_n=5)
    bases = [p["base"] for p in picks]
    assert "A" in bases
    assert "C" in bases
    assert "B" not in bases  # Mature excluded


def test_excludes_noisy():
    rows = [_FakeRow(base_asset="X", composite_score=80, setup_quality_label="⚠️ Noisy")]
    assert pick_best_opportunities(rows) == []


def test_excludes_late():
    rows = [_FakeRow(base_asset="X", composite_score=80, setup_quality_label="⏰ Late bull")]
    assert pick_best_opportunities(rows) == []


def test_sorted_by_absolute_score_descending():
    rows = [
        _FakeRow(base_asset="LOW", composite_score=35, setup_quality_label="🚀 Fresh bull"),
        _FakeRow(base_asset="HIGH", composite_score=85, setup_quality_label="📈 Building bull"),
        _FakeRow(base_asset="MID", composite_score=55, setup_quality_label="🚀 Fresh bull"),
    ]
    picks = pick_best_opportunities(rows, top_n=3)
    assert [p["base"] for p in picks] == ["HIGH", "MID", "LOW"]


def test_negative_scores_sorted_by_magnitude():
    rows = [
        _FakeRow(base_asset="A", composite_score=-80, setup_quality_label="💥 Fresh bear"),
        _FakeRow(base_asset="B", composite_score=40, setup_quality_label="🚀 Fresh bull"),
        _FakeRow(base_asset="C", composite_score=-50, setup_quality_label="📉 Building bear"),
    ]
    picks = pick_best_opportunities(rows, top_n=3)
    assert [p["base"] for p in picks] == ["A", "C", "B"]  # by |score|


def test_top_n_caps_results():
    rows = [
        _FakeRow(base_asset=f"X{i}", composite_score=80 - i,
                 setup_quality_label="🚀 Fresh bull")
        for i in range(10)
    ]
    assert len(pick_best_opportunities(rows, top_n=3)) == 3
    assert len(pick_best_opportunities(rows, top_n=5)) == 5


def test_returns_empty_for_empty_or_none_input():
    assert pick_best_opportunities([]) == []
    assert pick_best_opportunities(None) == []  # type: ignore[arg-type]


def test_skips_rows_missing_score_or_label():
    rows = [
        _FakeRow(base_asset="A", composite_score=None, setup_quality_label="🚀 Fresh bull"),
        _FakeRow(base_asset="B", composite_score=70, setup_quality_label=None),
        _FakeRow(base_asset="C", composite_score=60, setup_quality_label="📈 Building bull"),
    ]
    picks = pick_best_opportunities(rows, top_n=5)
    assert [p["base"] for p in picks] == ["C"]


def test_funding_string_falls_back_through_sides():
    rows = [
        _FakeRow(
            base_asset="A", binance_symbol=None, mexc_symbol="A_USDT",
            composite_score=70, setup_quality_label="🚀 Fresh bull",
            binance_rate_8h_norm_percent=None,
            mexc_rate_8h_norm_percent=-1.5,
        ),
    ]
    picks = pick_best_opportunities(rows)
    assert picks[0]["funding"] == "-1.5000%/8h"


def test_age_string_minutes_for_under_1h():
    rows = [
        _FakeRow(base_asset="A", composite_score=70, setup_quality_label="🚀 Fresh bull",
                 signal_age_hours=0.4),  # 24 min
    ]
    picks = pick_best_opportunities(rows)
    assert picks[0]["age"] == "24m"


def test_age_string_hours_for_over_1h():
    rows = [
        _FakeRow(base_asset="A", composite_score=70, setup_quality_label="📈 Building bull",
                 signal_age_hours=2.5),
    ]
    picks = pick_best_opportunities(rows)
    assert picks[0]["age"] == "2.5h"


def test_age_string_dash_when_none():
    rows = [
        _FakeRow(base_asset="A", composite_score=70, setup_quality_label="🚀 Fresh bull",
                 signal_age_hours=None),
    ]
    picks = pick_best_opportunities(rows)
    assert picks[0]["age"] == "—"
