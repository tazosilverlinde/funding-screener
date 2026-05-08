"""Symbol sector / category lookup loaded from `config/symbol_sectors.yaml`.

Provides a fast `sector_for(base_asset)` mapping plus the inverse — list of
tokens per sector — for the sidebar filter on Pages 2/3/4.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "symbol_sectors.yaml"


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, list[str]]:
    """Load YAML once — sectors don't change at runtime."""
    path = _config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    sectors = data.get("sectors") or {}
    out: dict[str, list[str]] = {}
    for sector_name, tickers in sectors.items():
        if not isinstance(tickers, list):
            continue
        out[sector_name] = [t.strip().upper() for t in tickers if isinstance(t, str)]
    return out


@lru_cache(maxsize=1)
def _build_inverse() -> dict[str, str]:
    """Reverse map: {SYMBOL_UPPER: sector_name}. First sector wins on duplicates."""
    raw = _load_raw()
    out: dict[str, str] = {}
    for sector_name, tickers in raw.items():
        for t in tickers:
            if t not in out:
                out[t] = sector_name
    return out


def sector_for(base_asset: Optional[str]) -> Optional[str]:
    """Return the sector for a given base asset (uppercase ticker), or None."""
    if not base_asset:
        return None
    return _build_inverse().get(base_asset.upper())


def all_sectors() -> list[str]:
    """Sorted list of every sector present in the YAML."""
    return sorted(_load_raw().keys())


def symbols_in_sector(sector: str) -> set[str]:
    """All base assets assigned to a sector (uppercase)."""
    return set(_load_raw().get(sector, []))


def sector_aggregates(combined_rows) -> list[dict]:
    """Aggregate composite scores by sector.

    Input: iterable of objects with `.base_asset` (or `.sector`) and `.composite_score`.
    Returns: one dict per sector that has ≥1 row, with:
      - sector
      - row_count        (how many tracked tokens contributed)
      - avg_score        (mean composite score)
      - median_score
      - bullish_count    (rows with score >= +30)
      - bearish_count    (rows with score <= -30)
      - sample_symbols   (up to 3 base assets — for display)

    Sorted: most-bullish (highest avg) sectors first. Sectors with 0 tracked
    rows are skipped — no need to show empty sectors.
    """
    by_sector: dict[str, list] = {}
    for r in combined_rows or []:
        score = getattr(r, "composite_score", None)
        if score is None:
            continue
        # Prefer explicit sector if the row carries one; else look up.
        sector = getattr(r, "sector", None) or sector_for(getattr(r, "base_asset", "") or "")
        if not sector:
            continue
        by_sector.setdefault(sector, []).append((score, getattr(r, "base_asset", "") or ""))

    out: list[dict] = []
    for sector, items in by_sector.items():
        scores = [s for s, _ in items]
        if not scores:
            continue
        scores_sorted = sorted(scores)
        median = scores_sorted[len(scores_sorted) // 2]
        avg = sum(scores) / len(scores)
        bullish = sum(1 for s in scores if s >= 30)
        bearish = sum(1 for s in scores if s <= -30)
        # Pick a few representative tickers (highest absolute score).
        items_sorted = sorted(items, key=lambda x: abs(x[0]), reverse=True)
        sample = ", ".join(b for _, b in items_sorted[:3] if b)
        out.append({
            "sector": sector,
            "row_count": len(scores),
            "avg_score": round(avg, 1),
            "median_score": median,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "sample_symbols": sample,
        })
    out.sort(key=lambda d: d["avg_score"], reverse=True)
    return out
