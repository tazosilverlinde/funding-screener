"""Tests for realized-volatility computation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funding_screener.models import Kline
from funding_screener.signals import compute_realized_volatility


def _kline(close: float, days_ago: int) -> Kline:
    t = datetime(2026, 5, 5, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return Kline(open_time=t, open=close, high=close, low=close, close=close, volume=1.0, quote_volume=close)


def test_volatility_zero_when_constant_price():
    # Identical closes → zero stddev → zero vol.
    klines = [_kline(close=100.0, days_ago=d) for d in range(30, 0, -1)]
    assert compute_realized_volatility(klines, days=30) == pytest.approx(0.0, abs=1e-9)


def test_volatility_returns_none_for_too_few_klines():
    klines = [_kline(close=100.0, days_ago=d) for d in range(2, 0, -1)]
    assert compute_realized_volatility(klines, days=30) is None


def test_volatility_handles_zero_close_gracefully():
    klines = [_kline(close=0.0, days_ago=d) for d in range(30, 0, -1)]
    assert compute_realized_volatility(klines, days=30) is None


def test_volatility_positive_for_oscillating_prices():
    # 50%/-50% daily moves → very high annualized vol.
    klines = []
    price = 100.0
    for d in range(30, 0, -1):
        klines.append(_kline(close=price, days_ago=d))
        price *= 1.5 if d % 2 == 0 else 0.6667  # ~zigzag
    vol = compute_realized_volatility(klines, days=30)
    assert vol is not None
    assert vol > 100  # very volatile


def test_volatility_realistic_btc_ish():
    # Smooth ~2%/day fluctuations → realistic blue-chip vol (~30-80% annualized).
    import math, random
    random.seed(42)
    closes = [100.0]
    for _ in range(60):
        closes.append(closes[-1] * math.exp(random.gauss(0, 0.02)))
    klines = [_kline(close=c, days_ago=len(closes) - i) for i, c in enumerate(closes)]
    vol = compute_realized_volatility(klines, days=60)
    assert vol is not None
    assert 20 < vol < 80  # roughly blue-chip territory
