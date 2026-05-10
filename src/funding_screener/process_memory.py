"""Process-memory introspection (Round 51).

Surfaces the current process's RSS so the perf expander on the landing page
can show how much RAM the daemon thread + Streamlit + caches are using
against the 2GB per-process budget.

psutil is the standard cross-platform way to read RSS in Python; on Windows
it uses the Win32 PSAPI under the hood, on Linux it reads /proc/self/status.
None-returning fallback when psutil isn't installed or the read fails — the
UI then just hides the metric instead of crashing.
"""

from __future__ import annotations

from typing import Optional

# Hard cap from user feedback: never let a single process exceed 2GB RAM.
# Surfacing the budget AS A CONSTANT lets pages render context-aware
# coloring without copy-pasting the magic number.
PROCESS_MEMORY_BUDGET_MB: float = 2048.0


def current_process_memory_mb() -> Optional[float]:
    """Return the current process's resident set size in megabytes.

    Returns None when psutil isn't available, the process can't be inspected,
    or any unexpected error occurs — callers should treat None as "no data"
    and skip rendering rather than failing.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process()
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def estimate_buffer_memory(store) -> list[dict]:
    """Per-buffer entry count + rough MB estimate (Round 53).

    When the overall RSS shows pressure, operators need to know WHICH buffer
    is growing. This walks the DataStore's in-memory containers, counts
    entries, and multiplies by per-entry byte estimates that come from
    eyeballing typical Python object sizes:

      - score_history     : ~120 bytes/sample (datetime + int + tuple overhead)
      - liquidations      : ~200 bytes/event (frozen dataclass + 6 fields)
      - klines            : ~180 bytes/bar   (Kline frozen dataclass + 7 fields)
      - onchain flows     : ~700 bytes/row   (dict with nested by_exchange)
      - macro daily flows : ~250 bytes/row   (dict with 4 numeric fields + date string)
      - alert log         : ~300 bytes/record (frozen dataclass + message string)
      - enrichments       : ~400 bytes/entry (rates list + 8 fields)
      - market caps       : ~50 bytes/entry  (string key + float)

    The MB numbers are HEURISTIC — psutil RSS is the source of truth. Use
    these to spot WHICH buffer is the outlier (e.g. liquidations at 80% of
    breakdown total → cap or trim it).

    Returns rows ordered by descending estimated MB so the biggest buffer
    appears first.
    """
    rows: list[dict] = []

    # Score history: dict[(base, quote), list[(ts, score)]]
    history_count = 0
    for samples in (getattr(store, "score_history", {}) or {}).values():
        history_count += len(samples)
    rows.append({
        "buffer": "score_history",
        "entries": history_count,
        "est_mb": round(history_count * 120 / (1024 * 1024), 2),
    })

    # Liquidations: per-symbol deque inside LiquidationsBuffer
    liq_count = 0
    liq_buf = getattr(store, "liquidations", None)
    if liq_buf is not None:
        for dq in getattr(liq_buf, "_buf", {}).values():
            liq_count += len(dq)
    rows.append({
        "buffer": "liquidations",
        "entries": liq_count,
        "est_mb": round(liq_count * 200 / (1024 * 1024), 2),
    })

    # Klines: per-snapshot dict[symbol, list[Kline]] for both exchanges
    kline_count = 0
    for snap in (getattr(store, "binance", None), getattr(store, "mexc", None)):
        if snap is None:
            continue
        for bars in (getattr(snap, "klines", {}) or {}).values():
            kline_count += len(bars)
    rows.append({
        "buffer": "klines",
        "entries": kline_count,
        "est_mb": round(kline_count * 180 / (1024 * 1024), 2),
    })

    # Onchain flows: per-chain list[flow_dict]
    onchain_count = 0
    for flows in (getattr(store, "onchain_flows_by_chain", {}) or {}).values():
        onchain_count += len(flows)
    rows.append({
        "buffer": "onchain_flows",
        "entries": onchain_count,
        "est_mb": round(onchain_count * 700 / (1024 * 1024), 2),
    })

    # Macro daily flows: per-chain dict[token, list[day_dict]]
    macro_count = 0
    for chain_data in (getattr(store, "macro_daily_flows_by_chain", {}) or {}).values():
        for series in chain_data.values():
            macro_count += len(series)
    rows.append({
        "buffer": "macro_daily_flows",
        "entries": macro_count,
        "est_mb": round(macro_count * 250 / (1024 * 1024), 2),
    })

    # Alert log: bounded ring buffer of AlertFireRecord
    alert_log = getattr(store, "alert_log", None)
    alert_count = len(alert_log) if alert_log is not None else 0
    rows.append({
        "buffer": "alert_log",
        "entries": alert_count,
        "est_mb": round(alert_count * 300 / (1024 * 1024), 2),
    })

    # Enrichments: dict[(exchange, symbol), EnrichmentData]
    enrichment_count = len(getattr(store, "enrichments", {}) or {})
    rows.append({
        "buffer": "enrichments",
        "entries": enrichment_count,
        "est_mb": round(enrichment_count * 400 / (1024 * 1024), 2),
    })

    # Market caps: dict[base, mcap_usd]
    mcap_count = len(getattr(store, "market_caps_usd", {}) or {})
    rows.append({
        "buffer": "market_caps",
        "entries": mcap_count,
        "est_mb": round(mcap_count * 50 / (1024 * 1024), 2),
    })

    rows.sort(key=lambda r: r["est_mb"], reverse=True)
    return rows


def memory_pressure_label(rss_mb: Optional[float], budget_mb: float = PROCESS_MEMORY_BUDGET_MB) -> str:
    """Map current RSS to a coarse pressure bucket: 'ok' / 'warn' / 'crit' / 'unknown'.

    Buckets:
      < 50% of budget   → 'ok'
      50-75% of budget  → 'warn' (still safe but worth watching)
      ≥ 75% of budget   → 'crit' (approaching the cap; investigate before adding more)
      None              → 'unknown'

    Picked at 50%/75% rather than 70%/90% because the user's cap is a
    HARD limit (process kill / system instability above 2GB), so we want
    to surface pressure earlier than typical OOM-killer thresholds.
    """
    if rss_mb is None or budget_mb <= 0:
        return "unknown"
    pct = (rss_mb / budget_mb) * 100.0
    if pct >= 75.0:
        return "crit"
    if pct >= 50.0:
        return "warn"
    return "ok"
