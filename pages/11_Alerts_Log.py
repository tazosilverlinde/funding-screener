"""Page 11 — Alerts audit log (Round 24).

Every time the alerts loop fires (active or resolved transition) it records
the event to an in-memory ring buffer. This page renders that buffer so users
have a per-app history of what fired and when, even without Telegram set up.

Buffer is in-memory only and bounded at 500 entries — a process restart
clears it. The audit log is for "what happened recently?" not long-term
analytics; persistence is intentionally out of scope.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from funding_screener.streamlit_helpers import (  # noqa: E402
    auto_rerun, boot, sidebar_status,
)


st.set_page_config(page_title="Alerts log", layout="wide")

store = boot()
sidebar_status(store)
auto_rerun(interval_ms=30_000, key="alerts_log_tick")

st.title("Alerts log — recent fires")
st.caption(
    "Every alert that fired since process start, newest first. Includes the "
    "key, status (active vs resolved), the exact message that was sent, and "
    "which channels accepted it. Bounded at 500 entries — older fires drop. "
    "Wiped on restart."
)

log = store.alert_log
total_logged = len(log)


# ---- mute controls (Round 25) ----
with st.expander("🔕 Mute alerts", expanded=False):
    st.caption(
        "Suppress noisy alert kinds or symbols for a few hours without changing "
        "your config files. Mutes are in-memory and clear on restart. Muted "
        "alerts skip Telegram delivery AND the audit log below."
    )
    active_mutes = store.read_alert_mutes()
    if active_mutes:
        st.markdown("**Active mutes:**")
        for pattern, expiry in sorted(active_mutes.items(), key=lambda x: x[1]):
            mins_left = int((expiry - datetime.now(timezone.utc).timestamp()) / 60)
            mc1, mc2 = st.columns([4, 1])
            mc1.write(f"`{pattern}` — expires in {mins_left} min")
            if mc2.button("Unmute", key=f"unmute_{pattern}"):
                store.unmute_alert(pattern)
                st.rerun()

    st.markdown("**Add a new mute:**")
    add_c1, add_c2, add_c3, add_c4 = st.columns([1, 2, 1, 1])
    pattern_type = add_c1.selectbox(
        "Type", ["kind", "symbol"],
        help="kind = mute every alert of this type (e.g. composite, score_delta). "
             "symbol = mute every alert mentioning this symbol substring (e.g. BTCUSDT).",
    )
    pattern_value = add_c2.text_input(
        "Value",
        placeholder="e.g. score_delta" if pattern_type == "kind" else "e.g. BTCUSDT",
    )
    duration_h = add_c3.number_input(
        "Hours", min_value=0.5, max_value=72.0, value=4.0, step=0.5,
        help="Mute duration. Capped at 72h to prevent forgotten silencing.",
    )
    if add_c4.button("Mute", disabled=not pattern_value):
        full_pattern = f"{pattern_type}:{pattern_value.strip()}"
        store.mute_alert(full_pattern, hours=float(duration_h))
        st.success(f"Muted `{full_pattern}` for {duration_h:g}h")
        st.rerun()


if total_logged == 0:
    st.info(
        "No alerts have fired since process start. Either the market is "
        "quiet, your thresholds are tight, or the alerts loop hasn't reached "
        "a transition yet (gives ~1 minute after deploy)."
    )
    st.stop()


# ---- filter controls ----
all_kinds = sorted(log.kinds())
col_filter, col_limit = st.columns([3, 1])
selected_kinds = col_filter.multiselect(
    "Filter by alert type",
    all_kinds,
    default=all_kinds,
    help="Each alert key has a 'kind' prefix (e.g. composite, liq_cascade, "
         "funding_dev). Leave all selected to see everything; pick subset "
         "to focus on one source.",
)
limit = col_limit.selectbox(
    "Show",
    [25, 50, 100, 200, 500],
    index=1,
    help="Number of most-recent entries to render.",
)

# Pull the filtered slice. We over-fetch then post-filter when the user
# selected a strict subset of kinds — otherwise the kind-filter loop in
# AlertLog.recent runs once with no filter (faster path).
if set(selected_kinds) == set(all_kinds):
    rows = log.recent(limit=limit)
else:
    rows = [r for r in log.recent(limit=10_000) if r.kind in set(selected_kinds)]
    rows = rows[:limit]


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_age(ts: float) -> str:
    age_s = (datetime.now(timezone.utc).timestamp() - ts)
    if age_s < 60:
        return f"{age_s:.0f}s ago"
    if age_s < 3600:
        return f"{age_s / 60:.0f}m ago"
    if age_s < 86400:
        return f"{age_s / 3600:.0f}h ago"
    return f"{age_s / 86400:.0f}d ago"


df = pd.DataFrame([
    {
        "When": _fmt_time(r.fired_at),
        "Age": _fmt_age(r.fired_at),
        "Kind": r.kind,
        "Status": "🚨 active" if r.status == "active" else "✅ resolved",
        "Key": r.key,
        "Delivered": ", ".join(r.delivered_to) if r.delivered_to else "—",
        "Message": r.message,
    }
    for r in rows
])

st.dataframe(
    df,
    hide_index=True,
    use_container_width=True,
    column_config={
        "When": st.column_config.TextColumn(
            "When",
            help="UTC timestamp at which the alert fired.",
        ),
        "Age": st.column_config.TextColumn("Age", help="Relative age."),
        "Kind": st.column_config.TextColumn(
            "Kind",
            help="Alert type prefix (composite, liq_cascade, funding_dev, etc.).",
        ),
        "Status": st.column_config.TextColumn(
            "Status",
            help="🚨 active = condition crossed into the alert region; "
                 "✅ resolved = condition cleared, follow-up sent.",
        ),
        "Key": st.column_config.TextColumn(
            "Key",
            help="Full de-dupe key — same key never fires twice in a row "
                 "without a resolved-and-re-trigger.",
        ),
        "Delivered": st.column_config.TextColumn(
            "Delivered",
            help="Channels that accepted the message. '—' means evaluator "
                 "fired but no channel was configured / all errored.",
        ),
        "Message": st.column_config.TextColumn(
            "Message",
            help="Exact text sent to Telegram (and email when applicable).",
            width="large",
        ),
    },
)


# ---- summary footer ----
st.divider()
counts_by_kind: dict[str, int] = {}
for r in log.recent(limit=10_000):
    counts_by_kind[r.kind] = counts_by_kind.get(r.kind, 0) + 1
counts_text = ", ".join(f"{k} ({v})" for k, v in sorted(counts_by_kind.items(), key=lambda x: -x[1]))
st.caption(
    f"{total_logged} alerts in buffer. By kind: {counts_text}. "
    "Buffer wraps at 500."
)
