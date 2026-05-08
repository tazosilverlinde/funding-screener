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
