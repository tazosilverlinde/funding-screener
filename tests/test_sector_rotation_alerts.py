"""Tests for evaluate_sector_rotation_alerts (Round 42)."""

from __future__ import annotations

from funding_screener.notifications import evaluate_sector_rotation_alerts


def _sector(
    name: str = "DeFi",
    avg: float = 0.0,
    rows: int = 5,
    bull: int = 0,
    bear: int = 0,
    sample: list[str] | None = None,
) -> dict:
    return {
        "sector": name,
        "avg_score": avg,
        "row_count": rows,
        "bullish_count": bull,
        "bearish_count": bear,
        "sample_symbols": sample or [],
    }


# ---------------- happy paths ----------------


def test_fires_up_when_avg_above_threshold():
    sectors = [_sector("DeFi", avg=45, rows=8, bull=6, bear=0,
                       sample=["AAVE", "UNI", "MKR"])]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    actives = [(k, m) for (k, s, m) in out if s == "active"]
    assert any("DeFi" in m for (_k, m) in actives)
    assert any("turns bullish" in m for (_k, m) in actives)
    assert any(k == "sector_rot:DeFi:up" for (k, _m) in actives)


def test_fires_down_when_avg_below_negative_threshold():
    sectors = [_sector("Memes", avg=-50, rows=10, bull=1, bear=8)]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    actives = [(k, m) for (k, s, m) in out if s == "active"]
    assert any("Memes" in m for (_k, m) in actives)
    assert any("turns bearish" in m for (_k, m) in actives)
    assert any(k == "sector_rot:Memes:down" for (k, _m) in actives)


def test_active_message_includes_sample_symbols():
    sectors = [_sector("AI", avg=50, rows=5, bull=4, bear=0,
                       sample=["FET", "AGIX", "OCEAN"])]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "FET" in msg
    assert "AGIX" in msg


def test_active_message_omits_sample_when_empty():
    sectors = [_sector("X", avg=50, rows=5, bull=4, bear=0, sample=[])]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    msg = next(m for (_k, s, m) in out if s == "active")
    assert "Sample symbols:" not in msg


# ---------------- direction independence ----------------


def test_both_direction_keys_emitted_per_sector():
    """Each sector evaluates BOTH up and down keys so a flip from bullish
    to bearish properly fires both transitions.
    """
    sectors = [_sector("DeFi", avg=50, rows=5, bull=4, bear=0)]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    keys = {k for (k, _s, _m) in out}
    assert "sector_rot:DeFi:up" in keys
    assert "sector_rot:DeFi:down" in keys


def test_sector_within_band_resolves_both_directions():
    sectors = [_sector("Neutral", avg=10, rows=5)]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


# ---------------- guard rails ----------------


def test_skips_sector_below_min_token_count():
    sectors = [_sector("Tiny", avg=80, rows=2)]
    assert evaluate_sector_rotation_alerts(sectors, threshold=30, min_token_count=3) == []


def test_skips_sector_with_none_avg():
    sectors = [{"sector": "X", "avg_score": None, "row_count": 5}]
    assert evaluate_sector_rotation_alerts(sectors) == []


def test_skips_sector_with_no_name():
    sectors = [{"sector": None, "avg_score": 50, "row_count": 5}]
    assert evaluate_sector_rotation_alerts(sectors) == []


def test_threshold_tuneable():
    sectors = [_sector("X", avg=20, rows=5)]
    default = [s for (_k, s, _m) in evaluate_sector_rotation_alerts(sectors, threshold=30)
               if s == "active"]
    assert default == []
    relaxed = [s for (_k, s, _m) in evaluate_sector_rotation_alerts(sectors, threshold=15)
               if s == "active"]
    assert relaxed == ["active"]


# ---------------- multiple sectors ----------------


def test_multiple_sectors_independent():
    sectors = [
        _sector("DeFi", avg=50, rows=5, bull=4),
        _sector("Memes", avg=-50, rows=5, bear=4),
        _sector("Stables", avg=5, rows=3),
    ]
    out = evaluate_sector_rotation_alerts(sectors, threshold=30)
    actives = {k for (k, s, _m) in out if s == "active"}
    assert "sector_rot:DeFi:up" in actives
    assert "sector_rot:Memes:down" in actives
    # Stables within band → no actives.
    assert "sector_rot:Stables:up" not in actives
    assert "sector_rot:Stables:down" not in actives


def test_handles_empty_input():
    assert evaluate_sector_rotation_alerts([]) == []
    assert evaluate_sector_rotation_alerts(None) == []  # type: ignore[arg-type]
