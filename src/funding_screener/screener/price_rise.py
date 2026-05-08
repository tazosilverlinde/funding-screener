"""Price-rise screener: pure function over cached klines + volume map + funding map.

Inputs:
  contracts: list[ContractInfo] for the universe
  klines_by_symbol: dict[str, list[Kline]] — populated by the background slow loop
  vol_map: dict[str, float] — 24h quote volume by symbol
  funding_rows: per-symbol funding (used to attach 8h-normalized funding rate)
  threshold_percent: filter; flag if any window's pct change exceeds this
  windows_days: e.g. [1, 7, 30]
  min_24h_quote_volume: drop symbols below this 24h quote volume

Each output row carries: 1d/7d/30d returns, 8h funding rate, all-time-high (over the
cached kline horizon), 24h quote volume in millions, and per-day quote volumes for
today / yesterday / day-before-yesterday.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..models import ContractInfo, FundingRow, Kline, PriceRiseRow


def screen_price_rise(
    contracts: Iterable[ContractInfo],
    klines_by_symbol: dict[str, list[Kline]],
    vol_map: dict[str, float],
    funding_rows: Iterable[FundingRow],
    market_caps_usd: dict[str, float],
    threshold_percent: float,
    windows_days: list[int],
    min_24h_quote_volume: float,
) -> list[PriceRiseRow]:
    funding_by_symbol = {r.symbol: r for r in funding_rows}
    out: list[PriceRiseRow] = []
    for c in contracts:
        if c.status != "TRADING":
            continue
        v = vol_map.get(c.symbol, 0.0)
        if min_24h_quote_volume > 0 and v < min_24h_quote_volume:
            continue
        klines = klines_by_symbol.get(c.symbol)
        if not klines:
            continue
        mcap_usd = market_caps_usd.get(c.base_asset.upper())
        row = _row_from_klines(c, klines, windows_days, v, funding_by_symbol.get(c.symbol), mcap_usd)
        if row is not None and row.max_pct > threshold_percent:
            out.append(row)
    out.sort(key=lambda r: r.max_pct, reverse=True)
    return out


def _row_from_klines(
    c: ContractInfo,
    klines: list[Kline],
    windows: list[int],
    quote_volume_24h: float | None,
    funding: FundingRow | None,
    market_cap_usd: float | None,
) -> PriceRiseRow | None:
    if not klines:
        return None
    closes = [k.close for k in klines]
    current = closes[-1]
    if current <= 0:
        return None

    pct_by_window: dict[int, float | None] = {}
    for w in windows:
        idx = len(closes) - 1 - w
        if idx < 0 or closes[idx] <= 0:
            pct_by_window[w] = None
        else:
            pct_by_window[w] = (current / closes[idx] - 1.0) * 100.0

    valid = [(w, p) for w, p in pct_by_window.items() if p is not None]
    if not valid:
        return None
    max_window, max_pct = max(valid, key=lambda x: x[1])

    ath = max((k.high for k in klines), default=None)

    return PriceRiseRow(
        exchange=c.exchange,
        symbol=c.symbol,
        base_asset=c.base_asset,
        quote_asset=c.quote_asset,
        current_price=current,
        pct_1d=pct_by_window.get(1),
        pct_7d=pct_by_window.get(7),
        pct_30d=pct_by_window.get(30),
        funding_rate_8h_norm_percent=funding.rate_8h_norm_percent if funding else None,
        ath_price=ath,
        market_cap_millions=_to_millions(market_cap_usd),
        quote_volume_24h_millions=_to_millions(quote_volume_24h),
        volume_today_millions=_kline_quote_volume_M(klines, -1),
        volume_yesterday_millions=_kline_quote_volume_M(klines, -2),
        volume_day_before_millions=_kline_quote_volume_M(klines, -3),
        max_pct=max_pct,
        max_window_days=max_window,
    )


def _to_millions(v: float | None) -> Optional[float]:
    if v is None:
        return None
    return v / 1_000_000.0


def _kline_quote_volume_M(klines: list[Kline], idx: int) -> Optional[float]:
    if abs(idx) > len(klines):
        return None
    return klines[idx].quote_volume / 1_000_000.0
