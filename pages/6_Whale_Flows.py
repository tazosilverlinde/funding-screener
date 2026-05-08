"""Page 6 — Whale flows: exchange netflow + smart-money buys.

Powered by Arkham Intelligence's free public API. Requires an `ARKHAM_API_KEY`
env var (free signup at https://intel.arkm.com → API). When the key isn't
set, this page shows configuration instructions instead of failing.

Data shown:
  1. **Exchange netflow** per top-volume token: deposits to CEX (sell pressure)
     vs withdrawals from CEX (accumulation off-exchange).
  2. **Smart-money top buys**: tokens most net-bought by Arkham-labeled
     smart-money wallets in the last 24h.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.arkham import ArkhamClient, is_arkham_configured  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    run_async,
    sidebar_status,
)

st.set_page_config(page_title="Whale Flows", layout="wide")

store = boot()
sidebar_status(store)
auto_rerun(interval_ms=60_000, key="page6_tick")

st.title("Whale flows — exchange netflow & smart-money buys")
st.caption(
    "On-chain whale activity from Arkham Intelligence. Exchange deposits = sell pressure; "
    "withdrawals to cold storage = accumulation. Smart-money buys = tokens "
    "Arkham-labeled profitable wallets are accumulating."
)

if not is_arkham_configured():
    st.warning("**Arkham API key not configured** — page is dormant.")
    st.markdown(
        """
This page needs a free Arkham Intelligence API key to fetch on-chain whale data. Setup:

1. Visit **https://intel.arkm.com** and sign up (free, no card)
2. Open your account → API → generate a key
3. Add it to your environment:
   - **Local dev**: set `ARKHAM_API_KEY=your_key_here` before launching Streamlit
   - **Render deploy**: Render dashboard → service → Environment → add `ARKHAM_API_KEY`
4. Restart the app — this page will start populating automatically

Until configured, the rest of the screener (funding, price rise, signals) keeps working normally.
"""
    )
    st.stop()


# ---------------- exchange netflow ----------------

st.subheader("Exchange netflow (24h)")
st.caption(
    "Net USD value moving INTO vs OUT of labeled CEX wallets per token. "
    "Negative net = withdrawals exceed deposits = accumulation off-exchange (bullish bias)."
)


@st.cache_data(ttl=600, show_spinner="Fetching whale flows from Arkham…")
def _fetch_netflow(symbols: tuple[str, ...]) -> list[dict]:
    async def _go():
        client = ArkhamClient()
        try:
            results = await asyncio.gather(
                *(client.fetch_exchange_netflow(s, hours=24) for s in symbols),
                return_exceptions=True,
            )
        finally:
            await client.aclose()
        out = []
        for r in results:
            if isinstance(r, dict) and r:
                out.append(r)
        return out

    return run_async(_go)


# Use the top base assets from our existing Binance+MEXC contracts cache.
binance = store.read_binance()
mexc = store.read_mexc()
seen = set()
top_assets: list[str] = []
for c in sorted(
    list(binance.contracts) + list(mexc.contracts),
    key=lambda x: binance.volumes.get(x.symbol, 0) + mexc.volumes.get(x.symbol, 0),
    reverse=True,
):
    if c.base_asset.upper() not in seen and c.quote_asset == "USDT":
        seen.add(c.base_asset.upper())
        top_assets.append(c.base_asset.upper())
    if len(top_assets) >= 30:
        break

netflow_rows = _fetch_netflow(tuple(top_assets)) if top_assets else []

if netflow_rows:
    df = pd.DataFrame(netflow_rows)
    df = df.rename(columns={
        "token": "Token",
        "deposits_usd": "Deposits ($)",
        "withdrawals_usd": "Withdrawals ($)",
        "net_usd": "Net ($)",
        "transfer_count": "# transfers",
    })
    df = df.sort_values("Net ($)")  # most-bullish (most negative net) at top
    st.dataframe(df, hide_index=True, use_container_width=True, column_config={
        "Deposits ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Withdrawals ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Net ($)": st.column_config.NumberColumn(format="$%+,.0f"),
    })
    st.caption(f"Top {len(df)} tracked tokens by combined Binance+MEXC volume. Sorted by net flow (most-bullish first).")
else:
    st.info("Waiting for first Arkham response — refresh in a few seconds.")

st.divider()


# ---------------- smart money ----------------

st.subheader("Smart-money top buys (24h)")
st.caption(
    "Tokens most net-bought by Arkham's smart-money cohort in the last 24h. "
    "These are wallets Arkham has labeled as historically profitable / institutional."
)


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_smart_money() -> list[dict]:
    async def _go():
        client = ArkhamClient()
        try:
            return await client.fetch_smart_money_top_buys(hours=24, limit=20)
        finally:
            await client.aclose()

    return run_async(_go)


sm_rows = _fetch_smart_money()
if sm_rows:
    df_sm = pd.DataFrame(sm_rows).rename(columns={
        "token": "Token",
        "net_buy_usd": "Smart-money net buy ($)",
        "wallet_count": "Wallets buying",
    })
    st.dataframe(df_sm, hide_index=True, use_container_width=True, column_config={
        "Smart-money net buy ($)": st.column_config.NumberColumn(format="$%+,.0f"),
        "Wallets buying": st.column_config.NumberColumn(format="%d"),
    })
else:
    st.info("No smart-money buys returned (data may not be available for the requested window).")

st.divider()
st.caption(
    "**Caveat**: Arkham's exchange-wallet labels are good for major CEXs (Binance, MEXC, OKX, Bybit, "
    "Coinbase) but coverage thins out for smaller exchanges and brand-new tokens. Smart-money labels "
    "lag — wallets that were profitable 6 months ago may not be now."
)
