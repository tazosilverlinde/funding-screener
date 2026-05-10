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
