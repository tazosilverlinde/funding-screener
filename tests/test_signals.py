"""Tests for the signal classifier and funding-streak helper."""

from __future__ import annotations

from funding_screener.signals import classify_signal, compute_funding_streak


# ---------------- compute_funding_streak ----------------


def test_streak_empty():
    assert compute_funding_streak([]) == (0, None)


def test_streak_zero_first():
    """A leading zero rate has no direction → count zero."""
    assert compute_funding_streak([0.0, 0.5, 0.4]) == (0, None)


def test_streak_all_positive():
    assert compute_funding_streak([0.5, 0.4, 0.3]) == (3, "pos")


def test_streak_all_negative():
    assert compute_funding_streak([-0.2, -0.3, -0.1]) == (3, "neg")


def test_streak_breaks_on_sign_flip():
    """[+, +, -, +] → streak of 2 positive, then breaks."""
    assert compute_funding_streak([0.5, 0.4, -0.1, 0.3]) == (2, "pos")


def test_streak_first_is_short():
    """[+, -, -] → streak of 1 positive."""
    assert compute_funding_streak([0.5, -0.1, -0.2]) == (1, "pos")


# ---------------- classify_signal ----------------


def test_classify_risk_overrides_everything():
    """Big mark/index spread → RISK label regardless of streak/funding."""
    s = classify_signal(
        funding_8h_norm_pct=2.0,
        streak_count=5,
        streak_direction="pos",
        mark_index_spread_pct=0.7,
    )
    assert s.emoji == "⚠️"
    assert "Risk" in s.short


def test_classify_persistent_bear():
    """Streak 3+ positive → persistent bear (longs over-leveraged)."""
    s = classify_signal(
        funding_8h_norm_pct=0.6,
        streak_count=4,
        streak_direction="pos",
        mark_index_spread_pct=0.0,
    )
    assert s.emoji == "📉"
    assert s.color == "red"
    assert "4 consecutive" in s.breakdown


def test_classify_persistent_bull():
    """Streak 3+ negative → persistent bull (shorts over-leveraged)."""
    s = classify_signal(
        funding_8h_norm_pct=-0.6,
        streak_count=3,
        streak_direction="neg",
        mark_index_spread_pct=None,
    )
    assert s.emoji == "📈"
    assert s.color == "green"


def test_classify_extreme_funding_no_streak():
    """High positive funding with weak/no streak → bearish but mild."""
    s = classify_signal(
        funding_8h_norm_pct=1.5,
        streak_count=1,
        streak_direction="pos",
        mark_index_spread_pct=None,
    )
    assert s.emoji == "🔴"
    assert s.short == "Bearish"


def test_classify_extreme_negative_funding_no_streak():
    s = classify_signal(
        funding_8h_norm_pct=-1.5,
        streak_count=1,
        streak_direction="neg",
        mark_index_spread_pct=None,
    )
    assert s.emoji == "🟢"
    assert s.short == "Bullish"


def test_classify_neutral():
    """Funding within ±1% per 8h → neutral."""
    s = classify_signal(
        funding_8h_norm_pct=0.3,
        streak_count=0,
        streak_direction=None,
        mark_index_spread_pct=None,
    )
    assert s.emoji == "🟡"
    assert s.short == "Neutral"


def test_classify_streak2_with_extreme_amplifies():
    """Streak 2 with funding > 1% → bearish (still). Streak 2 alone wouldn't qualify; combined does."""
    s = classify_signal(
        funding_8h_norm_pct=1.2,
        streak_count=2,
        streak_direction="pos",
        mark_index_spread_pct=None,
    )
    assert s.emoji == "🔴"
