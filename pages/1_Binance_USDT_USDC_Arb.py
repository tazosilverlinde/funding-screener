"""Page 1 — Binance USDT/USDC funding-rate arbitrage. Reads from background cache."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from funding_screener.config import is_binance_enabled, settings  # noqa: E402
from funding_screener.screener import screen_usdt_usdc_arb  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    cooldown_banner,
    filter_dataframe_to_watchlist,
    freshness_banner,
    minutes_to,
    render_table,
    sidebar_status,
    symbol_search_sidebar,
    to_df,
    watchlist_sidebar,
)

st.set_page_config(page_title="Binance USDT/USDC Arb", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
watchlist = watchlist_sidebar()
auto_rerun(interval_ms=30_000, key="page1_tick")

st.title("Binance — USDT/USDC funding arbitrage")
st.caption(
    "Pairs that have BOTH a USDT and a USDC perpetual contract on Binance, where "
    "(|funding_USDT − funding_USDC| − 4 × maker_fee) > 0 at the next funding period. "
    "**Long the lower-rate leg; short the higher-rate leg.**"
)

freshness_banner(store)
cooldown_banner(store)

if not is_binance_enabled():
    st.error(
        "Binance is disabled in this deployment (env var `BINANCE_ENABLED=false`). "
        "This page needs Binance funding data, so it can't show anything here. "
        "The MEXC pages still work — see the sidebar."
    )
    st.stop()

st.divider()

snap = store.read_binance()
rows = screen_usdt_usdc_arb(snap.funding, snap.contracts, exchange_name="Binance")

cap = int(settings()["row_limit"])
df = to_df(
    [r.model_dump() for r in rows[:cap]],
    column_order=[
        "base_asset",
        "long_leg_symbol",
        "short_leg_symbol",
        "rate_usdt_percent",
        "rate_usdc_percent",
        "diff_abs_percent",
        "fees_total_percent",
        "net_percent",
        "net_8h_norm_percent",
        "interval_hours",
        "next_funding_time_usdt",
        "funding_time_skew_minutes",
    ],
)

if not df.empty:
    # Make symbols clickable → detail page.
    df["long_leg_symbol"] = df["long_leg_symbol"].apply(
        lambda s: f"/Symbol_Detail?exchange=Binance&symbol={s}" if s else ""
    )
    df["short_leg_symbol"] = df["short_leg_symbol"].apply(
        lambda s: f"/Symbol_Detail?exchange=Binance&symbol={s}" if s else ""
    )
    # Convert next-funding datetime to relative minutes-left.
    df["next_funding_time_usdt"] = df["next_funding_time_usdt"].apply(minutes_to)

    df = df.rename(
        columns={
            "base_asset": "Base",
            "long_leg_symbol": "LONG leg",
            "short_leg_symbol": "SHORT leg",
            "rate_usdt_percent": "USDT rate %",
            "rate_usdc_percent": "USDC rate %",
            "diff_abs_percent": "|diff| %",
            "fees_total_percent": "Fees %",
            "net_percent": "Net % / period",
            "net_8h_norm_percent": "Net % / 8h",
            "interval_hours": "Interval (h)",
            "next_funding_time_usdt": "Next funding in",
            "funding_time_skew_minutes": "Funding skew (min)",
        }
    )
    cfg = {
        "Base": st.column_config.TextColumn(
            "Base", help="The underlying asset. Has both a USDT perp and a USDC perp on Binance."
        ),
        "LONG leg": st.column_config.LinkColumn(
            "LONG leg",
            display_text=r"symbol=([A-Z0-9_]+)",
            help=(
                "The contract you should LONG — lower-rate side, so you receive more "
                "funding (or pay less). Click to open full detail for this symbol."
            ),
        ),
        "SHORT leg": st.column_config.LinkColumn(
            "SHORT leg",
            display_text=r"symbol=([A-Z0-9_]+)",
            help=(
                "The contract you should SHORT — higher-rate side, so you collect more "
                "funding being short. Click to open full detail for this symbol."
            ),
        ),
        "USDT rate %": st.column_config.NumberColumn(
            format="%.4f",
            help="Signed funding rate of the USDT-quoted perp at the next settlement. Positive = longs pay shorts.",
        ),
        "USDC rate %": st.column_config.NumberColumn(
            format="%.4f", help="Same as USDT rate but for the USDC-quoted perp."
        ),
        "|diff| %": st.column_config.NumberColumn(
            format="%.4f",
            help="Absolute difference between USDT and USDC rates per period. Bigger diff = bigger arb edge.",
        ),
        "Fees %": st.column_config.NumberColumn(
            format="%.4f",
            help=(
                "Total round-trip maker fees: 2 × (USDT-leg maker) + 2 × (USDC-leg maker).\n"
                "Both legs are opened AND closed → 4 fees total."
            ),
        ),
        "Net % / period": st.column_config.NumberColumn(
            format="%.4f",
            help=(
                "(|diff| − fees) per single funding period.\n"
                "Must be > 0 to qualify; this column is the actual realised edge per 4h/8h cycle."
            ),
        ),
        "Net % / 8h": st.column_config.NumberColumn(
            format="%.4f",
            help=(
                "Net normalized to an 8h period — useful for ranking pairs with different intervals.\n"
                "Higher = better arb opportunity per unit time."
            ),
        ),
        "Interval (h)": st.column_config.NumberColumn(format="%.0f", help="Funding settlement interval in hours."),
        "Next funding in": st.column_config.TextColumn(
            "Next funding in",
            help=(
                "Time remaining until USDT-leg funding settles. Format: '47m' or '2h 15m'.\n"
                "Both legs should settle within a few minutes of each other (see Funding skew column)."
            ),
        ),
        "Funding skew (min)": st.column_config.NumberColumn(
            format="%.1f",
            help=(
                "Time gap between USDT and USDC funding settlements.\n"
                "Should be small (< 5 min) for a clean arb. Large skew = legs settle at different times = "
                "exposure to one side's funding without the offset."
            ),
        ),
    }
    df = filter_dataframe_to_watchlist(df, watchlist, ["LONG leg", "SHORT leg"])
    render_table(df, column_config=cfg, download_basename="binance_usdt_usdc_arb")
    cap_msg = (f"watchlist of {len(watchlist)} symbols" if watchlist
               else f"top {len(df)} of {len(rows)} qualifying pairs")
    st.caption(f"Showing {len(df)} pairs ({cap_msg}), sorted by 8h-normalized net %.")
else:
    st.info("No USDT/USDC pair currently has positive net funding after fees on Binance.")
