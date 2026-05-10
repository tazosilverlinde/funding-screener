"""Page 5 — Per-symbol detail view.

Opened via clickable symbol cells on every other page. Reads `?exchange=&symbol=`
from the URL. Shows every metric we have for that single contract, with an
explanation comment and a directional signal for each section so the user can
understand *why* a signal lights up rather than guessing.

Sections:
  1. Header  — symbol, exchange, top-of-page signal banner, price/ATH/mcap metrics
  2. Funding — current rate + streak + history + comment + signal
  3. Risk    — mark/index spread, Open Interest + Δ, Long/Short ratio (Binance) + signal
  4. Price action — 1d/7d/30d returns, drawdown, daily kline chart
  5. Volume profile — today/yesterday/day-before quote volumes
  6. Market context — fees, interval, next funding, market cap rank

Cached cache reads are instant; OI history + L/S ratio are fetched on-demand
once per session for Binance symbols only (MEXC doesn't expose these publicly).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.exchanges import BinanceClient, MexcClient  # noqa: E402
from funding_screener.signals import (  # noqa: E402
    classify_signal,
    compute_composite_score,
    compute_funding_deviation,
    estimate_funding_income,
)
from funding_screener.thesis import compose_trade_thesis  # noqa: E402
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    cooldown_banner,
    minutes_to,
    run_async,
    sidebar_status,
    symbol_search_sidebar,
)

st.set_page_config(page_title="Symbol Detail", layout="wide")

store = boot()
sidebar_status(store)
symbol_search_sidebar(store)
auto_rerun(interval_ms=60_000, key="detail_tick")


# ---------------- query params ----------------

params = st.query_params
exchange_raw = params.get("exchange", "").lower()
symbol_q = params.get("symbol", "")

if exchange_raw == "binance":
    exchange = "Binance"
elif exchange_raw == "mexc":
    exchange = "MEXC"
else:
    exchange = ""

if not exchange or not symbol_q:
    st.title("Symbol Detail")
    st.info(
        "This page shows the full picture for one perpetual contract: "
        "funding, streak, mark vs index, open interest, long/short ratio, "
        "price action, and volume — each with an explanation and a signal.\n\n"
        "Open it by clicking a symbol on any other page, or pick manually:"
    )
    cols = st.columns([1, 3, 1])
    ex_in = cols[0].selectbox("Exchange", ["Binance", "MEXC"], key="ex_in")
    sym_in = cols[1].text_input(
        "Symbol",
        placeholder="BTCUSDT" if ex_in == "Binance" else "BTC_USDT",
        key="sym_in",
    )
    if cols[2].button("Show", use_container_width=True) and sym_in:
        st.query_params["exchange"] = ex_in
        st.query_params["symbol"] = sym_in.strip()
        st.rerun()
    st.stop()


# ---------------- read cached data ----------------

snap = store.read_binance() if exchange == "Binance" else store.read_mexc()
funding_row = next((r for r in snap.funding if r.symbol == symbol_q), None)
contract = next((c for c in snap.contracts if c.symbol == symbol_q), None)
klines = snap.klines.get(symbol_q, [])
enrichment = store.read_enrichments().get((exchange, symbol_q))
volume_24h_raw = snap.volumes.get(symbol_q)
market_caps = store.read_market_caps()

if funding_row is None and contract is None:
    st.title(f"{symbol_q} — {exchange}")
    st.error(
        f"Symbol `{symbol_q}` is not in the {exchange} cache. Either the contract "
        "doesn't exist or the background updater hasn't fetched yet — wait ~30s and refresh."
    )
    st.stop()

base_asset = (funding_row.base_asset if funding_row else contract.base_asset).upper()
mcap_usd = market_caps.get(base_asset)


# ---------------- on-demand: OI history + L/S ratio (Binance only) ----------------


@st.cache_data(ttl=120, show_spinner="Fetching open-interest and long/short ratio…")
def _binance_extras(symbol: str) -> dict:
    """Fetch 24h history of OI, retail L/S ratio, and top-trader L/S ratio.

    All three use period=1h, limit=24 so we get matching x-axes for charts.
    The L/S endpoints accept the same period/limit params as OI; we used to
    request limit=1 (current value only) but now grab the trajectory so the
    page can render a 24h trend mini-chart for each.
    """
    async def _go():
        client = BinanceClient()
        try:
            results = await asyncio.gather(
                client.fetch_open_interest_history(symbol, period="1h", limit=24),
                client.fetch_long_short_ratio_global_history(symbol, period="1h", limit=24),
                client.fetch_long_short_ratio_top_history(symbol, period="1h", limit=24),
                return_exceptions=True,
            )
        finally:
            await client.aclose()
        return {
            "oi_history": results[0] if not isinstance(results[0], Exception) else [],
            "ls_global_history": results[1] if not isinstance(results[1], Exception) else [],
            "ls_top_history": results[2] if not isinstance(results[2], Exception) else [],
        }

    return run_async(_go)


if exchange == "Binance":
    extras = _binance_extras(symbol_q)
else:
    extras = {"oi_history": [], "ls_global": None, "ls_top": None}


# Fetch a longer funding-rate history (90 settlements ≈ 30 days at 8h cadence)
# for the line chart. Cached for 5 min per (exchange, symbol) so revisiting is instant.
@st.cache_data(ttl=300, show_spinner=False)
def _funding_history(exchange: str, symbol: str, limit: int = 90) -> list[float]:
    async def _go():
        client = BinanceClient() if exchange == "Binance" else MexcClient()
        try:
            return await client.fetch_funding_rate_history(symbol, limit=limit)
        finally:
            await client.aclose()

    return run_async(_go)


funding_history_pct = _funding_history(exchange, symbol_q, limit=90)


# ---------------- compute top-line numbers ----------------

current_price = klines[-1].close if klines else (funding_row.mark_price if funding_row else None)
ath = max((k.high for k in klines), default=None) if klines else None
drawdown_pct = ((current_price / ath - 1.0) * 100.0) if (ath and current_price) else None

# Top signal: the same one Page 2 shows for this row.
sig = classify_signal(
    funding_8h_norm_pct=funding_row.rate_8h_norm_percent if funding_row else None,
    streak_count=enrichment.funding_streak_count if enrichment else 0,
    streak_direction=enrichment.funding_streak_direction if enrichment else None,
    mark_index_spread_pct=enrichment.mark_index_spread_percent if enrichment else None,
)

# Composite numeric score using every available input.
onchain_flows_list, _ = store.read_onchain_flows()
onchain_net = next(
    (f.get("net_usd") for f in onchain_flows_list if f.get("token") == base_asset), None
)
_liq_for_score = store.read_liquidations(symbol=symbol_q, window_seconds=24 * 3600)
composite = compute_composite_score(
    funding_8h_norm_pct=funding_row.rate_8h_norm_percent if funding_row else None,
    streak_count=enrichment.funding_streak_count if enrichment else 0,
    streak_direction=enrichment.funding_streak_direction if enrichment else None,
    mark_index_spread_pct=enrichment.mark_index_spread_percent if enrichment else None,
    oi_change_24h_pct=enrichment.oi_change_24h_pct if enrichment else None,
    ls_ratio_global=enrichment.ls_ratio_global if enrichment else None,
    ls_ratio_top=enrichment.ls_ratio_top if enrichment else None,
    onchain_net_usd=onchain_net,
    liq_long_usd_24h=_liq_for_score.get("long_liq_usd") if _liq_for_score else None,
    liq_short_usd_24h=_liq_for_score.get("short_liq_usd") if _liq_for_score else None,
)


# ---------------- HEADER ----------------

st.title(f"{symbol_q} — {exchange}")
cooldown_banner(store)
st.caption(f"Base asset: **{base_asset}** • Quote: **{(contract.quote_asset if contract else 'USDT')}**")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Price", f"{current_price:,.6g}" if current_price else "—")
if ath and current_price:
    m2.metric("ATH (1y lookback)", f"{ath:,.6g}", f"{drawdown_pct:+.2f}% from ATH")
else:
    m2.metric("ATH (1y lookback)", "—")
if mcap_usd:
    if mcap_usd >= 1e9:
        m3.metric("Market cap (CoinGecko)", f"${mcap_usd / 1e9:.2f}B")
    else:
        m3.metric("Market cap (CoinGecko)", f"${mcap_usd / 1e6:.1f}M")
else:
    m3.metric("Market cap (CoinGecko)", "—", help="Coin not in CoinGecko top-1000")
m4.metric(
    "Score",
    f"{composite.score:+d}",
    f"{composite.emoji} {composite.short}",
    help="Composite signal score (-100..+100). Positive = long bias. See breakdown below.",
)

# Banner with full breakdown
banner_text = f"**{sig.emoji} {sig.short}**\n\n{sig.breakdown}"
if sig.color == "green":
    st.success(banner_text)
elif sig.color == "red":
    st.error(banner_text)
elif sig.color == "orange":
    st.warning(banner_text)
else:
    st.info(banner_text)

# Composite score breakdown
with st.expander(f"Composite score breakdown ({composite.score:+d} {composite.emoji} {composite.short})"):
    for line in composite.breakdown:
        st.write(f"• {line}")

# ── Auto-generated trade thesis (Round 37) ─────────────────────────────────
# Synthesizes all available signals into a structured English summary so the
# user gets a bullish-reasons / bearish-reasons / risks breakdown without
# having to mentally combine the 12+ columns from Page 2.
_thesis_funding_dev = compute_funding_deviation(
    funding_row.rate_percent if funding_row else None,
    enrichment.prev_funding_rates_percent if enrichment else [],
)
_thesis_liq = liq_stats if 'liq_stats' in dir() and liq_stats else store.read_liquidations(
    symbol=symbol_q, window_seconds=24 * 3600,
)
thesis = compose_trade_thesis(
    symbol=symbol_q,
    composite_score=composite.score,
    funding_8h_norm_pct=funding_row.rate_8h_norm_percent if funding_row else None,
    funding_streak_count=enrichment.funding_streak_count if enrichment else 0,
    funding_streak_direction=enrichment.funding_streak_direction if enrichment else None,
    funding_deviation_z=_thesis_funding_dev.z_score if _thesis_funding_dev else None,
    mark_index_spread_pct=enrichment.mark_index_spread_percent if enrichment else None,
    oi_change_24h_pct=enrichment.oi_change_24h_pct if enrichment else None,
    ls_ratio_global=enrichment.ls_ratio_global if enrichment else None,
    ls_ratio_top=enrichment.ls_ratio_top if enrichment else None,
    onchain_net_usd=onchain_net,
    liq_long_usd_24h=_thesis_liq.get("long_liq_usd") if _thesis_liq else None,
    liq_short_usd_24h=_thesis_liq.get("short_liq_usd") if _thesis_liq else None,
)
with st.expander(f"📝 Auto-thesis — {thesis['headline']}", expanded=False):
    if thesis["bullish_reasons"]:
        st.markdown("**🟢 Bullish reasons:**")
        for r in thesis["bullish_reasons"]:
            st.markdown(f"- {r}")
    if thesis["bearish_reasons"]:
        st.markdown("**🔴 Bearish reasons:**")
        for r in thesis["bearish_reasons"]:
            st.markdown(f"- {r}")
    if thesis["risks"]:
        st.markdown("**⚠️ Risks:**")
        for r in thesis["risks"]:
            st.markdown(f"- {r}")
    if not (thesis["bullish_reasons"] or thesis["bearish_reasons"] or thesis["risks"]):
        st.caption(
            "Not enough data attached to this symbol to form a thesis yet. "
            "Wait for the enrichment loop to populate (~3 min after deploy)."
        )
    st.caption(
        "Auto-generated from current signal values. Each line maps to one "
        "section above — this expander is purely a consolidation."
    )

# ── Score history chart (Round 26) ─────────────────────────────────────────
# Shows how the composite score has evolved for this pair over the last 24h.
# A score that's been climbing is a different signal than one that's been at
# the same level all day — momentum tells you whether you're catching the
# move at the start or chasing it.
quote_for_history = funding_row.quote_asset if funding_row else (contract.quote_asset if contract else "USDT")
score_history = store.read_score_history((base_asset, quote_for_history))
if len(score_history) >= 2:
    history_df = pd.DataFrame(
        {"Score": [s for _ts, s in score_history]},
        index=pd.to_datetime([ts for ts, _s in score_history]),
    )
    st.write(f"**Composite score history — last {len(score_history)} samples**")
    st.line_chart(history_df, height=180)
    first_score = score_history[0][1]
    latest_score = score_history[-1][1]
    drift = latest_score - first_score
    delta_label = "rising" if drift > 5 else ("falling" if drift < -5 else "flat")
    st.caption(
        f"Sampled every 10 min since the score-history loop kicked in. "
        f"Trend: **{delta_label}** ({first_score:+d} → {latest_score:+d}, Δ {drift:+d})."
    )
elif score_history:
    st.caption(
        f"Score history is just initializing — only {len(score_history)} sample(s) so far. "
        "Snapshots are taken every 10 min; come back in a bit."
    )

st.divider()


# ---------------- 1. FUNDING ----------------

st.subheader("1. Funding")

if funding_row:
    f1, f2, f3 = st.columns(3)
    f1.metric(
        "Current rate (per period)",
        f"{funding_row.rate_percent:+.4f}%",
        help="Funding rate that will be paid/received at the next settlement. Positive = longs pay shorts.",
    )
    f2.metric(
        "8h-normalized rate",
        f"{funding_row.rate_8h_norm_percent:+.4f}%",
        help="Rate scaled to an 8h period for cross-pair comparison.",
    )
    f3.metric(
        "Funding interval",
        f"{funding_row.interval_hours:.0f}h",
        help="How often funding settles. Common: 8h. Some: 4h, 2h, 1h.",
    )

    f4, f5 = st.columns(2)
    nf = funding_row.next_funding_time
    f4.metric(
        "Next funding in",
        minutes_to(nf) or "—",
        help="Time remaining until the current rate settles. Format: '47m' or '2h 15m'.",
    )
    if enrichment and enrichment.funding_streak_count:
        arrow = "↑" if enrichment.funding_streak_direction == "pos" else "↓"
        f5.metric(
            "Streak",
            f"{enrichment.funding_streak_count}{arrow}",
            help="Consecutive same-sign settled rates from most recent.",
        )
    else:
        f5.metric("Streak", "pending", help="Waiting for enrichment loop to fetch funding history…")

    # ── Funding income estimator (Round 26) ────────────────────────────────
    # Translates the funding percent into a concrete-dollar 24h cash flow for
    # the user's chosen position size. Helps answer "is the funding actually
    # worth taking the trade for, or is the spread too small relative to fees?"
    with st.expander("💰 Funding income estimator", expanded=False):
        est_c1, est_c2 = st.columns([1, 1])
        position_size_usd = est_c1.number_input(
            "Position size (USD)",
            min_value=100.0, max_value=10_000_000.0, value=10_000.0, step=1000.0,
            help="Notional value of the position you'd open. Fees not included.",
        )
        side_choice = est_c2.radio(
            "Direction",
            options=["long", "short"],
            horizontal=True,
            help="Pick the side. Long pays positive funding, receives negative; "
                 "short is the opposite.",
        )
        long_24h = estimate_funding_income(
            funding_row.rate_8h_norm_percent, position_size_usd, hold_hours=24.0,
            direction=side_choice,
        )
        long_8h = estimate_funding_income(
            funding_row.rate_8h_norm_percent, position_size_usd, hold_hours=8.0,
            direction=side_choice,
        )
        if long_24h is not None and long_8h is not None:
            est_c3, est_c4 = st.columns(2)
            label_8h = "Receive" if long_8h >= 0 else "Pay"
            label_24h = "Receive" if long_24h >= 0 else "Pay"
            est_c3.metric(
                f"{label_8h} per 8h",
                f"${abs(long_8h):,.2f}",
                help="Per-funding-period dollar amount based on the current rate. "
                     "Positive = you collect; negative = you pay.",
            )
            est_c4.metric(
                f"{label_24h} per 24h",
                f"${abs(long_24h):,.2f}",
                f"{(long_24h / position_size_usd) * 100:+.4f}% of position",
                help="24h cash flow assuming three settlements at the current rate. "
                     "Real-world the rate changes between settlements, so treat as "
                     "a snapshot estimate.",
            )

    # 30-day funding history line chart (replaces the small last-3-rates table).
    if funding_history_pct:
        # API returns most-recent first; reverse so the chart x-axis goes oldest → newest.
        hist = list(reversed(funding_history_pct))
        chart_df = pd.DataFrame(
            {
                "Settlement": list(range(-len(hist) + 1, 1)),  # negative → past
                "Rate %": hist,
            }
        ).set_index("Settlement")
        st.write(
            f"**Funding history — last {len(hist)} settlements** "
            f"(≈ {len(hist) // 3} days at the typical 8h cadence)"
        )
        st.line_chart(chart_df, height=200)
        # Stats below the chart so user can quickly read magnitude/persistence.
        avg = sum(hist) / len(hist)
        positives = sum(1 for r in hist if r > 0)
        negatives = sum(1 for r in hist if r < 0)
        c_avg, c_pos, c_neg, c_max = st.columns(4)
        c_avg.metric("Average", f"{avg:+.4f}%")
        c_pos.metric("Positive periods", positives)
        c_neg.metric("Negative periods", negatives)
        c_max.metric("Max abs", f"{max(abs(r) for r in hist):.4f}%")
    elif enrichment and enrichment.prev_funding_rates_percent:
        # Fallback: fewer rates from the enrichment cache.
        st.write("**Last settled rates** (most-recent first, fallback view):")
        hist_df = pd.DataFrame(
            {
                "Period": [f"t-{i+1}" for i in range(len(enrichment.prev_funding_rates_percent))],
                "Rate %": enrichment.prev_funding_rates_percent,
            }
        )
        st.dataframe(hist_df, hide_index=True, use_container_width=False)

    # Funding-rate deviation indicator — surfaces fresh anomalies vs the
    # 30-period mean. Computed only when we have ≥10 historical settlements.
    if funding_history_pct and funding_row:
        deviation = compute_funding_deviation(
            current_pct=funding_row.rate_percent,
            history_pct=funding_history_pct,
        )
        if deviation:
            box_text = f"**{deviation.emoji} Funding deviation:** {deviation.comment}"
            if deviation.classification in ("extreme_overshoot", "extreme_undershoot"):
                st.warning(box_text)
            elif deviation.classification in ("overshoot", "undershoot"):
                st.info(box_text)
            else:
                st.markdown(box_text)

    # Explanation
    f_pct = funding_row.rate_8h_norm_percent
    if f_pct > 0.5:
        explanation = (
            "💡 **Positive funding** means **longs are paying shorts**. "
            "If you're a shorts-only screener, this contract is paying you to be short. "
            "But high positive funding *and* a price uptrend often means longs are over-leveraged "
            "chasing the move — late-cycle / mean-revert candidate."
        )
    elif f_pct < -0.5:
        explanation = (
            "💡 **Negative funding** means **shorts are paying longs**. "
            "Going long this contract collects funding while you wait. "
            "High negative funding *and* a downtrend = shorts over-leveraged → squeeze candidate."
        )
    else:
        explanation = (
            "💡 Funding within ±0.5% per 8h is normal. No directional pressure from funding alone — "
            "look at price action and OI to decide."
        )
    st.markdown(explanation)

    # Section signal
    section_sig = classify_signal(
        funding_8h_norm_pct=funding_row.rate_8h_norm_percent,
        streak_count=enrichment.funding_streak_count if enrichment else 0,
        streak_direction=enrichment.funding_streak_direction if enrichment else None,
        mark_index_spread_pct=None,  # mark/idx is handled in the Risk section
    )
    st.markdown(f"**Funding signal:** {section_sig.emoji} {section_sig.short}")
else:
    st.info("Funding data not yet cached for this symbol.")

st.divider()


# ---------------- 2. RISK METRICS ----------------

st.subheader("2. Risk metrics")

if exchange == "Binance":
    r1, r2, r3, r4 = st.columns(4)
    if enrichment and enrichment.mark_index_spread_percent is not None:
        spread = enrichment.mark_index_spread_percent
        r1.metric(
            "Mark vs Index",
            f"{spread:+.4f}%",
            delta="Risk" if abs(spread) > 0.5 else "OK",
            delta_color="inverse" if abs(spread) > 0.5 else "normal",
            help="Mark price drives liquidations; index is the spot reference. Big spread = risk.",
        )
    else:
        r1.metric("Mark vs Index", "—")

    oi_hist = extras.get("oi_history") or []
    if oi_hist:
        try:
            current_oi_usd = float(oi_hist[-1].get("sumOpenInterestValue", 0))
            if len(oi_hist) >= 24:
                old_oi_usd = float(oi_hist[0].get("sumOpenInterestValue", 0))
                oi_24h_pct = (current_oi_usd / old_oi_usd - 1.0) * 100.0 if old_oi_usd > 0 else None
            else:
                oi_24h_pct = None
            oi_label = (
                f"${current_oi_usd / 1e9:.2f}B"
                if current_oi_usd >= 1e9
                else f"${current_oi_usd / 1e6:.1f}M"
            )
            r2.metric(
                "Open Interest",
                oi_label,
                f"{oi_24h_pct:+.2f}% 24h" if oi_24h_pct is not None else None,
                help="Total notional in this perp. Rising = real money entering. Falling = unwind.",
            )
        except Exception:
            r2.metric("Open Interest", "—")
    else:
        r2.metric("Open Interest", "—")

    # L/S ratio cards now read the latest value from the history series so the
    # cards and the trend chart below stay in sync.
    ls_global_hist = extras.get("ls_global_history") or []
    ls_top_hist = extras.get("ls_top_history") or []

    def _last_ratio(hist: list[dict]) -> float | None:
        if not hist:
            return None
        try:
            return float(hist[-1].get("longShortRatio", 0))
        except (TypeError, ValueError):
            return None

    ls_global = _last_ratio(ls_global_hist)
    ls_top = _last_ratio(ls_top_hist)
    r3.metric(
        "L/S ratio (retail)",
        f"{ls_global:.2f}" if ls_global is not None else "—",
        help="Global account long/short ratio. > 2 = crowded long; < 0.5 = crowded short. Contrarian at extremes.",
    )
    r4.metric(
        "L/S ratio (top traders)",
        f"{ls_top:.2f}" if ls_top is not None else "—",
        help="Top-20%-by-collateral account L/S ratio. Compare to retail — divergence = smart vs dumb money.",
    )

    # ── 24h trend charts: OI + L/S — added Round 11 ───────────────────────
    # Two side-by-side charts so the user sees the *direction* of these two
    # metrics, not just their current value. A ratio of 2.0 that just spiked
    # from 1.0 reads very differently than 2.0 trending steady for 24h.
    chart_col1, chart_col2 = st.columns(2)
    if oi_hist:
        try:
            oi_chart_df = pd.DataFrame(
                {
                    "OI ($M)": [
                        float(p.get("sumOpenInterestValue", 0) or 0) / 1e6
                        for p in oi_hist
                    ],
                },
                index=pd.to_datetime(
                    [int(p.get("timestamp", 0)) for p in oi_hist], unit="ms",
                ),
            )
            chart_col1.write("**Open Interest — last 24h (hourly)**")
            chart_col1.line_chart(oi_chart_df, height=200)
            chart_col1.caption(
                "Rising trend = real notional entering. Sharp drop = liquidation/unwind."
            )
        except Exception as e:
            chart_col1.write("OI chart unavailable.")
    else:
        chart_col1.info("OI history not yet available.")

    if ls_global_hist or ls_top_hist:
        try:
            ls_data: dict[pd.Timestamp, dict[str, float]] = {}
            for p in ls_global_hist:
                ts = pd.to_datetime(int(p.get("timestamp", 0)), unit="ms")
                ls_data.setdefault(ts, {})["Retail"] = float(p.get("longShortRatio", 0) or 0)
            for p in ls_top_hist:
                ts = pd.to_datetime(int(p.get("timestamp", 0)), unit="ms")
                ls_data.setdefault(ts, {})["Top traders"] = float(p.get("longShortRatio", 0) or 0)
            if ls_data:
                ls_chart_df = pd.DataFrame.from_dict(ls_data, orient="index").sort_index()
                chart_col2.write("**L/S ratio — last 24h (hourly)**")
                chart_col2.line_chart(ls_chart_df, height=200)
                chart_col2.caption(
                    "Both lines crossing 1.0 in the same direction = consensus shift. "
                    "Diverging = retail vs smart money disagreement."
                )
        except Exception as e:
            chart_col2.write("L/S chart unavailable.")
    else:
        chart_col2.info("L/S history not yet available.")

    # Risk explanation
    risk_msgs: list[str] = []
    risk_color = "neutral"
    if enrichment and enrichment.mark_index_spread_percent is not None and abs(enrichment.mark_index_spread_percent) > 0.5:
        risk_msgs.append("⚠️ Mark/Index spread is large — possible liquidation cascade or manipulation.")
        risk_color = "warning"
    if oi_hist:
        try:
            cur = float(oi_hist[-1].get("sumOpenInterestValue", 0))
            if len(oi_hist) >= 24:
                old = float(oi_hist[0].get("sumOpenInterestValue", 0))
                oi_change = (cur / old - 1.0) * 100.0 if old > 0 else 0
                if oi_change > 20:
                    risk_msgs.append(f"📈 OI surged +{oi_change:.1f}% in 24h — fresh leverage entering.")
                elif oi_change < -20:
                    risk_msgs.append(f"📉 OI dropped {oi_change:.1f}% in 24h — unwind / position cleanup.")
        except Exception:
            pass
    if ls_global is not None:
        if ls_global > 3:
            risk_msgs.append(f"🔴 Retail extremely long ({ls_global:.2f}). Contrarian: shorts may benefit.")
        elif ls_global < 0.4:
            risk_msgs.append(f"🟢 Retail extremely short ({ls_global:.2f}). Contrarian: squeeze risk for shorts.")
    if ls_top is not None and ls_global is not None:
        if ls_top < 1 < ls_global:
            risk_msgs.append("⚠️ Top traders short while retail long — smart money positioned against retail.")
        elif ls_global < 1 < ls_top:
            risk_msgs.append("✅ Top traders long while retail short — smart money on the other side.")

    if risk_msgs:
        joined = "  \n".join(risk_msgs)
        if risk_color == "warning":
            st.warning(joined)
        else:
            st.info(joined)
    else:
        st.markdown(
            "💡 **No risk flags right now.** Mark/index in line, OI stable, L/S ratio in normal range."
        )

    # ── Liquidation snapshot for THIS symbol — added Round 15 ─────────────
    liq_stats = store.read_liquidations(symbol=symbol_q, window_seconds=24 * 3600)
    if liq_stats and liq_stats.get("events_count", 0) > 0:
        st.write("**Liquidations — last 24h** (Binance forceOrder stream)")
        l1, l2, l3, l4 = st.columns(4)
        l1.metric(
            "Total liq ($)",
            f"${liq_stats['total_usd'] / 1e6:.2f}M",
            help="Sum of long + short liquidations in the last 24h.",
        )
        l2.metric(
            "Long liq ($)",
            f"${liq_stats['long_liq_usd'] / 1e6:.2f}M",
            help="Notional of LONG positions force-liquidated. Spikes follow sharp drops.",
        )
        l3.metric(
            "Short liq ($)",
            f"${liq_stats['short_liq_usd'] / 1e6:.2f}M",
            help="Notional of SHORT positions force-liquidated. Spikes follow squeezes.",
        )
        biggest_label = (liq_stats.get("biggest_single_side") or "—").title()
        l4.metric(
            "Biggest single ($)",
            f"${liq_stats['biggest_single_usd'] / 1e6:.2f}M",
            f"{biggest_label} side" if biggest_label != "—" else None,
            help="Largest single liquidation event in the window. "
                 "Outsized values often signal one large fund getting blown out.",
        )
        # ── 24h hourly histogram chart — added Round 18 ─────────────────────
        hist = store.read_liquidations_histogram(symbol_q, bin_seconds=3600, window_seconds=24 * 3600)
        if hist:
            from datetime import datetime as _dt, timezone as _tz
            hist_df = pd.DataFrame(
                {
                    "Long $": [(b["long_liq_usd"] / 1e6) for b in hist],
                    "Short $": [(b["short_liq_usd"] / 1e6) for b in hist],
                },
                index=pd.to_datetime([_dt.fromtimestamp(b["ts"], tz=_tz.utc) for b in hist]),
            )
            st.bar_chart(hist_df, height=220, color=["#ff4b4b", "#21ba45"])
            st.caption(
                "Hourly bars. Red = LONG positions liquidated (precedes / accompanies "
                "drops). Green = SHORT positions liquidated (squeezes). "
                "When one color dominates a recent bar that aligns with the score's "
                "liquidation contribution above."
            )
else:
    st.info(
        "MEXC's public API doesn't expose mark/index spread, OI history, or L/S ratio for individual contracts. "
        "Risk metrics shown only for Binance symbols."
    )

st.divider()


# ---------------- 3. PRICE ACTION ----------------

st.subheader("3. Price action")

if klines:
    closes = [k.close for k in klines]
    times = [k.open_time for k in klines]

    def _pct(days_ago: int) -> float | None:
        idx = len(closes) - 1 - days_ago
        if idx < 0 or closes[idx] <= 0:
            return None
        return (closes[-1] / closes[idx] - 1.0) * 100.0

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("1d %", f"{_pct(1):+.2f}%" if _pct(1) is not None else "—")
    p2.metric("7d %", f"{_pct(7):+.2f}%" if _pct(7) is not None else "—")
    p3.metric("30d %", f"{_pct(30):+.2f}%" if _pct(30) is not None else "—")
    p4.metric("Distance from ATH", f"{drawdown_pct:+.2f}%" if drawdown_pct is not None else "—")

    # Daily close chart — last 90 days
    chart_n = min(90, len(klines))
    chart_df = pd.DataFrame(
        {"close": closes[-chart_n:]},
        index=pd.to_datetime([k.open_time for k in klines[-chart_n:]]),
    )
    st.line_chart(chart_df, height=250)
    st.caption(f"Last {chart_n} daily closes.")

    # Explanation
    pct30 = _pct(30) or 0
    if pct30 > 200:
        st.markdown(
            "💡 **Massive 30-day rise** — suggests either a recent listing pump, narrative-driven mania, "
            "or fundamental change. Combine with funding sign: high positive funding = late longs, lower-quality "
            "rally; deep negative funding = shorts wrong, structural rally."
        )
    elif pct30 < -50:
        st.markdown(
            "💡 **Heavy 30-day decline** — capitulation territory. Watch for funding flips negative + "
            "OI declining = short interest peaking → bottoming setup."
        )
    elif drawdown_pct is not None and drawdown_pct < -50:
        st.markdown(
            f"💡 **{drawdown_pct:.0f}% off the 1-year high.** Significant drawdown — could be "
            "consolidation phase or trend continuation."
        )
    else:
        st.markdown("💡 Price action within normal volatility band over the cached history.")
else:
    st.info("No klines cached yet for this symbol — the slow loop fetches them every 5 min.")

st.divider()


# ---------------- 4. VOLUME PROFILE ----------------

st.subheader("4. Volume profile")

if klines and len(klines) >= 3:

    def _qv(idx: int) -> float | None:
        if abs(idx) > len(klines):
            return None
        return klines[idx].quote_volume / 1e6

    today_v = _qv(-1)
    yest_v = _qv(-2)
    before_v = _qv(-3)

    v1, v2, v3, v4 = st.columns(4)
    v1.metric(
        "24h ticker (rolling)",
        f"{volume_24h_raw / 1e6:.2f}M" if volume_24h_raw else "—",
        help="From the 24h ticker endpoint. Continuously rolling 24h window.",
    )
    v2.metric("Today (so far)", f"{today_v:.2f}M" if today_v is not None else "—",
              help="Newest daily kline's quote volume — incomplete day.")
    v3.metric("Yesterday", f"{yest_v:.2f}M" if yest_v is not None else "—")
    v4.metric("Day before", f"{before_v:.2f}M" if before_v is not None else "—")

    # Tiny bar chart of last 14 days quote volume
    chart_n = min(14, len(klines))
    vol_df = pd.DataFrame(
        {"quote_volume_M": [k.quote_volume / 1e6 for k in klines[-chart_n:]]},
        index=pd.to_datetime([k.open_time for k in klines[-chart_n:]]),
    )
    st.bar_chart(vol_df, height=200)
    st.caption(f"Last {chart_n} daily quote volumes (in millions USDT).")

    # Volume momentum signal
    if today_v and yest_v:
        ratio = today_v / yest_v
        if ratio > 1.5:
            st.markdown(f"💡 Today's volume is **{ratio:.1f}× yesterday** — interest accelerating.")
        elif ratio < 0.5:
            st.markdown(f"💡 Today's volume is only **{ratio:.1f}× yesterday** — interest fading.")
        else:
            st.markdown("💡 Volume in line with recent days.")
else:
    st.info("Not enough kline history to break down per-day volumes yet.")

st.divider()


# ---------------- 5. MARKET CONTEXT ----------------

st.subheader("5. Market context")

c1, c2, c3, c4 = st.columns(4)
if contract:
    c1.metric(
        "Maker fee",
        f"{contract.maker_fee_percent:.4f}%",
        help="Per-contract maker fee. Used in arb calculations: 4 × this for round trip on a paired position.",
    )
    c2.metric(
        "Taker fee",
        f"{contract.taker_fee_percent:.4f}%",
        help="Per-contract taker fee.",
    )
else:
    c1.metric("Maker fee", "—")
    c2.metric("Taker fee", "—")

if funding_row:
    c3.metric("Funding interval", f"{funding_row.interval_hours:.0f}h")
    nf = funding_row.next_funding_time
    c4.metric("Next funding in", minutes_to(nf) or "—")
else:
    c3.metric("Funding interval", "—")
    c4.metric("Next funding in", "—")

if mcap_usd:
    st.markdown(
        f"**Market cap:** ${mcap_usd / 1e9:.2f}B (CoinGecko top-1000) — "
        "compare to similar-sized peers to gauge whether the move is normal or exceptional."
    )
else:
    st.markdown(
        "**Market cap:** not in CoinGecko top-1000. Likely a smaller / newer coin — bigger % moves are normal."
    )

st.divider()


# ---------------- footer ----------------

st.markdown(
    f"_Detail page for **{symbol_q}** ({exchange}). Data refreshes from the background updater every 30–180s; "
    "Open Interest and Long/Short ratio are fetched live with a 2-minute cache._"
)
