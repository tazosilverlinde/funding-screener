"""Tests for the token-unlocks loader/filter logic."""

from __future__ import annotations

from datetime import date

import pytest

from funding_screener.unlocks import (
    UnlockEvent,
    _parse_one,
    attach_usd_values,
)


TODAY = date(2026, 5, 8)


def test_parse_basic_entry():
    e = _parse_one({
        "symbol": "ARB",
        "name": "Arbitrum",
        "date": "2026-05-16",
        "amount_tokens": 92_655_000,
        "pct_of_supply": 1.84,
        "type": "cliff",
        "notes": "Monthly cliff",
    }, TODAY)
    assert e is not None
    assert e.symbol == "ARB"
    assert e.days_until == 8
    assert e.unlock_type == "cliff"
    assert e.impact_label() == "Medium"
    assert e.impact_emoji() == "🟡"


def test_parse_high_impact():
    e = _parse_one({"symbol": "X", "date": "2026-06-01", "amount_tokens": 1, "pct_of_supply": 5.0, "type": "cliff"}, TODAY)
    assert e is not None
    assert e.impact_label() == "High"
    assert e.impact_emoji() == "🔴"


def test_parse_low_impact():
    e = _parse_one({"symbol": "X", "date": "2026-06-01", "amount_tokens": 1, "pct_of_supply": 0.4, "type": "linear"}, TODAY)
    assert e is not None
    assert e.impact_label() == "Low"
    assert e.impact_emoji() == "🟢"


def test_parse_unknown_impact_when_pct_missing():
    e = _parse_one({"symbol": "X", "date": "2026-06-01", "amount_tokens": 1, "type": "cliff"}, TODAY)
    assert e is not None
    assert e.impact_label() == "Unknown"
    assert e.impact_emoji() == "⚪"


def test_invalid_date_skipped():
    assert _parse_one({"symbol": "X", "date": "not-a-date", "amount_tokens": 1, "type": "cliff"}, TODAY) is None


def test_missing_symbol_skipped():
    assert _parse_one({"date": "2026-06-01", "amount_tokens": 1, "type": "cliff"}, TODAY) is None


def test_unknown_type_falls_back_to_cliff():
    e = _parse_one({"symbol": "X", "date": "2026-06-01", "amount_tokens": 1, "type": "weird"}, TODAY)
    assert e is not None
    assert e.unlock_type == "cliff"


def test_attach_usd_values_fills_missing():
    ev = _parse_one({"symbol": "ARB", "date": "2026-05-16", "amount_tokens": 1000, "type": "cliff"}, TODAY)
    out = attach_usd_values([ev], {"ARB": 0.5})
    assert out[0].amount_usd == pytest.approx(500.0)


def test_attach_usd_values_keeps_existing():
    ev = _parse_one({
        "symbol": "ARB", "date": "2026-05-16", "amount_tokens": 1000,
        "amount_usd": 999.0, "type": "cliff",
    }, TODAY)
    out = attach_usd_values([ev], {"ARB": 0.5})
    assert out[0].amount_usd == pytest.approx(999.0)
