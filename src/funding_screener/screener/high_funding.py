"""High-funding screener: pure function over already-fetched FundingRow data.

Filter: `abs(rate_8h_norm_percent) > threshold_percent`.
"""

from __future__ import annotations

from typing import Iterable

from ..models import FundingRow


def screen_high_funding(
    rows: Iterable[FundingRow],
    threshold_percent: float,
) -> list[FundingRow]:
    flagged = [r for r in rows if abs(r.rate_8h_norm_percent) > threshold_percent]
    flagged.sort(key=lambda r: abs(r.rate_8h_norm_percent), reverse=True)
    return flagged
