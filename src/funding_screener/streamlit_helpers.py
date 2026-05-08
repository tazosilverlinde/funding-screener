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
from .config import is_binance_enabled, is_mexc_enabled
from .sectors import all_sectors, symbols_in_sector

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
        # Dampen transient network errors — only escalate to red when stale.
        is_transient = "Timeout" in err or "ConnectError" in err or "cooldown" in err
        is_stale = last_fast is None or (
            (datetime.now(timezone.utc) - last_fast).total_seconds() > 300
        )
        if is_transient and not is_stale:
            cols[2].info(f"⏱ Transient: {err}")
        else:
            cols[2].error(f"Background error: {err}")
    else:
        cols[2].success("Background updater healthy")


def symbol_search_sidebar(store: DataStore) -> None:
    """Sidebar widget that lets the user jump straight to a symbol's detail page.

    Looks up the typed ticker against the current contracts cache; if it matches,
    sets query params to /Symbol_Detail?exchange=...&symbol=... and reruns
    Streamlit so navigation kicks in. Tolerates lower/upper case and the two
    naming conventions (Binance "BTCUSDT" vs MEXC "BTC_USDT") so both work.
    """
    st.sidebar.divider()
    st.sidebar.header("Quick search")
    typed = st.sidebar.text_input(
        "Symbol",
        value="",
        key="symbol_search_input",
        placeholder="e.g. BTCUSDT, BTC_USDT, WIF",
        help=(
            "Type any ticker. Match is case-insensitive. If you type just the base "
            "(e.g. 'WIF') we'll prefer the Binance USDT pair, then MEXC USDT."
        ),
    )
    go = st.sidebar.button("Open detail", use_container_width=True, key="symbol_search_btn")
    if not (go and typed.strip()):
        return

    target = typed.strip().upper()
    bnb = store.read_binance()
    mxc = store.read_mexc()

    # 1. Exact symbol match on either exchange.
    for c in bnb.contracts:
        if c.symbol.upper() == target:
            st.query_params["exchange"] = "Binance"
            st.query_params["symbol"] = c.symbol
            st.switch_page("pages/5_Symbol_Detail.py")
            return
    for c in mxc.contracts:
        if c.symbol.upper() == target:
            st.query_params["exchange"] = "MEXC"
            st.query_params["symbol"] = c.symbol
            st.switch_page("pages/5_Symbol_Detail.py")
            return

    # 2. Match by base asset — pick the canonical USDT pair.
    base = target.removesuffix("USDT").removesuffix("USDC").rstrip("_")
    for c in bnb.contracts:
        if c.base_asset.upper() == base and c.quote_asset == "USDT":
            st.query_params["exchange"] = "Binance"
            st.query_params["symbol"] = c.symbol
            st.switch_page("pages/5_Symbol_Detail.py")
            return
    for c in mxc.contracts:
        if c.base_asset.upper() == base and c.quote_asset == "USDT":
            st.query_params["exchange"] = "MEXC"
            st.query_params["symbol"] = c.symbol
            st.switch_page("pages/5_Symbol_Detail.py")
            return

    # 3. Tell the user nothing matched.
    st.sidebar.warning(
        f"`{typed}` not found in the current Binance or MEXC perp contracts. "
        "Either it's a different ticker format, or the contract isn't TRADING."
    )


def sector_sidebar() -> set[str]:
    """Render a sector multi-select in the sidebar; return the union of base
    assets in the chosen sectors.

    Returns an empty set when nothing's selected, which the caller treats as
    "no filtering" (don't restrict the table).
    """
    sectors = all_sectors()
    if not sectors:
        return set()
    st.sidebar.divider()
    st.sidebar.header("Sector filter")
    chosen = st.sidebar.multiselect(
        "Sectors",
        options=sectors,
        default=[],
        key="sector_filter",
        help=(
            "Narrow tables to base assets in one or more sectors. "
            "Mappings live in `config/symbol_sectors.yaml`. "
            "Tokens not classified there appear as '—' and get filtered out "
            "when any sector is selected."
        ),
    )
    if not chosen:
        return set()
    bases: set[str] = set()
    for s in chosen:
        bases |= symbols_in_sector(s)
    return bases


def watchlist_sidebar() -> set[str]:
    """Render the watchlist controls in the sidebar; return the set of upper-case
    symbols the user has enabled. Empty set → no filtering.

    Persists across page navigation via st.session_state.
    """
    st.sidebar.divider()
    st.sidebar.header("Watchlist")
    raw = st.sidebar.text_area(
        "Symbols",
        value=st.session_state.get("watchlist_input", ""),
        key="watchlist_input",
        placeholder="BTCUSDT, ARBUSDT, WIFUSDT",
        help=(
            "Comma- or newline-separated tickers (Binance or MEXC format). "
            "Tickers not matching any contract are silently ignored."
        ),
        height=80,
    )
    enabled = st.sidebar.checkbox(
        "Filter every page to watchlist only",
        value=st.session_state.get("watchlist_enabled", False),
        key="watchlist_enabled",
        help="When checked, all tables on all pages show only these symbols.",
    )
    if not enabled or not raw.strip():
        return set()
    parts: set[str] = set()
    # Tolerate commas, newlines, semicolons, whitespace.
    for tok in raw.replace(",", "\n").replace(";", "\n").split("\n"):
        s = tok.strip().upper()
        if s:
            parts.add(s)
    return parts


