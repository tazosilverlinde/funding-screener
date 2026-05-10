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


# ---- Single source of truth for the landing combined-screener rows -----------
# Round 36 consolidation: this block used to run inside each render_* helper
# (sentiment hero, best opportunities, daily highlights, sector rotation) —
# four duplicate screener invocations per page render. Now compute once and
# pass to every consumer.
from funding_screener.screener import screen_combined_high_funding as _screen  # noqa: E402

_bnb = store.read_binance()
_mxc = store.read_mexc()
_enrichments = store.read_enrichments()
_onchain_flows, _ = store.read_onchain_flows()
_onchain_by_base = {f["token"]: f.get("net_usd", 0.0) for f in _onchain_flows}
_klines_combined: dict = {}
_klines_combined.update(_bnb.klines)
_klines_combined.update(_mxc.klines)
_score_histories = store.read_score_histories()
_liq_stats_24h = store.read_liquidations(window_seconds=24 * 3600)
# Per-symbol histograms for sparkline data (only for symbols with WS activity).
_liq_histogram_by_symbol = {
    sym: store.read_liquidations_histogram(sym, bin_seconds=3600, window_seconds=24 * 3600)
    for sym in _liq_stats_24h.keys()
}

_combined_rows = _screen(
    _bnb.funding, _mxc.funding,
    _bnb.contracts, _mxc.contracts,
    _enrichments,
    threshold_percent=0.0,
    binance_volumes=_bnb.volumes, mexc_volumes=_mxc.volumes,
    min_volume_usd_per_side=0.0,
    onchain_netflow_by_base=_onchain_by_base,
    klines_by_symbol=_klines_combined,
    score_histories=_score_histories,
    liq_stats_by_symbol=_liq_stats_24h,
    liq_histogram_by_symbol=_liq_histogram_by_symbol,
)


# ---- Market sentiment hero (Round 30) ----------------------------------------
# Distills every tracked pair into one risk-on / risk-off read at the very top
# of the landing page. Computed up-front because it's the most important number
# on the page; everything below is detail.

def _render_market_sentiment_hero(rows: list) -> None:
    scored = [r for r in rows if r.composite_score is not None]
    if not scored:
        return  # too early — fast loop hasn't populated enough data yet
    n_total = len(scored)
    avg_score = sum(r.composite_score for r in scored) / n_total
    n_bull = sum(1 for r in scored if r.composite_score >= 30)
    n_bear = sum(1 for r in scored if r.composite_score <= -30)
    n_strong_bull = sum(1 for r in scored if r.composite_score >= 70)
    n_strong_bear = sum(1 for r in scored if r.composite_score <= -70)
    pct_bull = (n_bull / n_total) * 100.0
    pct_bear = (n_bear / n_total) * 100.0
    bull_minus_bear = pct_bull - pct_bear

    if avg_score >= 20 and bull_minus_bear >= 10:
        emoji, regime, box = "🚀", "Risk-on — bullish breadth", st.success
    elif avg_score <= -20 and bull_minus_bear <= -10:
        emoji, regime, box = "💥", "Risk-off — bearish breadth", st.error
    elif avg_score >= 5:
        emoji, regime, box = "🟢", "Mildly bullish — leaning long", st.info
    elif avg_score <= -5:
        emoji, regime, box = "🔴", "Mildly bearish — leaning short", st.warning
    else:
        emoji, regime, box = "🟡", "Mixed / no clear regime", st.info

    box(
        f"**{emoji} Market sentiment: {regime}**  \n"
        f"Across {n_total} tracked pairs: "
        f"avg composite score `{avg_score:+.1f}`  •  "
        f"🟢 bullish (≥+30): **{n_bull}** ({pct_bull:.0f}%, {n_strong_bull} strong)  •  "
        f"🔴 bearish (≤−30): **{n_bear}** ({pct_bear:.0f}%, {n_strong_bear} strong)  •  "
        f"breadth Δ {bull_minus_bear:+.0f} pp"
    )


_render_market_sentiment_hero(_combined_rows)


# ---- Best opportunities widget (Round 35) -----------------------------------
# Picks the top 3 actionable setups (Fresh / Building only — Mature, Late, and
# Noisy excluded) by absolute composite score. Renders as side-by-side cards
# directly under the sentiment hero so users see the punchy picks before
# scrolling to longer-form sections.

