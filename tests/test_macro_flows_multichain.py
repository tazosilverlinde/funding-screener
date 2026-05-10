"""Tests for per-chain macro daily flows (Round 29)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.background import DataStore


def _sample_data() -> dict[str, list[dict]]:
    return {
        "USDT": [{"date": "2026-05-08", "net_usd": 1e6, "deposits_usd": 0, "withdrawals_usd": 1e6}],
        "USDC": [{"date": "2026-05-08", "net_usd": -5e5, "deposits_usd": 5e5, "withdrawals_usd": 0}],
    }


def test_update_then_read_per_chain():
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    data, last_at = s.read_macro_daily_flows("ethereum")
    assert "USDT" in data
    assert data["USDT"][0]["net_usd"] == pytest.approx(1e6)
    assert last_at is not None


def test_read_default_chain_is_ethereum():
    """No-arg read returns the ethereum slice (backward compat for Page 6's
    legacy single-chain call site).
    """
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    data, _ = s.read_macro_daily_flows()  # no arg
    assert "USDT" in data


def test_unknown_chain_returns_empty():
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    data, last_at = s.read_macro_daily_flows("solana")
    assert data == {}
    assert last_at is None


def test_chains_are_isolated():
    """Updating one chain doesn't affect another."""
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    s.update_macro_daily_flows("bsc", {"BTCB": [{"date": "2026-05-08", "net_usd": 9e6,
                                                  "deposits_usd": 0, "withdrawals_usd": 9e6}]})
    eth_data, _ = s.read_macro_daily_flows("ethereum")
    bsc_data, _ = s.read_macro_daily_flows("bsc")
    assert "USDT" in eth_data and "USDC" in eth_data
    assert "BTCB" in bsc_data
    # BSC update did not bleed into ETH.
    assert "BTCB" not in eth_data
    assert "USDT" not in bsc_data


def test_read_macro_daily_flows_by_chain_returns_full_split():
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    s.update_macro_daily_flows("bsc", {"BTCB": [{"date": "2026-05-08", "net_usd": 9e6,
                                                  "deposits_usd": 0, "withdrawals_usd": 9e6}]})
    by_chain = s.read_macro_daily_flows_by_chain()
    assert set(by_chain.keys()) == {"ethereum", "bsc"}
    eth_data, eth_at = by_chain["ethereum"]
    bsc_data, bsc_at = by_chain["bsc"]
    assert "USDT" in eth_data
    assert "BTCB" in bsc_data
    assert eth_at is not None and bsc_at is not None


def test_overwriting_chain_replaces_data_not_merges():
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    s.update_macro_daily_flows("ethereum", {"WBTC": [{"date": "x", "net_usd": 1.0,
                                                       "deposits_usd": 0, "withdrawals_usd": 1.0}]})
    data, _ = s.read_macro_daily_flows("ethereum")
    # Old keys gone, only WBTC remains.
    assert set(data.keys()) == {"WBTC"}


def test_empty_state_round_trip():
    s = DataStore()
    data, last_at = s.read_macro_daily_flows("ethereum")
    assert data == {}
    assert last_at is None
    assert s.read_macro_daily_flows_by_chain() == {}


def test_read_returns_copy_not_alias():
    """Mutating returned data must not affect the store's internal state."""
    s = DataStore()
    s.update_macro_daily_flows("ethereum", _sample_data())
    data, _ = s.read_macro_daily_flows("ethereum")
    data["MUTATED"] = []
    fresh, _ = s.read_macro_daily_flows("ethereum")
    assert "MUTATED" not in fresh
