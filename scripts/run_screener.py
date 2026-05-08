"""One-shot screener runner — used by the `verify-screener-output` skill.

Fetches data once via the public REST clients, runs the requested screener,
prints the result. Bypasses the Streamlit-side background updater so it works
standalone from the CLI.

Usage:
  python scripts/run_screener.py <name>

Where <name> is one of:
  binance-arb, combined-high-funding, binance-rise, mexc-rise
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from funding_screener.config import settings  # noqa: E402
from funding_screener.exchanges import BinanceClient, MexcClient  # noqa: E402
from funding_screener.screener import (  # noqa: E402
    screen_combined_high_funding,
    screen_price_rise,
    screen_usdt_usdc_arb,
)


async def _binance_arb() -> list[dict]:
    c = BinanceClient()
    try:
        funding, contracts = await asyncio.gather(c.fetch_funding_rows(), c.fetch_contracts())
        rows = screen_usdt_usdc_arb(funding, contracts, exchange_name="Binance")
    finally:
        await c.aclose()
    return [r.model_dump() for r in rows[: int(settings()["row_limit"])]]


async def _combined_high_funding() -> list[dict]:
    b = BinanceClient()
    m = MexcClient()
    try:
        bf, mf, bc, mc = await asyncio.gather(
            b.fetch_funding_rows(),
            m.fetch_funding_rows(),
            b.fetch_contracts(),
            m.fetch_contracts(),
        )
        # No enrichment in the CLI — keep it fast.
        rows = screen_combined_high_funding(
            bf, mf, bc, mc, {}, float(settings()["high_funding"]["threshold_percent"])
        )
    finally:
        await asyncio.gather(b.aclose(), m.aclose())
    return [r.model_dump() for r in rows[: int(settings()["row_limit"])]]


async def _price_rise(client_cls) -> list[dict]:
    from funding_screener.market_data import CoinPaprikaClient

    c = client_cls()
    g = CoinPaprikaClient()
    cfg = settings()
    pcfg = cfg["price_rise"]
    try:
        contracts, vol, funding, mcaps = await asyncio.gather(
            c.fetch_contracts(),
            c.fetch_24h_quote_volume(),
            c.fetch_funding_rows(),
            g.fetch_market_caps_top_n(top_n=1000),
        )
        days = int(pcfg.get("kline_history_days", 1500))
        cap = int(cfg["row_limit"]) * int(pcfg.get("kline_candidate_multiplier", 5))
        eligible = [x for x in contracts if x.status == "TRADING" and x.quote_asset in ("USDT", "USDC")]
        eligible.sort(key=lambda x: vol.get(x.symbol, 0.0), reverse=True)
        eligible = eligible[:cap]
        sem = asyncio.Semaphore(8)

        async def _one(sym: str):
            async with sem:
                try:
                    return sym, await c.fetch_daily_klines(sym, days)
                except Exception:
                    return sym, []

        pairs = await asyncio.gather(*[_one(x.symbol) for x in eligible])
        klines = {sym: ks for sym, ks in pairs if ks}
        rows = screen_price_rise(
            contracts,
            klines_by_symbol=klines,
            vol_map=vol,
            funding_rows=funding,
            market_caps_usd=mcaps,
            threshold_percent=float(pcfg["threshold_percent"]),
            windows_days=[int(w) for w in pcfg["windows_days"]],
            min_24h_quote_volume=float(pcfg["min_24h_quote_volume"]),
        )
    finally:
        await asyncio.gather(c.aclose(), g.aclose())
    return [r.model_dump() for r in rows[: int(settings()["row_limit"])]]


SCREENERS = {
    "binance-arb": _binance_arb,
    "combined-high-funding": _combined_high_funding,
    "binance-rise": lambda: _price_rise(BinanceClient),
    "mexc-rise": lambda: _price_rise(MexcClient),
}


def _print(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(show_header=True, header_style="bold")
        for col in rows[0].keys():
            table.add_column(col)
        for row in rows:
            table.add_row(*[_fmt(row[c]) for c in rows[0].keys()])
        console.print(table)
    except ImportError:
        cols = list(rows[0].keys())
        print("\t".join(cols))
        for row in rows:
            print("\t".join(_fmt(row[c]) for c in cols))


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in SCREENERS:
        print(__doc__)
        print(f"\nValid names: {', '.join(SCREENERS.keys())}")
        sys.exit(2)
    name = sys.argv[1]
    rows = asyncio.run(SCREENERS[name]())
    print(f"\nScreener: {name}    Rows: {len(rows)}\n")
    _print(rows)


if __name__ == "__main__":
    main()
