"""Tests for score-history persistence (Round 67)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from funding_screener.background import DataStore


def _store_with_persist(path: Path, monkeypatch) -> DataStore:
    monkeypatch.setenv("SCORE_HISTORY_PATH", str(path))
    return DataStore()


def test_in_memory_only_when_env_unset(monkeypatch):
    """Default — no env var, no disk I/O, no file created."""
    monkeypatch.delenv("SCORE_HISTORY_PATH", raising=False)
    s = DataStore()
    s.snapshot_scores({("BTC", "USDT"): 60})
    assert len(s.score_history) == 1


def test_snapshot_writes_file(monkeypatch, tmp_path: Path):
    path = tmp_path / "scores.json"
    s = _store_with_persist(path, monkeypatch)
    s.snapshot_scores({("BTC", "USDT"): 60, ("ETH", "USDT"): -40})
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"BTC|USDT", "ETH|USDT"}
    # Each entry is a list of [timestamp, score] tuples; one sample each.
    assert len(data["BTC|USDT"]) == 1
    assert data["BTC|USDT"][0][1] == 60


def test_reload_on_construction(monkeypatch, tmp_path: Path):
    path = tmp_path / "scores.json"
    a = _store_with_persist(path, monkeypatch)
    a.snapshot_scores({("BTC", "USDT"): 60})
    a.snapshot_scores({("BTC", "USDT"): 75})
    # New DataStore reads the same file.
    b = _store_with_persist(path, monkeypatch)
    samples = b.score_history.get(("BTC", "USDT"), [])
    assert len(samples) == 2
    assert [int(s) for _ts, s in samples] == [60, 75]


def test_old_samples_dropped_on_reload(monkeypatch, tmp_path: Path):
    """If the file was written more than 24h ago, old samples should be
    trimmed on load — replay shouldn't resurrect stale state.
    """
    path = tmp_path / "scores.json"
    # Write a file with one fresh and one ancient sample.
    fresh_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    ancient_ts = datetime.now(timezone.utc) - timedelta(hours=48)
    path.write_text(json.dumps({
        "BTC|USDT": [
            [ancient_ts.isoformat(), 50],
            [fresh_ts.isoformat(), 70],
        ],
    }), encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    samples = s.score_history.get(("BTC", "USDT"), [])
    assert len(samples) == 1
    assert samples[0][1] == 70


def test_missing_file_is_fine(monkeypatch, tmp_path: Path):
    path = tmp_path / "never_existed.json"
    s = _store_with_persist(path, monkeypatch)
    assert s.score_history == {}


def test_corrupted_file_logged_and_skipped(monkeypatch, tmp_path: Path):
    path = tmp_path / "scores.json"
    path.write_text("{not valid", encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    # Construction succeeded; no history loaded.
    assert s.score_history == {}


def test_atomic_write_uses_tmp_then_replace(monkeypatch, tmp_path: Path):
    """The tmp file should NOT linger after a successful write."""
    path = tmp_path / "scores.json"
    s = _store_with_persist(path, monkeypatch)
    s.snapshot_scores({("BTC", "USDT"): 60})
    # Look for any *.tmp files in the dir — should be none.
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_write_failure_does_not_crash_snapshot(monkeypatch, tmp_path: Path):
    """If the disk goes read-only mid-run, snapshot_scores must keep working
    in-memory.
    """
    path = tmp_path / "scores.json"
    s = _store_with_persist(path, monkeypatch)
    # First write succeeds.
    s.snapshot_scores({("BTC", "USDT"): 60})

    # Sabotage write_text for subsequent writes.
    def _broken_write_text(self, *args, **kwargs):
        raise PermissionError("simulated")
    monkeypatch.setattr(Path, "write_text", _broken_write_text)
    # Should not crash.
    s.snapshot_scores({("BTC", "USDT"): 80})
    # In-memory still updated.
    samples = s.score_history.get(("BTC", "USDT"), [])
    assert len(samples) == 2


def test_malformed_entries_skipped(monkeypatch, tmp_path: Path):
    """A mix of valid and invalid entries on disk → only valid are loaded."""
    path = tmp_path / "scores.json"
    fresh_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    path.write_text(json.dumps({
        "BTC|USDT": [
            [fresh_ts.isoformat(), 60],
            ["not-a-date", 80],
            [fresh_ts.isoformat(), "not-a-number"],
            "totally-wrong",
        ],
    }), encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    samples = s.score_history.get(("BTC", "USDT"), [])
    assert len(samples) == 1
    assert samples[0][1] == 60


def test_keys_without_pipe_skipped(monkeypatch, tmp_path: Path):
    """Defensive — keys not in 'BASE|QUOTE' shape are skipped."""
    path = tmp_path / "scores.json"
    fresh_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    path.write_text(json.dumps({
        "BTC|USDT": [[fresh_ts.isoformat(), 60]],
        "BAD_KEY_NO_PIPE": [[fresh_ts.isoformat(), 80]],
    }), encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    assert ("BTC", "USDT") in s.score_history
    assert len(s.score_history) == 1  # only the valid one


def test_empty_file_handled(monkeypatch, tmp_path: Path):
    path = tmp_path / "scores.json"
    path.write_text("", encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    assert s.score_history == {}


def test_parent_directory_created(monkeypatch, tmp_path: Path):
    nested = tmp_path / "a" / "b" / "scores.json"
    monkeypatch.setenv("SCORE_HISTORY_PATH", str(nested))
    s = DataStore()
    s.snapshot_scores({("BTC", "USDT"): 60})
    assert nested.exists()


def test_naive_timestamps_treated_as_utc(monkeypatch, tmp_path: Path):
    """Timestamps written without tzinfo (legacy or external tools) should
    load as UTC rather than being dropped.
    """
    path = tmp_path / "scores.json"
    naive_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
    path.write_text(json.dumps({
        "BTC|USDT": [[naive_ts.isoformat(), 60]],
    }), encoding="utf-8")
    s = _store_with_persist(path, monkeypatch)
    samples = s.score_history.get(("BTC", "USDT"), [])
    assert len(samples) == 1


def test_unicode_in_keys_preserved(monkeypatch, tmp_path: Path):
    """ensure_ascii=False in the writer should preserve any unicode in base
    asset names (e.g. some Korean memes).
    """
    path = tmp_path / "scores.json"
    s = _store_with_persist(path, monkeypatch)
    s.snapshot_scores({("ASTER", "USDT"): 50})
    raw = path.read_text(encoding="utf-8")
    assert "ASTER" in raw  # ASCII case — sanity
    # Read back via a new store.
    b = _store_with_persist(path, monkeypatch)
    assert ("ASTER", "USDT") in b.score_history
