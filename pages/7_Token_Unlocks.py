"""Page 7 — Token unlock calendar.

Shows upcoming unlock events for tokens tradable on Binance or MEXC futures,
sorted by date. Data lives in `config/token_unlocks.yaml` (manually maintained
since free unlock APIs are paywalled). Colour-coded by impact: high (🔴) when
the unlock is ≥3% of circulating supply, medium (🟡) ≥1%, low (🟢) otherwise.
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
    sidebar_status,
)
from funding_screener.unlocks import (  # noqa: E402
    attach_usd_values,
    load_upcoming_unlocks,
)

st.set_page_config(page_title="Token Unlocks", layout="wide")

store = boot()
sidebar_status(store)
auto_rerun(interval_ms=300_000, key="page7_tick")

st.title("Token unlock calendar")
st.caption(
    "Upcoming unlock events for tokens listed on Binance or MEXC futures. "
    "Data lives in `config/token_unlocks.yaml` and is manually maintained — "
    "free unlock APIs are paywalled, so populate it yourself from "
    "[defillama.com/unlocks](https://defillama.com/unlocks). "
    "Past events are auto-hidden; weekly maintenance is plenty."
)


# ---------------- pull tradable universe ----------------

binance = store.read_binance()
mexc = store.read_mexc()
tradable = {c.base_asset.upper() for c in binance.contracts} | {c.base_asset.upper() for c in mexc.contracts}

# Build a USD price map for converting amount_tokens → amount_usd when not given.
price_map: dict[str, float] = {}
for r in binance.funding:
    if r.quote_asset == "USDT" and r.mark_price:
        price_map.setdefault(r.base_asset.upper(), r.mark_price)
for r in mexc.funding:
    if r.quote_asset == "USDT" and r.mark_price:
        price_map.setdefault(r.base_asset.upper(), r.mark_price)


# ---------------- load unlocks ----------------

all_events = load_upcoming_unlocks(tradable_symbols=tradable)
all_events = attach_usd_values(all_events, price_map)

# ---------------- filter controls ----------------

st.markdown("### Filters")
fc1, fc2, fc3 = st.columns([1.2, 1, 2])

window_choice = fc1.selectbox(
    "Time window",
    ["This week (7d)", "Next 30d", "All upcoming"],
    index=0,
    help="Filter to events within this many days.",
)
min_pct = fc2.slider(
    "Min % of supply",
    min_value=0.0,
    max_value=10.0,
    value=5.0,
    step=0.5,
    help="Drop events smaller than this % of circulating supply. 5% is the default — 'big enough to matter'.",
)
quick_button = fc3.button(
    "Reset to default (≥5% this week)",
    help="Reset both filters to their defaults.",
    use_container_width=False,
)
if quick_button:
    st.rerun()  # streamlit re-renders with default values

window_days_map = {"This week (7d)": 7, "Next 30d": 30, "All upcoming": 10**6}
window_days = window_days_map[window_choice]

events = [
    e for e in all_events
    if e.days_until <= window_days
    and (e.pct_of_supply is not None and e.pct_of_supply >= min_pct
         or (min_pct == 0.0 and (e.pct_of_supply is None or e.pct_of_supply >= 0)))
]
# When min_pct > 0 we strictly require the event to have a known pct_of_supply
# above the threshold (events with unknown pct_of_supply are excluded — we
# can't tell their impact). When min_pct == 0, include everything.
if min_pct > 0:
    events = [e for e in events if e.pct_of_supply is not None and e.pct_of_supply >= min_pct]


# ---------------- empty state with instructions ----------------

if not all_events:
    st.info(
        "**No upcoming unlocks in `config/token_unlocks.yaml`** for tokens currently "
        "listed on Binance or MEXC futures.\n\n"
        "If this is a fresh deploy, the file ships with examples commented out — "
        "uncomment them or add your own entries from "
        "[defillama.com/unlocks](https://defillama.com/unlocks). The format and "
        "all required fields are documented in the YAML file's header."
    )
    st.markdown(
        """
