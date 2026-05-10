"""Page 9 — Daily digest preview.

Shows the structured digest that's intended for once-a-day email delivery.
Renders every section that has data; sections with no data are silently
omitted so a quiet market produces a short page rather than empty headers.

The digest composer is a pure function — `compose_daily_digest` in
`funding_screener.digest`. SMTP delivery (Round 22 or later) will reuse the
same composer, so what you see on this page is exactly what the email
contains.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.config import settings  # noqa: E402
from funding_screener.digest import compose_daily_digest  # noqa: E402
from funding_screener.screener import screen_combined_high_funding  # noqa: E402
from funding_screener.sectors import sector_aggregates  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun, boot, cooldown_banner, sidebar_status,
)
from funding_screener.unlocks import load_upcoming_unlocks  # noqa: E402


st.set_page_config(page_title="Daily digest", layout="wide")

store = boot()
sidebar_status(store)
auto_rerun(interval_ms=60_000, key="digest_tick")

st.title("Daily digest preview")
st.caption(
    "What a once-a-day market summary email would contain. Composed from the "
    "same data the other pages render, so it's always in sync. Email delivery "
    "is on the roadmap; this preview is rendered live."
)
cooldown_banner(store)


# ---- gather inputs ----
bnb = store.read_binance()
mxc = store.read_mexc()
enrichments = store.read_enrichments()
onchain_flows, _ = store.read_onchain_flows()
onchain_by_base = {f["token"]: f.get("net_usd", 0.0) for f in onchain_flows}
score_histories = store.read_score_histories()
liq_stats_by_symbol = store.read_liquidations(window_seconds=24 * 3600)
combined_klines: dict = {}
combined_klines.update(bnb.klines)
combined_klines.update(mxc.klines)

combined_rows = screen_combined_high_funding(
    bnb.funding, mxc.funding,
    bnb.contracts, mxc.contracts,
    enrichments,
    threshold_percent=0.0,  # no pre-filter — digest sees the full universe
    binance_volumes=bnb.volumes, mexc_volumes=mxc.volumes,
    min_volume_usd_per_side=0.0,
    onchain_netflow_by_base=onchain_by_base,
    klines_by_symbol=combined_klines,
    score_histories=score_histories,
    liq_stats_by_symbol=liq_stats_by_symbol,
)

unlock_events = load_upcoming_unlocks()
sector_rows = sector_aggregates(combined_rows)
stablecoin_supply = store.read_stablecoin_supply()

cfg = settings()
top_n = int(cfg.get("digest", {}).get("top_n", 5))

digest = compose_daily_digest(
    combined_rows=combined_rows,
    liq_stats_by_symbol=liq_stats_by_symbol,
    onchain_flows=onchain_flows,
    unlock_events=unlock_events,
    stablecoin_supply=stablecoin_supply,
    sector_rows=sector_rows,
    top_n=top_n,
)


# ---- market overview (always shown) ----
ov = digest["market_overview"]
st.subheader("📊 Market overview")
m1, m2, m3, m4 = st.columns(4)
m1.metric("Symbols tracked", f"{ov['total_symbols']:,}")
m2.metric("🟢 Bullish (≥+30)", f"{ov['bullish']}", f"{ov['strong_bull']} strong")
m3.metric("🔴 Bearish (≤-30)", f"{ov['bearish']}", f"{ov['strong_bear']} strong")
m4.metric("🟡 Neutral", f"{ov['neutral']}")
if digest.get("macro"):
    m = digest["macro"]
    st.markdown(f"**{m['emoji']} Macro:** {m['headline']}")

st.divider()


# ---- top longs / top shorts (side by side) ----
col_long, col_short = st.columns(2)
col_long.subheader("🟢 Top long candidates")
if digest["top_longs"]:
    long_df = pd.DataFrame(digest["top_longs"]).rename(
        columns={"symbol": "Symbol", "base": "Base", "score": "Score",
                 "label": "Bias", "funding_8h_pct": "Funding/8h"}
    )[["Symbol", "Base", "Score", "Bias", "Funding/8h"]]
    col_long.dataframe(
        long_df, hide_index=True, use_container_width=True,
        column_config={
            "Score": st.column_config.NumberColumn(format="%+d"),
            "Funding/8h": st.column_config.NumberColumn(format="%.4f%%"),
        },
    )
else:
    col_long.write("_No bullish setups (Score ≥ +30) right now._")

col_short.subheader("🔴 Top short candidates")
if digest["top_shorts"]:
    short_df = pd.DataFrame(digest["top_shorts"]).rename(
        columns={"symbol": "Symbol", "base": "Base", "score": "Score",
                 "label": "Bias", "funding_8h_pct": "Funding/8h"}
    )[["Symbol", "Base", "Score", "Bias", "Funding/8h"]]
    col_short.dataframe(
        short_df, hide_index=True, use_container_width=True,
        column_config={
            "Score": st.column_config.NumberColumn(format="%+d"),
            "Funding/8h": st.column_config.NumberColumn(format="%.4f%%"),
        },
    )
else:
    col_short.write("_No bearish setups (Score ≤ −30) right now._")

st.divider()


# ---- sector rotation (top winners/losers) ----
if digest.get("sector_winners") or digest.get("sector_losers"):
    sw1, sw2 = st.columns(2)
    sw1.subheader("🏆 Sector winners (top 3 avg score)")
    if digest.get("sector_winners"):
        sw1.dataframe(
            pd.DataFrame(digest["sector_winners"])[["sector", "avg_score", "row_count"]].rename(
                columns={"sector": "Sector", "avg_score": "Avg score", "row_count": "Tokens"}
            ),
            hide_index=True, use_container_width=True,
            column_config={"Avg score": st.column_config.NumberColumn(format="%+.1f")},
        )
    sw2.subheader("📉 Sector laggards (bottom 3 avg score)")
    if digest.get("sector_losers"):
        sw2.dataframe(
            pd.DataFrame(digest["sector_losers"])[["sector", "avg_score", "row_count"]].rename(
                columns={"sector": "Sector", "avg_score": "Avg score", "row_count": "Tokens"}
            ),
            hide_index=True, use_container_width=True,
            column_config={"Avg score": st.column_config.NumberColumn(format="%+.1f")},
        )
    st.divider()


# ---- liquidation cascades / squeezes ----
liq_col1, liq_col2 = st.columns(2)
liq_col1.subheader("🟢 Top short squeezes (24h)")
if digest["top_squeezes"]:
    sq_df = pd.DataFrame(digest["top_squeezes"]).rename(
        columns={"symbol": "Symbol", "side_usd": "Short liq",
                 "other_usd": "Long liq", "events_count": "Events"}
    )
    liq_col1.dataframe(
        sq_df, hide_index=True, use_container_width=True,
        column_config={
            "Short liq": st.column_config.NumberColumn(format="$%,.0f"),
            "Long liq": st.column_config.NumberColumn(format="$%,.0f"),
        },
    )
else:
    liq_col1.write("_No notable short squeezes in the last 24h._")

liq_col2.subheader("🔴 Top long cascades (24h)")
if digest["top_cascades"]:
    ca_df = pd.DataFrame(digest["top_cascades"]).rename(
        columns={"symbol": "Symbol", "side_usd": "Long liq",
                 "other_usd": "Short liq", "events_count": "Events"}
    )
    liq_col2.dataframe(
        ca_df, hide_index=True, use_container_width=True,
        column_config={
            "Long liq": st.column_config.NumberColumn(format="$%,.0f"),
            "Short liq": st.column_config.NumberColumn(format="$%,.0f"),
        },
    )
else:
    liq_col2.write("_No notable long cascades in the last 24h._")

st.divider()


# ---- whale + upcoming unlocks ----
if digest.get("whale_highlight"):
    w = digest["whale_highlight"]
    st.subheader("🐋 Whale spotlight")
    st.markdown(f"**{w.get('emoji', '')}** {w.get('headline', '')}")

if digest.get("upcoming_unlocks"):
    st.subheader("🔓 Upcoming unlocks (next 7 days)")
    unlock_df = pd.DataFrame(digest["upcoming_unlocks"]).rename(
        columns={"symbol": "Symbol", "days_until": "Days",
                 "date_str": "Date", "amount_usd": "Amount ($)",
                 "pct_of_supply": "% of supply"}
    )
    st.dataframe(
        unlock_df, hide_index=True, use_container_width=True,
        column_config={
            "Amount ($)": st.column_config.NumberColumn(format="$%,.0f"),
            "% of supply": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
else:
    st.write("_No notable unlocks in the next 7 days._")

st.divider()
st.caption(
    "Composed live by `funding_screener.digest.compose_daily_digest`. "
    "Same composer will be reused when SMTP delivery lands — what you see "
    "here is exactly what would be sent."
)
