"""Signal-accuracy analytics over the persistent score history (Round 71).

Pure functions over the score-history samples — no I/O, no DataStore access.
The functions answer one question: "when the composite score crossed a
threshold, did it sustain or fade?" This is meta-quality of the signal — a
+70 cross that sustains 70% of the time across pairs is meaningfully more
predictive than one that sustains 40%.

Data shape from upstream:
  samples_by_pair: dict[(base, quote), list[(datetime_utc, score_int)]]
  Each value list is chronologically ordered, oldest → newest.

The window is bounded by the persistence's HISTORY_WINDOW_HOURS (24h by
default), so the lookback for crossings is at most 24h. Follow-up windows
shorter than 24h - max_cross_age work; we don't extrapolate beyond data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def find_threshold_crossings(
    samples: list[tuple[datetime, int]],
    threshold: int,
) -> list[int]:
    """Return INDICES of samples that crossed `threshold` from outside.

    For threshold > 0: a crossing is a sample where score >= threshold
    AND the previous sample had score < threshold (entering bullish region).
    For threshold < 0: score <= threshold AND previous > threshold
    (entering bearish region).
    For threshold == 0: defaults to upward direction (>= 0 from < 0).

    Returns empty list when fewer than 2 samples or no crossings found.
    The first sample alone is never a crossing (no predecessor).
    """
    if not samples or len(samples) < 2:
        return []
    out: list[int] = []
    if threshold >= 0:
        # Upward crossing.
        for i in range(1, len(samples)):
            if samples[i - 1][1] < threshold and samples[i][1] >= threshold:
                out.append(i)
    else:
        # Downward crossing.
        for i in range(1, len(samples)):
            if samples[i - 1][1] > threshold and samples[i][1] <= threshold:
                out.append(i)
    return out


def _find_sample_near(
    samples: list[tuple[datetime, int]],
    target_ts: datetime,
    tolerance: timedelta,
) -> Optional[tuple[datetime, int]]:
    """Return the sample closest to `target_ts` within `tolerance`, or None.

    Linear scan — fine for the bounded history sizes we work with (≤144
    samples per pair at the 10-min snapshot cadence).
    """
    best: Optional[tuple[datetime, int]] = None
    best_dist: Optional[timedelta] = None
    for ts, score in samples:
        dist = abs(ts - target_ts)
        if dist <= tolerance and (best_dist is None or dist < best_dist):
            best = (ts, score)
            best_dist = dist
    return best


def compute_signal_hit_rate(
    samples_by_pair: dict,
    threshold: int = 70,
    follow_up_hours: float = 1.0,
    sustain_threshold: Optional[int] = None,
    tolerance_minutes: float = 15.0,
) -> dict:
    """Aggregate "did the signal sustain?" stats across all tracked pairs.

    Walks each pair's chronological samples, finds every transition into the
    threshold region, looks at the score `follow_up_hours` later, and counts
    crossings where the score is still >= sustain_threshold (or for bearish
    thresholds, <= sustain_threshold).

    Inputs:
      samples_by_pair: dict[any, list[(datetime, int)]] — keys can be tuples
                       or strings; we just walk the values.
      threshold: cross into bullish at >=, or bearish at <= (sign determines)
      follow_up_hours: window after the cross to inspect
      sustain_threshold: defaults to `threshold` (same level still met).
                        Set to a lower abs value to ask "did it AT LEAST stay
                        elevated" (e.g. crossed +70, count as sustained at +50).
      tolerance_minutes: how close to the target follow-up time the sample
                        has to be. Samples are at 10-min cadence, so 15min
                        tolerance picks the nearest sample reliably.

    Returns dict with:
      n_crosses             — total crossings counted
      n_sustained           — sustained at follow_up_hours
      sustain_rate          — fraction (0.0..1.0); None when n_crosses == 0
      avg_score_at_cross    — mean score at the crossing sample
      avg_score_after       — mean score follow_up_hours later
      avg_score_delta       — avg_score_after - avg_score_at_cross
      n_followup_missing    — crossings where no follow-up sample was within tolerance

    None / empty input returns the zero structure (n_crosses=0, rate=None).
    """
    sustain = sustain_threshold if sustain_threshold is not None else threshold
    tol = timedelta(minutes=max(0.0, tolerance_minutes))
    fwd = timedelta(hours=max(0.0, follow_up_hours))
    upward = threshold >= 0

    n_crosses = 0
    n_sustained = 0
    n_missing = 0
    sum_at_cross = 0
    sum_after = 0
    n_with_followup = 0

    for samples in (samples_by_pair or {}).values():
        if not samples or len(samples) < 2:
            continue
        crossings = find_threshold_crossings(samples, threshold)
        for idx in crossings:
            cross_ts, cross_score = samples[idx]
            target_ts = cross_ts + fwd
            found = _find_sample_near(samples, target_ts, tol)
            n_crosses += 1
            sum_at_cross += cross_score
            if found is None:
                n_missing += 1
                continue
            _ts2, score2 = found
            sum_after += score2
            n_with_followup += 1
            sustained = (score2 >= sustain) if upward else (score2 <= sustain)
            if sustained:
                n_sustained += 1

    return {
        "n_crosses": n_crosses,
        "n_sustained": n_sustained,
        "sustain_rate": (n_sustained / n_crosses) if n_crosses else None,
        "avg_score_at_cross": (sum_at_cross / n_crosses) if n_crosses else None,
        "avg_score_after": (sum_after / n_with_followup) if n_with_followup else None,
        "avg_score_delta": (
            (sum_after / n_with_followup) - (sum_at_cross / n_crosses)
            if (n_with_followup and n_crosses) else None
        ),
        "n_followup_missing": n_missing,
    }
