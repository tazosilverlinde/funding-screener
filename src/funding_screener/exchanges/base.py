"""Common interface for exchange clients. Public REST only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import ContractInfo, FundingRow, Kline


@runtime_checkable
class ExchangeClient(Protocol):
    name: str

    async def fetch_funding_rows(self) -> list[FundingRow]:
        """Return current funding row per perp contract (signed rate, 8h-normalized)."""

    async def fetch_contracts(self) -> list[ContractInfo]:
        """Return per-contract metadata, including maker/taker fees if exchange exposes them."""

    async def fetch_daily_klines(self, symbol: str, days: int) -> list[Kline]:
        """Return up to `days` daily klines (most recent last)."""

    async def fetch_24h_quote_volume(self) -> dict[str, float]:
        """Return {symbol: quote_volume_24h} for all perp contracts."""

    async def aclose(self) -> None: ...
