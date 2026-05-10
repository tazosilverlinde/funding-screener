"""Tests for chains.py + multi-chain DataStore behavior (Round 10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.background import DataStore
from funding_screener.chains import ALL_CHAINS, BSC, ETHEREUM, ChainConfig
from funding_screener.onchain import (
    EvmOnchainClient,
    load_eth_token_contracts,
    load_exchange_wallets,
    load_non_whale_addresses,
)


# ------------- chains.py registry -------------


def test_chain_configs_have_required_fields():
    for chain in (ETHEREUM, BSC):
        assert isinstance(chain, ChainConfig)
        assert chain.name and chain.label
        assert chain.blocks_per_24h > 0
        assert len(chain.rpc_urls) >= 2  # always have at least one fallback
        for url in chain.rpc_urls:
            assert url.startswith("https://")


def test_eth_and_bsc_have_distinct_block_rates():
    # ETH is ~12s, BSC is ~3s → BSC should have ~4x the daily blocks.
    assert BSC.blocks_per_24h > ETHEREUM.blocks_per_24h * 2


def test_all_chains_registry_complete():
    assert ALL_CHAINS["ethereum"] is ETHEREUM
    assert ALL_CHAINS["bsc"] is BSC


# ------------- EvmOnchainClient is chain-aware -------------


def test_evm_client_carries_chain_identity():
    eth = EvmOnchainClient(ETHEREUM)
    bsc = EvmOnchainClient(BSC)
    assert eth.chain.name == "ethereum"
    assert bsc.chain.name == "bsc"
    assert "bsc" in bsc.name  # name is e.g. "EvmOnchain[bsc]"


def test_evm_client_uses_chain_specific_rpcs():
    bsc = EvmOnchainClient(BSC)
    # BSC RPCs should NOT include ethereum mainnet endpoints.
    bsc_rpcs = " ".join(bsc._rpc_urls)
    assert "bsc" in bsc_rpcs.lower() or "binance" in bsc_rpcs.lower()


# ------------- YAML loaders are chain-aware -------------


def test_load_exchange_wallets_returns_eth_default():
    eth = load_exchange_wallets()  # no arg
    assert "binance" in eth
    eth2 = load_exchange_wallets("ethereum")
    assert eth == eth2


def test_load_exchange_wallets_bsc_section_present():
    bsc = load_exchange_wallets("bsc")
    assert "binance" in bsc
    # BSC binance hot wallet (verified canonical address)
    assert "0x8894e0a0c962cb723c1976a4421c95949be2d4e3" in bsc["binance"]


def test_load_token_contracts_bsc_includes_btcb():
    bsc = load_eth_token_contracts("bsc")
    assert "BTCB" in bsc
    assert bsc["BTCB"].decimals == 18  # BSC uses 18 decimals (vs WBTC=8 on ETH)
    assert bsc["BTCB"].address == "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c"


def test_load_token_contracts_bsc_stables_have_18_decimals():
    """Critical: BSC USDT/USDC use 18 decimals — wrong decimals would 1e12-fold the USD."""
    bsc = load_eth_token_contracts("bsc")
    assert bsc["USDT"].decimals == 18
    assert bsc["USDC"].decimals == 18
    # ETH side still uses 6 decimals.
    eth = load_eth_token_contracts("ethereum")
    assert eth["USDT"].decimals == 6
    assert eth["USDC"].decimals == 6


def test_load_non_whale_addresses_bsc_includes_pancakeswap():
    bsc = load_non_whale_addresses("bsc")
    assert "0x10ed43c718714eb63d5aa57b78b54704e256024e" in bsc  # Pancake V2 Router


def test_load_unknown_chain_returns_empty():
    """Loading a chain not present in YAML returns empty rather than crashing."""
    assert load_exchange_wallets("solana") == {}
    assert load_eth_token_contracts("solana") == {}
    assert load_non_whale_addresses("solana") == set()


# ------------- DataStore multi-chain flow storage -------------


def _flow(token: str, chain: str, net: float = 0.0) -> dict:
    return {
        "token": token,
        "chain": chain,
        "net_usd": net,
        "deposits_usd": 0.0,
        "withdrawals_usd": net,
        "deposit_count": 0,
        "withdrawal_count": 1,
        "by_exchange": {},
        "whale_net_usd": 0.0,
        "whale_deposits_usd": 0.0,
        "whale_withdrawals_usd": 0.0,
        "whale_unique_count": 0,
        "signal_emoji": "🟢",
        "signal_short": "Accumulation",
    }


def test_update_onchain_flows_per_chain_round_trip():
    s = DataStore()
    s.update_onchain_flows("ethereum", [_flow("USDT", "ethereum", 1e6)])
    s.update_onchain_flows("bsc", [_flow("USDT", "bsc", 2e6), _flow("BTCB", "bsc", 5e6)])
    flows, last_at = s.read_onchain_flows()
    assert len(flows) == 3
    chains = {r["chain"] for r in flows}
    assert chains == {"ethereum", "bsc"}
    assert last_at is not None


def test_read_onchain_flows_by_chain_returns_split_view():
    s = DataStore()
    s.update_onchain_flows("ethereum", [_flow("USDT", "ethereum", 1e6)])
    s.update_onchain_flows("bsc", [_flow("BTCB", "bsc", 5e6)])
    by_chain = s.read_onchain_flows_by_chain()
    assert set(by_chain.keys()) == {"ethereum", "bsc"}
    eth_flows, eth_at = by_chain["ethereum"]
    bsc_flows, bsc_at = by_chain["bsc"]
    assert len(eth_flows) == 1 and len(bsc_flows) == 1
    assert eth_flows[0]["token"] == "USDT"
    assert bsc_flows[0]["token"] == "BTCB"
    assert eth_at is not None and bsc_at is not None


def test_freshness_returns_oldest_chain_timestamp():
    """Banner should reflect the slowest chain — so users know when stale."""
    s = DataStore()
    # Manually set timestamps to control test (BSC is older).
    old_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    fresh_ts = datetime.now(timezone.utc)
    s.onchain_flows_by_chain["ethereum"] = [_flow("USDT", "ethereum")]
    s.last_onchain_by_chain["ethereum"] = fresh_ts
    s.onchain_flows_by_chain["bsc"] = [_flow("BTCB", "bsc")]
    s.last_onchain_by_chain["bsc"] = old_ts
    _flows, last_at = s.read_onchain_flows()
    assert last_at == old_ts  # oldest, not freshest


def test_overwriting_one_chain_doesnt_affect_other():
    """Re-running ETH loop must NOT clear BSC flows — chains are independent."""
    s = DataStore()
    s.update_onchain_flows("ethereum", [_flow("USDT", "ethereum", 1e6)])
    s.update_onchain_flows("bsc", [_flow("BTCB", "bsc", 5e6)])
    # ETH cycle finishes again with new data.
    s.update_onchain_flows("ethereum", [_flow("USDT", "ethereum", 9e9)])
    flows, _ = s.read_onchain_flows()
    eth_total = next(r for r in flows if r["chain"] == "ethereum")
    bsc_total = next(r for r in flows if r["chain"] == "bsc")
    assert eth_total["net_usd"] == 9e9  # updated
    assert bsc_total["net_usd"] == 5e6  # untouched


def test_empty_store_returns_no_flows_and_no_timestamp():
    s = DataStore()
    flows, last_at = s.read_onchain_flows()
    assert flows == []
    assert last_at is None
    assert s.read_onchain_flows_by_chain() == {}
