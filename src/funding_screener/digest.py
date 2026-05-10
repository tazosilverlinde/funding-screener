"""Daily digest composer (Round 21).

Pure function over already-computed screener output. Produces a structured
summary of "what's worth knowing about the market right now" — designed to
be the body of a once-a-day email, but also rendered on its own preview
page so users can see what would be sent without configuring SMTP.

Sections:
  - market_overview: total symbols tracked, score distribution counts
  - top_longs:       N highest-composite-score rows (long candidates)
  - top_shorts:      N lowest-composite-score rows (short candidates)
  - top_squeezes:    biggest 24h short-liq cascades
  - top_cascades:    biggest 24h long-liq cascades
  - whale_highlight: biggest 24h whale flow (re-used from highlights.py)
  - upcoming_unlocks:unlock events within `days_ahead`
  - macro:           stablecoin supply change summary
  - sector_rotation: top bullish + top bearish sectors

Each section returns a list of small dicts, or None when no data fits the
criteria. The page renderer treats None as "skip the section".

Why a digest module separate from highlights.py: highlights produces ONE
pithy line per category for the always-on landing-page banner; digest
produces several rows per category and is meant for a richer, less-frequent
delivery. Different shape, different consumer.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .highlights import (
    biggest_unlock as _biggest_unlock,
    whale_spotlight as _whale_spotlight,
    macro_summary as _macro_summary,
)


def compose_market_overview(combined_rows: Iterable) -> dict:
    """Counts of bullish / bearish / neutral rows + total tracked.

    Uses the same +30 / -30 threshold the sector_rotation summary uses, so
    the numbers line up across pages.
    """
    rows = list(combined_rows)
    total = len(rows)
    bullish = sum(1 for r in rows if (r.composite_score or 0) >= 30)
    bearish = sum(1 for r in rows if (r.composite_score or 0) <= -30)
    strong_bull = sum(1 for r in rows if (r.composite_score or 0) >= 70)
    strong_bear = sum(1 for r in rows if (r.composite_score or 0) <= -70)
    neutral = total - bullish - bearish
    return {
        "total_symbols": total,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "strong_bull": strong_bull,
        "strong_bear": strong_bear,
    }


def top_long_candidates(combined_rows: Iterable, top_n: int = 5, min_score: int = 30) -> list[dict]:
    """Highest composite-score rows above `min_score`."""
    out: list[dict] = []
    sorted_rows = sorted(
        (r for r in combined_rows if (r.composite_score or -101) >= min_score),
        key=lambda r: r.composite_score or 0,
        reverse=True,
    )
    for r in sorted_rows[:top_n]:
        sym = r.binance_symbol or r.mexc_symbol or r.base_asset
        out.append({
            "symbol": sym,
            "base": r.base_asset,
            "quote": r.quote_asset,
            "score": r.composite_score,
            "label": f"{r.composite_emoji} {r.composite_short}",
            "funding_8h_pct": (
                r.binance_rate_8h_norm_percent
                if r.binance_rate_8h_norm_percent is not None
                else r.mexc_rate_8h_norm_percent
            ),
        })
    return out


def top_short_candidates(combined_rows: Iterable, top_n: int = 5, max_score: int = -30) -> list[dict]:
    out: list[dict] = []
    sorted_rows = sorted(
        (r for r in combined_rows if (r.composite_score or 101) <= max_score),
        key=lambda r: r.composite_score or 0,
    )
    for r in sorted_rows[:top_n]:
        sym = r.binance_symbol or r.mexc_symbol or r.base_asset
        out.append({
            "symbol": sym,
            "base": r.base_asset,
            "quote": r.quote_asset,
            "score": r.composite_score,
            "label": f"{r.composite_emoji} {r.composite_short}",
            "funding_8h_pct": (
                r.binance_rate_8h_norm_percent
                if r.binance_rate_8h_norm_percent is not None
                else r.mexc_rate_8h_norm_percent
            ),
        })
    return out


def top_liquidation_events(
    liq_stats_by_symbol: dict[str, dict],
    side: str,  # "short" → squeezes; "long" → cascades
    top_n: int = 5,
    min_total_usd: float = 1_000_000,
) -> list[dict]:
    """Symbols where one side dominantly liquidated, ranked by that side's notional."""
    if side not in ("short", "long"):
        raise ValueError("side must be 'short' or 'long'")
    side_key = f"{side}_liq_usd"
    other_key = f"{'long' if side == 'short' else 'short'}_liq_usd"
    out: list[dict] = []
    for symbol, stats in liq_stats_by_symbol.items():
        total = stats.get("total_usd", 0.0) or 0.0
        if total < min_total_usd:
            continue
        side_usd = stats.get(side_key, 0.0) or 0.0
        other_usd = stats.get(other_key, 0.0) or 0.0
        if side_usd <= other_usd:  # other side dominant — exclude
            continue
        out.append({
            "symbol": symbol,
            "side_usd": side_usd,
            "other_usd": other_usd,
            "events_count": stats.get("events_count", 0) or 0,
        })
    out.sort(key=lambda r: r["side_usd"], reverse=True)
    return out[:top_n]


