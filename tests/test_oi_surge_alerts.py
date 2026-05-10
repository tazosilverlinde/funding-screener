"""Tests for evaluate_oi_surge_alerts (Round 20)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from funding_screener.notifications import evaluate_oi_surge_alerts


@dataclass
class _FakeRow:
    base_asset: str
    quote_asset: str = "USDT"
    binance_symbol: Optional[str] = "BTCUSDT"
    mexc_symbol: Optional[str] = None
    binance_oi_change_24h_pct: Optional[float] = None


def test_no_event_when_oi_is_none():
    rows = [_FakeRow("BTC", binance_oi_change_24h_pct=None)]
    assert evaluate_oi_surge_alerts(rows) == []


def test_active_when_surge_above_threshold_up():
    rows = [_FakeRow("BTC", binance_symbol="BTCUSDT", binance_oi_change_24h_pct=60.0)]
    out = evaluate_oi_surge_alerts(rows, threshold_pct_24h=50.0)
    actives = [(k, m) for (k, status, m) in out if status == "active"]
    assert len(actives) == 1
    key, msg = actives[0]
    assert key == "oi_surge:BTC/USDT"
    assert "+60.0%" in msg
    assert "Fresh leverage" in msg


def test_active_when_surge_below_threshold_down():
    """Negative surge — rapid unwind."""
    rows = [_FakeRow("ETH", binance_symbol="ETHUSDT", binance_oi_change_24h_pct=-55.0)]
    out = evaluate_oi_surge_alerts(rows, threshold_pct_24h=50.0)
    actives = [m for (_k, status, m) in out if status == "active"]
    assert len(actives) == 1
    assert "-55.0%" in actives[0]
    assert "unwind" in actives[0].lower()


def test_resolved_when_within_threshold():
    rows = [_FakeRow("BTC", binance_oi_change_24h_pct=10.0)]
    out = evaluate_oi_surge_alerts(rows, threshold_pct_24h=50.0)
    statuses = {s for (_k, s, _m) in out}
    assert statuses == {"resolved"}


def test_threshold_is_inclusive():
    """At exactly threshold, alert fires (inclusive comparison)."""
    rows = [_FakeRow("BTC", binance_oi_change_24h_pct=50.0)]
    out = evaluate_oi_surge_alerts(rows, threshold_pct_24h=50.0)
    actives = [s for (_k, s, _m) in out if s == "active"]
    assert actives == ["active"]


def test_uses_mexc_symbol_when_no_binance():
    rows = [_FakeRow(
        "PEPE", binance_symbol=None, mexc_symbol="PEPE_USDT",
        binance_oi_change_24h_pct=80.0,
    )]
    out = evaluate_oi_surge_alerts(rows)
    actives = [m for (_k, s, m) in out if s == "active"]
    assert any("PEPE_USDT" in m for m in actives)


def test_custom_threshold_respected():
    rows = [_FakeRow("X", binance_oi_change_24h_pct=30.0)]
    # Default 50 → resolved
    default_actives = [s for (_k, s, _m) in evaluate_oi_surge_alerts(rows) if s == "active"]
    assert default_actives == []
    # Tighter threshold of 20 → active
    relaxed_actives = [s for (_k, s, _m) in evaluate_oi_surge_alerts(rows, threshold_pct_24h=20.0)
                       if s == "active"]
    assert len(relaxed_actives) == 1


def test_multiple_rows_independent():
    rows = [
        _FakeRow("BTC", binance_symbol="BTCUSDT", binance_oi_change_24h_pct=80.0),
        _FakeRow("SOL", binance_symbol="SOLUSDT", binance_oi_change_24h_pct=5.0),
    ]
    out = evaluate_oi_surge_alerts(rows, threshold_pct_24h=50.0)
    actives = {k for (k, s, _m) in out if s == "active"}
    assert "oi_surge:BTC/USDT" in actives
    assert "oi_surge:SOL/USDT" not in actives


def test_resolved_message_includes_current_value():
    rows = [_FakeRow("BTC", binance_oi_change_24h_pct=12.5)]
    out = evaluate_oi_surge_alerts(rows, threshold_pct_24h=50.0)
    resolved = next(m for (_k, s, m) in out if s == "resolved")
    assert "+12.5%" in resolved
