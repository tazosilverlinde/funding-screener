"""Tests for evaluate_memory_pressure_alert (Round 52)."""

from __future__ import annotations

from funding_screener.notifications import evaluate_memory_pressure_alert


# ---------------- happy paths ----------------


def test_fires_active_at_threshold():
    """At exactly 75% of 2048 = 1536 MB → active."""
    out = evaluate_memory_pressure_alert(1536.0, budget_mb=2048.0, pct_threshold=75.0)
    assert len(out) == 1
    key, status, msg = out[0]
    assert key == "memory_pressure"
    assert status == "active"
    assert "Memory pressure" in msg
    assert "75%" in msg


def test_fires_active_above_threshold():
    out = evaluate_memory_pressure_alert(1800.0, budget_mb=2048.0, pct_threshold=75.0)
    assert out[0][1] == "active"
    assert "1,800 MB" in out[0][2] or "1800 MB" in out[0][2]


def test_resolved_below_threshold():
    out = evaluate_memory_pressure_alert(1000.0, budget_mb=2048.0, pct_threshold=75.0)
    assert out[0][1] == "resolved"
    assert "cleared" in out[0][2]


def test_active_message_contains_actionable_guidance():
    out = evaluate_memory_pressure_alert(1700.0)
    msg = out[0][2]
    # Must mention specifically what buffers to investigate.
    assert "buffer" in msg.lower()
    assert "liquidations" in msg.lower() or "score history" in msg.lower()


# ---------------- guards ----------------


def test_no_alert_when_rss_is_none():
    assert evaluate_memory_pressure_alert(None) == []


def test_no_alert_when_budget_zero_or_negative():
    assert evaluate_memory_pressure_alert(1000.0, budget_mb=0) == []
    assert evaluate_memory_pressure_alert(1000.0, budget_mb=-1) == []


def test_threshold_tuneable():
    """At 50% threshold, 1024 MB on a 2048 budget fires; at 75% it doesn't."""
    fifty = evaluate_memory_pressure_alert(1024.0, budget_mb=2048.0, pct_threshold=50.0)
    seventyfive = evaluate_memory_pressure_alert(1024.0, budget_mb=2048.0, pct_threshold=75.0)
    assert fifty[0][1] == "active"
    assert seventyfive[0][1] == "resolved"


def test_custom_budget_supported():
    """Tighter budget (e.g. 1GB) for resource-constrained deploys."""
    out = evaluate_memory_pressure_alert(800.0, budget_mb=1000.0, pct_threshold=75.0)
    assert out[0][1] == "active"


# ---------------- state-machine integration shape ----------------


def test_returns_single_key_independent_of_state():
    """Always emits exactly one (key, status, msg) tuple — the state machine
    handles transitions externally. Tests both branches return single tuple.
    """
    active = evaluate_memory_pressure_alert(1700.0)
    resolved = evaluate_memory_pressure_alert(500.0)
    assert len(active) == 1
    assert len(resolved) == 1
    assert active[0][0] == resolved[0][0]  # same key for both branches


def test_message_includes_budget_for_context():
    """User reading the alert in Telegram should see the budget so they
    immediately know how much headroom is left.
    """
    out = evaluate_memory_pressure_alert(1700.0, budget_mb=2048.0)
    msg = out[0][2]
    assert "2,048 MB" in msg or "2048 MB" in msg
