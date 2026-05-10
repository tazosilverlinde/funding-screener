"""Page 10 — Binance↔MEXC funding-spread arbitrage.

Shows pairs where the same base/quote has materially different 8h-normalized
funding rates on Binance vs MEXC, AND the spread exceeds total round-trip
maker fees. Long the lower-funding side, short the higher-funding side; you
collect the difference each cycle.

The Page 1 USDT/USDC arb screener is structurally similar but operates on
DIFFERENT QUOTES on the SAME exchange. This page operates on the SAME quote
across DIFFERENT exchanges. Both filter for net > 0 after fees.

Caveats:
- Liquidity floor enforced via the same min_24h_volume_usd_per_side knob
  as Page 2, so untradeable spreads don't show.
- Funding-time skew between exchanges is exposed; large skews mean the
  carry isn't continuous (you pay one cycle before collecting another).
- Annualized yield is upper-bound; arbitrage compresses spreads as flow
  enters.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.config import settings  # noqa: E402
from funding_screener.screener import screen_cross_exchange_arb  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    cooldown_banner,
    freshness_banner,
    minutes_to,
    sidebar_status,
    symbol_search_sidebar,
    to_df,
)


st.set_page_config(page_title="Cross-exchange Arb", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
auto_rerun(interval_ms=30_000, key="page10_tick")

cfg = settings()
min_volume_per_side = float(
    cfg.get("high_funding", {}).get("min_24h_volume_usd_per_side", 1_000_000)
)

st.title("Cross-exchange funding-spread arb — Binance ↔ MEXC")
st.caption(
    "Pairs where Binance and MEXC quote meaningfully different 8h-normalized "
    "funding for the SAME base/quote, and the spread exceeds total maker fees "
    "for a full round trip (open + close on each leg = 4 fees). "
    "Long the lower-funding side, short the higher — you collect the difference. "
    f"Liquidity floor: ${min_volume_per_side:,.0f} 24h on each side."
)

freshness_banner(store)
cooldown_banner(store)
st.divider()


bnb = store.read_binance()
mxc = store.read_mexc()
rows = screen_cross_exchange_arb(
    bnb.funding, mxc.funding,
    bnb.contracts, mxc.contracts,
    binance_volumes=bnb.volumes,
    mexc_volumes=mxc.volumes,
    min_volume_usd_per_side=min_volume_per_side,
)


if not rows:
    st.info(
        "No profitable cross-exchange arb opportunities right now (after fees). "
        "Spreads are usually compressed; meaningful gaps appear when one side "
        "spikes funding due to one-sided positioning. Refresh in a minute or "
        "lower `min_24h_volume_usd_per_side` in settings.yaml."
    )
    st.stop()


df = to_df(
    [r.model_dump() for r in rows],
    column_order=[
        "base_asset", "quote_asset",
        "net_8h_norm_percent", "apr_estimate_percent",
        "binance_rate_8h_norm_percent", "mexc_rate_8h_norm_percent",
        "diff_8h_norm_percent", "fees_total_percent",
        "long_exchange", "long_symbol", "short_exchange", "short_symbol",
        "binance_symbol", "mexc_symbol",
        "binance_next_funding_time", "mexc_next_funding_time",
        "funding_time_skew_minutes",
        "binance_volume_24h_usd", "mexc_volume_24h_usd",
    ],
)

# Make symbol cells clickable into Detail page.
df["binance_symbol"] = df["binance_symbol"].apply(
    lambda s: f"/Symbol_Detail?exchange=Binance&symbol={s}" if s else ""
)
df["mexc_symbol"] = df["mexc_symbol"].apply(
    lambda s: f"/Symbol_Detail?exchange=MEXC&symbol={s}" if s else ""
)
df["binance_next_funding_time"] = df["binance_next_funding_time"].apply(minutes_to)
df["mexc_next_funding_time"] = df["mexc_next_funding_time"].apply(minutes_to)

df = df.rename(columns={
    "base_asset": "Base",
    "quote_asset": "Quote",
    "net_8h_norm_percent": "Net / 8h %",
    "apr_estimate_percent": "APR est %",
    "binance_rate_8h_norm_percent": "Bnb 8h %",
    "mexc_rate_8h_norm_percent": "MXC 8h %",
    "diff_8h_norm_percent": "|Diff| 8h %",
    "fees_total_percent": "Fees %",
    "long_exchange": "Long ex",
    "long_symbol": "Long sym",
    "short_exchange": "Short ex",
    "short_symbol": "Short sym",
    "binance_symbol": "Binance link",
    "mexc_symbol": "MEXC link",
    "binance_next_funding_time": "Bnb next in",
    "mexc_next_funding_time": "MXC next in",
    "funding_time_skew_minutes": "Skew (min)",
    "binance_volume_24h_usd": "Bnb 24h vol",
    "mexc_volume_24h_usd": "MXC 24h vol",
})

st.dataframe(
    df, hide_index=True, use_container_width=True,
    column_config={
        "Base": st.column_config.TextColumn("Base"),
        "Quote": st.column_config.TextColumn("Quote"),
        "Net / 8h %": st.column_config.NumberColumn(
            format="%+.4f%%",
            help="8h-normalized funding spread MINUS round-trip maker fees. "
                 "Positive = pure-arb opportunity; the higher, the better. "
                 "What you collect per 8h on a $-neutral position.",
        ),
        "APR est %": st.column_config.NumberColumn(
            format="%+.1f%%",
            help="Annualized upper-bound estimate (net_8h × 3 cycles × 365 days). "
                 "Real-world this compresses as more flow enters; treat as ceiling.",
        ),
        "Bnb 8h %": st.column_config.NumberColumn(
            format="%+.4f%%",
            help="Binance side, 8h-normalized funding rate.",
        ),
        "MXC 8h %": st.column_config.NumberColumn(
            format="%+.4f%%",
            help="MEXC side, 8h-normalized funding rate.",
        ),
        "|Diff| 8h %": st.column_config.NumberColumn(
            format="%.4f%%",
            help="Absolute difference between Binance and MEXC 8h-norm rates.",
        ),
        "Fees %": st.column_config.NumberColumn(
            format="%.4f%%",
            help="Total round-trip maker fees: 2 (open+close) × Binance fee + "
                 "2 × MEXC fee. Subtract this from |Diff| to get Net.",
        ),
        "Long ex": st.column_config.TextColumn(
            "Long ex",
            help="Exchange where you take the LONG side — the lower-funding leg.",
        ),
        "Short ex": st.column_config.TextColumn(
            "Short ex",
            help="Exchange where you take the SHORT side — the higher-funding leg.",
        ),
        "Long sym": st.column_config.TextColumn("Long sym"),
        "Short sym": st.column_config.TextColumn("Short sym"),
        "Binance link": st.column_config.LinkColumn(
            "Binance →", display_text=r".*symbol=([^&]+)",
        ),
        "MEXC link": st.column_config.LinkColumn(
            "MEXC →", display_text=r".*symbol=([^&]+)",
        ),
        "Bnb next in": st.column_config.TextColumn("Bnb next"),
        "MXC next in": st.column_config.TextColumn("MXC next"),
        "Skew (min)": st.column_config.NumberColumn(
            format="%.0f",
            help="Absolute difference between the two next-funding times, in "
                 "minutes. High skew = funding cycles don't align; you pay one "
                 "side a cycle before collecting the other.",
        ),
        "Bnb 24h vol": st.column_config.NumberColumn(format="$%,.0f"),
        "MXC 24h vol": st.column_config.NumberColumn(format="$%,.0f"),
    },
)

st.caption(
    f"Showing {len(df)} pairs with positive net after fees. Sorted by net descending."
)
