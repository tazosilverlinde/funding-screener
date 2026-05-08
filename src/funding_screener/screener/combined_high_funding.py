"""Combined high-funding screener: Binance + MEXC, USDT and USDC pairs, side by side.

Each output row represents a single (base_asset, quote_asset) combination. So
WIF appears as up to TWO rows: one for WIFUSDT and one for WIFUSDC, since both
have independent funding rates. A row is emitted when at least one exchange's
8h-normalized rate exceeds the threshold for that base/quote pair.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..models import CombinedFundingRow, ContractInfo, EnrichmentData, FundingRow
from ..signals import classify_signal

_QUOTES = ("USDT", "USDC")


def _index_by_base_quote(rows: Iterable[FundingRow]) -> dict[tuple[str, str], FundingRow]:
    """{(base_upper, quote): FundingRow} — kept restricted to USDT/USDC quotes."""
    out: dict[tuple[str, str], FundingRow] = {}
    for r in rows:
        if r.quote_asset not in _QUOTES:
            continue
        out[(r.base_asset.upper(), r.quote_asset)] = r
    return out


def _index_contracts(contracts: Iterable[ContractInfo]) -> dict[str, ContractInfo]:
    return {c.symbol: c for c in contracts}


def _streak_display(count: int, direction: Optional[str]) -> Optional[str]:
    if not count or not direction:
        return None
    arrow = "↑" if direction == "pos" else ("↓" if direction == "neg" else "·")
    return f"{count}{arrow}"


def screen_combined_high_funding(
    binance_rows: Iterable[FundingRow],
    mexc_rows: Iterable[FundingRow],
    binance_contracts: Iterable[ContractInfo],
    mexc_contracts: Iterable[ContractInfo],
    enrichments: dict[tuple[str, str], EnrichmentData],
    threshold_percent: float,
    binance_volumes: Optional[dict[str, float]] = None,
    mexc_volumes: Optional[dict[str, float]] = None,
    min_volume_usd_per_side: float = 0.0,
) -> list[CombinedFundingRow]:
    bnb = _index_by_base_quote(binance_rows)
    mxc = _index_by_base_quote(mexc_rows)
    bnb_contracts_by_sym = _index_contracts(binance_contracts)
    mxc_contracts_by_sym = _index_contracts(mexc_contracts)
    binance_volumes = binance_volumes or {}
    mexc_volumes = mexc_volumes or {}
    keys = set(bnb.keys()) | set(mxc.keys())

    out: list[CombinedFundingRow] = []
    for (base, quote) in keys:
        b = bnb.get((base, quote))
        m = mxc.get((base, quote))

        # Drop a side whose symbol is no longer in the (TRADING-only) contracts
        # list — premiumIndex returns funding even for delisted contracts.
        if b is not None and b.symbol not in bnb_contracts_by_sym:
            b = None
        if m is not None and m.symbol not in mxc_contracts_by_sym:
            m = None

        # Drop a side that's listed but trades below the liquidity floor.
        if b is not None and min_volume_usd_per_side > 0:
            if binance_volumes.get(b.symbol, 0.0) < min_volume_usd_per_side:
                b = None
        if m is not None and min_volume_usd_per_side > 0:
            if mexc_volumes.get(m.symbol, 0.0) < min_volume_usd_per_side:
                m = None

        # Both sides hidden → drop the row entirely.
        if b is None and m is None:
            continue

        b_norm = b.rate_8h_norm_percent if b else None
        m_norm = m.rate_8h_norm_percent if m else None
        max_abs = max(
            abs(b_norm) if b_norm is not None else 0.0,
            abs(m_norm) if m_norm is not None else 0.0,
        )
        if max_abs <= threshold_percent:
            continue

        spread = (b_norm - m_norm) if (b_norm is not None and m_norm is not None) else None
        b_fee = bnb_contracts_by_sym[b.symbol].maker_fee_percent if b and b.symbol in bnb_contracts_by_sym else None
        m_fee = mxc_contracts_by_sym[m.symbol].maker_fee_percent if m and m.symbol in mxc_contracts_by_sym else None

        b_enr = enrichments.get(("Binance", b.symbol)) if b else None
        m_enr = enrichments.get(("MEXC", m.symbol)) if m else None

        b_streak = _streak_display(b_enr.funding_streak_count, b_enr.funding_streak_direction) if b_enr else None
        m_streak = _streak_display(m_enr.funding_streak_count, m_enr.funding_streak_direction) if m_enr else None
        b_spread = b_enr.mark_index_spread_percent if b_enr else None
        b_oi_24h = b_enr.oi_change_24h_pct if b_enr else None
        b_ls_global = b_enr.ls_ratio_global if b_enr else None
        b_ls_top = b_enr.ls_ratio_top if b_enr else None

        # Pick the side with the larger absolute 8h-norm to drive the signal.
        if abs(b_norm or 0.0) >= abs(m_norm or 0.0):
            sig_funding = b_norm
            sig_streak_count = b_enr.funding_streak_count if b_enr else 0
            sig_streak_dir = b_enr.funding_streak_direction if b_enr else None
            sig_spread = b_spread
        else:
            sig_funding = m_norm
            sig_streak_count = m_enr.funding_streak_count if m_enr else 0
            sig_streak_dir = m_enr.funding_streak_direction if m_enr else None
            sig_spread = None  # MEXC doesn't expose mark/index

        signal = classify_signal(
            funding_8h_norm_pct=sig_funding,
            streak_count=sig_streak_count,
            streak_direction=sig_streak_dir,
            mark_index_spread_pct=sig_spread,
        )

        b_vol = binance_volumes.get(b.symbol) if b else None
        m_vol = mexc_volumes.get(m.symbol) if m else None

        out.append(
            CombinedFundingRow(
                base_asset=base,
                quote_asset=quote,
                binance_symbol=b.symbol if b else None,
                binance_rate_percent=b.rate_percent if b else None,
                binance_rate_8h_norm_percent=b_norm,
                binance_interval_hours=b.interval_hours if b else None,
                binance_next_funding_time=b.next_funding_time if b else None,
                binance_maker_fee_percent=b_fee,
                binance_mark_index_spread_percent=b_spread,
                binance_funding_streak=b_streak,
                binance_volume_24h_millions=(b_vol / 1e6) if b_vol is not None else None,
                binance_oi_change_24h_pct=b_oi_24h,
                binance_ls_ratio_global=b_ls_global,
                binance_ls_ratio_top=b_ls_top,
                mexc_symbol=m.symbol if m else None,
                mexc_rate_percent=m.rate_percent if m else None,
                mexc_rate_8h_norm_percent=m_norm,
                mexc_interval_hours=m.interval_hours if m else None,
                mexc_next_funding_time=m.next_funding_time if m else None,
                mexc_maker_fee_percent=m_fee,
                mexc_funding_streak=m_streak,
                mexc_volume_24h_millions=(m_vol / 1e6) if m_vol is not None else None,
                max_abs_8h_norm_percent=max_abs,
                spread_8h_norm_percent=spread,
                signal_emoji=signal.emoji,
                signal_short=signal.short,
                signal_breakdown=signal.breakdown,
                signal_color=signal.color,
            )
        )
    out.sort(key=lambda r: r.max_abs_8h_norm_percent, reverse=True)
    return out
