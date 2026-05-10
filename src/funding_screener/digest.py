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


def compose_system_status_line(
    rss_mb: Optional[float],
    budget_mb: float,
    n_loops_total: int,
    n_loops_stalled: int,
    n_recent_errors: int,
    n_alerts_24h: int,
) -> str:
    """One-line health summary for the daily digest footer (Round 58).

    Inputs are the same data the System Health page consumes — caller
    provides them so this stays a pure function. Output reads naturally
    in the digest:

      🩺 System: 14 loops healthy · RSS 412MB/2048MB · 0 recent errors · 47 alerts last 24h

    or with issues:

      🩺 System: 12 loops healthy + 2 STALLED · RSS 1700MB/2048MB · 8 recent errors · 47 alerts last 24h
    """
    pct = (rss_mb / budget_mb * 100.0) if (rss_mb is not None and budget_mb > 0) else None
    rss_part = (
        f"RSS {rss_mb:,.0f}MB/{budget_mb:,.0f}MB ({pct:.0f}%)"
        if rss_mb is not None and pct is not None
        else "RSS unknown"
    )
    healthy = n_loops_total - n_loops_stalled
    if n_loops_stalled > 0:
        loops_part = f"{healthy} loops healthy + {n_loops_stalled} STALLED"
    else:
        loops_part = f"{n_loops_total} loops healthy"
    err_part = f"{n_recent_errors} recent errors"
    alerts_part = f"{n_alerts_24h} alerts last 24h"
    return f"🩺 System: {loops_part} · {rss_part} · {err_part} · {alerts_part}"


def compose_daily_digest(
    *,
    combined_rows: Iterable,
    liq_stats_by_symbol: dict[str, dict] | None = None,
    onchain_flows: list[dict] | None = None,
    unlock_events: Iterable | None = None,
    stablecoin_supply: dict | None = None,
    sector_rows: Iterable | None = None,
    watchlist: set[str] | None = None,
    system_status_line: str | None = None,
    top_n: int = 5,
) -> dict:
    """Aggregate everything into one structured digest dict.

    Every section is independently computable, so an empty input (e.g. no
    onchain flows yet) just produces an empty/None section instead of
    crashing the rest of the digest.

    Watchlist (Round 49): when non-empty, top long/short picks and the
    liquidation cascade/squeeze sections only include rows whose base asset
    is in the set. Market overview, sector aggregates, whale highlight,
    unlocks, and macro all see the FULL universe (those are aggregate /
    schedule signals where filtering would distort the read).
    """
    rows_list = list(combined_rows)
    watchlist = watchlist or set()
    # Filter rows once for the per-symbol picks. Sections that need the
    # whole universe (market_overview, sector_*) keep the unfiltered rows.
    if watchlist:
        watchlisted_rows = [
            r for r in rows_list
            if (getattr(r, "base_asset", "") or "").upper() in watchlist
        ]
    else:
        watchlisted_rows = rows_list
    # Same for liquidation stats — strip USDT/USDC/BUSD suffix and match base.
    if watchlist and liq_stats_by_symbol:
        filtered_liq: dict[str, dict] = {}
        for sym, stats in liq_stats_by_symbol.items():
            sym_u = (sym or "").upper()
            for q in ("USDT", "USDC", "BUSD"):
                if sym_u.endswith(q):
                    if sym_u[: -len(q)] in watchlist:
                        filtered_liq[sym] = stats
                    break
        liq_for_picks = filtered_liq
    else:
        liq_for_picks = liq_stats_by_symbol or {}

    digest = {
        # market_overview always sees the full universe — it's a market-wide read.
        "market_overview": compose_market_overview(rows_list),
        # Top picks honor the watchlist when set.
        "top_longs": top_long_candidates(watchlisted_rows, top_n=top_n),
        "top_shorts": top_short_candidates(watchlisted_rows, top_n=top_n),
        "top_squeezes": top_liquidation_events(liq_for_picks, side="short", top_n=top_n),
        "top_cascades": top_liquidation_events(liq_for_picks, side="long", top_n=top_n),
        "whale_highlight": _whale_spotlight(onchain_flows or []),
        "upcoming_unlocks": upcoming_unlocks(unlock_events or [], days_ahead=7, top_n=top_n),
        "macro": _macro_summary(stablecoin_supply or {}),
    }
    if sector_rows is not None:
        winners, losers = sector_winners_and_losers(sector_rows, top_n=3)
        digest["sector_winners"] = winners
        digest["sector_losers"] = losers
    # Optional system-status footer (Round 58). Caller passes the prebuilt line
    # to keep compose_daily_digest a pure function with no DataStore access.
    if system_status_line:
        digest["system_status"] = system_status_line
    return digest


