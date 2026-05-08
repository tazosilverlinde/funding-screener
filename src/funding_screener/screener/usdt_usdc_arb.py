"""USDT/USDC funding-rate arbitrage screener.

Pure function — takes already-fetched funding rows + contracts (with maker fees).

Logic ported from the Java reference's
`Symbol.openPositionIsProfitableBetweenUSDTUSDC` and `Symbol.updateAvgDailyProfits`:

  net_per_period = abs(rate_usdt - rate_usdc) - 4 * maker_fee
  long_leg  = the lower-rate side  (pays less / receives more funding)
  short_leg = the higher-rate side (pays more / receives less)
"""

from __future__ import annotations

from typing import Iterable

from ..models import ArbRow, ContractInfo, FundingRow


def _index_by_base(rows: Iterable[FundingRow]) -> dict[str, dict[str, FundingRow]]:
    out: dict[str, dict[str, FundingRow]] = {}
    for r in rows:
        if r.quote_asset not in ("USDT", "USDC"):
            continue
        out.setdefault(r.base_asset, {})[r.quote_asset] = r
    return out


def screen_usdt_usdc_arb(
    funding_rows: Iterable[FundingRow],
    contracts: Iterable[ContractInfo],
    exchange_name: str,
) -> list[ArbRow]:
    funding = _index_by_base(funding_rows)
    contract_by_sym = {c.symbol: c for c in contracts}

    out: list[ArbRow] = []
    for base, by_quote in funding.items():
        usdt = by_quote.get("USDT")
        usdc = by_quote.get("USDC")
        if usdt is None or usdc is None:
            continue
        c_usdt = contract_by_sym.get(usdt.symbol)
        c_usdc = contract_by_sym.get(usdc.symbol)
        if c_usdt is None or c_usdc is None:
            continue
        if c_usdt.status != "TRADING" or c_usdc.status != "TRADING":
            continue

        interval = usdt.interval_hours
        diff_abs = abs(usdt.rate_percent - usdc.rate_percent)
        fees_total = 2.0 * c_usdt.maker_fee_percent + 2.0 * c_usdc.maker_fee_percent
        net = diff_abs - fees_total
        if net <= 0:
            continue

        if usdt.rate_percent < usdc.rate_percent:
            long_leg, short_leg = usdt.symbol, usdc.symbol
        else:
            long_leg, short_leg = usdc.symbol, usdt.symbol

        skew_min: float | None = None
        if usdt.next_funding_time and usdc.next_funding_time:
            skew_min = abs((usdt.next_funding_time - usdc.next_funding_time).total_seconds() / 60.0)

        out.append(
            ArbRow(
                exchange=exchange_name,
                base_asset=base,
                usdt_symbol=usdt.symbol,
                usdc_symbol=usdc.symbol,
                rate_usdt_percent=usdt.rate_percent,
                rate_usdc_percent=usdc.rate_percent,
                interval_hours=interval,
                diff_abs_percent=diff_abs,
                fees_total_percent=fees_total,
                net_percent=net,
                net_8h_norm_percent=net * 8.0 / interval if interval > 0 else 0.0,
                long_leg_symbol=long_leg,
                short_leg_symbol=short_leg,
                next_funding_time_usdt=usdt.next_funding_time,
                next_funding_time_usdc=usdc.next_funding_time,
                funding_time_skew_minutes=skew_min,
            )
        )
    out.sort(key=lambda r: r.net_8h_norm_percent, reverse=True)
    return out
