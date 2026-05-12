"""Auto-generated trade thesis composition (Round 37).

Takes the same inputs the composite score uses and emits a structured
English breakdown a trader can read in 10 seconds. Output structure:

  {
    "direction": "long" | "short" | "no_clear_bias",
    "conviction": "high" | "medium" | "low" | "none",
    "headline": one-line summary,
    "bullish_reasons": [str, ...],
    "bearish_reasons": [str, ...],
    "risks": [str, ...],
  }

This is purely a synthesis layer — every reason it emits is already visible
elsewhere on the page. The value is consolidating them into one paragraph
so the user doesn't have to mentally combine 12 columns.

No I/O, no external state — pure function over the inputs.
"""

from __future__ import annotations

from typing import Any, Optional


def _direction_from_score(score: Optional[int]) -> tuple[str, str]:
    """Return (direction, conviction) from a composite score."""
    if score is None:
        return ("no_clear_bias", "none")
    if score >= 70:
        return ("long", "high")
    if score >= 30:
        return ("long", "medium")
    if score >= 10:
        return ("long", "low")
    if score <= -70:
        return ("short", "high")
    if score <= -30:
        return ("short", "medium")
    if score <= -10:
        return ("short", "low")
    return ("no_clear_bias", "none")


def compose_trade_thesis(
    *,
    symbol: str,
    composite_score: Optional[int] = None,
    funding_8h_norm_pct: Optional[float] = None,
    funding_streak_count: int = 0,
    funding_streak_direction: Optional[str] = None,
    funding_deviation_z: Optional[float] = None,
    mark_index_spread_pct: Optional[float] = None,
    oi_change_24h_pct: Optional[float] = None,
    ls_ratio_global: Optional[float] = None,
    ls_ratio_top: Optional[float] = None,
    onchain_net_usd: Optional[float] = None,
    liq_long_usd_24h: Optional[float] = None,
    liq_short_usd_24h: Optional[float] = None,
    signal_age_hours: Optional[float] = None,
    score_stddev_24h: Optional[float] = None,
    setup_quality_label: Optional[str] = None,
) -> dict[str, Any]:
    """Build a structured thesis dict. Every reason is sourced from one input;
    fields not provided contribute nothing to the result.

    The headline picks a verb based on conviction so a +75 reads as "strongly
    biased long" while +20 reads as "leans long". This matches how traders
    actually describe setups.
    """
    direction, conviction = _direction_from_score(composite_score)

    bullish: list[str] = []
    bearish: list[str] = []
    risks: list[str] = []

    # ---------------- funding rate (the headline trade compensation) ---------
    if funding_8h_norm_pct is not None:
        if funding_8h_norm_pct <= -0.5:
            bullish.append(
                f"Funding deeply negative ({funding_8h_norm_pct:+.4f}%/8h) — "
                f"shorts paying longs; carry favors long bias"
            )
        elif funding_8h_norm_pct < -0.05:
            bullish.append(
                f"Funding negative ({funding_8h_norm_pct:+.4f}%/8h) — modest carry for longs"
            )
        elif funding_8h_norm_pct >= 0.5:
            bearish.append(
                f"Funding deeply positive ({funding_8h_norm_pct:+.4f}%/8h) — "
                f"longs paying shorts; carry favors short bias"
            )
        elif funding_8h_norm_pct > 0.05:
            bearish.append(
                f"Funding positive ({funding_8h_norm_pct:+.4f}%/8h) — modest carry for shorts"
            )

    # ---------------- funding streak (persistence) ---------------------------
    if funding_streak_count >= 3 and funding_streak_direction:
        if funding_streak_direction == "neg":
            bullish.append(
                f"{funding_streak_count} consecutive negative funding settlements — "
                f"persistent shorts-pay regime (squeeze-prone)"
            )
        elif funding_streak_direction == "pos":
            bearish.append(
                f"{funding_streak_count} consecutive positive funding settlements — "
                f"persistent longs-pay regime (over-leveraged longs)"
            )

    # ---------------- funding deviation (anomaly detection) ------------------
    if funding_deviation_z is not None:
        if funding_deviation_z >= 2.5:
            risks.append(
                f"Funding {funding_deviation_z:+.1f}σ above its 30-period mean — "
                f"extreme overshoot, mean-revert risk against any short carry"
            )
        elif funding_deviation_z <= -2.5:
            risks.append(
                f"Funding {funding_deviation_z:+.1f}σ below its 30-period mean — "
                f"extreme undershoot, sudden flip risk"
            )

    # ---------------- OI direction (with funding context) --------------------
    if oi_change_24h_pct is not None and abs(oi_change_24h_pct) > 5.0:
        if funding_8h_norm_pct is not None:
            if oi_change_24h_pct > 0 and funding_8h_norm_pct < 0:
                bullish.append(
                    f"OI rising +{oi_change_24h_pct:.1f}%/24h while funding is negative — "
                    f"fresh long positioning being paid to enter"
                )
            elif oi_change_24h_pct > 0 and funding_8h_norm_pct > 0:
                bearish.append(
                    f"OI rising +{oi_change_24h_pct:.1f}%/24h while funding is positive — "
                    f"longs piling in at peak premium (late-cycle)"
                )
            elif oi_change_24h_pct < 0:
                # Unwind — direction depends on which side was crowded.
                risks.append(
                    f"OI dropping {oi_change_24h_pct:.1f}%/24h — position unwind in progress"
                )

    # ---------------- L/S ratio extremes -------------------------------------
    if ls_ratio_global is not None:
        if ls_ratio_global >= 3.0:
            bearish.append(
                f"Retail L/S ratio {ls_ratio_global:.2f} — heavily crowded long; "
                f"contrarian short edge"
            )
        elif ls_ratio_global <= 0.4:
            bullish.append(
                f"Retail L/S ratio {ls_ratio_global:.2f} — heavily crowded short; "
                f"squeeze risk for shorts"
            )

    # Smart-vs-retail divergence
    if ls_ratio_global is not None and ls_ratio_top is not None:
        if ls_ratio_top < 1.0 and ls_ratio_global >= 1.5:
            bearish.append(
                "Top-trader L/S < 1.0 while retail > 1.5 — smart money positioned against retail"
            )
        elif ls_ratio_top > 1.0 and ls_ratio_global <= 0.7:
            bullish.append(
                "Top-trader L/S > 1.0 while retail < 0.7 — smart money on the other side of retail"
            )

    # ---------------- mark/index spread (risk metric) ------------------------
    if mark_index_spread_pct is not None and abs(mark_index_spread_pct) > 0.5:
        risks.append(
            f"Mark vs index spread {mark_index_spread_pct:+.3f}% — possible "
            f"liquidation cascade risk or thin spot reference"
        )

    # ---------------- on-chain whale flow ------------------------------------
    if onchain_net_usd is not None and abs(onchain_net_usd) >= 5_000_000:
        if onchain_net_usd > 0:
            bullish.append(
                f"24h on-chain net withdrawals ${onchain_net_usd / 1e6:+,.1f}M — "
                f"coins moving off-exchange (accumulation signal)"
            )
        else:
            bearish.append(
                f"24h on-chain net deposits ${-onchain_net_usd / 1e6:+,.1f}M — "
                f"coins moving to exchanges (distribution signal)"
            )

    # ---------------- liquidation skew --------------------------------------
    if (
        liq_long_usd_24h is not None
        and liq_short_usd_24h is not None
        and (liq_long_usd_24h + liq_short_usd_24h) >= 5_000_000
    ):
        long_u = liq_long_usd_24h or 0.0
        short_u = liq_short_usd_24h or 0.0
        total = long_u + short_u
        skew = (short_u - long_u) / total
        if skew >= 0.4:
            bullish.append(
                f"24h liquidations: ${short_u / 1e6:.1f}M shorts blown out vs "
                f"${long_u / 1e6:.1f}M longs — squeeze in progress"
            )
        elif skew <= -0.4:
            bearish.append(
                f"24h liquidations: ${long_u / 1e6:.1f}M longs blown out vs "
                f"${short_u / 1e6:.1f}M shorts — cascade in progress"
            )

    # ---------------- signal stability (risk on the conviction itself) ------
    if score_stddev_24h is not None and score_stddev_24h > 30:
        risks.append(
            f"Score σ {score_stddev_24h:.1f} over last 24h — signal has been "
            f"unstable; treat conviction with caution"
        )

    # ---------------- signal age (timing) ------------------------------------
    if signal_age_hours is not None and abs(composite_score or 0) >= 30:
        if signal_age_hours < 1.0:
            bullish.append(
                f"Signal just lit up ({signal_age_hours * 60:.0f} min ago) — "
                f"likely still un-priced"
            ) if direction == "long" else None
            bearish.append(
                f"Signal just lit up ({signal_age_hours * 60:.0f} min ago) — "
                f"likely still un-priced"
            ) if direction == "short" else None
        elif signal_age_hours >= 12.0:
            risks.append(
                f"Signal active for {signal_age_hours:.1f}h — likely already "
                f"in price, late-cycle entry risk"
            )

    # ---------------- headline ----------------------------------------------
    direction_text = {
        "long":          "leans long" if conviction == "low" else (
                         "biased long" if conviction == "medium" else "strongly biased long"),
        "short":         "leans short" if conviction == "low" else (
                         "biased short" if conviction == "medium" else "strongly biased short"),
        "no_clear_bias": "shows no clear directional bias",
    }[direction]
    quality_suffix = f" — quality: {setup_quality_label}" if setup_quality_label else ""
    score_suffix = f" (score {composite_score:+d})" if composite_score is not None else ""
    headline = f"{symbol} {direction_text}{score_suffix}{quality_suffix}"

    return {
        "direction": direction,
        "conviction": conviction,
        "headline": headline,
        "bullish_reasons": bullish,
        "bearish_reasons": bearish,
        "risks": risks,
    }


def format_thesis_for_telegram(thesis: dict[str, Any], max_reasons_per_section: int = 4) -> str:
    """Format a thesis dict as compact Markdown for Telegram alert payloads.

    Telegram messages cap at 4096 chars; the alerts loop already prepends a
    headline line, so we keep this body terse — at most `max_reasons_per_section`
    bullet points per section. Sections with zero entries are omitted entirely.

    Returns an empty string when the thesis has no reasons or risks at all
    (rather than emitting empty headers). Caller can `if format_thesis_for_telegram(...)`
    as a truthy guard.
    """
    sections: list[str] = []
    bullish = (thesis.get("bullish_reasons") or [])[:max_reasons_per_section]
    bearish = (thesis.get("bearish_reasons") or [])[:max_reasons_per_section]
    risks = (thesis.get("risks") or [])[:max_reasons_per_section]

    if bullish:
        sections.append("*🟢 Bullish reasons:*\n" + "\n".join(f"• {r}" for r in bullish))
    if bearish:
        sections.append("*🔴 Bearish reasons:*\n" + "\n".join(f"• {r}" for r in bearish))
    if risks:
        sections.append("*⚠️ Risks:*\n" + "\n".join(f"• {r}" for r in risks))

    return "\n\n".join(sections)
