"""Page 8 — Binance liquidation tape (last 24h).

Sourced live from the public `wss://fstream.binance.com/ws/!forceOrder@arr`
WebSocket — no API key, no rate limit on this stream. Each event is one
forced position close; we aggregate per symbol over the rolling 24h window.

Reading the table
=================
- **Long liq $** = total notional of LONG positions force-liquidated. Big
  long-liq spikes typically follow a sharp DROP (longs got blown out as the
  price fell into their stops).
- **Short liq $** = same for SHORT positions. Big short-liq spikes follow a
  sharp PUMP (shorts got squeezed).
- **Bias** = the side that lost more (proxy for which way the move went).

Limitations
===========
- Binance perps only. MEXC doesn't expose a public liquidation stream.
- 24h rolling. After process restart, the buffer rebuilds from zero;
  the first events arrive within seconds and a full 24h fills out over
  a day. The "Total events seen" counter at the bottom shows lifetime.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    cooldown_banner,
    sidebar_status,
    symbol_search_sidebar,
)

st.set_page_config(page_title="Liquidations (24h)", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
auto_rerun(interval_ms=30_000, key="liq_tick")

st.title("Liquidations — last 24h (Binance perps)")
st.caption(
    "Force-order events streamed from `!forceOrder@arr` WebSocket, aggregated "
    "per symbol over the rolling 24-hour window. Restart drops the buffer; "
    "expect 1-2 events per second under normal market conditions."
)

cooldown_banner(store)


# ---- Window picker ----
window_label_to_seconds = {
    "Last 1h": 3600,
    "Last 4h": 4 * 3600,
    "Last 12h": 12 * 3600,
    "Last 24h": 24 * 3600,
}
window_label = st.radio(
    "Window",
    list(window_label_to_seconds.keys()),
    index=3,
    horizontal=True,
)
window_seconds = window_label_to_seconds[window_label]


# ---- Health row ----
health = store.liquidations_health()
total_seen = health["total_events"]
last_at = health["last_event_at"]

h1, h2, h3 = st.columns(3)
h1.metric(
    "Lifetime events seen",
    f"{total_seen:,}",
    help="Total liquidation events received since process start. Buffer is "
         "in-memory only — restarts reset this to zero.",
)
if last_at:
    age_s = max(0, time.time() - last_at)
    if age_s < 5:
        h2.metric("Last event", f"{age_s:.1f}s ago", delta="LIVE")
    elif age_s < 60:
        h2.metric("Last event", f"{age_s:.0f}s ago")
    else:
        h2.metric(
            "Last event", f"{age_s / 60:.0f}m ago",
            delta="WS may be stalled" if age_s > 300 else None,
            delta_color="inverse" if age_s > 300 else "off",
        )
else:
    h2.metric("Last event", "—", help="No events received yet — WS still connecting?")
agg_now = store.read_liquidations(window_seconds=window_seconds)
h3.metric("Symbols active in window", f"{len(agg_now)}")


if not agg_now:
    st.info(
        "No liquidations in the selected window yet — either the WebSocket is "
        "still connecting (first events arrive within ~10s) or the market is "
        "unusually quiet. Refresh in a minute."
    )
    st.stop()


# ---- Build table ----
rows: list[dict] = []
for symbol, stats in agg_now.items():
    long_usd = stats["long_liq_usd"]
    short_usd = stats["short_liq_usd"]
    total = stats["total_usd"]
    if long_usd > short_usd:
        bias_emoji, bias = "🔴", "Longs liquidated"
    elif short_usd > long_usd:
        bias_emoji, bias = "🟢", "Shorts liquidated"
    else:
        bias_emoji, bias = "🟡", "Balanced"
    rows.append({
        "Symbol": f"/Symbol_Detail?exchange=Binance&symbol={symbol}",
        "Bias": f"{bias_emoji} {bias}",
        "Total ($)": total,
        "Long liq ($)": long_usd,
        "Short liq ($)": short_usd,
        "Biggest single ($)": stats["biggest_single_usd"],
        "Biggest side": (stats["biggest_single_side"] or "—").title(),
        "# events": stats["events_count"],
    })

df = pd.DataFrame(rows).sort_values("Total ($)", ascending=False)

st.dataframe(
    df,
    hide_index=True,
    use_container_width=True,
    column_config={
        "Symbol": st.column_config.LinkColumn(
            "Symbol",
            help="Click to open the per-symbol detail page.",
            display_text=r".*symbol=([^&]+)",
        ),
        "Bias": st.column_config.TextColumn(
            "Bias",
            help=(
                "Which side took bigger losses in this window.\n\n"
                "🔴 Longs liquidated — usually after a sharp DROP\n"
                "🟢 Shorts liquidated — usually after a sharp PUMP\n"
                "🟡 Balanced — mixed conditions"
            ),
        ),
        "Total ($)": st.column_config.NumberColumn(
            format="$%,.0f",
            help="Total notional value of all liquidations in the window (longs + shorts).",
        ),
        "Long liq ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Short liq ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Biggest single ($)": st.column_config.NumberColumn(
            format="$%,.0f",
            help="Notional of the single largest liquidation event in the window. "
                 "Outsized values often indicate one large trader getting blown out.",
        ),
        "Biggest side": st.column_config.TextColumn("Biggest side"),
        "# events": st.column_config.NumberColumn(format="%d"),
    },
)


st.divider()


# ---- Top movers (long vs short bias) ----
top_n = min(10, len(df))
left, right = st.columns(2)

long_dominant = df[df["Long liq ($)"] > df["Short liq ($)"]].head(top_n)
right.write(f"**Top {len(long_dominant)} long-liquidation symbols** ({window_label.lower()})")
right.caption(
    "Where longs got blown out the hardest. After a sharp drop these are "
    "where the cascade ran — sometimes setup for a relief bounce as forced "
    "selling abates."
)
right.dataframe(
    long_dominant[["Symbol", "Long liq ($)", "Short liq ($)", "# events"]],
    hide_index=True,
    column_config={
        "Symbol": st.column_config.LinkColumn(
            "Symbol", display_text=r".*symbol=([^&]+)"
        ),
        "Long liq ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Short liq ($)": st.column_config.NumberColumn(format="$%,.0f"),
    },
)

short_dominant = df[df["Short liq ($)"] > df["Long liq ($)"]].head(top_n)
left.write(f"**Top {len(short_dominant)} short-liquidation symbols** ({window_label.lower()})")
left.caption(
    "Where shorts got squeezed the hardest. Squeezes are self-reinforcing — "
    "forced buying lifts price further, triggering more shorts. Watch for "
    "exhaustion (volume drop while short-liq still climbing)."
)
left.dataframe(
    short_dominant[["Symbol", "Short liq ($)", "Long liq ($)", "# events"]],
    hide_index=True,
    column_config={
        "Symbol": st.column_config.LinkColumn(
            "Symbol", display_text=r".*symbol=([^&]+)"
        ),
        "Short liq ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Long liq ($)": st.column_config.NumberColumn(format="$%,.0f"),
    },
)
