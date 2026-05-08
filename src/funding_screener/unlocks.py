"""Token unlock calendar — loads from `config/token_unlocks.yaml`.

Free unlock APIs are paywalled, so this module reads a manually-maintained
YAML and exposes a filtered, sorted view. The page that displays it filters
again by the Binance/MEXC tradable universe so we never show unlocks for
tokens the user can't act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "token_unlocks.yaml"


VALID_TYPES = {"cliff", "linear", "milestone", "staking", "airdrop"}


@dataclass(frozen=True)
class UnlockEvent:
    symbol: str
    name: str
    date_str: str          # original "YYYY-MM-DD" string
    date: date             # parsed
    days_until: int        # computed at load time; can be negative for past
    amount_tokens: float
    amount_usd: Optional[float]
    pct_of_supply: Optional[float]
    pct_of_total: Optional[float]
    unlock_type: str
    notes: str

    def impact_emoji(self) -> str:
        """Color/emoji based on pct_of_supply (heuristic)."""
        p = self.pct_of_supply
        if p is None:
            return "⚪"
        if p >= 3.0:
            return "🔴"
        if p >= 1.0:
            return "🟡"
        return "🟢"

    def impact_label(self) -> str:
        p = self.pct_of_supply
        if p is None:
            return "Unknown"
        if p >= 3.0:
            return "High"
        if p >= 1.0:
            return "Medium"
        return "Low"


def _parse_one(entry: dict, today: date) -> Optional[UnlockEvent]:
    sym = (entry.get("symbol") or "").strip().upper()
    date_str = (entry.get("date") or "").strip()
    if not sym or not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    raw_type = (entry.get("type") or "cliff").strip().lower()
    if raw_type not in VALID_TYPES:
        raw_type = "cliff"
    try:
        amount_tokens = float(entry.get("amount_tokens", 0) or 0)
    except (TypeError, ValueError):
        amount_tokens = 0.0
    return UnlockEvent(
        symbol=sym,
        name=str(entry.get("name") or sym),
        date_str=date_str,
        date=d,
        days_until=(d - today).days,
        amount_tokens=amount_tokens,
        amount_usd=_safe_float(entry.get("amount_usd")),
        pct_of_supply=_safe_float(entry.get("pct_of_supply")),
        pct_of_total=_safe_float(entry.get("pct_of_total")),
        unlock_type=raw_type,
        notes=str(entry.get("notes") or ""),
    )


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_upcoming_unlocks(
    tradable_symbols: Optional[set[str]] = None,
    today: Optional[date] = None,
) -> list[UnlockEvent]:
    """Load and filter the YAML.

    Args:
        tradable_symbols: if provided, drop any unlock whose symbol isn't in
            this set. Use this with the union of Binance + MEXC base assets so
            the page only shows actionable rows.
        today: override "today" for testing; defaults to UTC today.

    Returns:
        Future-only events (date >= today) sorted by date ascending.
    """
    path = _config_path()
    if not path.exists():
        return []
    today = today or datetime.now(timezone.utc).date()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw_list = data.get("unlocks") or []
    out: list[UnlockEvent] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        ev = _parse_one(entry, today)
        if ev is None:
            continue
        if ev.days_until < 0:
            continue  # past — silently drop
        if tradable_symbols is not None and ev.symbol not in tradable_symbols:
            continue
        out.append(ev)
    out.sort(key=lambda e: (e.date, e.symbol))
    return out


def attach_usd_values(events: Iterable[UnlockEvent], price_map: dict[str, float]) -> list[UnlockEvent]:
    """Fill in `amount_usd` from `price_map` for events that left it null."""
    out: list[UnlockEvent] = []
    for ev in events:
        if ev.amount_usd is None and ev.symbol in price_map and price_map[ev.symbol] > 0:
            usd = ev.amount_tokens * price_map[ev.symbol]
            out.append(UnlockEvent(
                symbol=ev.symbol,
                name=ev.name,
                date_str=ev.date_str,
                date=ev.date,
                days_until=ev.days_until,
                amount_tokens=ev.amount_tokens,
                amount_usd=usd,
                pct_of_supply=ev.pct_of_supply,
                pct_of_total=ev.pct_of_total,
                unlock_type=ev.unlock_type,
                notes=ev.notes,
            ))
        else:
            out.append(ev)
    return out
