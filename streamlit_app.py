"""Funding screener landing page.

Run with:  streamlit run streamlit_app.py

The first time any page renders, a daemon thread starts and polls Binance + MEXC
public APIs in the background:
  - funding rates / contracts / 24h volume     every 60s
  - daily klines for top-N volume symbols       every 5 minutes
All pages read from this shared in-memory cache, so navigation is instant.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from funding_screener.config import fees, settings  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    freshness_banner,
    sidebar_status,
    symbol_search_sidebar,
)

st.set_page_config(page_title="Funding Screener", layout="wide", page_icon=":material/monitoring:")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
auto_rerun(interval_ms=30_000, key="landing_tick")

st.title("Funding Screener")
st.caption("Read-only Binance & MEXC perpetual-futures screener. Public APIs only.")

freshness_banner(store)

# Macro banner — stablecoin supply trend gives a read on liquidity entering/leaving crypto.
_supply = store.read_stablecoin_supply()
if _supply.get("TOTAL", {}).get("change_24h_pct") is not None:
    total = _supply["TOTAL"]
    usdt = _supply.get("USDT", {})
    usdc = _supply.get("USDC", {})
    total_change = total.get("change_24h_pct") or 0.0
    if total_change >= 0.3:
        macro_msg = "🟢 Stablecoin supply expanding — liquidity entering crypto, bullish backdrop"
        macro_box = st.success
    elif total_change <= -0.3:
        macro_msg = "🔴 Stablecoin supply contracting — liquidity leaving crypto, bearish backdrop"
        macro_box = st.error
    else:
        macro_msg = "🟡 Stablecoin supply stable — neutral macro liquidity"
        macro_box = st.info
    macro_box(
        f"**Macro:** {macro_msg}  \n"
        f"Total stables: ${total.get('now', 0)/1e9:.1f}B "
        f"({total_change:+.2f}% 24h, {total.get('change_7d_pct') or 0:+.2f}% 7d)  •  "
        f"USDT: ${usdt.get('now', 0)/1e9:.1f}B ({usdt.get('change_24h_pct') or 0:+.2f}% 24h)  •  "
        f"USDC: ${usdc.get('now', 0)/1e9:.1f}B ({usdc.get('change_24h_pct') or 0:+.2f}% 24h)"
    )

st.divider()

st.markdown(
    """
### Pages (sidebar)

| # | Page | What it shows |
|---|------|---------------|
| 1 | Binance USDT/USDC arb | Pairs where shorting one quote and longing the other yields positive net funding after maker fees. |
| 2 | High funding (combined) | Both exchanges side-by-side. Pairs where 8h-normalized funding rate is above the threshold on at least one exchange. Missing side shown as `—`. |
| 3 | Binance price rise | Pairs whose close-to-close return over 1d / 7d / 30d exceeds the threshold (default 1000%). |
| 4 | MEXC price rise | Same as #3 for MEXC. |
| 5 | Symbol detail | Click any symbol on other pages → full per-contract view (funding history, OI, L/S ratio, kline chart, signal). |
| 6 | Exchange flows | 24h on-chain netflow per token (our own ETH-RPC implementation, no API keys). |
| 7 | Token unlocks | Upcoming unlock events for tokens tradable on Binance/MEXC. Manually maintained YAML. |