def filter_dataframe_to_watchlist(df, watchlist: set[str], symbol_columns: list[str]) -> "pd.DataFrame":
    """Keep rows where ANY of the listed symbol columns matches the watchlist.

    Each value in `symbol_columns` may itself be a string OR may be a clickable
    `/Symbol_Detail?...&symbol=BTCUSDT` URL — we extract the trailing symbol.
    """
    if df.empty or not watchlist:
        return df
    import re
    pat = re.compile(r"symbol=([A-Z0-9_]+)$", re.IGNORECASE)

    def matches(row) -> bool:
        for col in symbol_columns:
            val = row.get(col)
            if not isinstance(val, str) or not val:
                continue
            m = pat.search(val)
            sym = (m.group(1) if m else val).upper()
            if sym in watchlist:
                return True
        return False

    mask = df.apply(matches, axis=1)
    return df[mask]


def sidebar_status(store: DataStore) -> None:
    """Render fees + freshness + manual refresh in the sidebar. Call once per page."""
    st.sidebar.header("Status")
    last_fast, last_slow, _ = store.freshness()
    st.sidebar.write(f"**Funding:** {_age(last_fast)}")
    st.sidebar.write(f"**Klines:** {_age(last_slow)}")
    if not is_binance_enabled():
        st.sidebar.warning("Binance disabled (set `BINANCE_ENABLED=true` to re-enable)")
    if not is_mexc_enabled():
        st.sidebar.warning("MEXC disabled (set `MEXC_ENABLED=true` to re-enable)")
    # Surface auto-cooldown so users know the platform geo-blocked us.
    try:
        from .background import _runner_clients  # type: ignore[attr-defined]
        # Only some setups expose this — silently skip if not available.
        bnb_client, mxc_client = _runner_clients()
        if bnb_client and bnb_client.is_cooled_down():
            mins = bnb_client.cooldown_remaining_seconds() // 60
            st.sidebar.warning(f"Binance auto-disabled by host (cooldown {mins}m left)")
        if mxc_client and mxc_client.is_cooled_down():
            mins = mxc_client.cooldown_remaining_seconds() // 60
            st.sidebar.warning(f"MEXC auto-disabled by host (cooldown {mins}m left)")
    except Exception:
        pass

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


def render_table(
    df: pd.DataFrame,
    column_config: Optional[dict] = None,
    height: int | None = None,
    download_basename: Optional[str] = None,
) -> None:
    """Render a dataframe with optional CSV export button.

    `download_basename` (e.g. "high_funding") activates a "📥 Download CSV"
    button below the table. The exported CSV contains the *displayed* rows
    (after any filtering the page already applied), with columns in the same
    order Streamlit shows them.
    """
    if df.empty:
        st.info("No rows match the filter at the moment.")
        return
    kwargs: dict = {"hide_index": True, "use_container_width": True}
    if column_config:
        kwargs["column_config"] = column_config
    if height is not None:
        kwargs["height"] = height
    st.dataframe(df, **kwargs)
    if download_basename:
        from datetime import datetime as _dt, timezone as _tz
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        st.download_button(
            label="📥 Download CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"{download_basename}_{ts}.csv",
            mime="text/csv",
            help="Exports the rows currently shown above. Filename is timestamped (UTC).",
        )


def cooldown_banner(store: DataStore) -> None:
    """If any exchange is in cooldown (post-rate-limit / 451 geo-block), show a
    prominent in-page banner. Pages call this near their freshness banner.
    """
    try:
        from .background import _runner_clients  # type: ignore[attr-defined]
        bnb_client, mxc_client = _runner_clients()
    except Exception:
        return
    msgs: list[str] = []
    if bnb_client and bnb_client.is_cooled_down():
        mins = bnb_client.cooldown_remaining_seconds() // 60
        msgs.append(f"**Binance** rate-limit cooldown: {mins}m remaining")
    if mxc_client and mxc_client.is_cooled_down():
        mins = mxc_client.cooldown_remaining_seconds() // 60
        msgs.append(f"**MEXC** rate-limit cooldown: {mins}m remaining")
    if msgs:
        st.warning(
            "⏱ " + " · ".join(msgs)
            + " — that exchange's data is frozen at the last successful fetch. "
            "Background loops will resume automatically when the cooldown lifts."
        )


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