def _render_best_opportunities(rows: list) -> None:
    from funding_screener.highlights import pick_best_opportunities  # noqa: E402
    picks = pick_best_opportunities(rows, top_n=3)
    if not picks:
        return  # Quiet market or warmup — silently skip rather than fake-empty cards.

    st.subheader("🎯 Best opportunities right now")
    st.caption(
        "Top 3 highest-conviction actionable setups across all tracked pairs. "
        "Filtered to Fresh / Building quality only — Mature setups are likely "
        "already in motion, Late ones priced in, and Noisy ones not trustworthy. "
        "Click a symbol to drill into its full Detail page."
    )
    cols = st.columns(len(picks))
    for col, p in zip(cols, picks):
        # Side-by-side metric cards. Score as the headline number, quality
        # bucket as the delta caption.
        with col:
            st.metric(
                p["symbol"],
                f"{p['score']:+d}",
                delta=p["quality"],
                delta_color="off",  # neutral caption — emoji in label encodes mood
                help=(
                    f"{p['score_label']} · funding {p['funding']} · "
                    f"signal age {p['age']}\n\n"
                    f"Click → Detail page for full breakdown."
                ),
            )
            if p.get("binance_symbol"):
                st.markdown(
                    f"[Open {p['symbol']} detail →]"
                    f"(/Symbol_Detail?exchange=Binance&symbol={p['binance_symbol']})"
                )
    st.divider()


_render_best_opportunities(_combined_rows)


# ---- daily highlights (top of page — newspaper-style digest) ----
# Aggregates one headline from each major signal source so the user sees
# what matters at first glance without clicking through pages.
from funding_screener.highlights import all_highlights  # noqa: E402
from funding_screener.unlocks import load_upcoming_unlocks  # noqa: E402

_landing_tradable_bases = {c.base_asset.upper() for c in _bnb.contracts} | {c.base_asset.upper() for c in _mxc.contracts}
_landing_unlocks = load_upcoming_unlocks(tradable_symbols=_landing_tradable_bases)
_landing_supply = store.read_stablecoin_supply()

_highlights = all_highlights(_combined_rows, _landing_unlocks, _onchain_flows, _landing_supply)

if _highlights:
    st.subheader("📰 Today's signals")
    st.caption(
        "One headline per source — best long, best short, hottest funding, biggest "
        "upcoming unlock, whale spotlight, macro liquidity read. Pulls from the "
        "same data the dedicated pages render, just curated to the single top "
        "signal each. Empty entries mean that source hasn't produced a strong "
        "signal yet (or first scan still pending)."
    )
    for h in _highlights:
        st.markdown(f"**{h['emoji']} {h['label']}** — {h['headline']}")
    st.divider()


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

c1, c2, c3, c4 = st.columns(4)
c1.metric("Binance perps", len(_bnb.contracts))
c2.metric("MEXC perps", len(_mxc.contracts))
c3.metric("Binance klines cached", len(_bnb.klines))
c4.metric("MEXC klines cached", len(_mxc.klines))

## ---- recent alerts feed (round 27) -----------------------------------------
## Shows what's fired in the last hour from the AlertLog buffer. Lands here
## above the Top Movers because "what just happened" is more time-sensitive
## than "biggest moves over a longer window".
import time as _t  # noqa: E402

