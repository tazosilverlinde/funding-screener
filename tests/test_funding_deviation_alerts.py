"""Tests for evaluate_funding_deviation_alerts (Round 13)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.notifications import evaluate_funding_deviation_alerts


@dataclass
class _FakeRow:
    base_asset: str
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = "BTCUSDT"
    mexc_symbol: Optional[str] = None
    funding_deviation_z: Optional[float] = None


def test_no_event_when_z_is_none():
    rows = [_FakeRow("BTC", funding_deviation_z=None)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    assert out == []


def test_active_overshoot_at_or_above_threshold():
    rows = [_FakeRow("BTC", binance_symbol="BTCUSDT", funding_deviation_z=3.0)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    # Both directions evaluated; overshoot active, undershoot resolved.
    actives = [(k, m) for (k, status, m) in out if status == "active"]
    assert len(actives) == 1
    key, msg = actives[0]
    assert key == "funding_dev:BTC/USDT:over"
    assert "🔥" in msg
    assert "+3.0σ" in msg
    assert "fading" in msg.lower()  # mean-revert overshoot framing


def test_active_undershoot_at_or_below_negative_threshold():
    rows = [_FakeRow("ETH", binance_symbol="ETHUSDT", funding_deviation_z=-2.7)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    actives = [(k, m) for (k, status, m) in out if status == "active"]
    assert len(actives) == 1
    key, msg = actives[0]
    assert key == "funding_dev:ETH/USDT:under"
    assert "❄" in msg
    assert "squeeze" in msg.lower()


def test_resolved_when_within_band():
    rows = [_FakeRow("BTC", funding_deviation_z=0.5)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    statuses = {status for (_k, status, _m) in out}
    assert statuses == {"resolved"}


def test_separate_keys_per_direction():
    """Same row evaluates BOTH 'over' and 'under' keys so a regime flip re-fires."""
    rows = [_FakeRow("BTC", funding_deviation_z=3.0)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    keys = {k for (k, _s, _m) in out}
    assert "funding_dev:BTC/USDT:over" in keys
    assert "funding_dev:BTC/USDT:under" in keys


def test_uses_mexc_symbol_when_no_binance():
    rows = [_FakeRow("PEPE", binance_symbol=None, mexc_symbol="PEPE_USDT", funding_deviation_z=2.8)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    actives = [m for (_k, status, m) in out if status == "active"]
    assert any("PEPE_USDT" in m for m in actives)


def test_falls_back_to_base_asset_when_no_symbols():
    rows = [_FakeRow("ARB", binance_symbol=None, mexc_symbol=None, funding_deviation_z=3.5)]
    out = evaluate_funding_deviation_alerts(rows, z_threshold=2.5)
    actives = [m for (_k, status, m) in out if status == "active"]
    assert any("ARB" in m for m in actives)


def test_custom_z_threshold_respected():
    rows = [_FakeRow("X", funding_deviation_z=1.8)]
    # At default 2.5, this is in band → resolved only.
    default_actives = [
        m for (_k, s, m) in evaluate_funding_deviation_alerts(rows) if s == "active"
    ]
    assert default_actives == []
    # At threshold 1.5, this becomes active.
    relaxed_actives = [
        m for (_k, s, m) in evaluate_funding_deviation_alerts(rows, z_threshold=1.5)
        if s == "active"
    ]
    assert len(relaxed_actives) == 1


def test_multiple_rows_independent():
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", funding_deviation_z=3.0),
        _FakeRow("ETH", binance_symbol="ETHUSDT", funding_deviation_z=-3.0),
        _FakeRow("SOL", binance_symbol="SOLUSDT", funding_deviation_z=0.0),
    ]
    out = evaluate_funding_deviation_alerts(rows)
    actives = {k: m for (k, s, m) in out if s == "active"}
    assert "funding_dev:BTC/USDT:over" in actives
    assert "funding_dev:ETH/USDT:under" in actives
    # SOL should produce no actives — both 'over' and 'under' keys resolved.
    sol_keys = [k for k in actives if k.startswith("funding_dev:SOL/")]
    assert sol_keys == []
