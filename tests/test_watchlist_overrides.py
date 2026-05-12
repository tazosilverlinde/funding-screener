"""Tests for watchlist live overrides (Round 68)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from funding_screener.background import DataStore


# ---------------- watchlist_include / watchlist_exclude ----------------


def test_include_adds_to_additions():
    s = DataStore()
    s.watchlist_include("BTC")
    adds, rems = s.read_watchlist_overrides()
    assert adds == {"BTC"}
    assert rems == set()


def test_include_case_normalized():
    s = DataStore()
    s.watchlist_include("btc")
    s.watchlist_include("Eth")
    adds, _ = s.read_watchlist_overrides()
    assert adds == {"BTC", "ETH"}


def test_include_strips_whitespace():
    s = DataStore()
    s.watchlist_include("  BTC  ")
    adds, _ = s.read_watchlist_overrides()
    assert adds == {"BTC"}


def test_empty_input_no_op():
    s = DataStore()
    s.watchlist_include("")
    s.watchlist_include("   ")
    s.watchlist_exclude("")
    adds, rems = s.read_watchlist_overrides()
    assert adds == set() and rems == set()


def test_exclude_adds_to_removals():
    s = DataStore()
    s.watchlist_exclude("WIF")
    adds, rems = s.read_watchlist_overrides()
    assert adds == set()
    assert rems == {"WIF"}


def test_include_clears_existing_exclude():
    """A user changing their mind — include should beat a prior exclude."""
    s = DataStore()
    s.watchlist_exclude("BTC")
    s.watchlist_include("BTC")
    adds, rems = s.read_watchlist_overrides()
    assert "BTC" in adds
    assert "BTC" not in rems


def test_exclude_clears_existing_include():
    s = DataStore()
    s.watchlist_include("BTC")
    s.watchlist_exclude("BTC")
    adds, rems = s.read_watchlist_overrides()
    assert "BTC" not in adds
    assert "BTC" in rems


def test_clear_override_removes_from_both():
    s = DataStore()
    s.watchlist_include("BTC")
    s.watchlist_clear_override("BTC")
    adds, rems = s.read_watchlist_overrides()
    assert "BTC" not in adds and "BTC" not in rems

    s.watchlist_exclude("BTC")
    s.watchlist_clear_override("BTC")
    adds, rems = s.read_watchlist_overrides()
    assert "BTC" not in adds and "BTC" not in rems


# ---------------- read_effective_watchlist ----------------


def test_effective_yaml_alone():
    """No overrides → effective == YAML."""
    s = DataStore()
    assert s.read_effective_watchlist({"BTC", "ETH"}) == {"BTC", "ETH"}


def test_effective_empty_when_no_yaml_no_overrides():
    """Empty everything → empty set → 'no filter' downstream."""
    s = DataStore()
    assert s.read_effective_watchlist(set()) == set()


def test_effective_addition_supplements_yaml():
    s = DataStore()
    s.watchlist_include("WIF")
    assert s.read_effective_watchlist({"BTC"}) == {"BTC", "WIF"}


def test_effective_removal_subtracts_from_yaml():
    s = DataStore()
    s.watchlist_exclude("BTC")
    assert s.read_effective_watchlist({"BTC", "ETH"}) == {"ETH"}


def test_effective_addition_only_no_yaml():
    """Adding to an empty YAML set should activate filtering on just that base."""
    s = DataStore()
    s.watchlist_include("PEPE")
    assert s.read_effective_watchlist(set()) == {"PEPE"}


def test_effective_addition_and_removal_combined():
    s = DataStore()
    s.watchlist_include("WIF")
    s.watchlist_exclude("ETH")
    assert s.read_effective_watchlist({"BTC", "ETH"}) == {"BTC", "WIF"}


def test_effective_uppercases_yaml_input_defensively():
    """If a caller hands us lowercase YAML data, effective should still match."""
    s = DataStore()
    s.watchlist_include("WIF")
    assert s.read_effective_watchlist({"btc"}) == {"BTC", "WIF"}


def test_overrides_are_snapshots_not_aliases():
    s = DataStore()
    s.watchlist_include("BTC")
    adds, rems = s.read_watchlist_overrides()
    adds.add("INJECTED")
    fresh_adds, _ = s.read_watchlist_overrides()
    assert "INJECTED" not in fresh_adds


# ---------------- persistence ----------------


def _store_with_persist(path: Path, monkeypatch) -> DataStore:
    monkeypatch.setenv("WATCHLIST_OVERRIDES_PATH", str(path))
    return DataStore()


def test_in_memory_only_when_env_unset(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("WATCHLIST_OVERRIDES_PATH", raising=False)
    s = DataStore()
    s.watchlist_include("BTC")
    # No file created anywhere.
    files = list(tmp_path.glob("*"))
    assert files == []


def test_persists_to_disk(monkeypatch, tmp_path: Path):
    path = tmp_path / "wl.json"
    s = _store_with_persist(path, monkeypatch)
    s.watchlist_include("BTC")
    s.watchlist_exclude("WIF")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "BTC" in data["additions"]
    assert "WIF" in data["removals"]


def test_reload_on_construction(monkeypatch, tmp_path: Path):
    path = tmp_path / "wl.json"
    a = _store_with_persist(path, monkeypatch)
    a.watchlist_include("BTC")
    a.watchlist_exclude("WIF")
    # Simulated restart.
    b = _store_with_persist(path, monkeypatch)
    adds, rems = b.read_watchlist_overrides()
    assert adds == {"BTC"}
    assert rems == {"WIF"}


def test_clear_persists(monkeypatch, tmp_path: Path):
    path = tmp_path / "wl.json"
    s = _store_with_persist(path, monkeypatch)
    s.watchlist_include("BTC")
    s.watchlist_clear_override("BTC")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "BTC" not in data["additions"]


def test_missing_file_is_fine(monkeypatch, tmp_path: Path):
    path = tmp_path / "never_existed.json"
    s = _store_with_persist(path, monkeypatch)
    adds, rems = s.read_watchlist_overrides()
    assert adds == set() and rems == set()


def test_corrupted_json_skipped(monkeypatch, tmp_path: Path):
    path = tmp_path / "wl.json"
    path.write_text("{ not json", encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    adds, rems = s.read_watchlist_overrides()
    assert adds == set() and rems == set()


def test_atomic_write_no_leftover_tmp(monkeypatch, tmp_path: Path):
    path = tmp_path / "wl.json"
    s = _store_with_persist(path, monkeypatch)
    s.watchlist_include("BTC")
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_failure_does_not_crash(monkeypatch, tmp_path: Path):
    path = tmp_path / "wl.json"
    s = _store_with_persist(path, monkeypatch)

    def _broken_write_text(self, *args, **kwargs):
        raise PermissionError("simulated")
    monkeypatch.setattr(Path, "write_text", _broken_write_text)
    s.watchlist_include("BTC")  # should not crash
    adds, _ = s.read_watchlist_overrides()
    assert adds == {"BTC"}  # in-memory still works


def test_invalid_entries_in_file_skipped(monkeypatch, tmp_path: Path):
    """Non-string or empty entries in persisted file should be skipped."""
    path = tmp_path / "wl.json"
    path.write_text(json.dumps({
        "additions": ["BTC", "", "  ", 123, None],
        "removals": ["WIF", []],
    }), encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    adds, rems = s.read_watchlist_overrides()
    assert adds == {"BTC"}
    assert rems == {"WIF"}
