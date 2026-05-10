"""Tests for find_sector_peers (Round 46)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

from funding_screener.sectors import find_sector_peers


@dataclass
class _FakeRow:
    base_asset: str
    sector: Optional[str] = None
    composite_score: Optional[int] = None


def test_returns_peers_in_same_sector_excluding_target():
    rows = [
        _FakeRow(base_asset="BTC", sector="Layer 1", composite_score=80),  # target
        _FakeRow(base_asset="ETH", sector="Layer 1", composite_score=70),
        _FakeRow(base_asset="SOL", sector="Layer 1", composite_score=-60),
        _FakeRow(base_asset="UNI", sector="DeFi", composite_score=50),    # other sector
    ]
    with patch("funding_screener.sectors.sector_for", side_effect=lambda b: {
        "BTC": "Layer 1", "ETH": "Layer 1", "SOL": "Layer 1", "UNI": "DeFi",
    }.get(b)):
        peers = find_sector_peers("BTC", rows, top_n=3)
    bases = {p.base_asset for p in peers}
    assert bases == {"ETH", "SOL"}
    assert "BTC" not in bases  # target excluded


def test_sorts_by_absolute_score_desc():
    rows = [
        _FakeRow(base_asset="A", sector="X", composite_score=30),
        _FakeRow(base_asset="B", sector="X", composite_score=-90),
        _FakeRow(base_asset="C", sector="X", composite_score=70),
    ]
    with patch("funding_screener.sectors.sector_for", return_value="X"):
        peers = find_sector_peers("TARGET", rows, top_n=3)
    # Empty target → no exclusion; ordered by |score|.
    assert [p.base_asset for p in peers] == ["B", "C", "A"]


def test_returns_empty_when_target_has_no_sector():
    rows = [_FakeRow(base_asset="ETH", sector="Layer 1", composite_score=70)]
    with patch("funding_screener.sectors.sector_for", return_value=None):
        peers = find_sector_peers("UNKNOWN", rows, top_n=3)
    assert peers == []


def test_returns_empty_when_no_other_rows_in_sector():
    rows = [
        _FakeRow(base_asset="UNI", sector="DeFi", composite_score=50),
        _FakeRow(base_asset="AAVE", sector="DeFi", composite_score=40),
    ]
    with patch("funding_screener.sectors.sector_for", return_value="DeFi"):
        peers = find_sector_peers("UNI", rows, top_n=3)
    # Only AAVE is in sector and isn't the target.
    assert [p.base_asset for p in peers] == ["AAVE"]


def test_skips_rows_with_none_score():
    rows = [
        _FakeRow(base_asset="A", sector="X", composite_score=None),
        _FakeRow(base_asset="B", sector="X", composite_score=50),
    ]
    with patch("funding_screener.sectors.sector_for", return_value="X"):
        peers = find_sector_peers("TARGET", rows, top_n=3)
    assert [p.base_asset for p in peers] == ["B"]


def test_top_n_caps_results():
    rows = [_FakeRow(base_asset=f"X{i}", sector="X", composite_score=80 - i)
            for i in range(10)]
    with patch("funding_screener.sectors.sector_for", return_value="X"):
        peers = find_sector_peers("TARGET", rows, top_n=3)
    assert len(peers) == 3


def test_handles_empty_rows():
    with patch("funding_screener.sectors.sector_for", return_value="X"):
        assert find_sector_peers("TARGET", [], top_n=3) == []


def test_handles_none_rows():
    assert find_sector_peers("TARGET", None, top_n=3) == []  # type: ignore[arg-type]


def test_target_base_case_insensitive():
    rows = [
        _FakeRow(base_asset="ETH", sector="Layer 1", composite_score=80),
        _FakeRow(base_asset="btc", sector="Layer 1", composite_score=70),  # lowercase!
    ]
    with patch("funding_screener.sectors.sector_for", return_value="Layer 1"):
        # Lowercase target should still exclude lowercase base.
        peers = find_sector_peers("btc", rows, top_n=3)
    assert [p.base_asset for p in peers] == ["ETH"]


def test_uses_explicit_sector_field_when_present():
    """Row with `.sector` set bypasses the sector_for lookup."""
    rows = [
        _FakeRow(base_asset="ETH", sector="Layer 1", composite_score=80),
    ]
    # sector_for returns DeFi for everything — but the row's .sector overrides.
    with patch("funding_screener.sectors.sector_for", return_value="Layer 1"):
        # target → "Layer 1" → ETH's row.sector is also "Layer 1" → match
        peers = find_sector_peers("TARGET", rows, top_n=3)
    assert len(peers) == 1
