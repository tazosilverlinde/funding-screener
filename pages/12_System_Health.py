"""Page 12 — System Health (Round 56).

Operator-facing dashboard consolidating everything that can break or get
slow about the running deploy. Pulls from:
  - process_memory: RSS + per-buffer breakdown (Rounds 51-53)
  - loop_timings + last_loop_ran_at: stall detection (Round 55)
  - recent_errors: bounded ring of recent errors (Round 56)
  - alert_log: 24h fire counts by kind
  - liquidations buffer health: WS connection liveness (Round 14)
  - per-data-source freshness timestamps

Pure presentation — every signal it shows comes from a DataStore method
already used elsewhere. Nothing computed here that isn't already computed
upstream. Renders even when data is empty (cold start).
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.process_memory import (  # noqa: E402
    PROCESS_MEMORY_BUDGET_MB,
    current_process_memory_mb,
    estimate_buffer_memory,
    memory_pressure_label,
)
from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun,
    boot,
    sidebar_status,
)


st.set_page_config(page_title="System Health", layout="wide")

store = boot()
sidebar_status(store)
auto_rerun(interval_ms=15_000, key="health_tick")


st.title("🩺 System Health")
st.caption(
    "Operator dashboard. Memory budget, loop status, recent errors, alert "
    "fire counts, WebSocket health, cache freshness. Everything in one place "
    "so a stuck deploy is debuggable in 30 seconds."
)


# ---------------- Memory ----------------

st.subheader("1. Memory")
_rss_mb = current_process_memory_mb()
if _rss_mb is None:
    st.info("psutil not available; memory introspection disabled.")
else:
    _pressure = memory_pressure_label(_rss_mb)
    _pct_used = (_rss_mb / PROCESS_MEMORY_BUDGET_MB) * 100.0
    _bar_box = {"ok": st.success, "warn": st.info, "crit": st.error}.get(_pressure, st.info)
    _bar_box(
        f"**Process RSS:** `{_rss_mb:,.0f} MB` of `{PROCESS_MEMORY_BUDGET_MB:,.0f} MB` "
        f"budget used ({_pct_used:.0f}%) — "
        f"{'OK' if _pressure == 'ok' else 'WATCH' if _pressure == 'warn' else 'CRITICAL'}"
    )
    _buffer_rows = estimate_buffer_memory(store)
    st.dataframe(
        pd.DataFrame(_buffer_rows),
        hide_index=True, use_container_width=True,
        column_config={
            "buffer": st.column_config.TextColumn("Buffer"),
            "entries": st.column_config.NumberColumn("Entries", format="%d"),
            "est_mb": st.column_config.NumberColumn("Est MB", format="%.2f"),
        },
    )

st.divider()


# ---------------- Loops ----------------

st.subheader("2. Background loops")
_loop_stats = store.read_loop_stats()
_last_ran = store.read_last_loop_ran_at()
if not _loop_stats:
    st.caption("No loop timings recorded yet — process is still warming up.")
else:
    rows: list[dict] = []
    now_utc = datetime.now(timezone.utc)
    restart_counts = getattr(store, "task_restart_counts", {}) or {}
    # Hardcoded expected intervals — same as the loop-stall alert evaluator.
    _expected = {
        "fast": 60, "slow": 300, "market_caps": 300, "enrichment": 180,
        "macro": 900, "score_history": 600,
        "onchain.ethereum": 900, "onchain.bsc": 900,
        "macro_flow.ethereum": 21600, "macro_flow.bsc": 21600,
    }
    for name in sorted(_loop_stats.keys()):
        s = _loop_stats[name]
        ran_at = _last_ran.get(name)
        if ran_at:
            elapsed_s = (now_utc - ran_at).total_seconds()
            age_str = (
                f"{elapsed_s:.0f}s ago" if elapsed_s < 60 else
                f"{elapsed_s / 60:.1f}m ago" if elapsed_s < 3600 else
                f"{elapsed_s / 3600:.1f}h ago"
            )
        else:
            age_str = "—"
            elapsed_s = None
        # Stall flag — same threshold the alert uses.
        expected_s = _expected.get(name)
        if expected_s and elapsed_s is not None and elapsed_s >= 3.0 * expected_s:
            status = "⚠️ STALLED"
        elif elapsed_s is not None and elapsed_s < 3 * (expected_s or 600):
            status = "✅ ok"
        else:
            status = "—"
        rows.append({
            "Loop": name,
            "Status": status,
            "Last cycle": age_str,
            "Expected (s)": expected_s if expected_s else "—",
            "Avg (s)": round(s["avg_s"], 2),
            "P95 (s)": round(s["p95_s"], 2),
            "Last (s)": round(s["last_s"], 2),
            "Samples": s["samples"],
            "Restarts": restart_counts.get(name, 0),
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, use_container_width=True,
        column_config={
            "Avg (s)": st.column_config.NumberColumn(format="%.2f"),
            "P95 (s)": st.column_config.NumberColumn(format="%.2f"),
            "Last (s)": st.column_config.NumberColumn(format="%.2f"),
            "Samples": st.column_config.NumberColumn(format="%d"),
            "Restarts": st.column_config.NumberColumn(
                format="%d",
                help="Number of times the supervisor (Round 64) had to catch a "
                     "fatal exception and restart this task. 0 = healthy lifetime; "
                     "> 0 = task crashed but recovered; persistent climb = real bug.",
            ),
        },
    )
st.caption(
    "Status `⚠️ STALLED` means the loop hasn't completed a cycle in ≥3× its "
    "expected interval — same threshold the loop_stall Telegram alert uses. "
    "**Restarts** counts supervisor recoveries (Round 64) — should stay at 0."
)

st.divider()


# ---------------- WebSocket health ----------------

st.subheader("3. Liquidation tape (WebSocket)")
_liq_health = store.liquidations_health()
_total_events = _liq_health.get("total_events", 0) or 0
_last_event_at_ts = _liq_health.get("last_event_at")

ws_c1, ws_c2 = st.columns(2)
ws_c1.metric("Lifetime events", f"{_total_events:,}",
             help="Total liquidation events received since process start.")
if _last_event_at_ts:
    age_s = max(0, time.time() - _last_event_at_ts)
    if age_s < 5:
        ws_c2.metric("Last event", f"{age_s:.1f}s ago", delta="LIVE")
    elif age_s < 60:
        ws_c2.metric("Last event", f"{age_s:.0f}s ago", delta="OK", delta_color="off")
    elif age_s < 300:
        ws_c2.metric(
            "Last event", f"{age_s / 60:.1f}m ago",
            delta="QUIET", delta_color="off",
            help="Liquidations are bursty — quiet markets can have 1-3 min gaps.",
        )
    else:
        ws_c2.metric(
            "Last event", f"{age_s / 60:.0f}m ago",
            delta="WS may be stalled",
            delta_color="inverse",
            help="WebSocket connection may have dropped. Auto-reconnect should "
                 "kick in within ~60s; if this number keeps climbing, restart.",
        )
else:
    ws_c2.metric("Last event", "—", help="No events received yet — WS may still be connecting.")

st.divider()


# ---------------- Telegram rate-limit drops (Round 61) ----------------

# Surface the cumulative dropped count from the rate limiter. When > 0 it
# means the alerts loop tried to send more than the configured per-minute
# cap; users should consider tuning thresholds, watchlist, or cooldown.
from funding_screener.background import _runner_state  # noqa: E402
_telegram_client = _runner_state.get("telegram")
if _telegram_client is not None:
    _dropped = _telegram_client.dropped_count()
    if _dropped > 0:
        st.warning(
            f"⚠️ **Telegram rate-limit drops:** {_dropped} message(s) dropped "
            "since process start due to the per-minute cap. Consider tuning "
            "alert thresholds, enabling the watchlist filter, or raising "
            "`alerts.telegram_rate_limit_per_minute` in `config/alerts.yaml`."
        )
    else:
        st.caption(
            "Telegram rate limiter: 0 messages dropped since process start ✅"
        )

st.divider()


# ---------------- Recent errors ----------------

st.subheader("4. Recent errors")
_errors = store.read_recent_errors()
if not _errors:
    st.success("No errors recorded since process start. ✅")
else:
    err_rows = [
        {"When (UTC)": ts.strftime("%Y-%m-%d %H:%M:%S"), "Message": msg}
        for ts, msg in reversed(_errors[-20:])  # newest first, last 20
    ]
    st.dataframe(
        pd.DataFrame(err_rows), hide_index=True, use_container_width=True,
    )
    st.caption(f"{len(_errors)} errors in buffer (showing last 20). Buffer caps at 50.")

st.divider()


# ---------------- Alert fire counts (last 24h, by kind) ----------------

st.subheader("5. Alert fires — last 24h, by kind")
_now_ts = time.time()
_last_24h = [r for r in store.alert_log.recent(limit=10_000)
             if (_now_ts - r.fired_at) <= 24 * 3600]
if not _last_24h:
    st.caption("No alerts have fired in the last 24h.")
else:
    counts: dict[str, dict[str, int]] = {}
    for r in _last_24h:
        bucket = counts.setdefault(r.kind, {"active": 0, "resolved": 0})
        bucket[r.status] = bucket.get(r.status, 0) + 1
    counts_rows = sorted(
        [{"Kind": k, "Active fires": v["active"], "Resolved fires": v["resolved"],
          "Total": v["active"] + v["resolved"]}
         for k, v in counts.items()],
        key=lambda r: r["Total"], reverse=True,
    )
    st.dataframe(
        pd.DataFrame(counts_rows), hide_index=True, use_container_width=True,
        column_config={
            "Active fires": st.column_config.NumberColumn(format="%d"),
            "Resolved fires": st.column_config.NumberColumn(format="%d"),
            "Total": st.column_config.NumberColumn(format="%d"),
        },
    )
    st.caption(
        f"{len(_last_24h)} fires in the last 24h. See Page 11 for the full audit log."
    )

st.divider()


# ---------------- Signal hit-rate analytics (Round 71) ----------------

st.subheader("6. Signal hit-rate — last 24h")
st.caption(
    "Did composite-score crossings actually sustain? For each pair, we find "
    "every time the score crossed into ±threshold, then look at the score "
    "1 hour later. Sustained = still in the threshold region after 1h. "
    "Aggregated across all tracked pairs. Useful for gauging the SIGNAL'S "
    "own predictive value — meta-quality."
)
from funding_screener.analytics import compute_signal_hit_rate  # noqa: E402

_score_histories_for_analytics = store.read_score_histories()
if not _score_histories_for_analytics:
    st.caption("Score history empty — wait for the first snapshot (~10 min).")
else:
    hit_rate_cols = st.columns(2)
    bull_stats = compute_signal_hit_rate(
        _score_histories_for_analytics,
        threshold=70, follow_up_hours=1.0,
    )
    bear_stats = compute_signal_hit_rate(
        _score_histories_for_analytics,
        threshold=-70, follow_up_hours=1.0,
    )

    with hit_rate_cols[0]:
        st.markdown("**🚀 +70 bullish crossings**")
        if bull_stats["n_crosses"] == 0:
            st.caption("No bullish crossings in the window yet.")
        else:
            sr = bull_stats["sustain_rate"] or 0.0
            st.metric(
                "Sustain rate (1h)",
                f"{sr * 100:.0f}%",
                f"{bull_stats['n_sustained']} / {bull_stats['n_crosses']} sustained",
            )
            if bull_stats["avg_score_after"] is not None:
                st.caption(
                    f"Avg at cross: `{bull_stats['avg_score_at_cross']:+.1f}` → "
                    f"avg 1h later: `{bull_stats['avg_score_after']:+.1f}` "
                    f"(Δ {bull_stats['avg_score_delta']:+.1f})"
                )
            if bull_stats["n_followup_missing"]:
                st.caption(
                    f"_{bull_stats['n_followup_missing']} cross(es) had no "
                    "follow-up sample within tolerance (too recent or gap in data)._"
                )

    with hit_rate_cols[1]:
        st.markdown("**💥 −70 bearish crossings**")
        if bear_stats["n_crosses"] == 0:
            st.caption("No bearish crossings in the window yet.")
        else:
            sr = bear_stats["sustain_rate"] or 0.0
            st.metric(
                "Sustain rate (1h)",
                f"{sr * 100:.0f}%",
                f"{bear_stats['n_sustained']} / {bear_stats['n_crosses']} sustained",
            )
            if bear_stats["avg_score_after"] is not None:
                st.caption(
                    f"Avg at cross: `{bear_stats['avg_score_at_cross']:+.1f}` → "
                    f"avg 1h later: `{bear_stats['avg_score_after']:+.1f}` "
                    f"(Δ {bear_stats['avg_score_delta']:+.1f})"
                )

st.divider()


# ---------------- Cache freshness ----------------

st.subheader("6. Cache freshness")
freshness_pairs: list[tuple[str, datetime | None]] = [
    ("Funding/contracts/volumes (fast loop)", store.last_fast_at),
    ("Klines (slow loop)", store.last_slow_at),
    ("Market caps", store.last_market_caps_at),
    ("Enrichments (OI, L/S, funding history)", store.last_enrichment_at),
    ("Stablecoin supply (macro loop)", store.last_macro_at),
    ("Score-history snapshot", store.last_score_snapshot_at),
]
fresh_rows: list[dict] = []
for label, ts in freshness_pairs:
    if ts is None:
        fresh_rows.append({"Source": label, "Last refresh": "—", "Age": "—"})
        continue
    elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
    if elapsed < 60:
        age = f"{elapsed:.0f}s ago"
    elif elapsed < 3600:
        age = f"{elapsed / 60:.1f}m ago"
    else:
        age = f"{elapsed / 3600:.1f}h ago"
    fresh_rows.append({
        "Source": label,
        "Last refresh": ts.strftime("%H:%M:%S UTC"),
        "Age": age,
    })
# Onchain freshness — per chain.
for chain, ts in (store.last_onchain_by_chain or {}).items():
    elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
    age = f"{elapsed / 60:.1f}m ago" if elapsed < 3600 else f"{elapsed / 3600:.1f}h ago"
    fresh_rows.append({
        "Source": f"On-chain whale flows ({chain})",
        "Last refresh": ts.strftime("%H:%M:%S UTC"),
        "Age": age,
    })
st.dataframe(
    pd.DataFrame(fresh_rows), hide_index=True, use_container_width=True,
)

st.divider()
st.caption(
    "_Page 12 — System Health. Auto-refreshes every 15s. Hidden when no data; "
    "every section degrades gracefully on cold start._"
)
