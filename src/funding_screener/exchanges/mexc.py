"""MEXC perpetual-futures public REST client."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import mexc_default_maker_fee, fees, http_client_kwargs, settings
from ..models import ContractInfo, FundingRow, Kline

_BASE = "https://contract.mexc.com"
_log = logging.getLogger(__name__)


def _parse_retry_after(value: str | None) -> int:
    if not value:
        return 60
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 60


class MexcClient:
    name = "MEXC"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(**http_client_kwargs())
        self._owns_http = http is None
        self._cooldown_until: float = 0.0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def is_cooled_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def cooldown_remaining_seconds(self) -> int:
        return max(0, int(self._cooldown_until - time.monotonic()))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if time.monotonic() < self._cooldown_until:
            remaining = int(self._cooldown_until - time.monotonic())
            raise RuntimeError(f"MEXC rate-limit cooldown active ({remaining}s remaining)")

        url = f"{_BASE}{path}"
        retries = int(settings()["http"]["max_retries"])
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await self._http.get(url, params=params)
                if r.status_code in (418, 429):
                    retry_after = _parse_retry_after(r.headers.get("Retry-After"))
                    self._cooldown_until = time.monotonic() + retry_after
                    _log.warning("MEXC %d on %s — backing off for %ds", r.status_code, path, retry_after)
                    r.raise_for_status()
                r.raise_for_status()
                payload = r.json()
                if not payload.get("success", True):
                    raise RuntimeError(f"MEXC error: {payload.get('message')}")
                return payload.get("data")
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                last_exc = exc
                if time.monotonic() < self._cooldown_until:
                    raise
                if attempt < retries:
                    await asyncio.sleep(0.4 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    # ---------- public methods ----------

    async def fetch_funding_rows(self) -> list[FundingRow]:
        rates, contracts = await asyncio.gather(
            self._get("/api/v1/contract/funding_rate"),
            self.fetch_contracts(),
        )
        contract_by_sym = {c.symbol: c for c in contracts}
        rows: list[FundingRow] = []
        for obj in rates or []:
            sym = obj["symbol"]
            ci = contract_by_sym.get(sym)
            if ci is None:
                continue
            rate_pct = float(obj.get("fundingRate", 0.0)) * 100.0
            interval_h = float(obj.get("collectCycle", 8))
            if interval_h <= 0:
                continue
            next_ts = obj.get("nextSettleTime")
            rows.append(
                FundingRow(
                    exchange=self.name,
                    symbol=sym,
                    base_asset=ci.base_asset,
                    quote_asset=ci.quote_asset,
                    rate_percent=rate_pct,
                    rate_8h_norm_percent=rate_pct * 8.0 / interval_h,
                    interval_hours=interval_h,
                    mark_price=None,
                    index_price=None,
                    next_funding_time=_ms_to_dt(next_ts) if next_ts else None,
                )
            )
        return rows

    async def fetch_contracts(self) -> list[ContractInfo]:
        data = await self._get("/api/v1/contract/detail")
        default_maker = mexc_default_maker_fee()
        default_taker = float(fees()["mexc"]["futures_taker"])
        out: list[ContractInfo] = []
        for s in data or []:
            if int(s.get("state", -1)) != 0:
                continue  # 0 = enabled
            base = s.get("baseCoin")
            quote = s.get("quoteCoin")
            if not base or not quote:
                continue
            maker = _safe_float(s.get("makerFeeRate"))
            taker = _safe_float(s.get("takerFeeRate"))
            out.append(
                ContractInfo(
                    exchange=self.name,
                    symbol=s["symbol"],
                    base_asset=base,
                    quote_asset=quote,
                    status="TRADING",
                    maker_fee_percent=(maker * 100.0) if maker is not None else default_maker,
                    taker_fee_percent=(taker * 100.0) if taker is not None else default_taker,
                )
            )
        return out

    async def fetch_daily_klines(self, symbol: str, days: int) -> list[Kline]:
        # MEXC kline returns vertically-stacked arrays under data.{time, open, high, low, close, vol, amount}
        params = {"interval": "Day1"}
        data = await self._get(f"/api/v1/contract/kline/{symbol}", params)
        if not data:
            return []
        times = data.get("time", [])
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        vols = data.get("vol", [])
        amts = data.get("amount", [])
        n = min(len(times), len(opens), len(highs), len(lows), len(closes), len(vols), len(amts))
        out: list[Kline] = []
        for i in range(max(0, n - days), n):
            out.append(
                Kline(
                    open_time=datetime.fromtimestamp(int(times[i]), tz=timezone.utc),
                    open=float(opens[i]),
                    high=float(highs[i]),
                    low=float(lows[i]),
                    close=float(closes[i]),
                    volume=float(vols[i]),
                    quote_volume=float(amts[i]),
                )
            )
        return out

    async def fetch_24h_quote_volume(self) -> dict[str, float]:
        data = await self._get("/api/v1/contract/ticker")
        out: dict[str, float] = {}
        for row in data or []:
            sym = row.get("symbol")
            if not sym:
                continue
            out[sym] = _safe_float(row.get("amount24")) or 0.0
        return out

    async def fetch_funding_rate_history(self, symbol: str, limit: int = 4) -> list[float]:
        """Return the last `limit` SETTLED funding rates for `symbol`, in percent,
        ordered MOST-RECENT FIRST (so index 0 = last settlement).

        MEXC's /api/v1/contract/funding_rate/history returns paged results; we
        ask for the first page only, sized to `limit`.
        """
        params = {"symbol": symbol, "page_num": 1, "page_size": limit}
        data = await self._get("/api/v1/contract/funding_rate/history", params)
        # MEXC returns {"resultList": [...], "totalCount": ...} under data.
        if not data:
            return []
        rows = data.get("resultList") if isinstance(data, dict) else data
        if not rows:
            return []
        out: list[float] = []
        # MEXC orders newest-first by default (settleTime desc).
        for entry in rows:
            try:
                out.append(float(entry["fundingRate"]) * 100.0)
            except (KeyError, TypeError, ValueError):
                continue
        return out


def _safe_float(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _ms_to_dt(ms: int | str) -> datetime:
    v = int(ms)
    # MEXC sometimes returns seconds, sometimes ms. Detect by magnitude.
    if v > 10_000_000_000:
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(v, tz=timezone.utc)
