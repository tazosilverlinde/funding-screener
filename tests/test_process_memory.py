"""Tests for the process-memory introspection helpers (Round 51)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from funding_screener.process_memory import (
    PROCESS_MEMORY_BUDGET_MB,
    current_process_memory_mb,
    memory_pressure_label,
)


# ---------------- current_process_memory_mb ----------------


def test_current_process_memory_returns_positive_float_when_psutil_works():
    """Real call — psutil is in requirements.txt; just check the shape."""
    out = current_process_memory_mb()
    # Allow None for environments where psutil isn't usable, but on the test
    # box we expect a real number.
    if out is not None:
        assert isinstance(out, float)
        assert out > 0
        # A pytest process is typically 50-200MB. Sanity-check it's plausible.
        assert out < 10_000  # 10GB upper bound — way over anything realistic


def test_returns_none_when_psutil_import_fails(monkeypatch):
    """Simulate environments where psutil isn't installed — must not crash."""
    import builtins
    real_import = builtins.__import__
    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated missing psutil")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    assert current_process_memory_mb() is None


def test_returns_none_when_psutil_raises():
    """Any unexpected error from psutil → None, never propagated."""
    fake_proc = MagicMock()
    fake_proc.memory_info.side_effect = RuntimeError("simulated psutil error")
    fake_psutil = MagicMock()
    fake_psutil.Process.return_value = fake_proc
    with patch.dict("sys.modules", {"psutil": fake_psutil}):
        assert current_process_memory_mb() is None


# ---------------- memory_pressure_label ----------------


def test_pressure_unknown_for_none():
    assert memory_pressure_label(None) == "unknown"


def test_pressure_ok_below_50_percent():
    # 1024 MB = 50% of default 2048 budget — boundary, NOT ok.
    assert memory_pressure_label(1023.0) == "ok"
    assert memory_pressure_label(500.0) == "ok"
    assert memory_pressure_label(0.0) == "ok"


def test_pressure_warn_50_to_75_percent():
    """At 50% (1024 MB) and up through < 75% (1536 MB) → warn."""
    assert memory_pressure_label(1024.0) == "warn"
    assert memory_pressure_label(1500.0) == "warn"
    assert memory_pressure_label(1535.99) == "warn"


def test_pressure_crit_at_or_above_75_percent():
    """At 75% (1536 MB) and up → crit."""
    assert memory_pressure_label(1536.0) == "crit"
    assert memory_pressure_label(1900.0) == "crit"
    assert memory_pressure_label(2048.0) == "crit"
    assert memory_pressure_label(3000.0) == "crit"


def test_pressure_with_custom_budget():
    """User can override the budget for testing or different deploys."""
    # 600 MB out of 1000 MB → 60% → warn.
    assert memory_pressure_label(600.0, budget_mb=1000.0) == "warn"
    # 800 MB out of 1000 MB → 80% → crit.
    assert memory_pressure_label(800.0, budget_mb=1000.0) == "crit"


def test_pressure_zero_budget_returns_unknown():
    """Defensive — degenerate budget doesn't divide-by-zero."""
    assert memory_pressure_label(500.0, budget_mb=0.0) == "unknown"
    assert memory_pressure_label(500.0, budget_mb=-1.0) == "unknown"


def test_budget_constant_matches_user_cap():
    """The 2GB number is load-bearing — locked in by test."""
    assert PROCESS_MEMORY_BUDGET_MB == 2048.0