# ----------------------------------------------------------------------
# Output formatters — both reuse the same digest dict so format never
# drifts between Telegram and email. Markdown for Telegram (which renders
# a subset), HTML for email (renders everywhere).
# ----------------------------------------------------------------------


def _fmt_dollars(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def format_digest_as_text(digest: dict, top_n: int = 5) -> str:
    """Plain-text digest — used as Telegram message body and email text part.

    Keep lines short and avoid markdown that doesn't render the same in both
    targets (Telegram uses MarkdownV2 escaping, email plain-text doesn't).
    """
    lines: list[str] = []
    ov = digest.get("market_overview") or {}
    lines.append(f"Daily market digest — {ov.get('total_symbols', 0)} symbols tracked")
    lines.append(
        f"  Bullish: {ov.get('bullish', 0)}  "
        f"({ov.get('strong_bull', 0)} strong)  |  "
        f"Bearish: {ov.get('bearish', 0)} "
        f"({ov.get('strong_bear', 0)} strong)  |  "
        f"Neutral: {ov.get('neutral', 0)}"
    )

    if digest.get("macro"):
        m = digest["macro"]
        lines.append("")
        lines.append(f"Macro: {m.get('emoji', '')} {m.get('headline', '')}")

    if digest.get("top_longs"):
        lines.append("")
        lines.append("Top long candidates:")
        for r in digest["top_longs"][:top_n]:
            f = r.get("funding_8h_pct")
            f_str = f"{f:+.4f}%/8h" if f is not None else "—"
            lines.append(f"  {r['symbol']:<14} score {r['score']:+d}  funding {f_str}")

    if digest.get("top_shorts"):
        lines.append("")
        lines.append("Top short candidates:")
        for r in digest["top_shorts"][:top_n]:
            f = r.get("funding_8h_pct")
            f_str = f"{f:+.4f}%/8h" if f is not None else "—"
            lines.append(f"  {r['symbol']:<14} score {r['score']:+d}  funding {f_str}")

    if digest.get("top_squeezes"):
        lines.append("")
        lines.append("Top short squeezes (24h liq):")
        for r in digest["top_squeezes"][:top_n]:
            lines.append(f"  {r['symbol']:<14} {_fmt_dollars(r['side_usd'])}")

    if digest.get("top_cascades"):
        lines.append("")
        lines.append("Top long cascades (24h liq):")
        for r in digest["top_cascades"][:top_n]:
            lines.append(f"  {r['symbol']:<14} {_fmt_dollars(r['side_usd'])}")

    if digest.get("whale_highlight"):
        w = digest["whale_highlight"]
        lines.append("")
        lines.append(f"Whale spotlight: {w.get('emoji', '')} {w.get('headline', '')}")

    if digest.get("upcoming_unlocks"):
        lines.append("")
        lines.append("Upcoming unlocks (next 7 days):")
        for u in digest["upcoming_unlocks"][:top_n]:
            amt = _fmt_dollars(u.get("amount_usd"))
            lines.append(
                f"  {u['symbol']:<10} {u.get('date_str', ''):<11} {amt} "
                f"({u.get('pct_of_supply', 0):.2f}% supply)"
            )

    if digest.get("sector_winners") or digest.get("sector_losers"):
        lines.append("")
        if digest.get("sector_winners"):
            winners = ", ".join(
                f"{s['sector']} ({s.get('avg_score', 0):+.0f})"
                for s in digest["sector_winners"]
            )
            lines.append(f"Sector winners: {winners}")
        if digest.get("sector_losers"):
            losers = ", ".join(
                f"{s['sector']} ({s.get('avg_score', 0):+.0f})"
                for s in digest["sector_losers"]
            )
            lines.append(f"Sector laggards: {losers}")

    # System status footer (Round 58) — appended last so the operator sees
    # market signals first, infra health as a sign-off.
    if digest.get("system_status"):
        lines.append("")
        lines.append(digest["system_status"])

    return "\n".join(lines)


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    """Inline-styled HTML table — survives email-client CSS stripping."""
    style_th = "padding:6px 10px;border-bottom:2px solid #ccc;text-align:left;background:#f7f7f7;"
    style_td = "padding:6px 10px;border-bottom:1px solid #eee;"
    head = "".join(f'<th style="{style_th}">{h}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td style="{style_td}">{c}</td>' for c in row) + "</tr>"
        for row in rows
    )
    return (
        '<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;'
        'min-width:300px;margin-bottom:12px;">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
    )


