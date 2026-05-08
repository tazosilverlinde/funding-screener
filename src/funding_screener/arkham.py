"""Arkham Intelligence client — whale tracking + exchange netflow.

Free public API (signup required at https://intel.arkm.com to get a key).
The key is read from the ARKHAM_API_KEY env var. If missing, every method
returns empty results, so the rest of the app degrades gracefully and the
whale-flows page just shows a "configure API key" message.

This module is intentionally minimal — we hit two endpoints:
  - /intelligence/transfers: large transfers in/out of labeled exchanges
  - /intelligence/balances:  per-token holdings of labeled smart-money wallets

Real usage will reveal which fields/filters work best; treat this as a
foundation rather than a finished product.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from .config import settings

_BASE = "https://api.arkhamintelligence.com"
_log = logging.getLogger(__name__)


def is_arkham_configured() -> bool:
    return bool(os.getenv("ARKHAM_API_KEY", "").strip())


def get_arkham_api_key() -> Optional[str]:
    key = os.getenv("ARKHAM_API_KEY", "").strip()
    return key or None


class ArkhamClient:
    """Minimal Arkham wrapper. Returns empty/None if no API key is configured."""

    name = "Arkham"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        timeout = float(settings()["http"]["timeout_seconds"])
        self._http = http or httpx.AsyncClient(timeout=max(timeout, 30.0))
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def configured(self) -> bool:
        return is_arkham_configured()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = get_arkham_api_key()
        if not key:
            return None
        url = f"{_BASE}{path}"
        retries = int(settings()["http"]["max_retries"])
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await self._http.get(
                    url,
                    params=params,
                    headers={"API-Key": key},
                )
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
        if last_exc is not None:
            _log.warning("Arkham request failed for %s: %s", path, last_exc)
        return None

    async def fetch_exchange_netflow(self, token_symbol: str, hours: int = 24) -> Optional[dict]:
        """Aggregate exchange in/outflow for `token_symbol` over the last `hours`.

        Returns: {"deposits_usd": X, "withdrawals_usd": Y, "net_usd": Y - X, ...}
        or None if API not configured / call fails.
        """
        # NOTE: This is the intended shape. Adjust path/params once we have a working
        # API key and can confirm the actual Arkham endpoint and response format.
        if not self.configured():
            return None
        data = await self._get(
            "/intelligence/transfers",
            {"asset": token_symbol, "hours": hours, "category": "exchange"},
        )
        if not data:
            return None
        # Best-effort parser — endpoint shape will be confirmed on first live call.
        deposits = float(data.get("deposits_usd", 0) or 0)
        withdrawals = float(data.get("withdrawals_usd", 0) or 0)
        return {
            "token": token_symbol,
            "hours": hours,
            "deposits_usd": deposits,
            "withdrawals_usd": withdrawals,
            "net_usd": withdrawals - deposits,
            "transfer_count": int(data.get("transfer_count", 0) or 0),
        }

    async def fetch_smart_money_top_buys(self, hours: int = 24, limit: int = 20) -> list[dict]:
        """Tokens most net-bought by Arkham-labeled 'smart money' wallets in last N hours.

        Returns list of {"token": "...", "net_buy_usd": X, "wallet_count": N}.
        """
        if not self.configured():
            return []
        data = await self._get(
            "/intelligence/smart-money-flows",
            {"hours": hours, "limit": limit, "direction": "buy"},
        )
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for entry in data:
            try:
                out.append({
                    "token": entry.get("symbol") or entry.get("token") or "",
                    "net_buy_usd": float(entry.get("net_buy_usd", 0) or 0),
                    "wallet_count": int(entry.get("wallet_count", 0) or 0),
                })
            except (TypeError, ValueError):
                continue
        return out
