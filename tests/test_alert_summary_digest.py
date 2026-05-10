"""Tests for format_alert_summary_digest (Round 50)."""

from __future__ import annotations

import time

import pytest

from funding_screener.notifications import (
    AlertFireRecord,
    format_alert_summary_digest,
)


def _rec(kind: str, key: str, status: str = "active", message: str = "msg") -> AlertFireRecord:
    return AlertFireRecord(
        fired_at=time.time(),
        key=f"{kind}:{key}" if ":" not in key else key,
        kind=kind,
        status=status,
        message=message,
        delivered_to=("telegram",),
    )


def test_returns_empty_for_empty_input():
    assert format_alert_summary_digest([], interval_minutes=30) == ""


def test_header_includes_count_and_interval():
    out = format_alert_summary_digest([_rec("composite", "BTC/USDT")], interval_minutes=30)
    assert "Alert summary" in out
    assert "30 min" in out
    assert "1 fire" in out


def test_header_pluralizes_correctly():
    fires = [_rec("composite", "BTC/USDT"), _rec("composite", "ETH/USDT")]
    out = format_alert_summary_digest(fires, interval_minutes=30)
    assert "2 fires" in out


def test_groups_by_kind():
    fires = [
        _rec("composite", "BTC/USDT"),
        _rec("composite", "ETH/USDT"),
        _rec("liq_cascade", "BTCUSDT"),
    ]
    out = format_alert_summary_digest(fires, interval_minutes=30)
    # Each kind gets its own line/section.
    assert "composite (2)" in out
    assert "liq_cascade (1)" in out


def test_subject_extracted_from_key():
    """Key 'composite:BTC/USDT' should appear as 'BTC/USDT' in the digest body."""
    out = format_alert_summary_digest([_rec("composite", "BTC/USDT")], interval_minutes=30)
    assert "BTC/USDT" in out


def test_status_emoji_active_vs_resolved():
    out_active = format_alert_summary_digest([_rec("composite", "X/USDT", status="active")], 30)
    out_resolved = format_alert_summary_digest([_rec("composite", "Y/USDT", status="resolved")], 30)
    assert "🚨" in out_active
    assert "✅" in out_resolved


def test_max_per_kind_caps_listed_symbols():
    fires = [_rec("composite", f"X{i}/USDT") for i in range(8)]
    out = format_alert_summary_digest(fires, interval_minutes=30, max_per_kind=5)
    # Listed: X0..X4. The remaining 3 collapsed into "+3 more".
    assert "+3 more" in out
    assert "X4" in out
    assert "X5" not in out  # truncated


def test_no_more_suffix_when_under_cap():
    fires = [_rec("composite", "X/USDT") for _ in range(2)]
    out = format_alert_summary_digest(fires, interval_minutes=30, max_per_kind=5)
    assert "more" not in out


def test_multiple_kinds_each_get_own_count():
    fires = [
        _rec("composite", "A"),
        _rec("composite", "B"),
        _rec("liq_cascade", "C"),
        _rec("liq_cascade", "D"),
        _rec("liq_cascade", "E"),
        _rec("fresh", "F"),
    ]
    out = format_alert_summary_digest(fires, interval_minutes=15)
    assert "composite (2)" in out
    assert "liq_cascade (3)" in out
    assert "fresh (1)" in out
    assert "6 fires" in out


def test_interval_propagates_to_header():
    out = format_alert_summary_digest([_rec("composite", "X/USDT")], interval_minutes=15)
    assert "15 min" in out


def test_resolved_only_no_active():
    """When all fires are 'resolved' the digest still produces output —
    resolved-side fires are part of the audit trail.
    """
    fires = [
        _rec("composite", "X/USDT", status="resolved"),
        _rec("composite", "Y/USDT", status="resolved"),
    ]
    out = format_alert_summary_digest(fires, interval_minutes=30)
    assert "composite (2)" in out
    assert out.count("✅") == 2
    assert "🚨" not in out
