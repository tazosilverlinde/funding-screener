"""Daily highlights — pick one headline per signal category for landing page.

Each function takes already-computed data structures (no I/O, no DataStore
access) and returns either a dict with `{emoji, label, headline, detail}` or
None if the source has no data yet. Pure functions so they're trivially
unit-testable.

The landing page renders the non-None highlights as a single curated digest
at the top of the page — like a newspaper front page for the screener.
"""

from __future__ import annotations

from typing import Iterable, Optional


def pick_best_opportunities(combined_rows: Iterable, top_n: int = 3) -> list[dict]:
    """Pick the top-N highest-conviction actionable setups (Round 35).

    "Actionable" = setup_quality starts with Fresh or Building. Mature is
    intentionally excluded — by the time a setup is mature the move is
    usually already in motion; the conviction reads "fresh" or "building"
    are where the asymmetric edge is. Late and Noisy are also excluded.

    Within the actionable set we rank by absolute composite score so a
    +85 Fresh bull beats a +35 Fresh bull. Returns up to `top_n` rows as
    dicts with the fields the landing page needs.

    Empty list when no actionable setups exist (quiet market or warmup).
    """
    actionable: list = []
    for r in (combined_rows or []):
        score = getattr(r, "composite_score", None)
        label = getattr(r, "setup_quality_label", None)
        if score is None or label is None:
            continue
        # Quality label format: "🚀 Fresh bull", "📈 Building bear", etc.
        if "Fresh" in label or "Building" in label:
            actionable.append(r)
    if not actionable:
        return []
    actionable.sort(key=lambda r: abs(r.composite_score), reverse=True)

    out: list[dict] = []
    for r in actionable[:top_n]:
        sym = r.binance_symbol or r.mexc_symbol or r.base_asset
        # Pick a directional funding-rate string for the card.
        bnb_pct = getattr(r, "binance_rate_8h_norm_percent", None)
        mxc_pct = getattr(r, "mexc_rate_8h_norm_percent", None)
        fpct = bnb_pct if bnb_pct is not None else mxc_pct
        funding_str = f"{fpct:+.4f}%/8h" if fpct is not None else "—"
        age_h = getattr(r, "signal_age_hours", None)
        age_str = (
            f"{age_h:.1f}h" if (age_h is not None and age_h >= 1.0) else
            (f"{int(age_h * 60)}m" if age_h is not None else "—")
        )
        out.append({
            "symbol": sym,
            "base": r.base_asset,
            "quote": r.quote_asset,
            "score": r.composite_score,
            "quality": r.setup_quality_label,
            "score_label": getattr(r, "composite_short", "") or "",
            "funding": funding_str,
            "age": age_str,
            "binance_symbol": r.binance_symbol,
        })
    return out


def best_long_candidate(combined_rows: Iterable) -> Optional[dict]:
    """Highest composite score across all combined rows. None if no scored rows."""
    rows = [r for r in (combined_rows or []) if getattr(r, "composite_score", None) is not None]
    if not rows:
        return None
    top = max(rows, key=lambda r: r.composite_score)
    if top.composite_score < 30:
        return None  # nothing actually bullish — don't fabricate a signal
    sym = top.binance_symbol or top.mexc_symbol or top.base_asset
    return {
        "emoji": "🚀",
        "label": "Best long",
        "headline": f"**{sym}** — composite `{top.composite_score:+d}` ({top.composite_short})",
        "detail": top.composite_breakdown or "",
    }


def best_short_candidate(combined_rows: Iterable) -> Optional[dict]:
    """Most-negative composite score. None if nothing bearish enough."""
    rows = [r for r in (combined_rows or []) if getattr(r, "composite_score", None) is not None]
    if not rows:
        return None
    bot = min(rows, key=lambda r: r.composite_score)
    if bot.composite_score > -30:
        return None
    sym = bot.binance_symbol or bot.mexc_symbol or bot.base_asset
    return {
        "emoji": "💥",
        "label": "Best short",
        "headline": f"**{sym}** — composite `{bot.composite_score:+d}` ({bot.composite_short})",
        "detail": bot.composite_breakdown or "",
    }


