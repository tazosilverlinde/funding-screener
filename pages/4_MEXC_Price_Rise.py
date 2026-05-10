"""Page 4 — MEXC pairs whose close-to-close return over 1d / 7d / 30d exceeds threshold."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from funding_screener.config import settings  # noqa: E402
from funding_screener.screener import screen_combined_high_funding, screen_price_rise  # noqa: E402
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

st.set_page_config(page_title="MEXC Price Rise", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
watchlist = watchlist_sidebar()
auto_rerun(interval_ms=60_000, key="page4_tick")

cfg = settings()["price_rise"]
threshold = float(cfg["threshold_percent"])
windows = [int(w) for w in cfg["windows_days"]]
min_volume = float(cfg["min_24h_quote_volume"])

st.title(f"MEXC — {threshold:.0f}%+ price rise (1d / 7d / 30d)")
st.caption(
    f"Pairs whose close-to-close return over any of {windows} day windows exceeds "
    f"{threshold:.0f}%. Pairs with 24h quote volume < ${min_volume:,.0f} are excluded. "
    f"Klines refresh in the background every 5 minutes."
)

freshness_banner(store)
cooldown_banner(store)
st.divider()

snap = store.read_mexc()
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

# Round 47: enrich each row with Quality / Age (h) by joining against the
# combined screener output keyed by (base, quote). Mirrors Page 3's logic.
_bnb_snap = store.read_binance()
_score_histories = store.read_score_histories()
_combined_for_join = screen_combined_high_funding(
    _bnb_snap.funding, snap.funding,
    _bnb_snap.contracts, snap.contracts,
    enrichments,
    threshold_percent=0.0,
    binance_volumes=_bnb_snap.volumes, mexc_volumes=snap.volumes,
    min_volume_usd_per_side=0.0,
    onchain_netflow_by_base=onchain_by_base,
    klines_by_symbol=snap.klines,
    score_histories=_score_histories,
)
_combined_by_key: dict[tuple[str, str], object] = {
    (r.base_asset.upper(), r.quote_asset): r for r in _combined_for_join
}

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
    df["Score"] = df["composite_score"]
    df["Score label"] = (
        df["composite_emoji"].fillna("") + " " + df["composite_short"].fillna("")
    )
    df = df.drop(columns=["composite_score", "composite_emoji", "composite_short"])

    # Round 47: pull Quality + Age from the combined-row lookup.
    def _lookup_quality(row_dict: dict) -> str:
        base = (row_dict.get("base_asset") or "").upper()
        quote = row_dict.get("quote_asset") or "USDT"
        peer = _combined_by_key.get((base, quote))
        return getattr(peer, "setup_quality_label", "") or ""

    def _lookup_age(row_dict: dict) -> float | None:
        base = (row_dict.get("base_asset") or "").upper()
        quote = row_dict.get("quote_asset") or "USDT"
        peer = _combined_by_key.get((base, quote))
        return getattr(peer, "signal_age_hours", None)

    raw_rows_dump = [r.model_dump() for r in rows[:cap]]
    df["Quality"] = [_lookup_quality(d) for d in raw_rows_dump]
    df["Age (h)"] = [_lookup_age(d) for d in raw_rows_dump]
    df["symbol"] = df["symbol"].apply(
        lambda s: f"/Symbol_Detail?exchange=MEXC&symbol={s}" if s else ""
    )
    front = ["Score", "Score label", "Quality", "Age (h)"]
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
                "Composite signal score, signed [-100..+100]. Positive = long bias.\n\n"
                "Tells you whether a 500%+ price rise is structurally bullish (funding "
                "negative, OI rising, off-exchange accumulation) or fragile (funding spiking "
                "positive = leveraged longs piling in = squeeze risk)."
            ),
        ),
        "Score label": st.column_config.TextColumn(
            "Score label",
            help="Human-readable bucket: 🚀 Strong bull / 🟢 Bullish / ↗ Mild bull / 🟡 Neutral / ↘ Mild bear / 🔴 Bearish / 💥 Strong bear",
        ),
        "Quality": st.column_config.TextColumn(
            "Quality",
            help=(
                "Setup-quality classification (Round 34) — Fresh / Building / "
                "Mature / Late / Noisy. On a price-rise page: a 500%+ rise with "
                "🚀 Fresh bull is early; with ⏰ Late or ⚠️ Noisy is suspect."
            ),
        ),
        "Age (h)": st.column_config.NumberColumn(
            "Age (h)",
            format="%.1f",
            help=(
                "Hours since the score most-recently entered the bullish/bearish "
                "region. Cross-reference with % return: short age + big return "
                "= signal tracking the move; long age + small return = signal "
                "predicted it but price hasn't moved yet."
            ),
        ),
        "Symbol": st.column_config.LinkColumn(
            "Symbol",
            display_text=r"symbol=([A-Z0-9_]+)",
            help="MEXC perp contract name. Click to open full detail.",
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
            format="%.2f", help="Close-to-close return over 1 day."
        ),
        "7d %": st.column_config.NumberColumn(format="%.2f", help="Close-to-close return over 7 days."),
        "30d %": st.column_config.NumberColumn(format="%.2f", help="Close-to-close return over 30 days."),
        "8h funding %": st.column_config.NumberColumn(
            format="%.4f",
            help=(
                "Current 8h-normalized funding rate. High positive funding alongside a big price rise = "
                "longs paying premium to chase; often a late-cycle / mean-revert signal."
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
    render_table(df, column_config=col_cfg, download_basename="mexc_price_rise")
    cap_msg = (f"watchlist of {len(watchlist)} symbols" if watchlist
               else f"top {len(df)} of {len(rows)} flagged pairs")
    st.caption(f"Showing {len(df)} ({cap_msg}).")
else:
    st.info(
        f"No MEXC pair currently exceeds the {threshold:.0f}% rise threshold. "
        "Lower `price_rise.threshold_percent` in `config/settings.yaml` to see less-extreme rises."
    )
