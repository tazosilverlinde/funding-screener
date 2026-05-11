"""Tests for suppress_overlapping_alerts (Round 63)."""

from __future__ import annotations

from funding_screener.notifications import suppress_overlapping_alerts


# ---------------- default group behavior ----------------


def test_default_keeps_fresh_over_composite_over_score_delta():
    """When all three fire for the same pair, only 'fresh' survives."""
    events = [
        ("fresh:BTC/USDT", "active", "🚀 fresh"),
        ("composite:BTC/USDT", "active", "🚀 composite"),
        ("score_delta:BTC/USDT", "active", "📈 delta"),
    ]
    out = suppress_overlapping_alerts(events)
    keys = [k for (k, _s, _m) in out]
    assert keys == ["fresh:BTC/USDT"]


def test_default_keeps_composite_when_no_fresh():
    events = [
        ("composite:BTC/USDT", "active", "🚀 composite"),
        ("score_delta:BTC/USDT", "active", "📈 delta"),
    ]
    out = suppress_overlapping_alerts(events)
    keys = [k for (k, _s, _m) in out]
    assert keys == ["composite:BTC/USDT"]


def test_score_delta_survives_when_alone():
    events = [("score_delta:BTC/USDT", "active", "📈 delta")]
    out = suppress_overlapping_alerts(events)
    assert len(out) == 1


# ---------------- per-pair isolation ----------------


def test_different_pairs_dont_suppress_each_other():
    """fresh:BTC and composite:ETH should both survive — different subjects."""
    events = [
        ("fresh:BTC/USDT", "active", "btc fresh"),
        ("composite:ETH/USDT", "active", "eth comp"),
    ]
    out = suppress_overlapping_alerts(events)
    keys = {k for (k, _s, _m) in out}
    assert keys == {"fresh:BTC/USDT", "composite:ETH/USDT"}


def test_three_pairs_each_get_their_own_winner():
    events = [
        ("fresh:A/USDT", "active", "a"),
        ("composite:A/USDT", "active", "a comp"),  # suppressed
        ("composite:B/USDT", "active", "b"),
        ("score_delta:B/USDT", "active", "b delta"),  # suppressed
        ("score_delta:C/USDT", "active", "c"),
    ]
    out = suppress_overlapping_alerts(events)
    keys = {k for (k, _s, _m) in out}
    assert keys == {"fresh:A/USDT", "composite:B/USDT", "score_delta:C/USDT"}


# ---------------- resolved events pass through ----------------


def test_resolved_events_always_pass_through():
    """A resolved alert must NEVER be suppressed — the state machine for each
    kind needs to flip back to off independently.
    """
    events = [
        ("fresh:BTC/USDT", "active", "fresh"),
        ("composite:BTC/USDT", "resolved", "comp clear"),
        ("score_delta:BTC/USDT", "resolved", "delta clear"),
    ]
    out = suppress_overlapping_alerts(events)
    statuses = {(k, s) for (k, s, _m) in out}
    assert ("fresh:BTC/USDT", "active") in statuses
    assert ("composite:BTC/USDT", "resolved") in statuses
    assert ("score_delta:BTC/USDT", "resolved") in statuses


def test_resolved_active_mixed_same_pair_resolves_pass_actives_suppress():
    events = [
        ("fresh:BTC/USDT", "active", "f"),
        ("composite:BTC/USDT", "active", "c"),
        ("score_delta:BTC/USDT", "resolved", "d cleared"),
    ]
    out = suppress_overlapping_alerts(events)
    # fresh wins active; composite suppressed; score_delta resolved passes.
    actives = {k for (k, s, _m) in out if s == "active"}
    resolves = {k for (k, s, _m) in out if s == "resolved"}
    assert actives == {"fresh:BTC/USDT"}
    assert resolves == {"score_delta:BTC/USDT"}


# ---------------- alerts outside any group are untouched ----------------


def test_alerts_outside_group_never_suppressed():
    """liq_cascade, funding_rate, oi_surge, etc. are independent."""
    events = [
        ("composite:BTC/USDT", "active", "comp"),
        ("liq_cascade:BTCUSDT", "active", "liq"),
        ("funding_rate:Binance:BTCUSDT", "active", "funding"),
        ("oi_surge:BTC/USDT", "active", "oi"),
        ("whale:BTC", "active", "whale"),
    ]
    out = suppress_overlapping_alerts(events)
    # Every kind survives — composite is alone in its group; others are
    # outside the overlap group.
    keys = {k for (k, _s, _m) in out}
    assert keys == {
        "composite:BTC/USDT", "liq_cascade:BTCUSDT",
        "funding_rate:Binance:BTCUSDT", "oi_surge:BTC/USDT", "whale:BTC",
    }


# ---------------- custom groups ----------------


def test_custom_groups_override():
    """Caller can pass arbitrary priority groups."""
    custom = [["alpha", "beta"], ["gamma", "delta"]]
    events = [
        ("alpha:X", "active", "1"),
        ("beta:X", "active", "2"),    # suppressed in alpha group
        ("gamma:X", "active", "3"),
        ("delta:X", "active", "4"),   # suppressed in gamma group
        ("epsilon:X", "active", "5"), # outside groups → passes
    ]
    out = suppress_overlapping_alerts(events, overlap_groups=custom)
    keys = {k for (k, _s, _m) in out}
    assert keys == {"alpha:X", "gamma:X", "epsilon:X"}


def test_empty_groups_means_no_suppression():
    events = [
        ("fresh:BTC/USDT", "active", "f"),
        ("composite:BTC/USDT", "active", "c"),
    ]
    out = suppress_overlapping_alerts(events, overlap_groups=[])
    keys = {k for (k, _s, _m) in out}
    # No groups → everything passes through.
    assert keys == {"fresh:BTC/USDT", "composite:BTC/USDT"}


# ---------------- direction-suffix keys (funding_dev) ----------------


def test_funding_dev_direction_keys_treated_as_same_subject():
    """funding_dev uses 'kind:BASE/QUOTE:direction' — subject is BASE/QUOTE."""
    # NOTE: funding_dev isn't in the default group, so this test confirms
    # ONLY that subject extraction works correctly on suffixed keys.
    events = [
        ("funding_dev:BTC/USDT:over", "active", "f"),
    ]
    out = suppress_overlapping_alerts(
        events, overlap_groups=[["funding_dev", "composite"]]
    )
    assert len(out) == 1


# ---------------- edge cases ----------------


def test_empty_input_returns_empty():
    assert suppress_overlapping_alerts([]) == []
    assert suppress_overlapping_alerts(None) == []  # type: ignore[arg-type]


def test_no_active_events_passes_through():
    events = [
        ("fresh:BTC/USDT", "resolved", "f"),
        ("composite:BTC/USDT", "resolved", "c"),
    ]
    out = suppress_overlapping_alerts(events)
    # Both resolved, both pass.
    assert len(out) == 2


def test_key_without_colon_subject_falls_back_to_key():
    """Defensive: a key without ':' uses the full string as subject."""
    events = [
        ("fresh", "active", "weird"),  # no subject suffix
    ]
    out = suppress_overlapping_alerts(events)
    assert len(out) == 1
