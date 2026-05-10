"""Cross-exchange funding-rate arbitrage screener (Round 23).

Same math as the USDT/USDC screener but the two legs are on DIFFERENT exchanges
instead of different quote currencies on the same exchange:

    net_8h = |bnb_rate_8h - mxc_rate_8h| - 4 * avg_maker_fee_8h

Long the LOWER-funding side, short the HIGHER-funding side. The trader pays
maker fees on both legs (open + close = 2 transactions per leg = 4 total).

The opportunity is structural: Binance and MEXC are independent pools with
different liquidity profiles, so funding rates can diverge meaningfully on
the same base asset. Pure arbitrage closes the gap; until execution catches
up, the spread is the carry.

Caveats / limitations
=====================
- Pure function — no I/O, no state. Deterministic given inputs.
- Requires BOTH sides to be actively trading (status == "TRADING").
- Quote must match — only compares USDT-vs-USDT or USDC-vs-USDC. Cross-quote
  + cross-exchange (Binance USDC vs MEXC USDT) introduces FX risk and is
  intentionally out of scope.
- Funding-time skew: the two exchanges may settle at different cadences (8h
  vs 4h). We expose the skew so users can decide whether to take it.
- Fees we use are MAKER (post-only). Taker execution would change the math.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from ..models import ContractInfo, CrossExchangeArbRow, FundingRow


def _index_by_base_quote(rows: Iterable[FundingRow]) -> dict[tuple[str, str], FundingRow]:
    out: dict[tuple[str, str], FundingRow] = {}
    for r in rows:
        if r.quote_asset not in ("USDT", "USDC"):
            continue
        out[(r.base_asset.upper(), r.quote_asset)] = r
    return out


def _index_contracts(contracts: Iterable[ContractInfo]) -> dict[str, ContractInfo]:
    return {c.symbol: c for c in contracts}


def screen_cross_exchange_arb(
    binance_rows: Iterable[FundingRow],
    mexc_rows: Iterable[FundingRow],
    binance_contracts: Iterable[ContractInfo],
    mexc_contracts: Iterable[ContractInfo],
    binance_volumes: Optional[dict[str, float]] = None,
    mexc_volumes: Optional[dict[str, float]] = None,
    min_volume_usd_per_side: float = 1_000_000,
) -> list[CrossExchangeArbRow]:
    """Screen for profitable Binance-vs-MEXC funding spreads on matching pairs.

    Returns rows where 8h-normalized spread minus total maker fees > 0,
    sorted by net descending.
    """
    bnb_by_bq = _index_by_base_quote(binance_rows)
    mxc_by_bq = _index_by_base_quote(mexc_rows)
    bnb_contracts_by_sym = _index_contracts(binance_contracts)
    mxc_contracts_by_sym = _index_contracts(mexc_contracts)
    binance_volumes = binance_volumes or {}
    mexc_volumes = mexc_volumes or {}

    common_keys = set(bnb_by_bq.keys()) & set(mxc_by_bq.keys())
    out: list[CrossExchangeArbRow] = []

    for base, quote in common_keys:
        b = bnb_by_bq[(base, quote)]
        m = mxc_by_bq[(base, quote)]
        b_c = bnb_contracts_by_sym.get(b.symbol)
        m_c = mxc_contracts_by_sym.get(m.symbol)
        if b_c is None or m_c is None:
            continue
        if b_c.status != "TRADING" or m_c.status != "TRADING":
            continue

        # Liquidity floor — surface only pairs an actual user can trade.
        b_vol = binance_volumes.get(b.symbol, 0.0)
        m_vol = mexc_volumes.get(m.symbol, 0.0)
        if min_volume_usd_per_side > 0:
            if b_vol < min_volume_usd_per_side or m_vol < min_volume_usd_per_side:
                continue

        # 8h-normalised funding on each side.
        b_8h = b.rate_8h_norm_percent
        m_8h = m.rate_8h_norm_percent
        diff_abs_8h = abs(b_8h - m_8h)
        # Total maker fees: 2 (open + close) on each leg = 4 fees.
        fees_total = 2.0 * b_c.maker_fee_percent + 2.0 * m_c.maker_fee_percent
        # 8h-normalised arb requires net>0 *after* fees over the chosen window.
        # Maker fees are charged ONCE per round trip regardless of funding cadence,
        # so we don't normalise them — they're a fixed cost. The funding-rate spread
        # is what scales with time. Our `net_8h_norm` answers: if you held this
        # position open for 8 hours (one funding cycle on the typical pair), what's
        # the net funding income minus the fees you'd pay to enter+exit?
        net_8h = diff_abs_8h - fees_total
        if net_8h <= 0:
            continue

        if b.rate_8h_norm_percent < m.rate_8h_norm_percent:
            long_exchange, long_symbol = "Binance", b.symbol
            short_exchange, short_symbol = "MEXC", m.symbol
        else:
            long_exchange, long_symbol = "MEXC", m.symbol
            short_exchange, short_symbol = "Binance", b.symbol

        # Funding-time skew between the two exchanges (in minutes).
        skew_min: Optional[float] = None
        if b.next_funding_time and m.next_funding_time:
            skew_min = abs(
                (b.next_funding_time - m.next_funding_time).total_seconds() / 60.0
            )

        # Annualised yield estimate: net per 8h × 3 cycles per day × 365 days.
        # Real-world this gets eaten by spread compression; treat as upper bound.
        apr_pct = net_8h * 3.0 * 365.0

        out.append(CrossExchangeArbRow(
            base_asset=base,
            quote_asset=quote,
            binance_symbol=b.symbol,
            mexc_symbol=m.symbol,
            binance_rate_8h_norm_percent=b_8h,
            mexc_rate_8h_norm_percent=m_8h,
            diff_8h_norm_percent=diff_abs_8h,
            binance_maker_fee_percent=b_c.maker_fee_percent,
            mexc_maker_fee_percent=m_c.maker_fee_percent,
            fees_total_percent=fees_total,
            net_8h_norm_percent=net_8h,
            apr_estimate_percent=apr_pct,
            long_exchange=long_exchange,
            long_symbol=long_symbol,
            short_exchange=short_exchange,
            short_symbol=short_symbol,
            binance_next_funding_time=b.next_funding_time,
            mexc_next_funding_time=m.next_funding_time,
            funding_time_skew_minutes=skew_min,
            binance_volume_24h_usd=b_vol,
            mexc_volume_24h_usd=m_vol,
        ))

    out.sort(key=lambda r: r.net_8h_norm_percent, reverse=True)
    return out
