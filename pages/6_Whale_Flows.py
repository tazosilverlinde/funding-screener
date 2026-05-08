"""Page 6 — Exchange flows: 24h netflow per token (our own implementation).

Built on top of:
  - Public Ethereum JSON-RPC (no API key)
  - `config/exchange_wallets.yaml` — labeled CEX hot/cold wallets
  - `config/eth_token_contracts.yaml` — ERC-20 contract addresses we know

Filtered to tokens that have a Binance or MEXC futures contract, so every row
is something the user can actually trade.

Net interpretation:
  - **net_usd > 0** (withdrawals exceed deposits) → coins moved off-exchange,
    likely to cold storage = **bullish accumulation** signal
  - **net_usd < 0** (deposits exceed withdrawals) → coins moved to exchange,
    typically pre-sale positioning = **bearish distribution** signal
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    minutes_to,
    sidebar_status,
)

st.set_page_config(page_title="Exchange Flows (on-chain)", layout="wide")

store = boot()
sidebar_status(store)
auto_rerun(interval_ms=60_000, key="page6_tick")

st.title("Exchange flows — 24h on-chain netflow per token")
st.caption(
    "Sums every ERC-20 transfer to/from labeled CEX hot wallets in the last 24h, "
    "converted to USD using the futures mark price. Tokens shown are those traded "
    "on Binance or MEXC futures with a known Ethereum contract — limitations are "
    "real, see the bottom of the page."
)

flows, fetched_at = store.read_onchain_flows()

# Freshness pill
if fetched_at:
    age = minutes_to(fetched_at) or "—"  # this returns "settled" for past, but we want elapsed
    from datetime import datetime, timezone
    delta_min = int((datetime.now(timezone.utc) - fetched_at).total_seconds() / 60)
    if delta_min < 1:
        st.success(f"Last on-chain scan: {int((datetime.now(timezone.utc) - fetched_at).total_seconds())}s ago")
    elif delta_min < 30:
        st.info(f"Last on-chain scan: {delta_min}m ago")
    else:
        st.warning(f"Last on-chain scan: {delta_min}m ago — RPC may be struggling")
else:
    st.info(
        "On-chain scan hasn't completed yet. The first scan can take 1-3 minutes "
        "(querying Ethereum logs for ~25 tokens × 2 directions). "
        "Refresh in a minute."
    )

if not flows:
    st.stop()

# Build a flat table.
df = pd.DataFrame([
    {
        "Signal": f"{r['signal_emoji']} {r['signal_short']}",
        "Token": r["token"],
        "Net (USD)": r["net_usd"],
        "Deposits (USD)": r["deposits_usd"],
        "Withdrawals (USD)": r["withdrawals_usd"],
        "Total (USD)": r["deposits_usd"] + r["withdrawals_usd"],
        "# transfers": r["deposit_count"] + r["withdrawal_count"],
        "Top exchange": _top_exchange(r.get("by_exchange") or {}),
    }
    for r in flows
])

st.dataframe(
    df,
    hide_index=True,
    use_container_width=True,
    column_config={
        "Signal": st.column_config.TextColumn(
            "Signal",
            help=(
                "🟢 Accumulation — withdrawals exceeded deposits; coins moving off-exchange.\n"
                "🟢 Strong accumulation — same, but |net| is more than 50% of total volume (high conviction).\n"
                "🔴 Distribution — deposits exceeded withdrawals; coins moving to exchanges (pre-sale).\n"
                "🔴 Heavy distribution — same, with |net| > 50% of total.\n"
                "🟡 Neutral — |net| < $250K, below the noise floor."
            ),
        ),
        "Token": st.column_config.TextColumn("Token", help="Symbol matching the Binance/MEXC futures ticker."),
        "Net (USD)": st.column_config.NumberColumn(
            format="$%+,.0f",
            help=(
                "Withdrawals − Deposits over the last 24h.\n"
                "Positive (bullish) = coins flowing OUT of exchanges to cold storage.\n"
                "Negative (bearish) = coins flowing IN to exchanges (often pre-sale)."
            ),
        ),
        "Deposits (USD)": st.column_config.NumberColumn(
            format="$%,.0f",
            help="Total USD value of token transfers TO labeled exchange hot wallets in 24h.",
        ),
        "Withdrawals (USD)": st.column_config.NumberColumn(
            format="$%,.0f",
            help="Total USD value of token transfers FROM labeled exchange hot wallets in 24h.",
        ),
        "Total (USD)": st.column_config.NumberColumn(
            format="$%,.0f",
            help="Deposits + Withdrawals (absolute throughput) — context for whether net is meaningful.",
        ),
        "# transfers": st.column_config.NumberColumn(format="%d"),
        "Top exchange": st.column_config.TextColumn(
            "Top exchange",
            help="Exchange with the largest absolute flow for this token (depositor or withdrawer).",
        ),
    },
)

st.caption(
    f"Showing {len(df)} tokens that have both a Binance/MEXC futures contract and a known "
    "ERC-20 address. Sorted by net flow (most-bullish at top)."
)

st.divider()

with st.expander("Per-exchange breakdown", expanded=False):
    rows: list[dict] = []
    for r in flows:
        for ex, pair in (r.get("by_exchange") or {}).items():
            rows.append({
                "Token": r["token"],
                "Exchange": ex.title(),
                "Deposits (USD)": pair.get("deposits_usd", 0.0),
                "Withdrawals (USD)": pair.get("withdrawals_usd", 0.0),
                "Net (USD)": pair.get("withdrawals_usd", 0.0) - pair.get("deposits_usd", 0.0),
            })
    if rows:
        bdf = pd.DataFrame(rows).sort_values("Net (USD)", ascending=False)
        st.dataframe(
            bdf, hide_index=True, use_container_width=True,
            column_config={
                "Deposits (USD)": st.column_config.NumberColumn(format="$%,.0f"),
                "Withdrawals (USD)": st.column_config.NumberColumn(format="$%,.0f"),
                "Net (USD)": st.column_config.NumberColumn(format="$%+,.0f"),
            },
        )
    else:
        st.write("No per-exchange detail available yet.")

st.divider()

# ---------------- 7-day daily flows for stables + BTC + ETH ----------------

st.divider()
st.subheader("Macro flows — last 7 days, daily")
st.caption(
    "Net flow per day for stablecoins (USDT, USDC) + BTC (via WBTC) + ETH (via WETH). "
    "Positive bars = withdrawals exceeded deposits (off-exchange accumulation). "
    "Negative bars = deposits exceeded withdrawals (likely sell-side flow)."
)

macro_flows, macro_at = store.read_macro_daily_flows()
if not macro_flows:
    st.info(
        "First 7-day macro-flow scan in progress. Refresh in a few minutes — "
        "this loop runs every 6 hours and the first scan after deploy takes 1-3 minutes."
    )
else:
    macro_cols = st.columns(min(4, len(macro_flows)))
    for i, sym in enumerate(["USDT", "USDC", "WBTC", "WETH"]):
        if sym not in macro_flows:
            continue
        col = macro_cols[i % len(macro_cols)]
        rows = macro_flows[sym]
        net_total_7d = sum(r["net_usd"] for r in rows)
        col.metric(
            f"{sym} 7d net",
            f"${net_total_7d / 1e6:+,.1f}M",
            help=f"Sum of net flow for {sym} over the 7-day window. "
                 f"Positive = net withdrawals (bullish bias for the asset).",
        )

    # Build a stacked bar chart.
    chart_rows: list[dict] = []
    for sym, rows in macro_flows.items():
        for r in rows:
            chart_rows.append({
                "Date": r["date"],
                "Token": sym,
                "Net (USD)": r["net_usd"],
            })
    chart_df = pd.DataFrame(chart_rows)
    if not chart_df.empty:
        # Pivot so each token is its own series for st.bar_chart.
        pivot = chart_df.pivot(index="Date", columns="Token", values="Net (USD)").fillna(0.0)
        st.bar_chart(pivot, height=280)
        st.caption(
            "Bar height per day = net USD flow (withdrawals − deposits). "
            "Stacked across the 4 macro tokens."
        )

    with st.expander("Per-day breakdown table", expanded=False):
        # Long table grouped by token.
        for sym, rows in macro_flows.items():
            st.markdown(f"**{sym}**")
            sym_df = pd.DataFrame(rows).rename(columns={
                "date": "Date",
                "deposits_usd": "Deposits (USD)",
                "withdrawals_usd": "Withdrawals (USD)",
                "net_usd": "Net (USD)",
            })
            st.dataframe(
                sym_df, hide_index=True, use_container_width=True,
                column_config={
                    "Deposits (USD)": st.column_config.NumberColumn(format="$%,.0f"),
                    "Withdrawals (USD)": st.column_config.NumberColumn(format="$%,.0f"),
                    "Net (USD)": st.column_config.NumberColumn(format="$%+,.0f"),
                },
            )

    if macro_at:
        from datetime import datetime, timezone as _tz
        age_min = int((datetime.now(_tz.utc) - macro_at).total_seconds() / 60)
        st.caption(f"Macro-flow scan: {age_min}m ago. Refreshes every 6 hours.")

st.divider()

st.markdown(
    """
