"""Tests for compose_trade_thesis (Round 37)."""

from __future__ import annotations

from funding_screener.thesis import compose_trade_thesis


def _has_match(reasons: list[str], substring: str) -> bool:
    return any(substring in r for r in reasons)


# ---------------- direction & conviction ----------------


def test_high_score_strongly_long():
    out = compose_trade_thesis(symbol="BTCUSDT", composite_score=80)
    assert out["direction"] == "long"
    assert out["conviction"] == "high"
    assert "strongly biased long" in out["headline"]


def test_medium_score_biased_long():
    out = compose_trade_thesis(symbol="BTCUSDT", composite_score=50)
    assert out["conviction"] == "medium"
    assert "biased long" in out["headline"]


def test_low_score_leans_long():
    out = compose_trade_thesis(symbol="BTCUSDT", composite_score=15)
    assert out["conviction"] == "low"
    assert "leans long" in out["headline"]


def test_negative_score_short():
    out = compose_trade_thesis(symbol="BTCUSDT", composite_score=-80)
    assert out["direction"] == "short"
    assert "strongly biased short" in out["headline"]


def test_neutral_score_no_bias():
    out = compose_trade_thesis(symbol="BTCUSDT", composite_score=5)
    assert out["direction"] == "no_clear_bias"
    assert out["conviction"] == "none"


def test_none_score_no_bias():
    out = compose_trade_thesis(symbol="BTCUSDT", composite_score=None)
    assert out["direction"] == "no_clear_bias"


# ---------------- funding rate reasons ----------------


def test_deeply_negative_funding_is_bullish_reason():
    out = compose_trade_thesis(symbol="X", composite_score=70, funding_8h_norm_pct=-0.8)
    assert _has_match(out["bullish_reasons"], "deeply negative")


def test_deeply_positive_funding_is_bearish_reason():
    out = compose_trade_thesis(symbol="X", composite_score=-70, funding_8h_norm_pct=0.8)
    assert _has_match(out["bearish_reasons"], "deeply positive")


def test_neutral_funding_no_reason():
    out = compose_trade_thesis(symbol="X", composite_score=70, funding_8h_norm_pct=0.0)
    assert not _has_match(out["bullish_reasons"], "Funding")
    assert not _has_match(out["bearish_reasons"], "Funding")


# ---------------- streak reasons ----------------


def test_streak_3_negative_is_bullish():
    out = compose_trade_thesis(
        symbol="X", composite_score=70,
        funding_streak_count=3, funding_streak_direction="neg",
    )
    assert _has_match(out["bullish_reasons"], "consecutive negative")


def test_streak_3_positive_is_bearish():
    out = compose_trade_thesis(
        symbol="X", composite_score=-70,
        funding_streak_count=4, funding_streak_direction="pos",
    )
    assert _has_match(out["bearish_reasons"], "consecutive positive")


def test_short_streak_no_reason():
    out = compose_trade_thesis(
        symbol="X", composite_score=70,
        funding_streak_count=2, funding_streak_direction="neg",
    )
    assert not _has_match(out["bullish_reasons"], "consecutive")


# ---------------- deviation reasons (risks) ----------------


def test_extreme_overshoot_is_risk():
    out = compose_trade_thesis(symbol="X", composite_score=70, funding_deviation_z=3.0)
    assert _has_match(out["risks"], "extreme overshoot")


def test_extreme_undershoot_is_risk():
    out = compose_trade_thesis(symbol="X", composite_score=-70, funding_deviation_z=-3.0)
    assert _has_match(out["risks"], "extreme undershoot")


# ---------------- OI direction with funding context ----------------


def test_oi_rising_with_neg_funding_is_bullish():
    out = compose_trade_thesis(
        symbol="X", composite_score=70,
        oi_change_24h_pct=20, funding_8h_norm_pct=-0.5,
    )
    assert _has_match(out["bullish_reasons"], "OI rising")


def test_oi_rising_with_pos_funding_is_bearish():
    out = compose_trade_thesis(
        symbol="X", composite_score=-70,
        oi_change_24h_pct=20, funding_8h_norm_pct=0.5,
    )
    assert _has_match(out["bearish_reasons"], "late-cycle")


