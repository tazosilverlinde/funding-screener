"""Signal classifier for the combined high-funding page.

Translates raw metrics (funding rate, funding streak, mark-vs-index spread) into
a single human-readable bias label with an emoji and a breakdown explaining the
"why". The breakdown text is what we surface in tooltips so users can see how
the verdict was reached.

Conventions used in classification:

  - Funding rate sign (settled rate the trader pays/receives at next funding):
      positive  → longs PAY shorts                        → bearish bias
      negative  → shorts PAY longs                        → bullish bias

  - Streak (consecutive same-sign settled rates) measures conviction:
      length 1  → noisy / single period
      length 2  → tentative consensus
      length 3+ → persistent — strong directional pressure

  - Mark vs index spread (Binance only; MEXC doesn't expose index price):
      |spread| > 0.5%  → flagged as RISK regardless of funding;
                          divergence often precedes liquidation cascades or
                          signals an illiquid contract being manipulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


StreakDirection = Literal["pos", "neg", "mixed"]


@dataclass(frozen=True)
class SignalLabel:
    emoji: str        # 🟢 / 🔴 / 🟡 / ⚠️ / 📈 / 📉
    short: str        # 1-3 word label, displayed in the table cell
    color: str        # green / red / gray / orange — for optional cell colour
    breakdown: str    # multi-line text shown in the column tooltip


def compute_funding_streak(rates: list[float]) -> tuple[int, Optional[StreakDirection]]:
    """Count consecutive same-sign rates starting from the most recent.

    `rates` must be ordered MOST-RECENT-FIRST (index 0 = last settlement).
    Returns (count, direction) where direction is "pos", "neg", or None when
    the most-recent rate is exactly zero or the list is empty.
    """
    if not rates:
        return (0, None)
    first = rates[0]
    if first > 0:
        direction: StreakDirection = "pos"
    elif first < 0:
        direction = "neg"
    else:
        return (0, None)

    count = 0
    for r in rates:
        if (direction == "pos" and r > 0) or (direction == "neg" and r < 0):
            count += 1
        else:
            break
    return (count, direction)


def classify_signal(
    *,
    funding_8h_norm_pct: Optional[float],
    streak_count: int,
    streak_direction: Optional[StreakDirection],
    mark_index_spread_pct: Optional[float],
) -> SignalLabel:
    """Turn the metrics for a single row into a SignalLabel.

    Order of precedence:
      1. RISK if mark/index spread is large
      2. Strong directional with streak >= 3
      3. Moderate directional with streak 2 + extreme funding
      4. Mild directional from funding alone
      5. Neutral fallback
    """
    f = funding_8h_norm_pct or 0.0
    spread = abs(mark_index_spread_pct) if mark_index_spread_pct is not None else 0.0

    # 1. Risk overrides everything — divergence between mark and index can
    #    mean liquidations imminent or manipulation of an illiquid contract.
    if spread >= 0.5:
        return SignalLabel(
            emoji="⚠️",
            short="Risk",
            color="orange",
            breakdown=(
                f"Mark vs index diverged by {mark_index_spread_pct:+.3f}%.\n"
                "Above ±0.5% is unusual and frequently precedes liquidation cascades "
                "or signals manipulation of an illiquid contract.\n"
                "Trade carefully — funding may snap back as price corrects to index."
            ),
        )

    # 2/3. Streak-amplified signals.
    if streak_count >= 3 and streak_direction == "pos":
        return SignalLabel(
            emoji="📉",
            short="Persistent bear",
            color="red",
            breakdown=(
                f"Funding has been positive for {streak_count} consecutive periods.\n"
                "Longs are paying shorts persistently — strong evidence of "
                "over-leveraged long positioning.\n"
                "Bias: shorts likely to keep collecting; mean-reversion candidates."
            ),
        )
    if streak_count >= 3 and streak_direction == "neg":
        return SignalLabel(
            emoji="📈",
            short="Persistent bull",
            color="green",
            breakdown=(
                f"Funding has been negative for {streak_count} consecutive periods.\n"
                "Shorts are paying longs persistently — strong evidence of "
                "over-leveraged short positioning.\n"
                "Bias: longs likely to keep collecting; squeeze candidates."
            ),
        )

    if streak_count == 2 and streak_direction == "pos" and f > 1.0:
        return SignalLabel(
            emoji="🔴",
            short="Bearish",
            color="red",
            breakdown=(
                f"Funding {f:+.3f}% / 8h, 2 consecutive positive periods.\n"
                "Long-side overcrowded; expect funding-rate normalisation."
            ),
        )
    if streak_count == 2 and streak_direction == "neg" and f < -1.0:
        return SignalLabel(
            emoji="🟢",
            short="Bullish",
            color="green",
            breakdown=(
                f"Funding {f:+.3f}% / 8h, 2 consecutive negative periods.\n"
                "Short-side overcrowded; squeeze risk elevated."
            ),
        )

    # 4. Single-period directional from funding magnitude alone.
    if f >= 1.0:
        return SignalLabel(
            emoji="🔴",
            short="Bearish",
            color="red",
            breakdown=(
                f"Funding {f:+.3f}% / 8h is high (longs paying).\n"
                "Single-period signal — watch for streak confirmation before sizing."
            ),
        )
    if f <= -1.0:
        return SignalLabel(
            emoji="🟢",
            short="Bullish",
            color="green",
            breakdown=(
                f"Funding {f:+.3f}% / 8h is deeply negative (shorts paying).\n"
                "Single-period signal — watch for streak confirmation before sizing."
            ),
        )

    # 5. Neutral — funding within normal band, no overcrowding.
    return SignalLabel(
        emoji="🟡",
        short="Neutral",
        color="gray",
        breakdown=(
            f"Funding {f:+.3f}% / 8h within normal band, no streak.\n"
            "No clear directional bias from funding alone."
        ),
    )


# ---------------- composite score ----------------


@dataclass(frozen=True)
class CompositeScore:
    """Signed [-100, +100] composite. Positive = long bias; negative = short bias."""
    score: int                  # -100..+100
    emoji: str
    short: str                  # 1-3 word label
    color: str                  # green / red / gray
    breakdown: list[str]        # itemized contributions, for tooltip


def compute_composite_score(
    *,
    funding_8h_norm_pct: Optional[float] = None,
    streak_count: int = 0,
    streak_direction: Optional[StreakDirection] = None,
    mark_index_spread_pct: Optional[float] = None,
    oi_change_24h_pct: Optional[float] = None,
    ls_ratio_global: Optional[float] = None,
    ls_ratio_top: Optional[float] = None,
    onchain_net_usd: Optional[float] = None,
) -> CompositeScore:
    """Combine all available signals into a single -100..+100 score.

    Sign convention: **positive = bullish (favors long), negative = bearish (favors short)**.
    Components and their max contribution:

      | Component            | Range       | Logic |
      |----------------------|-------------|-------|
      | Funding rate         | ±30         | Negative funding (shorts pay longs) → positive score |
      | Streak (3+)          | ±15         | Persistent same-sign reinforces direction |
      | OI 24h Δ × funding   | ±15         | OI rising while shorts pay = strong bull |
      | L/S ratio extreme    | ±10         | Crowded long → contrarian short |
      | On-chain netflow     | ±15         | Withdrawals exceed deposits → bullish |
      | Mark/Index spread    | -50% damp   | Big divergence reduces conviction (multiplicative) |
      | Top-vs-retail L/S    | ±5          | Smart money against retail = small confirm |

    Inputs may be None — score is computed from whatever's available.
    """
    contributions: list[str] = []
    s = 0.0

    # 1. Funding (±30)
    if funding_8h_norm_pct is not None:
        f = max(-2.0, min(2.0, funding_8h_norm_pct))  # clamp at ±2%
        delta = -15.0 * f  # negative funding ⇒ +score
        s += delta
        contributions.append(f"funding {funding_8h_norm_pct:+.3f}%/8h → {delta:+.1f}")

    # 2. Streak (±15)
    if streak_count >= 3 and streak_direction:
        delta = 15.0 if streak_direction == "neg" else -15.0
        s += delta
        contributions.append(f"streak {streak_count}× {streak_direction} → {delta:+.1f}")
    elif streak_count == 2 and streak_direction:
        delta = 7.5 if streak_direction == "neg" else -7.5
        s += delta
        contributions.append(f"streak {streak_count}× {streak_direction} → {delta:+.1f}")

    # 3. OI 24h Δ × funding direction (±15)
    if oi_change_24h_pct is not None and funding_8h_norm_pct is not None:
        if abs(oi_change_24h_pct) > 5.0:
            oi_clamped = max(-50.0, min(50.0, oi_change_24h_pct))
            # OI rising while shorts pay = strong bull confirmation
            if funding_8h_norm_pct < 0:
                delta = 0.3 * oi_clamped
            else:
                delta = -0.3 * oi_clamped
            s += delta
            contributions.append(f"OI 24h Δ {oi_change_24h_pct:+.1f}% → {delta:+.1f}")

    # 4. L/S ratio extreme (±10)
    if ls_ratio_global is not None:
        if ls_ratio_global > 3.0:
            s -= 10.0
            contributions.append(f"L/S retail {ls_ratio_global:.2f} (crowded long) → -10.0")
        elif ls_ratio_global < 0.4:
            s += 10.0
            contributions.append(f"L/S retail {ls_ratio_global:.2f} (crowded short) → +10.0")

    # 5. Top-trader vs retail divergence (±5)
    if ls_ratio_global is not None and ls_ratio_top is not None:
        if ls_ratio_top < 1.0 and ls_ratio_global > 1.5:
            s -= 5.0
            contributions.append("smart money short, retail long → -5.0")
        elif ls_ratio_top > 1.0 and ls_ratio_global < 0.7:
            s += 5.0
            contributions.append("smart money long, retail short → +5.0")

    # 6. On-chain netflow (±15)
    if onchain_net_usd is not None and abs(onchain_net_usd) > 1_000_000:
        net_clamped = max(-50_000_000.0, min(50_000_000.0, onchain_net_usd))
        delta = 15.0 * (net_clamped / 50_000_000.0)
        s += delta
        sign = "withdrawals" if net_clamped > 0 else "deposits"
        contributions.append(f"on-chain net ${onchain_net_usd:+,.0f} ({sign}) → {delta:+.1f}")

    # 7. Mark/Index spread risk damping (multiplicative)
    if mark_index_spread_pct is not None and abs(mark_index_spread_pct) > 0.5:
        s *= 0.5
        contributions.append(
            f"mark/idx {mark_index_spread_pct:+.3f}% (risk) → ×0.5 conviction damp"
        )

    score = int(max(-100, min(100, round(s))))
    emoji, short, color = _composite_label(score)
    return CompositeScore(
        score=score,
        emoji=emoji,
        short=short,
        color=color,
        breakdown=contributions or ["no inputs available"],
    )


def _composite_label(score: int) -> tuple[str, str, str]:
    """Map score → (emoji, short label, color). Symmetric around zero."""
    if score >= 70:
        return ("🚀", "Strong bull", "green")
    if score >= 30:
        return ("🟢", "Bullish", "green")
    if score >= 10:
        return ("↗", "Mild bull", "green")
    if score <= -70:
        return ("💥", "Strong bear", "red")
    if score <= -30:
        return ("🔴", "Bearish", "red")
    if score <= -10:
        return ("↘", "Mild bear", "red")
    return ("🟡", "Neutral", "gray")