**Quick add template** (copy into `config/token_unlocks.yaml`):

```yaml
unlocks:
  - symbol: ARB
    name: "Arbitrum"
    date: "2026-05-16"
    amount_tokens: 92655000
    pct_of_supply: 1.84
    type: "cliff"
    notes: "Monthly investor + team unlock"
```
"""
    )
    st.stop()

# After filtering: maybe nothing matches the filter even though YAML has events.
if not events:
    st.warning(
        f"**No upcoming events match the current filter** "
        f"(window: {window_choice}, min ≥ {min_pct:.1f}% of supply).\n\n"
        f"Loosen the filter above, or add more entries to `config/token_unlocks.yaml`."
    )
    st.stop()


# ---------------- display ----------------

# Quick stats — using the FILTERED set so they reflect what's on screen.
total_events = len(events)
total_unlock_usd = sum((e.amount_usd or 0.0) for e in events)
within_7d = sum(1 for e in events if e.days_until <= 7)
high_impact = sum(1 for e in events if e.impact_label() == "High")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Events shown", total_events)
c2.metric("Within 7 days", within_7d)
c3.metric("≥3% supply (high impact)", high_impact)
c4.metric(
    "Total $ unlocking",
    f"${total_unlock_usd / 1e6:,.1f}M" if total_unlock_usd > 0 else "—",
    help="Sum of USD value of all unlock events shown.",
)

st.divider()

# Build dataframe
rows = []
for e in events:
    rows.append({
        "Impact": f"{e.impact_emoji()} {e.impact_label()}",
        "Date": e.date_str,
        "Days": e.days_until,
        "Symbol": e.symbol,
        "Name": e.name,
        "Type": e.unlock_type,
        "Tokens unlocking": e.amount_tokens,
        "USD value": e.amount_usd,
        "% of circ. supply": e.pct_of_supply,
        "% of total supply": e.pct_of_total,
        "Notes": e.notes,
    })

df = pd.DataFrame(rows)

st.dataframe(
    df, hide_index=True, use_container_width=True,
    column_config={
        "Impact": st.column_config.TextColumn(
            "Impact",
            help=(
                "Heuristic based on % of circulating supply being unlocked:\n"
                "🔴 High   — ≥ 3% of supply (significant sell pressure expected)\n"
                "🟡 Medium — ≥ 1% (notable, often absorbable)\n"
                "🟢 Low    — < 1% (typically noise)\n"
                "⚪ Unknown — pct_of_supply not provided in YAML"
            ),
        ),
        "Date": st.column_config.TextColumn("Date", help="UTC date of the unlock event (YYYY-MM-DD)."),
        "Days": st.column_config.NumberColumn(
            format="%d",
            help="Calendar days until the unlock from today (UTC).",
        ),
        "Symbol": st.column_config.TextColumn("Symbol", help="Matches the Binance/MEXC futures ticker."),
        "Type": st.column_config.TextColumn(
            "Type",
            help=(
                "cliff — single large release on the date\n"
                "linear — daily/continuous emission; date is schedule peak / month\n"
                "milestone — release contingent on a project milestone\n"
                "staking — large staked-token withdrawal period ends\n"
                "airdrop — claim period or distribution start"
            ),
        ),
        "Tokens unlocking": st.column_config.NumberColumn(format="%,.0f"),
        "USD value": st.column_config.NumberColumn(
            format="$%,.0f",
            help="amount_tokens × current mark price. Computed from cache when YAML field is null.",
        ),
        "% of circ. supply": st.column_config.NumberColumn(
            format="%.2f",
            help="What fraction of circulating supply this single event releases.",
        ),
        "% of total supply": st.column_config.NumberColumn(format="%.2f"),
        "Notes": st.column_config.TextColumn("Notes"),
    },
)

st.caption(
    f"Showing {total_events} upcoming events. Filtered to tokens tradable on Binance or MEXC futures "
    "({len(tradable)} bases in the cache). Edit `config/token_unlocks.yaml` to add or correct entries."
)