def test_oi_dropping_is_risk():
    out = compose_trade_thesis(
        symbol="X", composite_score=70,
        oi_change_24h_pct=-15, funding_8h_norm_pct=-0.5,
    )
    assert _has_match(out["risks"], "unwind")


# ---------------- L/S extremes ----------------


def test_crowded_long_is_bearish():
    out = compose_trade_thesis(symbol="X", composite_score=-70, ls_ratio_global=3.5)
    assert _has_match(out["bearish_reasons"], "crowded long")


def test_crowded_short_is_bullish():
    out = compose_trade_thesis(symbol="X", composite_score=70, ls_ratio_global=0.3)
    assert _has_match(out["bullish_reasons"], "crowded short")


def test_smart_vs_retail_divergence_short_side():
    out = compose_trade_thesis(
        symbol="X", composite_score=-70,
        ls_ratio_global=2.0, ls_ratio_top=0.7,
    )
    assert _has_match(out["bearish_reasons"], "smart money positioned against retail")


# ---------------- mark/index risk ----------------


def test_big_mark_index_spread_is_risk():
    out = compose_trade_thesis(
        symbol="X", composite_score=70, mark_index_spread_pct=1.0,
    )
    assert _has_match(out["risks"], "Mark vs index")


# ---------------- on-chain ----------------


def test_big_on_chain_withdrawals_bullish():
    out = compose_trade_thesis(
        symbol="X", composite_score=70, onchain_net_usd=20_000_000,
    )
    assert _has_match(out["bullish_reasons"], "on-chain net withdrawals")


def test_big_on_chain_deposits_bearish():
    out = compose_trade_thesis(
        symbol="X", composite_score=-70, onchain_net_usd=-20_000_000,
    )
    assert _has_match(out["bearish_reasons"], "on-chain net deposits")


def test_small_on_chain_no_reason():
    """Below $5M threshold → no reason emitted."""
    out = compose_trade_thesis(
        symbol="X", composite_score=70, onchain_net_usd=1_000_000,
    )
    assert not _has_match(out["bullish_reasons"], "on-chain")


# ---------------- liquidation skew ----------------


def test_short_squeeze_is_bullish():
    out = compose_trade_thesis(
        symbol="X", composite_score=70,
        liq_long_usd_24h=2_000_000, liq_short_usd_24h=20_000_000,
    )
    assert _has_match(out["bullish_reasons"], "squeeze in progress")


def test_long_cascade_is_bearish():
    out = compose_trade_thesis(
        symbol="X", composite_score=-70,
        liq_long_usd_24h=20_000_000, liq_short_usd_24h=2_000_000,
    )
    assert _has_match(out["bearish_reasons"], "cascade in progress")


def test_balanced_liq_no_reason():
    out = compose_trade_thesis(
        symbol="X", composite_score=70,
        liq_long_usd_24h=10_000_000, liq_short_usd_24h=10_000_000,
    )
    assert not _has_match(out["bullish_reasons"], "squeeze")
    assert not _has_match(out["bearish_reasons"], "cascade")


# ---------------- signal age & stability ----------------


def test_fresh_signal_added_as_reason():
    out = compose_trade_thesis(
        symbol="X", composite_score=70, signal_age_hours=0.4,
    )
    assert _has_match(out["bullish_reasons"], "Signal just lit up")


def test_old_signal_added_as_risk():
    out = compose_trade_thesis(
        symbol="X", composite_score=70, signal_age_hours=18.0,
    )
    assert _has_match(out["risks"], "late-cycle entry risk")


def test_high_stddev_is_risk():
    out = compose_trade_thesis(
        symbol="X", composite_score=70, score_stddev_24h=40.0,
    )
    assert _has_match(out["risks"], "unstable")


# ---------------- empty inputs ----------------


def test_no_inputs_returns_empty_lists():
    out = compose_trade_thesis(symbol="BTCUSDT")
    assert out["bullish_reasons"] == []
    assert out["bearish_reasons"] == []
    assert out["risks"] == []
    assert out["direction"] == "no_clear_bias"


# ---------------- quality label ----------------


def test_quality_label_appears_in_headline():
    out = compose_trade_thesis(
        symbol="X", composite_score=70, setup_quality_label="🚀 Fresh bull",
    )
    assert "Fresh bull" in out["headline"]
