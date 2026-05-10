"""Tests for evaluate_liquidation_cascade_alerts (Round 15)."""

from __future__ import annotations

from funding_screener.notifications import evaluate_liquidation_cascade_alerts


def _stats(
    long_usd: float = 0.0,
    short_usd: float = 0.0,
    biggest: float = 0.0,
    biggest_side: str | None = None,
) -> dict:
    return {
        "long_liq_usd": long_usd,
        "short_liq_usd": short_usd,
        "total_usd": long_usd + short_usd,
        "biggest_single_usd": biggest,
        "biggest_single_side": biggest_side,
        "events_count": int(bool(long_usd) + bool(short_usd)),
    }


def test_no_alerts_when_below_thresholds():
    """Quiet symbol — both cascade and single keys resolved, neither active."""
    by_symbol = {"BTCUSDT": _stats(long_usd=1_000_000, short_usd=500_000, biggest=500_000)}
    out = evaluate_liquidation_cascade_alerts(by_symbol)
    actives = [m for (_k, status, m) in out if status == "active"]
    assert actives == []


def test_cascade_active_when_total_above_threshold():
    by_symbol = {"BTCUSDT": _stats(long_usd=40_000_000, short_usd=15_000_000, biggest=2_000_000)}
    out = evaluate_liquidation_cascade_alerts(
        by_symbol, cascade_threshold_usd=50_000_000, single_threshold_usd=10_000_000,
    )
    active_keys = {k for (k, s, _m) in out if s == "active"}
    assert "liq_cascade:BTCUSDT" in active_keys
    # Single threshold not breached → no single-active.
    assert "liq_single:BTCUSDT" not in active_keys


def test_cascade_message_describes_long_dominant():
    by_symbol = {"BTCUSDT": _stats(long_usd=40_000_000, short_usd=15_000_000)}
    out = evaluate_liquidation_cascade_alerts(by_symbol, cascade_threshold_usd=50_000_000)
    cascade_msg = next(m for (k, s, m) in out if k == "liq_cascade:BTCUSDT" and s == "active")
    assert "Longs liquidated dominantly" in cascade_msg
    assert "$55.0M" in cascade_msg


def test_cascade_message_describes_short_dominant():
    by_symbol = {"ETHUSDT": _stats(long_usd=10_000_000, short_usd=45_000_000)}
    out = evaluate_liquidation_cascade_alerts(by_symbol, cascade_threshold_usd=50_000_000)
    cascade_msg = next(m for (k, s, m) in out if k == "liq_cascade:ETHUSDT" and s == "active")
    assert "Shorts liquidated dominantly" in cascade_msg
    assert "squeeze" in cascade_msg.lower()


def test_single_active_when_one_event_above_threshold():
    """Total may be small but a single $15M liquidation is its own signal."""
    by_symbol = {"WIFUSDT": _stats(long_usd=1_000_000, short_usd=500_000, biggest=15_000_000, biggest_side="long")}
    out = evaluate_liquidation_cascade_alerts(by_symbol, single_threshold_usd=10_000_000)
    actives = {k: m for (k, s, m) in out if s == "active"}
    assert "liq_single:WIFUSDT" in actives
    msg = actives["liq_single:WIFUSDT"]
    assert "$15.0M" in msg
    assert "long position blown out" in msg


def test_single_uses_short_label_when_biggest_was_short():
    by_symbol = {"BTCUSDT": _stats(biggest=12_000_000, biggest_side="short")}
    out = evaluate_liquidation_cascade_alerts(by_symbol, single_threshold_usd=10_000_000)
    msg = next(m for (k, s, m) in out if k == "liq_single:BTCUSDT" and s == "active")
    assert "short position blown out" in msg


def test_both_alerts_fire_independently_when_both_breach():
    by_symbol = {"BTCUSDT": _stats(long_usd=80_000_000, short_usd=20_000_000, biggest=15_000_000, biggest_side="long")}
    out = evaluate_liquidation_cascade_alerts(
        by_symbol, cascade_threshold_usd=50_000_000, single_threshold_usd=10_000_000,
    )
    actives = {k for (k, s, _m) in out if s == "active"}
    assert "liq_cascade:BTCUSDT" in actives
    assert "liq_single:BTCUSDT" in actives


def test_handles_empty_input():
    assert evaluate_liquidation_cascade_alerts({}) == []
    assert evaluate_liquidation_cascade_alerts(None) == []  # type: ignore[arg-type]


def test_thresholds_are_tunable():
    """Same data, different thresholds → different verdicts."""
    by_symbol = {"BTCUSDT": _stats(long_usd=20_000_000, short_usd=10_000_000)}
    strict = evaluate_liquidation_cascade_alerts(by_symbol, cascade_threshold_usd=50_000_000)
    relaxed = evaluate_liquidation_cascade_alerts(by_symbol, cascade_threshold_usd=20_000_000)
    strict_actives = {k for (k, s, _m) in strict if s == "active"}
    relaxed_actives = {k for (k, s, _m) in relaxed if s == "active"}
    assert "liq_cascade:BTCUSDT" not in strict_actives
    assert "liq_cascade:BTCUSDT" in relaxed_actives


def test_multiple_symbols_independent():
    by_symbol = {
        "BTCUSDT": _stats(long_usd=80_000_000, short_usd=10_000_000),
        "ETHUSDT": _stats(long_usd=1_000_000, short_usd=500_000),
    }
    out = evaluate_liquidation_cascade_alerts(by_symbol, cascade_threshold_usd=50_000_000)
    actives = {k for (k, s, _m) in out if s == "active"}
    assert "liq_cascade:BTCUSDT" in actives
    assert "liq_cascade:ETHUSDT" not in actives
