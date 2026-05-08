"""Page 2 — Combined high funding (Binance + MEXC, side by side, USDT pairs).

One row per base asset. If the base only exists on one exchange, the other side
shows as empty. Filtered to rows where at least one side's 8h-normalized rate
exceeds the threshold from `config/settings.yaml`.

The "Signal" column gives a directional bias label (🟢/🔴/🟡/⚠️) computed from
funding rate, funding streak, and mark/index spread — see `signals.py` for the
full ruleset.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from funding_screener.config import settings  # noqa: E402
from funding_screener.screener import screen_combined_high_funding  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    filter_dataframe_to_watchlist,
    freshness_banner,
    minutes_to,
    render_table,
    sector_sidebar,
    sidebar_status,
    symbol_search_sidebar,
    to_df,
    watchlist_sidebar,
)

st.set_page_config(page_title="High Funding (combined)", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
watchlist = watchlist_sidebar()
sector_bases = sector_sidebar()
auto_rerun(interval_ms=30_000, key="page2_tick")

cfg = settings()
threshold = float(cfg["high_funding"]["threshold_percent"])
min_volume_per_side = float(cfg["high_funding"].get("min_24h_volume_usd_per_side", 0))

st.title("High funding — Binance & MEXC combined")
st.caption(
    f"USDT and USDC perpetuals from both exchanges, joined by (base, quote). "
    f"A row is shown when at least one side's **8h-normalized** funding rate "
    f"exceeds ±{threshold:.2f}%. WIF can appear twice — once for `WIFUSDT` and "
    f"once for `WIFUSDC` — since each has its own funding rate. Empty cells mean "
    f"the contract isn't actively trading on that exchange (delisted, or 24h "
    f"volume below the ${min_volume_per_side:,.0f} liquidity floor). "
    "Hover any column header for an explanation."
)

freshness_banner(store)
st.divider()

binance = store.read_binance()
mexc = store.read_mexc()
enrichments = store.read_enrichments()
onchain_flows, _ = store.read_onchain_flows()
onchain_by_base: dict[str, float] = {f["token"]: f.get("net_usd", 0.0) for f in onchain_flows}
# Combine Binance + MEXC kline maps so vol can be computed for either side.
combined_klines: dict = {}
combined_klines.update(binance.klines)
combined_klines.update(mexc.klines)
rows = screen_combined_high_funding(
    binance.funding,
    mexc.funding,
    binance.contracts,
    mexc.contracts,
    enrichments,
    threshold,
    binance_volumes=binance.volumes,
    mexc_volumes=mexc.volumes,
    min_volume_usd_per_side=min_volume_per_side,
    onchain_netflow_by_base=onchain_by_base,
    klines_by_symbol=combined_klines,
)

# Apply sector filter on the *row* set before truncation so sector picks
# don't get drowned out by row_limit.
if sector_bases:
    rows = [r for r in rows if r.base_asset.upper() in sector_bases]

cap = int(cfg["row_limit"])
df = to_df(
    [r.model_dump() for r in rows[:cap]],
    column_order=[
        "composite_score",
        "composite_emoji",
        "composite_short",
        "signal_emoji",
        "signal_short",
        "base_asset",
        "quote_asset",
        "sector",
        "binance_symbol",
        "binance_rate_percent",
        "binance_rate_8h_norm_percent",
        "binance_interval_hours",
        "binance_next_funding_time",
        "binance_maker_fee_percent",
        "binance_mark_index_spread_percent",
        "binance_funding_streak",
        "binance_oi_change_24h_pct",
        "binance_ls_ratio_global",
        "binance_ls_ratio_top",
        "binance_volume_24h_millions",
        "realized_vol_30d_pct",
        "funding_per_vol",
        "mexc_symbol",
        "mexc_rate_percent",
        "mexc_rate_8h_norm_percent",
        "mexc_interval_hours",
        "mexc_next_funding_time",
        "mexc_maker_fee_percent",
        "mexc_funding_streak",
        "mexc_volume_24h_millions",
        "spread_8h_norm_percent",
    ],
)

if not df.empty:
    # Composite Score column.
    df["Score"] = df["composite_score"]
    df["Score label"] = (
        df["composite_emoji"].fillna("") + " " + df["composite_short"].fillna("")
    )
    df = df.drop(columns=["composite_score", "composite_emoji", "composite_short"])
    # Combine emoji + short label into one cell for compact display.
    df["Signal"] = df["signal_emoji"].fillna("") + " " + df["signal_short"].fillna("")
    df = df.drop(columns=["signal_emoji", "signal_short"])
    # Move Score + Signal to the front.
    front = ["Score", "Score label", "Signal"]
    cols = front + [c for c in df.columns if c not in front]
    df = df[cols]

    # Make symbol columns clickable → detail page.
    df["binance_symbol"] = df["binance_symbol"].apply(
        lambda s: f"/Symbol_Detail?exchange=Binance&symbol={s}" if s else ""
    )
    df["mexc_symbol"] = df["mexc_symbol"].apply(
        lambda s: f"/Symbol_Detail?exchange=MEXC&symbol={s}" if s else ""
    )
    # Convert next-funding datetimes to minutes-left strings.
    df["binance_next_funding_time"] = df["binance_next_funding_time"].apply(minutes_to)
    df["mexc_next_funding_time"] = df["mexc_next_funding_time"].apply(minutes_to)

    df = df.rename(
        columns={
            "base_asset": "Base",
            "quote_asset": "Quote",
            "sector": "Sector",
            "binance_symbol": "Binance symbol",
            "binance_rate_percent": "Bnb rate %/period",
            "binance_rate_8h_norm_percent": "Bnb % / 8h",
            "binance_interval_hours": "Bnb interval (h)",
            "binance_next_funding_time": "Bnb next in",
            "binance_maker_fee_percent": "Bnb fee %",
            "binance_mark_index_spread_percent": "Bnb mark/idx %",
            "binance_funding_streak": "Bnb streak",
            "binance_oi_change_24h_pct": "Bnb OI 24h Δ%",
            "binance_ls_ratio_global": "Bnb L/S retail",
            "binance_ls_ratio_top": "Bnb L/S top",
            "binance_volume_24h_millions": "Bnb 24h vol (M)",
            "realized_vol_30d_pct": "30d vol %",
            "funding_per_vol": "Funding / vol",
            "mexc_symbol": "MEXC symbol",
            "mexc_rate_percent": "MXC rate %/period",
            "mexc_rate_8h_norm_percent": "MXC % / 8h",
            "mexc_interval_hours": "MXC interval (h)",
            "mexc_next_funding_time": "MXC next in",
            "mexc_maker_fee_percent": "MXC fee %",
            "mexc_funding_streak": "MXC streak",
            "mexc_volume_24h_millions": "MXC 24h vol (M)",
            "spread_8h_norm_percent": "Spread / 8h",
        }
    )
    pct = "%.4f"
    col_cfg = {
        "Score": st.column_config.NumberColumn(
            "Score",
            format="%+d",
            help=(
                "Composite signal score, signed [-100..+100]. **Positive = long bias**, "
                "negative = short bias.\n\n"
                "Combines: funding rate (±30), streak (±15), OI 24h Δ × funding direction (±15), "
                "L/S ratio extremity (±10), smart-vs-retail divergence (±5), on-chain netflow (±15). "
                "Mark/index divergence > 0.5% damps conviction by 50%.\n\n"
                "Score thresholds:\n"
                "  +70 .. +100 → 🚀 Strong bull\n"
                "  +30 .. +70  → 🟢 Bullish\n"
                "  +10 .. +30  → ↗ Mild bull\n"
                "  -10 .. +10  → 🟡 Neutral\n"
                "  -30 .. -10  → ↘ Mild bear\n"
                "  -70 .. -30  → 🔴 Bearish\n"
                "  -100 .. -70 → 💥 Strong bear"
            ),
        ),
        "Score label": st.column_config.TextColumn(
            "Score label",
            help="Human-readable bucket of the composite score.",
        ),
        "Signal": st.column_config.TextColumn(
            "Signal",
            help=(
                "Composite directional bias derived from funding rate, funding streak, "
                "and mark/index spread.\n\n"
                "Legend:\n"
                "🟢 Bullish — shorts paying longs (you'd want to be long for funding)\n"
                "🔴 Bearish — longs paying shorts (you'd want to be short for funding)\n"
                "📈 Persistent bull — 3+ consecutive negative-funding periods, strong squeeze setup\n"
                "📉 Persistent bear — 3+ consecutive positive-funding periods, longs over-leveraged\n"
                "🟡 Neutral — funding within normal band\n"
                "⚠️ Risk — mark vs index diverged > 0.5%, possible liquidation cascade or manipulation"
            ),
        ),
        "Base": st.column_config.TextColumn(
            "Base",
            help="The underlying asset (e.g., BTC, WIF). One row per (base, quote) combination.",
        ),
        "Quote": st.column_config.TextColumn(
            "Quote",
            help=(
                "Quote currency: USDT or USDC. Each base/quote combination gets its own row "
                "because their funding rates are independent — `WIFUSDT` and `WIFUSDC` may both "
                "have high funding at the same time."
            ),
        ),
        "Sector": st.column_config.TextColumn(
            "Sector",
            help=(
                "Category from `config/symbol_sectors.yaml`. Use the sidebar Sector filter "
                "to narrow rows to specific categories (e.g. only memes, only Layer-1s)."
            ),
        ),
        "Binance symbol": st.column_config.LinkColumn(
            "Binance symbol",
            display_text=r"symbol=([A-Z0-9_]+)",
            help="Full Binance perp contract name. Click to open full detail.",
        ),
        "Bnb rate %/period": st.column_config.NumberColumn(
            format=pct,
            help=(
                "Funding rate that will be paid/received at next settlement.\n"
                "Positive = longs pay shorts; negative = shorts pay longs.\n"
                "Settled every 'Bnb interval' hours."
            ),
        ),
        "Bnb % / 8h": st.column_config.NumberColumn(
            format=pct,
            help=(
                "Funding rate normalized to an 8h period. Lets you compare pairs with "
                "different funding intervals (some are 4h or 1h, most are 8h).\n"
                "Above ±1% per 8h is considered high."
            ),
        ),
        "Bnb interval (h)": st.column_config.NumberColumn(
            format="%.0f",
            help="How often funding settles on this contract. Common: 8h. Some: 4h, 2h, 1h.",
        ),
        "Bnb next in": st.column_config.TextColumn(
            "Bnb next in",
            help="Time remaining until next Binance funding settlement. Format: '47m' or '2h 15m'.",
        ),
        "Bnb fee %": st.column_config.NumberColumn(
            format=pct,
            help=(
                "Binance futures maker fee for this contract (default-tier).\n"
                "Used in arb math: total round-trip fees = 4 × this (open + close on each leg)."
            ),
        ),
        "Bnb mark/idx %": st.column_config.NumberColumn(
            format=pct,
            help=(
                "(mark_price − index_price) / index_price × 100.\n"
                "Mark price is what liquidations are computed against; index is the spot reference.\n"
                "|spread| > 0.5% is unusual and often precedes liquidation cascades or signals "
                "manipulation of an illiquid contract."
            ),
        ),
        "Bnb streak": st.column_config.TextColumn(
            "Bnb streak",
            help=(
                "Consecutive same-sign funding periods, starting from the most recent settlement.\n"
                "Format: '3↑' = 3 consecutive POSITIVE periods (longs paying)\n"
                "         '2↓' = 2 consecutive NEGATIVE periods (shorts paying)\n"
                "Streaks of 3+ indicate persistent over-leveraged positioning."
            ),
        ),
        "MEXC symbol": st.column_config.LinkColumn(
            "MEXC symbol",
            display_text=r"symbol=([A-Z0-9_]+)",
            help="Full MEXC perp contract name. Click to open full detail.",
        ),
        "MXC rate %/period": st.column_config.NumberColumn(format=pct, help="Same as Bnb rate %/period but for MEXC."),
        "MXC % / 8h": st.column_config.NumberColumn(format=pct, help="Same as Bnb % / 8h but for MEXC."),
        "MXC interval (h)": st.column_config.NumberColumn(format="%.0f", help="MEXC funding settlement interval."),
        "MXC next in": st.column_config.TextColumn(
            "MXC next in",
            help="Time remaining until next MEXC funding settlement. Format: '47m' or '2h 15m'.",
        ),
        "MXC fee %": st.column_config.NumberColumn(
            format=pct,
            help=(
                "MEXC per-contract maker fee from /contract/detail (varies by symbol).\n"
                "Many MEXC perps are 0% maker — check column for actual values."
            ),
        ),
        "MXC streak": st.column_config.TextColumn(
            "MXC streak", help="Same as Bnb streak but for MEXC funding settlements."
        ),
        "Bnb 24h vol (M)": st.column_config.NumberColumn(
            format="%.2f",
            help=(
                "Binance contract's 24h quote volume in millions of USDT/USDC.\n"
                "Below ~1M → illiquid. Funding rate on a low-volume contract is noisy and may "
                "not reflect a tradeable opportunity (slippage and orderbook gaps eat the edge)."
            ),
        ),
        "Bnb OI 24h Δ%": st.column_config.NumberColumn(
            format="%+.2f",
            help=(
                "Binance open-interest 24h change in percent.\n"
                "+30%+ with rising price = real new money buying (momentum, often follow-able).\n"
                "+30%+ with falling price = shorts loading up (squeeze fuel building).\n"
                "−20%+ = mass position unwind (capitulation or take-profit)."
            ),
        ),
        "Bnb L/S retail": st.column_config.NumberColumn(
            format="%.2f",
            help=(
                "Binance global account long/short ratio (retail-dominated).\n"
                ">3 = ~75% of accounts long → crowded long, contrarian short signal.\n"
                "<0.4 = ~70%+ short → crowded short, squeeze risk for shorts.\n"
                "0.7–1.5 = normal/balanced."
            ),
        ),
        "Bnb L/S top": st.column_config.NumberColumn(
            format="%.2f",
            help=(
                "Binance top-trader long/short ratio (top 20% by collateral).\n"
                "Compare to L/S retail — when top traders disagree with retail, it's a smart-vs-dumb-money signal:\n"
                "  • Top short + retail long → smart money positioned against retail (bearish bias)\n"
                "  • Top long + retail short → smart money positioned against retail (bullish bias)"
            ),
        ),
        "30d vol %": st.column_config.NumberColumn(
            format="%.1f",
            help=(
                "Annualized 30-day realized volatility (%), computed from daily kline closes.\n"
                "BTC ≈ 30-50% / blue chips ≈ 50-80% / mid-caps ≈ 80-150% / memes 200%+.\n"
                "High funding paired with HIGH vol means the funding edge is being eaten by price risk."
            ),
        ),
        "Funding / vol": st.column_config.NumberColumn(
            format="%.4f",
            help=(
                "Signed ratio: 8h-normalized funding rate ÷ realized 30-day vol (decimal).\n"
                "Positive = longs pay; negative = shorts pay. Bigger magnitude = more attractive funding "
                "*relative to volatility risk*. Use this to rank cross-symbol — it removes the bias where "
                "high-vol meme coins show high funding just because vol is high."
            ),
        ),
        "MXC 24h vol (M)": st.column_config.NumberColumn(
            format="%.2f",
            help=(
                "MEXC contract's 24h quote volume in millions of USDT/USDC.\n"
                "Many MEXC USDC perps exist on paper but trade < $1M/24h — when in doubt, look here.\n"
                "Example: WIF_USDC trades ~$0.4M/24h vs WIF_USDT ~$24M — same coin, very different liquidity."
            ),
        ),
        "Spread / 8h": st.column_config.NumberColumn(
            format=pct,
            help=(
                "Binance 8h-normalized funding − MEXC 8h-normalized funding.\n"
                "Positive: Binance funding higher → shorting Binance + longing MEXC captures the diff.\n"
                "Negative: MEXC funding higher → opposite trade.\n"
                "This is the core cross-exchange funding-arb signal."
            ),
        ),
    }
    df = filter_dataframe_to_watchlist(df, watchlist, ["Binance symbol", "MEXC symbol"])
    render_table(df, column_config=col_cfg)
    cap_msg = (f"watchlist of {len(watchlist)} symbols" if watchlist
               else f"top {len(df)} of {len(rows)} flagged pairs")
    st.caption(f"Showing {len(df)} rows ({cap_msg}), sorted by max |8h-normalized rate|.")
else:
    st.info("No pair on either exchange currently exceeds the 8h-normalized funding threshold.")
