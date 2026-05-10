"""Binance USD-M futures public REST client."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import binance_maker_fee_for, binance_taker_fee_for, fees, http_client_kwargs, settings
from ..models import ContractInfo, FundingRow, Kline

_BASE = "https://fapi.binance.com"
_log = logging.getLogger(__name__)


def _parse_retry_after(value: str | None) -> int:
    """Binance returns Retry-After as integer seconds. Default to 60 if missing."""
    if not value:
        return 60
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 60


class BinanceClient:
    """Public REST client.

    Implements a short-circuit when Binance returns HTTP 418 ("I'm a teapot",
    their IP-ban code) or 429: we honour the Retry-After header and refuse
    further requests until the cooldown expires, so the fast loop doesn't keep
    hammering a banned IP and lengthening the ban.
    """

    name = "Binance"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        # Use the shared httpx config: split connect/read timeouts + pooled connections.
        self._http = http or httpx.AsyncClient(**http_client_kwargs())
        self._owns_http = http is None
        self._cooldown_until: float = 0.0  # monotonic seconds

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def is_cooled_down(self) -> bool:
        """True while a 418/429/451 cooldown is active (skip calling)."""
        return time.monotonic() < self._cooldown_until

    def cooldown_remaining_seconds(self) -> int:
        return max(0, int(self._cooldown_until - time.monotonic()))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if time.monotonic() < self._cooldown_until:
            remaining = int(self._cooldown_until - time.monotonic())
            raise RuntimeError(f"Binance rate-limit cooldown active ({remaining}s remaining)")

        url = f"{_BASE}{path}"
        retries = int(settings()["http"]["max_retries"])
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await self._http.get(url, params=params)
                # Don't retry on rate-limit / geo-block — honour Retry-After and propagate.
                # 451 = Unavailable For Legal Reasons (geo-block); set a 24h cooldown
                # since geo-blocks don't lift on a short timer.
                if r.status_code in (418, 429, 451):
                    if r.status_code == 451:
                        retry_after = 24 * 60 * 60
                    else:
                        retry_after = _parse_retry_after(r.headers.get("Retry-After"))
                    self._cooldown_until = time.monotonic() + retry_after
                    _log.warning(
                        "Binance %d on %s — backing off for %ds",
                        r.status_code, path, retry_after,
                    )
                    r.raise_for_status()
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if time.monotonic() < self._cooldown_until:
                    raise
                if attempt < retries:
                    await asyncio.sleep(0.4 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    # ---------- public methods ----------

    async def fetch_funding_rows(self) -> list[FundingRow]:
        """Build FundingRow per perp using premiumIndex + fundingInfo."""
        premium, intervals = await asyncio.gather(
            self._get("/fapi/v1/premiumIndex"),
            self._get("/fapi/v1/fundingInfo"),
        )
        # fundingInfo only includes symbols whose interval != default 8.
        interval_map = {row["symbol"]: int(row["fundingIntervalHours"]) for row in intervals}

        rows: list[FundingRow] = []
        for obj in premium:
            sym = obj["symbol"]
            quote = _binance_quote_for(sym)
            if quote is None:
                continue
            rate_pct = float(obj.get("lastFundingRate", 0.0)) * 100.0
            interval_h = float(interval_map.get(sym, 8))
            if interval_h <= 0:
                continue
            next_ts = obj.get("nextFundingTime")
            rows.append(
                FundingRow(
                    exchange=self.name,
                    symbol=sym,
                    base_asset=sym[: -len(quote)],
                    quote_asset=quote,
                    rate_percent=rate_pct,
                    rate_8h_norm_percent=rate_pct * 8.0 / interval_h,
                    interval_hours=interval_h,
                    mark_price=_safe_float(obj.get("markPrice")),
                    index_price=_safe_float(obj.get("indexPrice")),
                    next_funding_time=_ms_to_dt(next_ts) if next_ts else None,
                )
            )
        return rows

    async def fetch_contracts(self) -> list[ContractInfo]:
        info = await self._get("/fapi/v1/exchangeInfo")
        out: list[ContractInfo] = []
        for s in info.get("symbols", []):
            if s.get("contractType") != "PERPETUAL":
                continue
            quote = s.get("quoteAsset")
            if quote not in ("USDT", "USDC"):
                continue
            # Skip non-TRADING contracts. SETTLING / BREAK / HALT / PENDING_TRADING /
            # PRE_DELIVERING are all stale or pre-launch — including them surfaces
            # ghost contracts (e.g. ORBSUSDT) on the screener pages.
            status = s.get("status", "UNKNOWN")
            if status != "TRADING":
                continue
            symbol = s["symbol"]
            out.append(
                ContractInfo(
                    exchange=self.name,
                    symbol=symbol,
                    base_asset=s["baseAsset"],
                    quote_asset=quote,
                    status=status,
                    maker_fee_percent=binance_maker_fee_for(symbol, quote),
                    taker_fee_percent=binance_taker_fee_for(symbol, quote),
                )
            )
        return out

    async def fetch_daily_klines(self, symbol: str, days: int) -> list[Kline]:
        params = {"symbol": symbol, "interval": "1d", "limit": days}
        raw = await self._get("/fapi/v1/klines", params)
        return [_kline_from_binance(k) for k in raw]

    async def fetch_24h_quote_volume(self) -> dict[str, float]:
        raw = await self._get("/fapi/v1/ticker/24hr")
        return {row["symbol"]: float(row.get("quoteVolume", 0.0)) for row in raw}

    async def fetch_funding_rate_history(self, symbol: str, limit: int = 4) -> list[float]:
        """Return the last `limit` SETTLED funding rates for `symbol`, in percent,
        ordered MOST-RECENT FIRST (so index 0 = last settlement).

        These are the rates that already paid/received — distinct from the
        upcoming `lastFundingRate` we get from /premiumIndex which is in-progress.
        """
        params = {"symbol": symbol, "limit": limit}
        raw = await self._get("/fapi/v1/fundingRate", params)
        # API returns oldest -> newest. Reverse so callers can index 0 = most recent.
        out: list[float] = []
        for entry in reversed(raw or []):
            try:
                out.append(float(entry["fundingRate"]) * 100.0)
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def fetch_open_interest_history(
        self, symbol: str, period: str = "1h", limit: int = 24
    ) -> list[dict]:
        """Returns OI samples ordered oldest -> newest.
        Each item: {timestamp, sumOpenInterest (base units), sumOpenInterestValue (USD)}.
        """
        params = {"symbol": symbol, "period": period, "limit": limit}
        raw = await self._get("/futures/data/openInterestHist", params)
        return raw or []

    async def fetch_long_short_ratio_global(
        self, symbol: str, period: str = "1h", limit: int = 1
    ) -> Optional[float]:
        """Most recent global account long/short ratio (all retail accounts)."""
        params = {"symbol": symbol, "period": period, "limit": limit}
        raw = await self._get("/futures/data/globalLongShortAccountRatio", params)
        if not raw:
            return None
        try:
            return float(raw[-1].get("longShortRatio", 0))
        except (TypeError, ValueError):
            return None

    async def fetch_long_short_ratio_top(
        self, symbol: str, period: str = "1h", limit: int = 1
    ) -> Optional[float]:
        """Most recent TOP-TRADER account long/short ratio (top 20% by collateral)."""
        params = {"symbol": symbol, "period": period, "limit": limit}
        raw = await self._get("/futures/data/topLongShortAccountRatio", params)
        if not raw:
            return None
        try:
            return float(raw[-1].get("longShortRatio", 0))
        except (TypeError, ValueError):
            return None

    async def fetch_long_short_ratio_global_history(
        self, symbol: str, period: str = "1h", limit: int = 24,
    ) -> list[dict]:
        """Full L/S ratio history (oldest → newest), each item:
        {timestamp, longShortRatio, longAccount, shortAccount}.

        Used by the Symbol Detail page to render a 24h trend chart rather than
        showing only a single point-in-time value.
        """
        params = {"symbol": symbol, "period": period, "limit": limit}
        raw = await self._get("/futures/data/globalLongShortAccountRatio", params)
        return raw or []

    async def fetch_long_short_ratio_top_history(
        self, symbol: str, period: str = "1h", limit: int = 24,
    ) -> list[dict]:
        """Top-trader L/S ratio history (oldest → newest)."""
        params = {"symbol": symbol, "period": period, "limit": limit}
        raw = await self._get("/futures/data/topLongShortAccountRatio", params)
        return raw or []


def _binance_quote_for(symbol: str) -> str | None:
    for q in ("USDT", "USDC"):
        if symbol.endswith(q):
            return q
    return None


def _safe_float(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _ms_to_dt(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def _kline_from_binance(k: list[Any]) -> Kline:
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]
    return Kline(
        open_time=_ms_to_dt(k[0]),
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=float(k[5]),
        quote_volume=float(k[7]),
    )
