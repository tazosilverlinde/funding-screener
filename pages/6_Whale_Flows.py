"""Page 6 — Exchange flows: 24h netflow per token (our own implementation).

Multi-chain (Round 10): scans Ethereum AND BNB Chain in parallel, two
independent loops with their own RPC fallback lists. Every row carries a
`chain` field so the user can filter by chain or see them merged.

Built on top of:
  - Public EVM JSON-RPC for each chain (no API keys, public free endpoints)
  - `config/exchange_wallets.yaml` — labeled CEX hot/cold wallets per chain
  - `config/eth_token_contracts.yaml` — ERC-20/BEP-20 contract addresses

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
    cooldown_banner,
    minutes_to,
    sidebar_status,
    symbol_search_sidebar,
)

st.set_page_config(page_title="Exchange Flows (on-chain)", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
auto_rerun(interval_ms=60_000, key="page6_tick")

st.title("Exchange flows — 24h on-chain netflow per token")
st.caption(
    "Sums every ERC-20 transfer to/from labeled CEX hot wallets in the last 24h, "
    "converted to USD using the futures mark price. Tokens shown are those traded "
    "on Binance or MEXC futures with a known Ethereum contract — limitations are "
    "real, see the bottom of the page."
)

cooldown_banner(store)
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

# Per-chain freshness — surfaces which chain is lagging.
by_chain = store.read_onchain_flows_by_chain()
if len(by_chain) > 1:
    cols = st.columns(len(by_chain))
    from datetime import datetime, timezone as _tz
    for i, (chain_name, (chain_flows, chain_at)) in enumerate(sorted(by_chain.items())):
        if chain_at:
            age_s = int((datetime.now(_tz.utc) - chain_at).total_seconds())
            label = f"{age_s}s ago" if age_s < 60 else f"{age_s // 60}m ago"
        else:
            label = "no data yet"
        cols[i].metric(f"{chain_name.upper()} flows", f"{len(chain_flows)} tokens", label)

# Chain filter — when both ETH and BSC have flows, lets the user narrow to one.
chain_options = sorted({r.get("chain", "ethereum") for r in flows})
if len(chain_options) > 1:
    selected_chains = st.multiselect(
        "Chains",
        chain_options,
        default=chain_options,
        format_func=lambda c: {"ethereum": "Ethereum", "bsc": "BNB Chain"}.get(c, c.title()),
        help="Filter rows by source chain. Token symbols can appear on multiple chains "
             "(e.g. USDT on ETH AND BSC) — both will show as separate rows.",
    )
    flows = [r for r in flows if r.get("chain", "ethereum") in selected_chains]

# Build a flat table — Chain column included so duplicates across chains are clear.
df = pd.DataFrame([
    {
        "Signal": f"{r['signal_emoji']} {r['signal_short']}",
        "Chain": r.get("chain", "ethereum").upper(),
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
        "Chain": st.column_config.TextColumn(
            "Chain",
            help="Source chain — ETHEREUM (most blue-chips, USDT/USDC native), "
                 "BSC (Binance-Peg wrapped versions: BTCB ≈ BTC, ETH-on-BSC). "
                 "A token can appear on both chains as separate rows.",
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

# ---------------- whale flows (large-transfer subset) ----------------

st.divider()
st.subheader("Whale flows — large transfers (>$500K), 24h")
st.caption(
    "Subset of the table above: only transfers ≥ $500K **between an exchange and a "
    "non-excluded wallet**. We strip out DEX routers, bridges, protocol contracts, "
    "and known market makers (see `config/non_whale_addresses.yaml`) so what's left "
    "is genuine whale-vs-exchange activity. **Auto-discovered** — no manual list of "
    "specific whales is required; any wallet that moved > $500K to/from an exchange "
    "in 24h gets counted."
)

# Conviction filter — single whale moves can be one wallet's idiosyncratic
# behaviour and shouldn't show as a "signal". Default to 2 distinct whales for
# conviction; user can tighten or loosen via the slider.
min_whales = st.slider(
    "Conviction filter — minimum distinct whale addresses",
    min_value=1, max_value=10, value=2, step=1,
    help=(
        "Drop tokens where fewer than this many DISTINCT non-excluded addresses "
        "transacted with exchanges. With min=1 you see every flagged token, but "
        "single-whale rows can be one wallet's idiosyncratic move (not a cohort signal). "
        "Default 2 = 'at least two independent whales agree on direction'."
    ),
)

# Pull whale subset from the same flows we already loaded above.
whale_rows = []
filtered_low_conviction = 0
for r in flows:
    wn = r.get("whale_net_usd", 0.0) or 0.0
    wd = r.get("whale_deposits_usd", 0.0) or 0.0
    ww = r.get("whale_withdrawals_usd", 0.0) or 0.0
    wcount = r.get("whale_unique_count", 0) or 0
    if wcount == 0 and wd == 0 and ww == 0:
        continue  # nothing whale-class for this token
    if wcount < min_whales:
        filtered_low_conviction += 1
        continue  # below conviction threshold
    # Signal classification — same heuristic as the broader netflow.
    emoji, short = ("🟢", "Whale accumulation") if wn > 0 else (
        ("🔴", "Whale distribution") if wn < 0 else ("🟡", "Mixed")
    )
    whale_rows.append({
        "Signal": f"{emoji} {short}",
        "Chain": r.get("chain", "ethereum").upper(),
        "Token": r["token"],
        "Whale net (USD)": wn,
        "Whale withdrawals (USD)": ww,
        "Whale deposits (USD)": wd,
        "Unique whales": wcount,
    })

if not whale_rows:
    st.info(
        "No transfers above the $500K threshold in the last 24h for tracked tokens — "
        "either market is calm, or wait for the next 15-min on-chain cycle. "
        "Lower the threshold in `compute_token_netflow` (background.py) to be more sensitive."
    )
else:
    whale_df = pd.DataFrame(whale_rows).sort_values("Whale net (USD)", ascending=False)
    st.dataframe(
        whale_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Signal": st.column_config.TextColumn(
                "Signal",
                help=(
                    "🟢 Whale accumulation — large withdrawals exceed large deposits, "
                    "i.e. whales are moving coins OFF exchanges (often bullish setup).\n"
                    "🔴 Whale distribution — large deposits exceed large withdrawals "
                    "(often bearish, pre-sale positioning).\n"
                    "🟡 Mixed — roughly balanced large activity."
                ),
            ),
            "Whale net (USD)": st.column_config.NumberColumn(
                format="$%+,.0f",
                help="Whale withdrawals − whale deposits, USD. Positive = bullish bias.",
            ),
            "Whale withdrawals (USD)": st.column_config.NumberColumn(
                format="$%,.0f",
                help="Sum of single transfers ≥ $500K from an exchange to a non-excluded address in 24h.",
            ),
            "Whale deposits (USD)": st.column_config.NumberColumn(
                format="$%,.0f",
                help="Sum of single transfers ≥ $500K from a non-excluded address to an exchange in 24h.",
            ),
            "Unique whales": st.column_config.NumberColumn(
                format="%d",
                help="Distinct non-excluded counterparty addresses that participated. "
                     "More whales = stronger signal; 1-whale rows can be one wallet's idiosyncratic move.",
            ),
        },
    )
    n_distinct_whales = sum(r["Unique whales"] for r in whale_rows)
    suffix = (
        f" — {filtered_low_conviction} additional tokens with <{min_whales} whales "
        f"hidden by conviction filter."
        if filtered_low_conviction > 0 else ""
    )
    st.caption(
        f"{len(whale_df)} tokens with whale activity in 24h, "
        f"{n_distinct_whales} distinct whale addresses involved across all tokens" + suffix
    )

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
- **EVM chains only (Ethereum + BNB Chain).** Solana, TRON, Bitcoin native, Cosmos,
  etc. need their own RPC clients (different log format) — not yet supported.
- **Wallet coverage** — `config/exchange_wallets.yaml` has the major Binance/MEXC/OKX/
  Bybit/Coinbase/Kraken hot wallets per chain, but not every wallet of every exchange.
  Adding a missing wallet just makes the netflow more accurate.
- **Whale auto-discovery** — every wallet that moves > $500K to/from an exchange counts
  as a whale; we don't need a curated list. The exclusion list in
  `config/non_whale_addresses.yaml` filters out routine plumbing (DEX routers, bridges,
  market makers). Adding to that file just removes more noise; it never hides real whales.
- **Internal exchange shuffling** — when an exchange moves between its own wallets, both
  endpoints are in our list and we cancel the flow out. So Binance-to-Binance moves
  correctly net to zero (per chain).
- **Cross-chain duplicates** — USDT, USDC, BTC etc. appear on both ETH and BSC as
  separate rows; they're not summed (different on-chain liquidity pools, different
  whale audiences). Use the Chains filter above to focus on one.
- **Macro flows below are still ETH-only** — adding BSC to the macro chart is on the
  roadmap but requires a separate per-chain layout (different liquidity dynamics).
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