**Honest limitations:**
- **ETH chain only.** Tokens whose primary chain is Solana, BNB, TRON, etc. show no rows
  (BTC, SOL, BNB, XRP, DOGE, ADA, AVAX, MATIC, TON, …).
- **Wallet coverage** — `config/exchange_wallets.yaml` has the major Binance/MEXC/OKX/
  Bybit/Coinbase/Kraken hot wallets, but not every wallet of every exchange. Adding a
  missing wallet just makes the netflow more accurate.
- **Internal exchange shuffling** — when an exchange moves between its own wallets, both
  endpoints are in our list and we cancel the flow out. So Binance-to-Binance moves
  correctly net to zero.
- **Token coverage** — `config/eth_token_contracts.yaml` has top ~25 tokens with both a
  Binance/MEXC futures contract AND an ERC-20 contract. Add to YAML to track more.
"""
)


# ---- helpers ----

def _top_exchange(by_exchange: dict) -> str:
    if not by_exchange:
        return "—"
    best: tuple[str, float] = ("", 0.0)
    for ex, pair in by_exchange.items():
        absflow = abs(pair.get("deposits_usd", 0.0)) + abs(pair.get("withdrawals_usd", 0.0))
        if absflow > best[1]:
            best = (ex, absflow)
    return best[0].title() or "—"
