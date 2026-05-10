"""Chain configuration for the on-chain flow tracker.

We started ETH-only; this module now lets the same code talk to any
EVM-compatible chain by parameterising:

  - the public RPC fallback list (each chain has its own free endpoints)
  - blocks_per_24h (block time differs per chain — ETH 12s ≈ 7200, BSC 3s ≈ 28800)

Adding a new EVM chain is one ChainConfig + a `bsc:`-style section in the
three YAMLs (exchange_wallets, non_whale_addresses, token contracts).

Non-EVM chains (Solana, Bitcoin) need a different client because their RPC
schema differs from JSON-RPC eth_getLogs — handled separately if added.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChainConfig:
    name: str            # YAML key used in config files (e.g. "ethereum", "bsc")
    label: str           # human-friendly display name (e.g. "Ethereum", "BNB Chain")
    rpc_urls: tuple[str, ...]
    blocks_per_24h: int  # ~24h block range for eth_getLogs queries


# ---- Ethereum mainnet — original chain we built for ----
ETHEREUM = ChainConfig(
    name="ethereum",
    label="Ethereum",
    rpc_urls=(
        "https://ethereum-rpc.publicnode.com",
        "https://eth.drpc.org",
        "https://cloudflare-eth.com",
        "https://eth.llamarpc.com",
        "https://rpc.ankr.com/eth",
    ),
    blocks_per_24h=7200,  # 12s blocks
)


# ---- BNB Chain (BSC) — added Round 10 ----
# Block time ~3s. Many "BEP-20" tokens are exchange-wrapped versions of mainnet
# assets (BTCB ≈ BTC, ETH-on-BSC ≈ ETH); they share Binance's Transfer event
# semantics so the same parsing works.
BSC = ChainConfig(
    name="bsc",
    label="BNB Chain",
    rpc_urls=(
        "https://bsc-dataseed1.binance.org",
        "https://bsc-dataseed2.defibit.io",
        "https://bsc.publicnode.com",
        "https://bsc.drpc.org",
        "https://rpc.ankr.com/bsc",
    ),
    blocks_per_24h=28800,  # 3s blocks
)


# Registered chains — used by the background loop to spawn one onchain task per chain.
ALL_CHAINS: dict[str, ChainConfig] = {
    "ethereum": ETHEREUM,
    "bsc": BSC,
}
