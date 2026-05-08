"""Tests for sector_aggregates — sector rotation summary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from funding_screener.sectors import sector_aggregates


@dataclass
class _FakeRow:
    base_asset: str
    composite_score: Optional[int]
    sector: Optional[str] = None


def test_empty_input_returns_empty():
    assert sector_aggregates([]) == []


def test_single_sector_single_token():
    out = sector_aggregates([_FakeRow("BTC", 50)])
    assert len(out) == 1
    assert out[0]["sector"] == "layer1"
    assert out[0]["avg_score"] == 50.0
    assert out[0]["bullish_count"] == 1
    assert out[0]["bearish_count"] == 0


def test_aggregates_compute_correctly():
    rows = [
        _FakeRow("BTC", 60),
        _FakeRow("ETH", 40),
        _FakeRow("SOL", -20),
    ]
    out = sector_aggregates(rows)
    assert len(out) == 1
    layer1 = out[0]
    assert layer1["sector"] == "layer1"
    assert layer1["row_count"] == 3
    # Avg = (60 + 40 + (-20)) / 3 = 80/3 ≈ 26.67 → 26.7
    assert layer1["avg_score"] == pytest.approx(26.7, abs=0.1)
    # Median of sorted [-20, 40, 60] = 40
    assert layer1["median_score"] == 40
    assert layer1["bullish_count"] == 2  # BTC + ETH at >=30
    assert layer1["bearish_count"] == 0


def test_multiple_sectors_sorted_by_avg_desc():
    rows = [
        _FakeRow("BTC", 50),       # layer1
        _FakeRow("PEPE", -60),     # meme
        _FakeRow("UNI", 20),       # defi
    ]
    out = sector_aggregates(rows)
    sectors_in_order = [d["sector"] for d in out]
    # layer1 (50) > defi (20) > meme (-60)
    assert sectors_in_order == ["layer1", "defi", "meme"]


def test_skips_rows_without_score():
    rows = [
        _FakeRow("BTC", None),     # no score → skipped
        _FakeRow("ETH", 30),
    ]
    out = sector_aggregates(rows)
    assert out[0]["row_count"] == 1


def test_skips_rows_with_unknown_sector():
    rows = [
        _FakeRow("WHATEVERTOKEN", 30),  # not in any sector
    ]
    out = sector_aggregates(rows)
    assert out == []


def test_explicit_sector_field_takes_precedence():
    """If the row already carries `.sector`, use it instead of looking up."""
    rows = [
        _FakeRow("BTC", 50, sector="custom_bucket"),
    ]
    out = sector_aggregates(rows)
    assert out[0]["sector"] == "custom_bucket"


def test_sample_symbols_picks_highest_abs_score():
    rows = [
        _FakeRow("PEPE", -80),
        _FakeRow("DOGE", -10),
        _FakeRow("WIF", -50),
        _FakeRow("SHIB", -30),
    ]
    out = sector_aggregates(rows)
    samples = out[0]["sample_symbols"].split(", ")
    # Top-3 by |score|: PEPE(80), WIF(50), SHIB(30)
    assert "PEPE" in samples
    assert "WIF" in samples
    assert len(samples) == 3