_alert_log = store.alert_log
_recent_window_s = 3600  # 1h
_now_ts = _t.time()
_recent_alerts = [
    r for r in _alert_log.recent(limit=200)
    if (_now_ts - r.fired_at) <= _recent_window_s
]
if _recent_alerts:
    st.subheader(f"🚨 Recent alerts — last hour ({len(_recent_alerts)})")
    st.caption(
        "Live feed of every alert that fired in the last 60 minutes. "
        "Active = condition crossed into the alert region; resolved = condition "
        "cleared. Mute noisy kinds on Page 11."
    )
    _alert_rows = []
    for r in _recent_alerts[:10]:  # cap landing-page list at 10
        _age_s = int(_now_ts - r.fired_at)
        _age_str = f"{_age_s}s" if _age_s < 60 else f"{_age_s // 60}m"
        _status_emoji = "🚨" if r.status == "active" else "✅"
        # Strip the kind prefix from the key for a cleaner first column.
        _short_key = r.key.split(":", 1)[1] if ":" in r.key else r.key
        _alert_rows.append({
            "When": _age_str + " ago",
            "Kind": r.kind,
            "Subject": _short_key,
            "Status": f"{_status_emoji} {r.status}",
        })
    st.dataframe(
        pd.DataFrame(_alert_rows),
        hide_index=True, use_container_width=True,
        column_config={
            "When": st.column_config.TextColumn(
                "When",
                help="Time since the alert fired.",
            ),
            "Kind": st.column_config.TextColumn(
                "Kind",
                help="Alert type (composite, score_delta, liq_cascade, "
                     "funding_dev, oi_surge, whale, new_listing, token_unlock).",
            ),
            "Subject": st.column_config.TextColumn(
                "Subject",
                help="The pair / symbol the alert is about.",
            ),
            "Status": st.column_config.TextColumn(
                "Status",
                help="🚨 active = condition just hit; ✅ resolved = it cleared.",
            ),
        },
    )
    st.caption(
        f"Showing {min(10, len(_recent_alerts))} most recent. "
        f"See Page 11 for the full audit log + mute controls."
    )
    st.divider()

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
## Reuses the consolidated _combined_rows from the top of the page (Round 36) —
## no separate screener invocation.
from funding_screener.sectors import sector_aggregates  # noqa: E402
import pandas as pd  # noqa: E402

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


