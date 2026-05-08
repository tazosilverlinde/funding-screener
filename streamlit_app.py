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