def hottest_funding(combined_rows: Iterable) -> Optional[dict]:
    """Pair with the largest |8h-normalized funding rate|."""
    rows = [r for r in (combined_rows or []) if r.binance_rate_8h_norm_percent is not None or r.mexc_rate_8h_norm_percent is not None]
    if not rows:
        return None
    top = max(rows, key=lambda r: r.max_abs_8h_norm_percent)
    if top.max_abs_8h_norm_percent < 1.0:
        return None
    rate = (top.binance_rate_8h_norm_percent
            if top.binance_rate_8h_norm_percent is not None and abs(top.binance_rate_8h_norm_percent) >= abs(top.mexc_rate_8h_norm_percent or 0)
            else top.mexc_rate_8h_norm_percent)
    sym = (top.binance_symbol if top.binance_rate_8h_norm_percent is not None and abs(top.binance_rate_8h_norm_percent) >= abs(top.mexc_rate_8h_norm_percent or 0)
           else top.mexc_symbol) or top.base_asset
    direction = "shorts paying longs" if (rate or 0) < 0 else "longs paying shorts"
    return {
        "emoji": "⚡",
        "label": "Hot funding",
        "headline": f"**{sym}** — `{rate:+.4f}% / 8h` ({direction})",
        "detail": "",
    }


def biggest_unlock(unlock_events: Iterable, days_ahead: int = 30) -> Optional[dict]:
    """Largest USD-value unlock event within `days_ahead`. None if nothing scheduled."""
    candidates = [
        e for e in (unlock_events or [])
        if 0 <= e.days_until <= days_ahead and e.amount_usd
    ]
    if not candidates:
        return None
    top = max(candidates, key=lambda e: e.amount_usd or 0)
    pct_text = f", {top.pct_of_supply:.2f}% of supply" if top.pct_of_supply is not None else ""
    return {
        "emoji": "🔓",
        "label": "Biggest unlock ahead",
        "headline": (
            f"**{top.symbol}** in `{top.days_until}d` ({top.date_str}) — "
            f"${top.amount_usd / 1e6:.1f}M{pct_text}"
        ),
        "detail": top.notes or "",
    }


def whale_spotlight(whale_flows: Iterable, min_unique_whales: int = 2) -> Optional[dict]:
    """Token with the largest absolute whale netflow (filtered by min unique count)."""
    rows = [
        f for f in (whale_flows or [])
        if f.get("whale_unique_count", 0) >= min_unique_whales
        and abs(f.get("whale_net_usd", 0.0) or 0.0) > 0
    ]
    if not rows:
        return None
    top = max(rows, key=lambda f: abs(f.get("whale_net_usd", 0.0) or 0.0))
    net = top.get("whale_net_usd", 0.0) or 0.0
    direction = "withdrew" if net > 0 else "deposited"
    arrow = "🟢" if net > 0 else "🔴"
    return {
        "emoji": "🐳",
        "label": "Whale spotlight",
        "headline": (
            f"{arrow} **{top['token']}** — {top.get('whale_unique_count', 0)} whales "
            f"{direction} `${abs(net):,.0f}` (24h, ETH chain)"
        ),
        "detail": "",
    }


def macro_summary(stablecoin_supply: dict) -> Optional[dict]:
    """One-liner read on stablecoin supply trend (drives whether risk is on or off)."""
    total = (stablecoin_supply or {}).get("TOTAL")
    if not total or total.get("change_24h_pct") is None:
        return None
    change_24h = total.get("change_24h_pct") or 0.0
    if change_24h >= 0.3:
        emoji = "🟢"
        verdict = "expanding — liquidity entering crypto, bullish backdrop"
    elif change_24h <= -0.3:
        emoji = "🔴"
        verdict = "contracting — liquidity leaving crypto, bearish backdrop"
    else:
        emoji = "🟡"
        verdict = "stable — no macro tailwind either way"
    return {
        "emoji": emoji,
        "label": "Macro",
        "headline": (
            f"Total stables `{total.get('now', 0)/1e9:.1f}B` "
            f"({change_24h:+.2f}% 24h) — {verdict}"
        ),
        "detail": "",
    }


def all_highlights(
    combined_rows: Iterable,
    unlock_events: Iterable,
    whale_flows: Iterable,
    stablecoin_supply: dict,
) -> list[dict]:
    """Run every aggregator and return the non-None set, in display order."""
    candidates = [
        best_long_candidate(combined_rows),
        best_short_candidate(combined_rows),
        hottest_funding(combined_rows),
        whale_spotlight(whale_flows),
        biggest_unlock(unlock_events),
        macro_summary(stablecoin_supply),
    ]
    return [c for c in candidates if c]
