"""In-memory composite-score history per (base_asset, quote_asset).

Why in-memory and not a DB:
- Render's free tier has ephemeral disk (resets on each deploy).
- A SQLite file would grow forever without TTL logic.
- The whole point is short-window momentum (1h–24h), so we don't need persistence.

Trade-off: process restarts wipe history. We accept it. The signal recovers
within ~10 minutes (the snapshot cadence).

Memory footprint: 24h × 6 snapshots/h × ~500 pairs × ~50 bytes ≈ 3.6 MB.
Trimmed automatically on every snapshot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


HISTORY_WINDOW_HOURS = 24


def trim_old(samples: list[tuple[datetime, int]], now: datetime) -> list[tuple[datetime, int]]:
    """Drop entries older than HISTORY_WINDOW_HOURS. Cheap O(n)."""
    cutoff = now - timedelta(hours=HISTORY_WINDOW_HOURS)
    return [(t, s) for t, s in samples if t >= cutoff]


def score_delta(samples: list[tuple[datetime, int]], minutes_ago: int) -> Optional[int]:
    """Return current_score − score_closest_to_target.

    Args:
      samples: chronologically sorted list of (timestamp_utc, score) tuples
      minutes_ago: how far back to look (e.g. 60 for 1h)

    Returns None when:
      - History has fewer than 2 samples
      - The closest sample to `target = now − minutes_ago` is more than
        50% off-target (so a 1h delta won't be computed from a 5-min-old sample)
    """
    if not samples or len(samples) < 2:
        return None
    now = datetime.now(timezone.utc)
    target = now - timedelta(minutes=minutes_ago)
    # Find the sample closest to the target time.
    best = min(samples, key=lambda x: abs((x[0] - target).total_seconds()))
    drift = abs((best[0] - target).total_seconds())
    if drift > minutes_ago * 60 * 0.5:
        return None
    current = samples[-1][1]
    return int(current - best[1])


def top_movers(
    histories: dict[tuple[str, str], list[tuple[datetime, int]]],
    minutes_ago: int = 60,
    limit: int = 5,
) -> tuple[list[dict], list[dict]]:
    """Return (top_risers, top_fallers) — each up to `limit` rows.

    Each row dict has: base_asset, quote_asset, current_score, delta.
    Risers sorted by delta desc; fallers by delta asc.
    """
    risers: list[dict] = []
    fallers: list[dict] = []
    for (base, quote), samples in histories.items():
        if not samples:
            continue
        d = score_delta(samples, minutes_ago)
        if d is None or d == 0:
            continue
        entry = {
            "base_asset": base,
            "quote_asset": quote,
            "current_score": samples[-1][1],
            "delta": d,
        }
        if d > 0:
            risers.append(entry)
        else:
            fallers.append(entry)
    risers.sort(key=lambda x: x["delta"], reverse=True)
    fallers.sort(key=lambda x: x["delta"])
    return risers[:limit], fallers[:limit]
