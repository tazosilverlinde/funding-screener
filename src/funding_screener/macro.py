"""Macro-context fetchers — stablecoin supply, BTC dominance, etc.

These are global signals (not per-symbol), refreshed slowly because they move
slowly. Used by the landing-page banner to give a quick read on whether
liquidity is entering or leaving crypto.

Free public APIs only — DefiLlama (stablecoins) and CoinPaprika (BTC dominance,
already integrated via market_data.py).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from .config import settings


_DEFI_LLAMA = "https://stablecoins.llama.fi"


class DefiLlamaClient:
    """Minimal DefiLlama wrapper for stablecoin macro flow data.

    Free public API, no auth, no key. Single endpoint we use:
      GET /stablecoins?includePrices=true
    Returns one entry per stablecoin with circulating supply now and 1d/7d ago.
    """

    name = "DefiLlama"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        timeout = float(settings()["http"]["timeout_seconds"])
        self._http = http or httpx.AsyncClient(timeout=max(timeout, 30.0))
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def fetch_stablecoin_supply(self) -> dict[str, dict[str, float]]:
        """Returns:
            {
              "USDT": {"now": 168_000_000_000, "d1": 167_500_000_000, "d7": 165_000_000_000,
                       "change_24h_pct": 0.30, "change_7d_pct": 1.81},
              "USDC": {...},
              "TOTAL": {...},  # all stablecoins combined
            }
        """
        url = f"{_DEFI_LLAMA}/stablecoins"
        params = {"includePrices": "true"}
        retries = int(settings()["http"]["max_retries"])
        last_exc: Exception | None = None
        data: Any = None
        for attempt in range(retries + 1):
            try:
                r = await self._http.get(url, params=params)
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(2.0 * (attempt + 1))
        if data is None:
            if last_exc is not None:
                raise last_exc
            return {}

        # data["peggedAssets"] is a list of stablecoins.
        out: dict[str, dict[str, float]] = {}
        total_now = 0.0
        total_d1 = 0.0
        total_d7 = 0.0
        for s in data.get("peggedAssets") or []:
            symbol = (s.get("symbol") or "").upper()
            now = _safe_float(_dig(s, "circulating", "peggedUSD"))
            d1 = _safe_float(_dig(s, "circulatingPrevDay", "peggedUSD"))
            d7 = _safe_float(_dig(s, "circulatingPrevWeek", "peggedUSD"))
            if now is None:
                continue
            if symbol in ("USDT", "USDC", "DAI", "FDUSD", "USDE"):
                out[symbol] = {
                    "now": now,
                    "d1": d1 if d1 is not None else now,
                    "d7": d7 if d7 is not None else now,
                    "change_24h_pct": _pct_change(d1, now),
                    "change_7d_pct": _pct_change(d7, now),
                }
            total_now += now
            if d1 is not None:
                total_d1 += d1
            if d7 is not None:
                total_d7 += d7
        out["TOTAL"] = {
            "now": total_now,
            "d1": total_d1,
            "d7": total_d7,
            "change_24h_pct": _pct_change(total_d1, total_now),
            "change_7d_pct": _pct_change(total_d7, total_now),
        }
        return out


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dig(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _pct_change(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if old is None or new is None or old <= 0:
        return None
    return (new / old - 1.0) * 100.0