def upcoming_unlocks(unlock_events: Iterable, days_ahead: int = 7, top_n: int = 5) -> list[dict]:
    """Token unlocks within `days_ahead`, sorted by USD value descending."""
    out: list[dict] = []
    for ev in unlock_events:
        days_until = getattr(ev, "days_until", None)
        if days_until is None or days_until > days_ahead or days_until < 0:
            continue
        amount_usd = getattr(ev, "amount_usd", None)
        out.append({
            "symbol": ev.symbol,
            "days_until": days_until,
            "date_str": getattr(ev, "date_str", ""),
            "amount_usd": amount_usd,
            "pct_of_supply": getattr(ev, "pct_of_supply", None),
        })
    out.sort(key=lambda r: r.get("amount_usd") or 0, reverse=True)
    return out[:top_n]


def sector_winners_and_losers(sector_rows: Iterable, top_n: int = 3) -> tuple[list[dict], list[dict]]:
    """Sectors with highest avg score (winners) and lowest (losers)."""
    rows = list(sector_rows)
    winners = sorted(rows, key=lambda r: r.get("avg_score") or 0, reverse=True)[:top_n]
    losers = sorted(rows, key=lambda r: r.get("avg_score") or 0)[:top_n]
    return winners, losers


def compose_daily_digest(
    *,
    combined_rows: Iterable,
    liq_stats_by_symbol: dict[str, dict] | None = None,
    onchain_flows: list[dict] | None = None,
    unlock_events: Iterable | None = None,
    stablecoin_supply: dict | None = None,
    sector_rows: Iterable | None = None,
    top_n: int = 5,
) -> dict:
    """Aggregate everything into one structured digest dict.

    Every section is independently computable, so an empty input (e.g. no
    onchain flows yet) just produces an empty/None section instead of
    crashing the rest of the digest.
    """
    rows_list = list(combined_rows)
    digest = {
        "market_overview": compose_market_overview(rows_list),
        "top_longs": top_long_candidates(rows_list, top_n=top_n),
        "top_shorts": top_short_candidates(rows_list, top_n=top_n),
        "top_squeezes": top_liquidation_events(liq_stats_by_symbol or {}, side="short", top_n=top_n),
        "top_cascades": top_liquidation_events(liq_stats_by_symbol or {}, side="long", top_n=top_n),
        "whale_highlight": _whale_spotlight(onchain_flows or []),
        "upcoming_unlocks": upcoming_unlocks(unlock_events or [], days_ahead=7, top_n=top_n),
        "macro": _macro_summary(stablecoin_supply or {}),
    }
    if sector_rows is not None:
        winners, losers = sector_winners_and_losers(sector_rows, top_n=3)
        digest["sector_winners"] = winners
        digest["sector_losers"] = losers
    return digest
