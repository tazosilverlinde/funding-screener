"""Tests for alert mute controls (Round 25)."""

from __future__ import annotations

import time

import pytest

from funding_screener.background import DataStore


def test_mute_kind_blocks_matching_alerts():
    s = DataStore()
    s.mute_alert("kind:composite", hours=1.0)
    assert s.is_alert_muted("composite", "composite:BTC/USDT")


def test_mute_kind_does_not_block_other_kinds():
    s = DataStore()
    s.mute_alert("kind:composite", hours=1.0)
    assert not s.is_alert_muted("liq_cascade", "liq_cascade:BTCUSDT")


def test_mute_symbol_substring_match():
    s = DataStore()
    s.mute_alert("symbol:BTCUSDT", hours=1.0)
    assert s.is_alert_muted("composite", "composite:BTCUSDT")
    assert s.is_alert_muted("liq_cascade", "liq_cascade:BTCUSDT")


def test_mute_symbol_does_not_block_unrelated_symbol():
    s = DataStore()
    s.mute_alert("symbol:BTCUSDT", hours=1.0)
    assert not s.is_alert_muted("composite", "composite:ETHUSDT")


def test_mute_expires_after_duration():
    s = DataStore()
    # Negative hours → expiry is already in the past.
    s.mute_alert("kind:test", hours=-1.0)
    assert not s.is_alert_muted("test", "test:X")


def test_unmute_removes_pattern():
    s = DataStore()
    s.mute_alert("kind:test", hours=1.0)
    s.unmute_alert("kind:test")
    assert not s.is_alert_muted("test", "test:X")


def test_unmute_unknown_pattern_is_safe():
    s = DataStore()
    s.unmute_alert("kind:never_existed")  # should not raise


def test_re_mute_extends_expiry():
    s = DataStore()
    s.mute_alert("kind:test", hours=0.001)  # very short
    expired_first = s.alert_mutes.get("kind:test")
    s.mute_alert("kind:test", hours=2.0)  # extend
    extended = s.alert_mutes.get("kind:test")
    assert extended > expired_first


def test_read_alert_mutes_excludes_expired():
    s = DataStore()
    s.mute_alert("kind:active", hours=1.0)
    s.mute_alert("kind:expired", hours=-0.001)
    visible = s.read_alert_mutes()
    assert "kind:active" in visible
    assert "kind:expired" not in visible


def test_is_alert_muted_drops_expired_mutes_lazily():
    """Calling is_alert_muted prunes expired entries from the underlying dict."""
    s = DataStore()
    s.mute_alert("kind:will_expire", hours=-0.001)
    s.mute_alert("kind:active", hours=1.0)
    assert "kind:will_expire" in s.alert_mutes  # still there before any read
    s.is_alert_muted("anything", "anything:X")
    assert "kind:will_expire" not in s.alert_mutes
    assert "kind:active" in s.alert_mutes


def test_multiple_mute_patterns_are_independent():
    s = DataStore()
    s.mute_alert("kind:composite", hours=1.0)
    s.mute_alert("symbol:WIFUSDT", hours=1.0)
    assert s.is_alert_muted("composite", "composite:BTC/USDT")  # via kind
    assert s.is_alert_muted("liq_cascade", "liq_cascade:WIFUSDT")  # via symbol
    assert not s.is_alert_muted("liq_cascade", "liq_cascade:ETHUSDT")
