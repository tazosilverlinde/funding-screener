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
)

st.set_page_config(page_title="Funding Screener", layout="wide", page_icon=":material/monitoring:")

store = boot()
sidebar_status(store)
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

with st.expander("Configuration", expanded=False):
    st.write("**Settings (`config/settings.yaml`)**")
    st.json(settings(), expanded=False)
    st.write("**Fees (`config/fees.yaml`)**")
    st.json(fees(), expanded=False)
