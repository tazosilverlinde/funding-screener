"""Market-data clients for attaching USD market cap to screener rows.

Two clients available:

  - CoinPaprikaClient (DEFAULT, used by the background loop) — fetches all
    active coins in a single /v1/tickers call. Free tier allows ~25,000 calls
    per day with a 10 req/sec cap, so one call every 5 minutes is well under.

  - CoinGeckoClient (fallback / reference) — top-1000 via 4 paginated calls.
    Free tier rate-limits aggressively (~10-30/min); the paginated burst
    pattern frequently triggers HTTP 429, so we don't use this by default.

Both return `dict[SYMBOL_UPPER, market_cap_usd]`. First-hit-wins on duplicate
tickers, relying on rank-ascending ordering to pick the dominant project.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import http_client_kwargs, settings

_BASE = "https://api.coingecko.com/api/v3"
_PAPRIKA = "https://api.coinpaprika.com/v1"


class CoinPaprikaClient:
    """Single-endpoint fetcher for USD market caps. Free public tier, no key."""

    name = "CoinPaprika"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        # /v1/tickers can return ~5MB; override timeout to be a bit more generous.
        if http is None:
            kwargs = http_client_kwargs()
            kwargs["timeout"] = httpx.Timeout(45.0, connect=5.0)
            self._http = httpx.AsyncClient(**kwargs)
        else:
            self._http = http
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def fetch_market_caps_top_n(self, top_n: int = 1000) -> dict[str, float]:
        """One call returns all active coins (rank-ascending). Slice to top-N,
        then map upper-case symbol → USD market cap (first hit wins).
        """
        url = f"{_PAPRIKA}/tickers"
        retries = int(settings()["http"]["max_retries"])
        last_exc: Exception | None = None
        data: Any = None
        for attempt in range(retries + 1):
            try:
                r = await self._http.get(url)
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

        # CoinPaprika returns coins sorted by rank ASC, so iteration order is
        # already what we want for first-hit-wins on duplicate tickers.
        out: dict[str, float] = {}
        for entry in (data or [])[:top_n]:
            sym = (entry.get("symbol") or "").upper()
            quotes = entry.get("quotes") or {}
            usd = quotes.get("USD") or {}
            mcap = usd.get("market_cap")
            if not sym or mcap is None:
                continue
            if sym in out:
                continue
            try:
                out[sym] = float(mcap)
            except (TypeError, ValueError):
                continue
        return out


class CoinGeckoClient:
    name = "CoinGecko"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(**http_client_kwargs())
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{_BASE}{path}"
        retries = int(settings()["http"]["max_retries"])
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await self._http.get(url, params=params)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    async def fetch_market_caps_top_n(self, top_n: int = 1000) -> dict[str, float]:
        """Return {SYMBOL_UPPER: market_cap_usd} for the top-N coins by market cap.

        Serial fetch with a 1.2s gap between pages — parallel pages trigger CoinGecko's
        free-tier 429 rate limit. First-hit-wins on duplicate symbols (CoinGecko's
        market-cap-desc ordering surfaces the dominant project for ambiguous tickers).
        """
        per_page = 250
        pages = (top_n + per_page - 1) // per_page

        out: dict[str, float] = {}
        for page_no in range(1, pages + 1):
            try:
                page = await self._get(
                    "/coins/markets",
                    {
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": per_page,
                        "page": page_no,
                        "sparkline": "false",
                    },
                )
            except Exception:
                # If a single page fails (rate-limit, transient network), skip it
                # and keep whatever we already have.
                continue
            for row in page or []:
                sym = (row.get("symbol") or "").upper()
                mcap = row.get("market_cap")
                if not sym or mcap is None:
                    continue
                if sym in out:
                    continue
                try:
                    out[sym] = float(mcap)
                except (TypeError, ValueError):
                    continue
            # Be polite between pages — CoinGecko free tier rate-limits aggressively.
            if page_no < pages:
                await asyncio.sleep(2.5)
        return out
