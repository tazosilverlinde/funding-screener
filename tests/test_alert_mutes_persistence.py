"""Tests for alert-mute persistence (Round 66)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from funding_screener.background import DataStore


def _store_with_persist(path: Path, monkeypatch) -> DataStore:
    """Construct a DataStore configured to persist mutes to `path`."""
    monkeypatch.setenv("ALERT_MUTES_PATH", str(path))
    return DataStore()


def test_in_memory_only_when_no_env_var(monkeypatch):
    """Default behavior unchanged — no disk I/O, no file created."""
    monkeypatch.delenv("ALERT_MUTES_PATH", raising=False)
    s = DataStore()
    s.mute_alert("kind:test", hours=1.0)
    assert s.is_alert_muted("test", "test:X") is True


def test_persists_mute_to_disk(monkeypatch, tmp_path: Path):
    mute_file = tmp_path / "mutes.json"
    s = _store_with_persist(mute_file, monkeypatch)
    s.mute_alert("kind:composite", hours=1.0)
    # File should exist with a single entry.
    data = json.loads(mute_file.read_text(encoding="utf-8"))
    assert "kind:composite" in data
    assert data["kind:composite"] > time.time()  # expiry in the future


def test_reloads_mutes_on_construction(monkeypatch, tmp_path: Path):
    """Second DataStore reading the same file picks up the mute."""
    mute_file = tmp_path / "mutes.json"
    a = _store_with_persist(mute_file, monkeypatch)
    a.mute_alert("kind:composite", hours=2.0)
    # Simulate restart: new DataStore reads the same file.
    b = _store_with_persist(mute_file, monkeypatch)
    assert b.is_alert_muted("composite", "composite:BTC/USDT") is True


def test_unmute_persists_to_disk(monkeypatch, tmp_path: Path):
    mute_file = tmp_path / "mutes.json"
    s = _store_with_persist(mute_file, monkeypatch)
    s.mute_alert("kind:A", hours=2.0)
    s.mute_alert("kind:B", hours=2.0)
    s.unmute_alert("kind:A")
    data = json.loads(mute_file.read_text(encoding="utf-8"))
    assert "kind:A" not in data
    assert "kind:B" in data


def test_expired_mutes_dropped_on_reload(monkeypatch, tmp_path: Path):
    """If a mute expired while the process was down, it should NOT come
    back as active on the next startup.
    """
    mute_file = tmp_path / "mutes.json"
    # Write an expired mute directly to the file.
    expired_ts = time.time() - 3600  # 1h ago
    future_ts = time.time() + 3600   # 1h ahead
    mute_file.write_text(
        json.dumps({"kind:expired": expired_ts, "kind:active": future_ts}),
        encoding="utf-8",
    )
    s = _store_with_persist(mute_file, monkeypatch)
    # Only the un-expired one should be active.
    assert s.is_alert_muted("expired", "expired:X") is False
    assert s.is_alert_muted("active", "active:X") is True


def test_expired_mutes_not_written_back(monkeypatch, tmp_path: Path):
    """After a save, the file shouldn't contain entries that have expired."""
    mute_file = tmp_path / "mutes.json"
    s = _store_with_persist(mute_file, monkeypatch)
    # Add a near-expired mute by injecting directly into the dict, then save.
    s.alert_mutes["kind:past"] = time.time() - 10
    s.alert_mutes["kind:future"] = time.time() + 3600
    s._save_mutes_to_disk()
    data = json.loads(mute_file.read_text(encoding="utf-8"))
    assert "kind:past" not in data
    assert "kind:future" in data


def test_missing_file_is_fine(monkeypatch, tmp_path: Path):
    """No existing file → cold start with no mutes."""
    mute_file = tmp_path / "never_existed.json"
    s = _store_with_persist(mute_file, monkeypatch)
    assert s.alert_mutes == {}


def test_corrupted_json_logged_and_skipped(monkeypatch, tmp_path: Path):
    """Garbage in the file shouldn't crash construction."""
    mute_file = tmp_path / "mutes.json"
    mute_file.write_text("{ not valid json", encoding="utf-8")
    s = _store_with_persist(mute_file, monkeypatch)
    # Construction succeeded; no mutes loaded.
    assert s.alert_mutes == {}


def test_non_string_pattern_skipped(monkeypatch, tmp_path: Path):
    """Defensive — a non-string key in the JSON shouldn't crash."""
    mute_file = tmp_path / "mutes.json"
    mute_file.write_text(
        json.dumps({"kind:valid": time.time() + 3600, "123": "not-a-number"}),
        encoding="utf-8",
    )
    s = _store_with_persist(mute_file, monkeypatch)
    assert "kind:valid" in s.alert_mutes
    assert "123" not in s.alert_mutes  # expiry wasn't a float → skipped


def test_write_error_does_not_crash_mute(monkeypatch, tmp_path: Path):
    """If the file becomes unwriteable, mute_alert still updates in-memory."""
    mute_file = tmp_path / "mutes.json"
    s = _store_with_persist(mute_file, monkeypatch)
    # Sabotage write_text.
    def _broken_write_text(self, *args, **kwargs):
        raise PermissionError("simulated")
    monkeypatch.setattr(Path, "write_text", _broken_write_text)
    # Should not crash.
    s.mute_alert("kind:x", hours=1.0)
    # In-memory still updated.
    assert "kind:x" in s.alert_mutes


def test_empty_file_handled(monkeypatch, tmp_path: Path):
    """An empty file (touched but never written) should load as empty mutes."""
    mute_file = tmp_path / "mutes.json"
    mute_file.write_text("", encoding="utf-8")
    s = _store_with_persist(mute_file, monkeypatch)
    assert s.alert_mutes == {}


def test_parent_directory_created(monkeypatch, tmp_path: Path):
    """Path with a missing parent dir — DataStore init should create it."""
    nested = tmp_path / "deep" / "nested" / "mutes.json"
    monkeypatch.setenv("ALERT_MUTES_PATH", str(nested))
    s = DataStore()
    s.mute_alert("kind:x", hours=1.0)
    assert nested.exists()


def test_idempotent_remute_replaces_expiry(monkeypatch, tmp_path: Path):
    """Re-muting the same pattern updates expiry; disk reflects the new value."""
    mute_file = tmp_path / "mutes.json"
    s = _store_with_persist(mute_file, monkeypatch)
    s.mute_alert("kind:x", hours=0.1)
    first_exp = s.alert_mutes["kind:x"]
    s.mute_alert("kind:x", hours=10.0)
    second_exp = s.alert_mutes["kind:x"]
    assert second_exp > first_exp
    # Disk reflects the new expiry.
    data = json.loads(mute_file.read_text(encoding="utf-8"))
    assert data["kind:x"] == second_exp