def format_digest_as_html(digest: dict, top_n: int = 5) -> str:
    """Email-friendly HTML — inline styles only (Gmail/Outlook strip <style>)."""
    parts: list[str] = []
    parts.append(
        '<div style="font-family:sans-serif;font-size:14px;line-height:1.5;'
        'max-width:780px;color:#222;">'
    )
    parts.append('<h2 style="margin-bottom:6px;">Daily market digest</h2>')

    ov = digest.get("market_overview") or {}
    parts.append(
        f'<p style="margin-top:0;color:#555;">'
        f"{ov.get('total_symbols', 0)} symbols tracked &mdash; "
        f"<b style='color:#2ca02c;'>{ov.get('bullish', 0)}</b> bullish "
        f"({ov.get('strong_bull', 0)} strong), "
        f"<b style='color:#d62728;'>{ov.get('bearish', 0)}</b> bearish "
        f"({ov.get('strong_bear', 0)} strong), "
        f"<b>{ov.get('neutral', 0)}</b> neutral."
        f"</p>"
    )

    if digest.get("macro"):
        m = digest["macro"]
        parts.append(
            f'<p><b>Macro:</b> {m.get("emoji", "")} {m.get("headline", "")}</p>'
        )

    def _funding_str(v: float | None) -> str:
        return f"{v:+.4f}%" if v is not None else "—"

    if digest.get("top_longs"):
        parts.append("<h3>🟢 Top long candidates</h3>")
        rows = [
            [r["symbol"], f"{r['score']:+d}", r.get("label", ""), _funding_str(r.get("funding_8h_pct"))]
            for r in digest["top_longs"][:top_n]
        ]
        parts.append(_html_table(["Symbol", "Score", "Bias", "Funding/8h"], rows))

    if digest.get("top_shorts"):
        parts.append("<h3>🔴 Top short candidates</h3>")
        rows = [
            [r["symbol"], f"{r['score']:+d}", r.get("label", ""), _funding_str(r.get("funding_8h_pct"))]
            for r in digest["top_shorts"][:top_n]
        ]
        parts.append(_html_table(["Symbol", "Score", "Bias", "Funding/8h"], rows))

    if digest.get("top_squeezes"):
        parts.append("<h3>🟢 Top short squeezes (24h)</h3>")
        rows = [
            [r["symbol"], _fmt_dollars(r["side_usd"]), _fmt_dollars(r["other_usd"]), str(r.get("events_count", 0))]
            for r in digest["top_squeezes"][:top_n]
        ]
        parts.append(_html_table(["Symbol", "Short liq", "Long liq", "Events"], rows))

    if digest.get("top_cascades"):
        parts.append("<h3>🔴 Top long cascades (24h)</h3>")
        rows = [
            [r["symbol"], _fmt_dollars(r["side_usd"]), _fmt_dollars(r["other_usd"]), str(r.get("events_count", 0))]
            for r in digest["top_cascades"][:top_n]
        ]
        parts.append(_html_table(["Symbol", "Long liq", "Short liq", "Events"], rows))

    if digest.get("whale_highlight"):
        w = digest["whale_highlight"]
        parts.append(
            f'<p><b>🐋 Whale spotlight:</b> {w.get("emoji", "")} {w.get("headline", "")}</p>'
        )

    if digest.get("upcoming_unlocks"):
        parts.append("<h3>🔓 Upcoming unlocks (next 7 days)</h3>")
        rows = [
            [
                u["symbol"], u.get("date_str", ""), str(u.get("days_until", "")),
                _fmt_dollars(u.get("amount_usd")), f"{u.get('pct_of_supply', 0):.2f}%",
            ]
            for u in digest["upcoming_unlocks"][:top_n]
        ]
        parts.append(_html_table(["Symbol", "Date", "Days", "Amount", "% supply"], rows))

    if digest.get("sector_winners") or digest.get("sector_losers"):
        parts.append("<h3>🏆 Sector rotation</h3>")
        if digest.get("sector_winners"):
            winners = ", ".join(
                f"{s['sector']} ({s.get('avg_score', 0):+.0f})"
                for s in digest["sector_winners"]
            )
            parts.append(f"<p><b>Winners:</b> {winners}</p>")
        if digest.get("sector_losers"):
            losers = ", ".join(
                f"{s['sector']} ({s.get('avg_score', 0):+.0f})"
                for s in digest["sector_losers"]
            )
            parts.append(f"<p><b>Laggards:</b> {losers}</p>")

    # System status footer (Round 58) — small italic line above the boilerplate
    # so operators see infra health as part of every digest.
    if digest.get("system_status"):
        parts.append(
            f'<p style="color:#555;font-size:13px;font-style:italic;">'
            f'{digest["system_status"]}</p>'
        )

    parts.append(
        '<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">'
        '<p style="color:#888;font-size:12px;">'
        "Generated by funding_screener. To opt out, unset the EMAIL_TO env "
        "var on the deployment."
        "</p>"
        "</div>"
    )
    return "".join(parts)

