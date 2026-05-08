"""Streamlit-specific helpers shared by every page under `pages/`.

Confined to this one module — the rest of `funding_screener` is UI-agnostic so
screener logic is unit-testable without Streamlit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, TypeVar

import pandas as pd
import streamlit as st

from .background import DataStore, get_store, start_background, wait_for_initial_data
from .config import fees as _fees_cfg

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # pragma: no cover
    st_autorefresh = None

T = TypeVar("T")


def boot() -> DataStore:
    """Idempotent: starts the background updater and waits for first data."""
    store = start_background()
    last_fast, _, _ = store.freshness()
    if last_fast is None:
        with st.spinner("Fetching first batch from Binance and MEXC…"):
            wait_for_initial_data(timeout_s=25.0)
    return store


def auto_rerun(interval_ms: int = 30_000, key: str = "tick") -> None:
    """Re-render the page every `interval_ms` so cache freshness updates visibly.

    Falls back silently if streamlit-autorefresh isn't installed.
    """
    if st_autorefresh is None:
        return
    st_autorefresh(interval=interval_ms, key=key)


def run_async(coro_factory: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(coro_factory())


def freshness_banner(store: DataStore) -> None:
    last_fast, last_slow, err = store.freshness()
    cols = st.columns([1, 1, 2])
    cols[0].metric("Funding/contracts", _age(last_fast))
    cols[1].metric("Klines (5m)", _age(last_slow))
    if err:
        cols[2].error(f"Background error: {err}")
    else:
        cols[2].success("Background updater healthy")


def sidebar_status(store: DataStore) -> None:
    """Render fees + freshness + manual refresh in the sidebar. Call once per page."""
    st.sidebar.header("Status")
    last_fast, last_slow, _ = store.freshness()
    st.sidebar.write(f"**Funding:** {_age(last_fast)}")
    st.sidebar.write(f"**Klines:** {_age(last_slow)}")

    st.sidebar.divider()
    st.sidebar.header("Maker fees")
    bnb_fees = _fees_cfg()["binance"]
    st.sidebar.write(f"**Binance USDT:** `{float(bnb_fees['futures_maker']):.4f}%`")
    bnb_quote_overrides = bnb_fees.get("futures_maker_by_quote") or {}
    if "USDC" in bnb_quote_overrides:
        st.sidebar.write(
            f"**Binance USDC:** `{float(bnb_quote_overrides['USDC']):.4f}%` "
            "(promotion — see `config/fees.yaml`)"
        )

    mxc = store.read_mexc()
    if mxc.contracts:
        fees = [c.maker_fee_percent for c in mxc.contracts if c.maker_fee_percent is not None]
        if fees:
            avg = sum(fees) / len(fees)
            st.sidebar.write(
                f"**MEXC:** avg `{avg:.4f}%` "
                f"(min `{min(fees):.4f}%` / max `{max(fees):.4f}%`, per-contract)"
            )
        else:
            st.sidebar.write("**MEXC:** per-contract (data pending)")
    else:
        st.sidebar.write("**MEXC:** per-contract (data pending)")

    st.sidebar.divider()
    if st.sidebar.button("Refresh now", use_container_width=True):
        st.rerun()


def to_df(rows: list[dict], column_order: list[str] | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if column_order:
        kept = [c for c in column_order if c in df.columns]
        df = df[kept]
    return df


def render_table(df: pd.DataFrame, column_config: Optional[dict] = None, height: int | None = None) -> None:
    if df.empty:
        st.info("No rows match the filter at the moment.")
        return
    kwargs: dict = {"hide_index": True, "use_container_width": True}
    if column_config:
        kwargs["column_config"] = column_config
    if height is not None:
        kwargs["height"] = height
    st.dataframe(df, **kwargs)


def _age(ts: datetime | None) -> str:
    if ts is None:
        return "pending"
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m {int(delta % 60)}s ago"
    return f"{int(delta // 3600)}h {int((delta % 3600) // 60)}m ago"


def minutes_to(ts) -> str | None:
    """Return relative time-until in 'Xm' or 'Yh Zm' form. Handles datetime,
    pandas Timestamp, ISO string, NaT, and None. Returns "settled" for past times.
    """
    if ts is None:
        return None
    # Reject NaT / NaN.
    try:
        if pd.isna(ts):  # type: ignore[arg-type]
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except Exception:
            return None
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta_min = int((ts - datetime.now(timezone.utc)).total_seconds() / 60)
    if delta_min < 0:
        return "settled"
    if delta_min < 60:
        return f"{delta_min}m"
    return f"{delta_min // 60}h {delta_min % 60}m"
