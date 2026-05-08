"""Page 3 — Binance pairs whose close-to-close return over 1d / 7d / 30d exceeds threshold."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from funding_screener.config import is_binance_enabled, settings  # noqa: E402
from funding_screener.screener import screen_price_rise  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    cooldown_banner,
    filter_dataframe_to_watchlist,
    freshness_banner,
    render_table,
    sidebar_status,
    symbol_search_sidebar,
    to_df,
    watchlist_sidebar,
)

st.set_page_config(page_title="Binance Price Rise", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
watchlist = watchlist_sidebar()
auto_rerun(interval_ms=60_000, key="page3_tick")

cfg = settings()["price_rise"]
threshold = float(cfg["threshold_percent"])
windows = [int(w) for w in cfg["windows_days"]]
min_volume = float(cfg["min_24h_quote_volume"])

st.title(f"Binance — {threshold:.0f}%+ price rise (1d / 7d / 30d)")
st.caption(
    f"Pairs whose close-to-close return over any of {windows} day windows exceeds "
    f"{threshold:.0f}%. Pairs with 24h quote volume < ${min_volume:,.0f} are excluded. "
    f"Klines refresh in the background every 5 minutes."
)

freshness_banner(store)
cooldown_banner(store)

if not is_binance_enabled():
    st.error(
        "Binance is disabled in this deployment (env var `BINANCE_ENABLED=false`). "
        "This page can't show anything. See the MEXC price-rise page instead."
    )
    st.stop()

st.divider()

snap = store.read_binance()
mcaps = store.read_market_caps()
enrichments = store.read_enrichments()
onchain_flows, _ = store.read_onchain_flows()
onchain_by_base = {f["token"]: f.get("net_usd", 0.0) for f in onchain_flows}
rows = screen_price_rise(
    snap.contracts,
    snap.klines,
    snap.volumes,
    snap.funding,
    mcaps,
    threshold_percent=threshold,
    windows_days=windows,
    min_24h_quote_volume=min_volume,
    enrichments_by_key=enrichments,
    onchain_netflow_by_base=onchain_by_base,
)

cap = int(settings()["row_limit"])
df = to_df(
    [r.model_dump() for r in rows[:cap]],
    column_order=[
        "composite_score",
        "composite_emoji",
        "composite_short",
        "symbol",
        "current_price",
        "ath_price",
        "pct_1d",
        "pct_7d",
        "pct_30d",
        "funding_rate_8h_norm_percent",
        "market_cap_millions",
        "volume_today_millions",
        "volume_yesterday_millions",
        "volume_day_before_millions",
    ],
)

if not df.empty:
    # Pre-format the composite columns: numeric "Score" + textual "Score label".
    df["Score"] = df["composite_score"]
    df["Score label"] = (
        df["composite_emoji"].fillna("") + " " + df["composite_short"].fillna("")
    )
    df = df.drop(columns=["composite_score", "composite_emoji", "composite_short"])
    # Make symbol clickable → detail page.
    df["symbol"] = df["symbol"].apply(
        lambda s: f"/Symbol_Detail?exchange=Binance&symbol={s}" if s else ""
    )
    # Move Score columns to the front; everything else preserves order.
    front = ["Score", "Score label"]
    cols = front + [c for c in df.columns if c not in front]
    df = df[cols]
    df = df.rename(
        columns={
            "symbol": "Symbol",
            "current_price": "Price",
            "ath_price": "ATH",
            "pct_1d": "1d %",
            "pct_7d": "7d %",
            "pct_30d": "30d %",
            "funding_rate_8h_norm_percent": "8h funding %",
            "market_cap_millions": "Market cap (M)",
            "volume_today_millions": "Today vol (M)",
            "volume_yesterday_millions": "Yesterday vol (M)",
            "volume_day_before_millions": "Day before vol (M)",
        }
    )
    col_cfg = {
        "Score": st.column_config.NumberColumn(
            "Score",
            format="%+d",
            help=(
                "Composite signal score, signed [-100..+100]. **Positive = long bias**, "
                "negative = short bias.\n\n"
                "On a price-rise page this answers: is this big % move backed by structural "
                "buy pressure (bullish funding + accumulation), or just a fragile squeeze "
                "that's about to reverse? Score > +30 with a 500%+ rise = real momentum; "
                "score < -10 with a 500%+ rise = leveraged longs piling in late, often a top.\n\n"
                "Same function as Page 2 — see that page's tooltip for the full ruleset."
            ),
        ),
        "Score label": st.column_config.TextColumn(
            "Score label",
            help="Human-readable bucket of the composite score.",
        ),
        "Symbol": st.column_config.LinkColumn(
            "Symbol",
            display_text=r"symbol=([A-Z0-9_]+)",
            help="Binance perp contract name. Click to open full detail.",
        ),
        "Price": st.column_config.NumberColumn(format="%.6g", help="Latest close from daily klines."),
        "ATH": st.column_config.NumberColumn(
            format="%.6g",
            help=(
                "Highest 'high' over the cached kline history (~1 year).\n"
                "Useful for gauging drawdown: current_price / ATH = where we stand vs the peak."
            ),
        ),
        "1d %": st.column_config.NumberColumn(
            format="%.2f", help="Close-to-close return over 1 day. (today's close / yesterday's close − 1) × 100."
        ),
        "7d %": st.column_config.NumberColumn(format="%.2f", help="Close-to-close return over 7 days."),
        "30d %": st.column_config.NumberColumn(format="%.2f", help="Close-to-close return over 30 days."),
        "8h funding %": st.column_config.NumberColumn(
            format="%.4f",
            help=(
                "Current 8h-normalized funding rate. High positive funding alongside a big price rise = "
                "longs are paying premium to chase; often a late-cycle / mean-revert signal."
            ),
        ),
        "Market cap (M)": st.column_config.NumberColumn(
            format="%.2f",
            help=(
                "USD market cap from CoinGecko top-1000, in millions. Blank if not in top-1000.\n"
                "Sanity-check pumps: a small-cap pumping 500% is normal; a top-50 coin doing the same is exceptional."
            ),
        ),
        "Today vol (M)": st.column_config.NumberColumn(
            format="%.2f",
            help=(
                "Today's so-far quote volume (incomplete day) in millions of USDT.\n"
                "Compare to yesterday/day-before to see whether interest is accelerating or fading."
            ),
        ),
        "Yesterday vol (M)": st.column_config.NumberColumn(
            format="%.2f", help="Quote volume of the previous full day, in millions USDT."
        ),
        "Day before vol (M)": st.column_config.NumberColumn(
            format="%.2f", help="Quote volume of the day before yesterday, in millions USDT."
        ),
    }
    df = filter_dataframe_to_watchlist(df, watchlist, ["Symbol"])
    render_table(df, column_config=col_cfg, download_basename="binance_price_rise")
    cap_msg = (f"watchlist of {len(watchlist)} symbols" if watchlist
               else f"top {len(df)} of {len(rows)} flagged pairs")
    st.caption(f"Showing {len(df)} ({cap_msg}).")
else:
    st.info(
        f"No Binance pair currently exceeds the {threshold:.0f}% rise threshold. "
        "Lower `price_rise.threshold_percent` in `config/settings.yaml` to see less-extreme rises."
    )
