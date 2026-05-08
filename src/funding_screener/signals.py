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