All data refreshes automatically via the background updater. The sidebar shows
the age of each data type and the maker-fee values in use.
    """
)

st.divider()

bnb = store.read_binance()
mxc = store.read_mexc()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Binance perps", len(bnb.contracts))
c2.metric("MEXC perps", len(mxc.contracts))
c3.metric("Binance klines cached", len(bnb.klines))
c4.metric("MEXC klines cached", len(mxc.klines))

## ---- top movers ---- (biggest 1-hour score changes across all tracked pairs)
from funding_screener.score_history import top_movers as _top_movers  # noqa: E402

_histories = store.read_score_histories()
_risers, _fallers = _top_movers(_histories, minutes_ago=60, limit=5)
if _risers or _fallers:
    st.subheader("Top movers — last hour")
    st.caption(
        "Pairs whose composite score moved the most in the last hour. Risers are pairs "
        "where bullish signals built up rapidly; fallers are pairs that lost conviction. "
        "Computed from snapshots taken every 10 minutes (in-memory only — wipes on restart)."
    )
    mc1, mc2 = st.columns(2)
    if _risers:
        mc1.markdown("**🚀 Risers (score going up)**")
        rdf = pd.DataFrame(_risers).rename(columns={
            "base_asset": "Base", "quote_asset": "Quote",
            "current_score": "Score", "delta": "Δ 1h",
        })
        mc1.dataframe(
            rdf, hide_index=True, use_container_width=True,
            column_config={
                "Score": st.column_config.NumberColumn(format="%+d"),
                "Δ 1h": st.column_config.NumberColumn(format="%+d"),
            },
        )
    if _fallers:
        mc2.markdown("**💥 Fallers (score going down)**")
        fdf = pd.DataFrame(_fallers).rename(columns={
            "base_asset": "Base", "quote_asset": "Quote",
            "current_score": "Score", "delta": "Δ 1h",
        })
        mc2.dataframe(
            fdf, hide_index=True, use_container_width=True,
            column_config={
                "Score": st.column_config.NumberColumn(format="%+d"),
                "Δ 1h": st.column_config.NumberColumn(format="%+d"),
            },
        )

st.divider()


## ---- sector rotation summary -------------------------------------------------
## Quick read on which sectors are currently bullish vs bearish in aggregate.
## Computed live from the same combined-screener output Page 2 uses, so what's
## on Page 2 drives what shows here.
from funding_screener.sectors import sector_aggregates  # noqa: E402
from funding_screener.screener import screen_combined_high_funding  # noqa: E402
import pandas as pd  # noqa: E402

bnb_for_sector = store.read_binance()
mxc_for_sector = store.read_mexc()
_enrichments = store.read_enrichments()
_onchain_flows, _ = store.read_onchain_flows()
_onchain_by_base = {f["token"]: f.get("net_usd", 0.0) for f in _onchain_flows}
_combined_klines: dict = {}
_combined_klines.update(bnb_for_sector.klines)
_combined_klines.update(mxc_for_sector.klines)
_combined_rows = screen_combined_high_funding(
    bnb_for_sector.funding, mxc_for_sector.funding,
    bnb_for_sector.contracts, mxc_for_sector.contracts,
    _enrichments,
    threshold_percent=0.0,  # no threshold so sector aggregates see every symbol
    binance_volumes=bnb_for_sector.volumes,
    mexc_volumes=mxc_for_sector.volumes,
    min_volume_usd_per_side=0.0,
    onchain_netflow_by_base=_onchain_by_base,
    klines_by_symbol=_combined_klines,
)
_sector_rows = sector_aggregates(_combined_rows)
if _sector_rows:
    st.subheader("Sector rotation — average composite score per category")
    st.caption(
        "Aggregates composite signal scores across every tracked symbol in each sector. "
        "**Avg** > 30 = the sector is bullish overall; < −30 = bearish. "
        "**Bullish/bearish counts** = rows with score ≥ ±30. Sectors with the most "
        "extreme aggregates are usually where money's rotating in/out."
    )
    sec_df = pd.DataFrame(_sector_rows)
    sec_df["Avg label"] = sec_df["avg_score"].apply(
        lambda s: "🚀 Strong bull" if s >= 70 else
                  "🟢 Bullish" if s >= 30 else
                  "↗ Mild bull" if s >= 10 else
                  "🟡 Neutral" if s > -10 else
                  "↘ Mild bear" if s > -30 else
                  "🔴 Bearish" if s > -70 else
                  "💥 Strong bear"
    )
    sec_df = sec_df.rename(columns={
        "sector": "Sector",
        "row_count": "Tokens",
        "avg_score": "Avg score",
        "median_score": "Median",
        "bullish_count": "Bullish (≥+30)",
        "bearish_count": "Bearish (≤-30)",
        "sample_symbols": "Strongest signals",
    })
    sec_df = sec_df[[
        "Sector", "Avg label", "Avg score", "Median",
        "Bullish (≥+30)", "Bearish (≤-30)", "Tokens", "Strongest signals",
    ]]
    st.dataframe(
        sec_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Avg score": st.column_config.NumberColumn(format="%+.1f"),
            "Median": st.column_config.NumberColumn(format="%+d"),
            "Bullish (≥+30)": st.column_config.NumberColumn(format="%d"),
            "Bearish (≤-30)": st.column_config.NumberColumn(format="%d"),
            "Tokens": st.column_config.NumberColumn(format="%d"),
        },
    )

st.divider()

with st.expander("Configuration", expanded=False):
    st.write("**Settings (`config/settings.yaml`)**")
    st.json(settings(), expanded=False)
    st.write("**Fees (`config/fees.yaml`)**")
    st.json(fees(), expanded=False)

# Telegram status banner — show only on landing page.
import os  # noqa: E402
_tg_token = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
_tg_chat = bool(os.getenv("TELEGRAM_CHAT_ID"))
if _tg_token and _tg_chat:
    st.success(
        "🔔 **Telegram alerts: configured.** Alert thresholds are in `config/alerts.yaml`. "
        "Alerts fire on transitions (off→on) only, with a 4h cooldown per "
        "(alert_type, symbol)."
    )
else:
    st.info(
        "🔕 **Telegram alerts: not configured.** Set `TELEGRAM_BOT_TOKEN` and "
        "`TELEGRAM_CHAT_ID` env vars to enable. Setup instructions are in "
        "`config/alerts.yaml`. The screener works without alerts — they're additive."
    )
