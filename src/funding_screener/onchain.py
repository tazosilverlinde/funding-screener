"""Our own ETH on-chain exchange-flow tracker — no third-party labeling APIs.

Computes 24h netflow per token to/from labeled exchange hot wallets:

    deposits_usd     = Σ token transfers TO any of {our exchange wallets}
    withdrawals_usd  = Σ token transfers FROM any of {our exchange wallets}
    net_usd          = withdrawals_usd − deposits_usd     (positive = bullish)

Filter: we only track tokens that have a USDT or USDC perp on Binance OR MEXC,
so the screener stays focused on what the user can actually trade.

Data sources:
  - Public Ethereum JSON-RPC (default https://eth.llamarpc.com — free, no key)
  - config/exchange_wallets.yaml — maintained list of CEX hot wallets
  - config/eth_token_contracts.yaml — maintained ticker → ERC-20 contract map
  - Token prices: pulled from the existing Binance/MEXC funding cache (mark price)

Limitations:
  - ETH chain only. SOL/BNB/native chains aren't covered.
  - Internal exchange shuffling (Binance → Binance) is correctly cancelled out
    if both addresses are in our list (counts as +deposit −withdrawal = 0 net).
  - New CEXs and new wallets need to be added to YAML manually.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
import yaml

from .chains import BSC, ETHEREUM, ChainConfig
from .config import http_client_kwargs, settings

# `Transfer(address indexed from, address indexed to, uint256 value)` event signature.
# Same on every EVM chain.
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Backward-compatible defaults (the original Ethereum-only constants).
_DEFAULT_RPCS = list(ETHEREUM.rpc_urls)
_BLOCKS_PER_24H = ETHEREUM.blocks_per_24h

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    address: str  # lowercase 0x...
    decimals: int


def _config_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config"


def _pad_address(addr: str) -> str:
    """Convert a 20-byte address to a 32-byte topic-format value (0x-prefixed)."""
    a = addr.lower().replace("0x", "")
    return "0x" + ("0" * (64 - len(a))) + a


def load_exchange_wallets(chain: str = "ethereum") -> dict[str, list[str]]:
    """Returns {exchange_name: [lowercase 0x addresses]} for the given chain.

    Default is "ethereum" for backward compatibility. Add new chains to
    `config/exchange_wallets.yaml` under their own top-level key.
    """
    path = _config_dir() / "exchange_wallets.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    chain_data = (data.get(chain) or {})
    out: dict[str, list[str]] = {}
    for exchange, wallets in chain_data.items():
        if not isinstance(wallets, list):
            continue
        out[exchange] = [w.lower() for w in wallets if isinstance(w, str)]
    return out


def load_non_whale_addresses(chain: str = "ethereum") -> set[str]:
    """Load `config/non_whale_addresses.yaml` for `chain` and flatten every
    category into one lowercase set. Used by the whale-flow tracker to filter
    out routine DEX-router / bridge / protocol traffic.
    """
    path = _config_dir() / "non_whale_addresses.yaml"
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    chain_data = (data.get(chain) or {})
    out: set[str] = set()
    for _category, addrs in chain_data.items():
        if not isinstance(addrs, list):
            continue
        for a in addrs:
            if isinstance(a, str):
                out.add(a.lower().strip())
    return out


def load_eth_token_contracts(chain: str = "ethereum") -> dict[str, TokenInfo]:
    """Returns {SYMBOL_UPPER: TokenInfo} from `config/eth_token_contracts.yaml`
    for the given chain. Despite the legacy filename it now holds multi-chain
    sections — top-level keys = chain names.
    """
    path = _config_dir() / "eth_token_contracts.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    chain_data = (data.get(chain) or {})
    out: dict[str, TokenInfo] = {}
    for sym, info in chain_data.items():
        if not isinstance(info, dict):
            continue
        addr = info.get("address")
        decimals = int(info.get("decimals", 18))
        if not isinstance(addr, str):
            continue
        sym_u = sym.upper()
        out[sym_u] = TokenInfo(symbol=sym_u, address=addr.lower(), decimals=decimals)
    return out


class EvmOnchainClient:
    """Minimal EVM JSON-RPC client (Ethereum, BSC, etc.).

    Uses a list of public RPC endpoints (no API keys). Each request walks the
    list and keeps trying the next one on 429 or transient error — gives us
    surprisingly good uptime without any signup.

    The client is chain-agnostic — pass a ChainConfig from chains.py to target
    a specific network. Defaults to Ethereum mainnet for backward compatibility.
    """

    def __init__(
        self,
        chain: ChainConfig | None = None,
        rpc_urls: list[str] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.chain = chain or ETHEREUM
        self.name = f"EvmOnchain[{self.chain.name}]"
        # Explicit rpc_urls override the chain's defaults — used by tests/forks.
        self._rpc_urls = list(rpc_urls or self.chain.rpc_urls)
        if http is None:
            kwargs = http_client_kwargs()
            # Block-range RPC queries can be slow; allow generous read timeout.
            kwargs["timeout"] = httpx.Timeout(45.0, connect=5.0)
            self._http = httpx.AsyncClient(**kwargs)
        else:
            self._http = http
        self._owns_http = http is None
        self._next_idx = 0  # round-robin starting point per request

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _rpc(self, method: str, params: list) -> Any:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last_exc: Optional[Exception] = None
        # Walk every RPC URL once; rotate the starting index so we don't hammer one node.
        n = len(self._rpc_urls)
        for offset in range(n):
            url = self._rpc_urls[(self._next_idx + offset) % n]
            try:
                r = await self._http.post(url, json=payload)
                if r.status_code in (429, 503):
                    last_exc = httpx.HTTPStatusError(
                        f"rate-limited at {url}", request=r.request, response=r,
                    )
                    continue
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"{url} returned RPC error: {data['error']}")
                # Rotate so the next call starts from the next URL.
                self._next_idx = (self._next_idx + offset + 1) % n
                return data.get("result")
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return None

    async def get_block_number(self) -> int:
        result = await self._rpc("eth_blockNumber", [])
        return int(result, 16) if result else 0

    async def get_transfer_logs(
        self,
        token_address: str,
        from_block: int,
        to_block: int,
        from_topic_filter: list[str] | None = None,
        to_topic_filter: list[str] | None = None,
    ) -> list[dict]:
        """Fetch ERC-20/BEP-20 Transfer events for a token in a block range,
        optionally filtered by `from` (topic[1]) or `to` (topic[2]) address sets.
        """
        topics: list = [_TRANSFER_TOPIC]
        topics.append(from_topic_filter if from_topic_filter is not None else None)
        topics.append(to_topic_filter if to_topic_filter is not None else None)
        params = [{
            "address": token_address,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": topics,
        }]
        result = await self._rpc("eth_getLogs", params)
        return result if isinstance(result, list) else []


# Backward-compat alias — old callers using "EthOnchainClient(...)" still work.
EthOnchainClient = EvmOnchainClient


def parse_transfer_log(log: dict, decimals: int) -> tuple[str, str, float]:
    """Decode a Transfer log into (from_addr, to_addr, amount_in_token_units)."""
    topics = log.get("topics") or []
    if len(topics) < 3:
        return ("", "", 0.0)
    from_addr = "0x" + topics[1][-40:].lower()
    to_addr = "0x" + topics[2][-40:].lower()
    data = log.get("data", "0x0") or "0x0"
    try:
        raw = int(data, 16)
    except (ValueError, TypeError):
        raw = 0
    amount = raw / (10 ** decimals) if decimals >= 0 else raw
    return (from_addr, to_addr, amount)


async def compute_token_netflow(
    client: EvmOnchainClient,
    token: TokenInfo,
    exchange_wallets: dict[str, list[str]],
    blocks_back: int,
    price_usd: Optional[float],
    non_whale_addresses: Optional[set[str]] = None,
    whale_threshold_usd: float = 500_000.0,
) -> Optional[dict]:
    """Compute one token's 24h exchange netflow + whale-class aggregation.

    The result includes both the all-transfer aggregate (existing) and a
    parallel whale-only aggregate that:
      - Counts only transfers with USD value >= `whale_threshold_usd`
      - Excludes counterparties in `non_whale_addresses` (DEX routers, bridges,
        protocol contracts, known market makers)

    Whale aggregate keys: whale_deposits_usd, whale_withdrawals_usd, whale_net_usd,
    whale_unique_count (distinct non-excluded addresses involved).

    Returns None if price missing or RPC fails.
    """
    if price_usd is None or price_usd <= 0:
        return None

    # Build padded-address sets for topic filtering + reverse map for attribution.
    padded_all: list[str] = []
    wallet_to_exchange: dict[str, str] = {}
    for exchange, wallets in exchange_wallets.items():
        for w in wallets:
            wal = w.lower()
            padded_all.append(_pad_address(wal))
            wallet_to_exchange[wal] = exchange

    if not padded_all:
        return None

    try:
        latest = await client.get_block_number()
        from_block = max(0, latest - blocks_back)
        # Sequential (not parallel) to be gentle on free RPCs. Fail fast: if either
        # leg errors after exhausting all RPC fallbacks, return None — partial data
        # (e.g. only deposits, no withdrawals) misleads the user.
        deposits_logs = await client.get_transfer_logs(
            token.address, from_block, latest,
            to_topic_filter=padded_all,
        )
        await asyncio.sleep(0.3)
        withdrawals_logs = await client.get_transfer_logs(
            token.address, from_block, latest,
            from_topic_filter=padded_all,
        )
    except Exception as e:
        _log.warning("Onchain netflow RPC error for %s: %s", token.symbol, e)
        return None

    total_deposits = 0.0
    total_withdrawals = 0.0
    deposit_count = 0
    withdrawal_count = 0
    by_exchange: dict[str, dict[str, float]] = {}

    # Whale-class parallel aggregate.
    excluded_for_whale = set(wallet_to_exchange.keys())
    if non_whale_addresses:
        excluded_for_whale |= non_whale_addresses
    whale_deposits_usd = 0.0
    whale_withdrawals_usd = 0.0
    whale_addresses: set[str] = set()

    for entry in deposits_logs:
        from_addr, to_addr, amount = parse_transfer_log(entry, token.decimals)
        exchange = wallet_to_exchange.get(to_addr)
        if not exchange:
            continue
        # exchange-to-exchange shuffle — skip
        if from_addr in wallet_to_exchange:
            continue
        usd = amount * price_usd
        by_exchange.setdefault(exchange, {"deposits_usd": 0.0, "withdrawals_usd": 0.0})
        by_exchange[exchange]["deposits_usd"] += usd
        total_deposits += usd
        deposit_count += 1
        # Whale class: deposits where the SENDER is a meaningful entity.
        if usd >= whale_threshold_usd and from_addr not in excluded_for_whale:
            whale_deposits_usd += usd
            whale_addresses.add(from_addr)

    for entry in withdrawals_logs:
        from_addr, to_addr, amount = parse_transfer_log(entry, token.decimals)
        exchange = wallet_to_exchange.get(from_addr)
        if not exchange:
            continue
        if to_addr in wallet_to_exchange:
            continue
        usd = amount * price_usd
        by_exchange.setdefault(exchange, {"deposits_usd": 0.0, "withdrawals_usd": 0.0})
        by_exchange[exchange]["withdrawals_usd"] += usd
        total_withdrawals += usd
        withdrawal_count += 1
        # Whale class: withdrawals where the RECIPIENT is a meaningful entity.
        if usd >= whale_threshold_usd and to_addr not in excluded_for_whale:
            whale_withdrawals_usd += usd
            whale_addresses.add(to_addr)

    return {
        "token": token.symbol,
        "deposits_usd": total_deposits,
        "withdrawals_usd": total_withdrawals,
        "net_usd": total_withdrawals - total_deposits,
        "deposit_count": deposit_count,
        "withdrawal_count": withdrawal_count,
        "by_exchange": by_exchange,
        "whale_deposits_usd": whale_deposits_usd,
        "whale_withdrawals_usd": whale_withdrawals_usd,
        "whale_net_usd": whale_withdrawals_usd - whale_deposits_usd,
        "whale_unique_count": len(whale_addresses),
    }


async def compute_daily_netflow_history(
    client: EvmOnchainClient,
    token: TokenInfo,
    exchange_wallets: dict[str, list[str]],
    days: int,
    price_usd: Optional[float],
) -> list[dict]:
    """Per-day exchange netflow over the last `days` days, oldest first.

    Each list element: {"date": "YYYY-MM-DD", "deposits_usd", "withdrawals_usd", "net_usd"}.

    Sequential queries (2 per day) — `days × 2` calls total. Pacing is gentle to
    stay under public-RPC limits, so a 7-day fetch takes ~10–20 seconds.
    """
    if price_usd is None or price_usd <= 0:
        return []

    padded_all: list[str] = []
    wallet_to_exchange: dict[str, str] = {}
    for exchange, wallets in exchange_wallets.items():
        for w in wallets:
            wal = w.lower()
            padded_all.append(_pad_address(wal))
            wallet_to_exchange[wal] = exchange
    if not padded_all:
        return []

    try:
        latest = await client.get_block_number()
    except Exception as e:
        _log.warning("daily history block-number fetch failed for %s: %s", token.symbol, e)
        return []

    out: list[dict] = []
    # Use the client's own chain blocks_per_24h so this works for both ETH and BSC.
    blocks_per_day = client.chain.blocks_per_24h if hasattr(client, "chain") else _BLOCKS_PER_24H
    for day_offset in range(days, 0, -1):
        # day_offset=days → oldest day; day_offset=1 → today.
        from_block = max(0, latest - day_offset * blocks_per_day)
        to_block = max(0, latest - (day_offset - 1) * blocks_per_day)
        try:
            deposits_logs = await client.get_transfer_logs(
                token.address, from_block, to_block, to_topic_filter=padded_all,
            )
            await asyncio.sleep(0.3)
            withdrawals_logs = await client.get_transfer_logs(
                token.address, from_block, to_block, from_topic_filter=padded_all,
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            _log.warning("daily history RPC failed for %s day -%d: %s", token.symbol, day_offset, e)
            continue

        deposits_total = 0.0
        withdrawals_total = 0.0
        for log_entry in deposits_logs:
            from_addr, to_addr, amount = parse_transfer_log(log_entry, token.decimals)
            if from_addr in wallet_to_exchange:  # internal exchange shuffle
                continue
            if to_addr in wallet_to_exchange:
                deposits_total += amount * price_usd
        for log_entry in withdrawals_logs:
            from_addr, to_addr, amount = parse_transfer_log(log_entry, token.decimals)
            if to_addr in wallet_to_exchange:
                continue
            if from_addr in wallet_to_exchange:
                withdrawals_total += amount * price_usd

        # Date label = end of this 24h window (closest to "today").
        from datetime import datetime as _dt, timedelta, timezone as _tz
        day_label = (_dt.now(_tz.utc) - timedelta(days=day_offset - 1)).date().isoformat()

        out.append({
            "date": day_label,
            "deposits_usd": deposits_total,
            "withdrawals_usd": withdrawals_total,
            "net_usd": withdrawals_total - deposits_total,
        })
    return out


def classify_netflow_signal(net_usd: float, total_usd: float) -> tuple[str, str]:
    """(emoji, short label) based on net flow vs absolute volume.

    Heuristic:
      - |net| < $250K               → 🟡 Neutral (noise)
      - net > 0 (withdrawals win)   → 🟢 Accumulation off-exchange (bullish)
      - net < 0 (deposits win)      → 🔴 Distribution to exchange (bearish)
      - |net|/|total| > 0.5         → ★ strong-conviction modifier (bigger label)
    """
    if abs(net_usd) < 250_000:
        return ("🟡", "Neutral")
    strong = total_usd > 0 and (abs(net_usd) / total_usd) > 0.5
    if net_usd > 0:
        return ("🟢", "Strong accumulation" if strong else "Accumulation")
    return ("🔴", "Heavy distribution" if strong else "Distribution")


def filter_tokens_traded_on_exchanges(
    contracts_by_chain: dict[str, TokenInfo],
    binance_base_assets: Iterable[str],
    mexc_base_assets: Iterable[str],
) -> list[TokenInfo]:
    """Keep only tokens that are listed on Binance OR MEXC futures."""
    tradeable = {b.upper() for b in binance_base_assets} | {b.upper() for b in mexc_base_assets}
    return [t for sym, t in contracts_by_chain.items() if sym in tradeable]
