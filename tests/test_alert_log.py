"""Tests for AlertLog (Round 24)."""

from __future__ import annotations

import time

from funding_screener.notifications import AlertFireRecord, AlertLog


def test_record_appends_and_extracts_kind_from_key():
    log = AlertLog()
    log.record("composite:BTC/USDT", "active", "🚀 BTC bullish", ("telegram",))
    assert len(log) == 1
    rec = log.recent()[0]
    assert isinstance(rec, AlertFireRecord)
    assert rec.kind == "composite"
    assert rec.key == "composite:BTC/USDT"
    assert rec.status == "active"
    assert rec.delivered_to == ("telegram",)


def test_kind_falls_back_to_full_key_when_no_colon():
    log = AlertLog()
    log.record("standalone_alert", "active", "msg", ())
    rec = log.recent()[0]
    assert rec.kind == "standalone_alert"


def test_recent_returns_newest_first():
    log = AlertLog()
    log.record("a:1", "active", "first", ())
    log.record("b:2", "active", "second", ())
    log.record("c:3", "active", "third", ())
    out = log.recent()
    assert [r.message for r in out] == ["third", "second", "first"]


def test_recent_respects_limit():
    log = AlertLog()
    for i in range(10):
        log.record(f"k:{i}", "active", f"msg{i}", ())
    assert len(log.recent(limit=3)) == 3
    assert len(log.recent(limit=20)) == 10


def test_recent_kind_filter():
    log = AlertLog()
    log.record("composite:BTC", "active", "comp", ())
    log.record("liq_cascade:ETH", "active", "casc", ())
    log.record("composite:ETH", "active", "comp2", ())
    only_comp = log.recent(kind="composite")
    assert len(only_comp) == 2
    assert all(r.kind == "composite" for r in only_comp)


def test_max_entries_drops_oldest():
    log = AlertLog(max_entries=5)
    for i in range(10):
        log.record(f"k:{i}", "active", f"msg{i}", ())
    assert len(log) == 5
    out = log.recent()
    # Newest 5 retained, in newest-first order.
    assert [r.message for r in out] == ["msg9", "msg8", "msg7", "msg6", "msg5"]


def test_kinds_returns_distinct():
    log = AlertLog()
    log.record("a:1", "active", "x", ())
    log.record("a:2", "active", "y", ())
    log.record("b:1", "active", "z", ())
    assert set(log.kinds()) == {"a", "b"}


def test_delivered_to_empty_when_unconfigured():
    log = AlertLog()
    log.record("a:1", "active", "x", ())
    assert log.recent()[0].delivered_to == ()


def test_fired_at_is_recent():
    log = AlertLog()
    before = time.time()
    log.record("a:1", "active", "x", ())
    after = time.time()
    rec = log.recent()[0]
    assert before <= rec.fired_at <= after


def test_record_immutable():
    """AlertFireRecord is frozen so logs can't be retroactively edited."""
    log = AlertLog()
    log.record("a:1", "active", "x", ())
    rec = log.recent()[0]
    import dataclasses
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        rec.message = "tampered"  # type: ignore[misc]


def test_resolved_status_recorded():
    log = AlertLog()
    log.record("a:1", "active", "broken", ())
    log.record("a:1", "resolved", "fixed", ("telegram",))
    out = log.recent()
    assert out[0].status == "resolved"
    assert out[1].status == "active"
