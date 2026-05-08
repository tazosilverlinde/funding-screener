"""Pydantic data models. All immutable; use `model_copy(update=...)` to mutate."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class FundingRow(_Frozen):
    """One funding-rate row for a single perp contract."""

    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    rate_percent: float           # signed, in % (e.g. 0.01 = 0.01%, NOT 1%)
    rate_8h_norm_percent: float   # rate * 8 / interval_hours
    interval_hours: float
    mark_price: Optional[float]
    index_price: Optional[float]
    next_funding_time: Optional[datetime]


class ContractInfo(_Frozen):
    """Per-contract metadata (status, fees)."""

    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    status: str                   # "TRADING" or equivalent
    maker_fee_percent: float
    taker_fee_percent: float


class Kline(_Frozen):
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float                 # base-asset volume
    quote_volume: float           # quote-asset volume


class ArbRow(_Frozen):
    """Output row for the USDT/USDC funding-arb screener."""

    exchange: str
    base_asset: str
    usdt_symbol: str
    usdc_symbol: str
    rate_usdt_percent: float      # signed, raw next-funding rate
    rate_usdc_percent: float
    interval_hours: float
    diff_abs_percent: float       # abs(rate_usdt - rate_usdc), per period
    fees_total_percent: float     # 4 * maker_fee
    net_percent: float            # diff_abs - fees_total, per period
    net_8h_norm_percent: float    # net normalized to 8h
    long_leg_symbol: str          # leg to LONG (lower-rate side)
    short_leg_symbol: str         # leg to SHORT (higher-rate side)
    next_funding_time_usdt: Optional[datetime]
    next_funding_time_usdc: Optional[datetime]
    funding_time_skew_minutes: Optional[float]


class EnrichmentData(_Frozen):
    """Per-symbol enrichment fetched on a slower cadence.

    The enrichment loop populates this for the top-N flagged symbols only,
    so most contracts in the universe don't have enrichment — that's fine.
    """

    exchange: str
    symbol: str
    prev_funding_rates_percent: list[float]   # most-recent-first; up to 3-4 settled rates
    funding_streak_count: int                  # consecutive same-sign at the front
    funding_streak_direction: Optional[str]    # "pos" | "neg" | None
    mark_index_spread_percent: Optional[float] # signed: mark - index, as % of index
    # Binance-only on-chain-ish enrichment (MEXC public API doesn't expose these):
    oi_usd: Optional[float] = None             # current open-interest in USD
    oi_change_1h_pct: Optional[float] = None
    oi_change_24h_pct: Optional[float] = None
    ls_ratio_global: Optional[float] = None    # retail accounts long/short
    ls_ratio_top: Optional[float] = None       # top traders (top-20% by collateral)
    fetched_at: datetime


class CombinedFundingRow(_Frozen):
    """One row per (base_asset, quote_asset), with Binance and MEXC funding side by side.

    Either side may be `None` if that exchange doesn't list this base/quote combo.
    Quote is always USDT or USDC — we surface BOTH quotes so e.g. `WIFUSDT` and
    `WIFUSDC` each get their own row when they have high funding.
    """

    base_asset: str
    quote_asset: str  # "USDT" or "USDC"

    binance_symbol: Optional[str]
    binance_rate_percent: Optional[float]
    binance_rate_8h_norm_percent: Optional[float]
    binance_interval_hours: Optional[float]
    binance_next_funding_time: Optional[datetime]
    binance_maker_fee_percent: Optional[float]
    binance_mark_index_spread_percent: Optional[float]
    binance_funding_streak: Optional[str]                  # display string, e.g. "3↑"
    binance_volume_24h_millions: Optional[float]           # 24h quote volume in M USDT/USDC
    binance_oi_change_24h_pct: Optional[float] = None      # open interest 24h Δ%
    binance_ls_ratio_global: Optional[float] = None        # retail account long/short
    binance_ls_ratio_top: Optional[float] = None           # top-trader long/short
    realized_vol_30d_pct: Optional[float] = None           # annualized 30d vol from daily klines
    funding_per_vol: Optional[float] = None                # signed: funding_8h_norm / (vol/100)

    mexc_symbol: Optional[str]
    mexc_rate_percent: Optional[float]
    mexc_rate_8h_norm_percent: Optional[float]
    mexc_interval_hours: Optional[float]
    mexc_next_funding_time: Optional[datetime]
    mexc_maker_fee_percent: Optional[float]
    mexc_funding_streak: Optional[str]
    mexc_volume_24h_millions: Optional[float]

    max_abs_8h_norm_percent: float  # used for sort + threshold (kept internally; not displayed)
    spread_8h_norm_percent: Optional[float]  # binance - mexc when both present

    # Composite signal — the headline label.
    signal_emoji: str
    signal_short: str
    signal_breakdown: str
    signal_color: str

    # Numeric composite score (signed [-100, +100]; positive = long bias).
    composite_score: Optional[int] = None
    composite_emoji: Optional[str] = None
    composite_short: Optional[str] = None
    composite_breakdown: Optional[str] = None  # newline-joined breakdown text


class PriceRiseRow(_Frozen):
    """Output row for the close-to-close price-rise screener."""

    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    current_price: float
    pct_1d: Optional[float]
    pct_7d: Optional[float]
    pct_30d: Optional[float]
    funding_rate_8h_norm_percent: Optional[float]   # joined from funding cache by symbol
    ath_price: Optional[float]                       # highest `high` seen in cached klines
    market_cap_millions: Optional[float]             # USD market cap from CoinGecko / 1e6
    quote_volume_24h_millions: Optional[float]       # internal — kept for the volume filter
    volume_today_millions: Optional[float]           # newest daily kline's quote_volume / 1e6
    volume_yesterday_millions: Optional[float]
    volume_day_before_millions: Optional[float]
    max_pct: float                                   # internal sort key — not displayed
    max_window_days: int                             # internal — not displayed