## ---- liquidation summary (round 19) ------------------------------------------
## Surfaces the WebSocket liquidation tape on the landing page so users see
## forced-flow activity without having to navigate to Page 8. Biggest squeezes
## (short cascades = bullish) and biggest crashes (long cascades = bearish)
## sit side-by-side; aggregate ticker shows market-wide forced flow.
_liq_all = store.read_liquidations(window_seconds=24 * 3600)
if _liq_all:
    _total_long = sum(s.get("long_liq_usd", 0.0) or 0.0 for s in _liq_all.values())
    _total_short = sum(s.get("short_liq_usd", 0.0) or 0.0 for s in _liq_all.values())
    _total_all = _total_long + _total_short
    _events = sum(s.get("events_count", 0) or 0 for s in _liq_all.values())

    st.subheader("Liquidations — last 24h, market-wide")
    st.caption(
        "Aggregate forced-flow across every Binance perp. **Long-dominant** "
        "totals usually accompany sharp drops (longs blown out as price falls "
        "into stops); **short-dominant** totals accompany squeezes (shorts "
        "force-bought back). Big mixed totals = volatile two-way market."
    )

    lq1, lq2, lq3, lq4 = st.columns(4)
    lq1.metric(
        "Total 24h liq",
        f"${_total_all / 1e6:,.0f}M" if _total_all >= 1e6 else f"${_total_all / 1e3:.0f}K",
        f"{_events:,} events",
    )
    lq2.metric(
        "Long liq",
        f"${_total_long / 1e6:,.0f}M",
        help="Sum across all symbols — drop-side forced selling.",
    )
    lq3.metric(
        "Short liq",
        f"${_total_short / 1e6:,.0f}M",
        help="Sum across all symbols — squeeze-side forced buying.",
    )
    if _total_all > 0:
        skew = (_total_short - _total_long) / _total_all
        if skew > 0.2:
            bias_emoji, bias_label = "🟢", "Shorts dominantly liquidated"
        elif skew < -0.2:
            bias_emoji, bias_label = "🔴", "Longs dominantly liquidated"
        else:
            bias_emoji, bias_label = "🟡", "Two-way / balanced"
        lq4.metric("Market bias", f"{bias_emoji} {bias_label}", f"skew {skew:+.2f}")

    # Top cascades (long-dominant) and top squeezes (short-dominant), 5 each.
    _liq_rows = []
    for sym, stats in _liq_all.items():
        total = stats.get("total_usd", 0.0) or 0.0
        if total < 1_000_000:  # noise floor on the landing page
            continue
        _liq_rows.append({
            "Symbol": sym,
            "Long ($)": stats.get("long_liq_usd", 0.0) or 0.0,
            "Short ($)": stats.get("short_liq_usd", 0.0) or 0.0,
            "Total ($)": total,
        })
    if _liq_rows:
        col_squeeze, col_cascade = st.columns(2)
        # Squeezes: short_liq > long_liq, ranked by short_liq descending
        squeezes = sorted(
            [r for r in _liq_rows if r["Short ($)"] > r["Long ($)"]],
            key=lambda r: r["Short ($)"], reverse=True,
        )[:5]
        cascades = sorted(
            [r for r in _liq_rows if r["Long ($)"] > r["Short ($)"]],
            key=lambda r: r["Long ($)"], reverse=True,
        )[:5]
        col_squeeze.markdown("**🟢 Top short squeezes** (shorts blown out)")
        if squeezes:
            sq_df = pd.DataFrame([
                {
                    "Symbol": f"/Symbol_Detail?exchange=Binance&symbol={r['Symbol']}",
                    "Short liq": r["Short ($)"],
                    "Long liq": r["Long ($)"],
                }
                for r in squeezes
            ])
            col_squeeze.dataframe(
                sq_df, hide_index=True, use_container_width=True,
                column_config={
                    "Symbol": st.column_config.LinkColumn(
                        "Symbol", display_text=r".*symbol=([^&]+)"
                    ),
                    "Short liq": st.column_config.NumberColumn(format="$%,.0f"),
                    "Long liq": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )
        else:
            col_squeeze.write("_None in the last 24h._")
        col_cascade.markdown("**🔴 Top long cascades** (longs blown out)")
        if cascades:
            ca_df = pd.DataFrame([
                {
                    "Symbol": f"/Symbol_Detail?exchange=Binance&symbol={r['Symbol']}",
                    "Long liq": r["Long ($)"],
                    "Short liq": r["Short ($)"],
                }
                for r in cascades
            ])
            col_cascade.dataframe(
                ca_df, hide_index=True, use_container_width=True,
                column_config={
                    "Symbol": st.column_config.LinkColumn(
                        "Symbol", display_text=r".*symbol=([^&]+)"
                    ),
                    "Long liq": st.column_config.NumberColumn(format="$%,.0f"),
                    "Short liq": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )
        else:
            col_cascade.write("_None in the last 24h._")
    st.divider()


## ---- performance: per-loop cycle timings -----------------------------------
_loop_stats = store.read_loop_stats()
if _loop_stats:
    with st.expander(f"Performance — background loop timings ({len(_loop_stats)} loops)", expanded=False):
        st.caption(
            "Each background loop reports its wall-clock cycle duration. "
            "Compare avg vs p95 to spot spikes; large p95/avg ratio means at "
            "least one cycle stalled (slow RPC, network blip, rate-limit). "
            "Last-50-cycles rolling window."
        )
        # ── Process memory budget (Round 51) ─────────────────────────────
        # Surfaces RSS against the user's 2GB hard cap. Color-coded by
        # pressure bucket so excess shows up immediately.
        from funding_screener.process_memory import (  # noqa: E402
            PROCESS_MEMORY_BUDGET_MB,
            current_process_memory_mb,
            memory_pressure_label,
        )
        _rss_mb = current_process_memory_mb()
        if _rss_mb is not None:
            _pressure = memory_pressure_label(_rss_mb)
            _pct_used = (_rss_mb / PROCESS_MEMORY_BUDGET_MB) * 100.0
            _bar_box = {
                "ok": st.success,
                "warn": st.info,
                "crit": st.error,
            }.get(_pressure, st.info)
            _bar_box(
                f"**Process memory:** `{_rss_mb:,.0f} MB` of "
                f"`{PROCESS_MEMORY_BUDGET_MB:,.0f} MB` budget used ({_pct_used:.0f}%) — "
                f"{'OK' if _pressure == 'ok' else 'WATCH' if _pressure == 'warn' else 'CRITICAL'}"
            )
        perf_rows = []
        for name in sorted(_loop_stats.keys()):
            s = _loop_stats[name]
            perf_rows.append({
                "Loop": name,
                "Samples": s["samples"],
                "Last (s)": round(s["last_s"], 2),
                "Avg (s)": round(s["avg_s"], 2),
                "P50 (s)": round(s["p50_s"], 2),
                "P95 (s)": round(s["p95_s"], 2),
            })
        st.dataframe(
            pd.DataFrame(perf_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Samples": st.column_config.NumberColumn(format="%d"),
            },
        )

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
