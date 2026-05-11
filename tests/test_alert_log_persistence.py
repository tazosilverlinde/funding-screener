"""Tests for AlertLog file persistence (Round 62)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from funding_screener.notifications import AlertLog


def test_in_memory_only_when_no_persist_path():
    """Default behavior unchanged — no disk I/O, no file created."""
    log = AlertLog()
    log.record("composite:BTC/USDT", "active", "msg", ())
    assert len(log) == 1


def test_record_appends_to_file_when_persist_path_set(tmp_path: Path):
    log_path = tmp_path / "alerts.jsonl"
    log = AlertLog(persist_path=log_path)
    log.record("composite:BTC/USDT", "active", "BTC rallying", ("telegram",))
    log.record("liq_cascade:ETHUSDT", "resolved", "cleared", ())
    # File should exist with two JSON lines.
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    d1 = json.loads(lines[0])
    d2 = json.loads(lines[1])
    assert d1["key"] == "composite:BTC/USDT"
    assert d1["status"] == "active"
    assert d1["delivered_to"] == ["telegram"]
    assert d2["status"] == "resolved"


def test_replays_from_file_on_construction(tmp_path: Path):
    """A second AlertLog instance reading the same file should see all records."""
    log_path = tmp_path / "alerts.jsonl"
    a = AlertLog(persist_path=log_path)
    a.record("composite:BTC/USDT", "active", "first", ("telegram",))
    a.record("composite:ETH/USDT", "active", "second", ())
    # Simulate restart: new instance reading the same file.
    b = AlertLog(persist_path=log_path)
    out = b.recent()
    keys = {r.key for r in out}
    assert keys == {"composite:BTC/USDT", "composite:ETH/USDT"}


def test_replay_preserves_chronological_order(tmp_path: Path):
    log_path = tmp_path / "alerts.jsonl"
    a = AlertLog(persist_path=log_path)
    for i in range(5):
        a.record(f"k:{i}", "active", f"msg{i}", ())
    b = AlertLog(persist_path=log_path)
    # recent() returns newest-first; the buffer order should be oldest-first.
    msgs_newest_first = [r.message for r in b.recent()]
    assert msgs_newest_first == ["msg4", "msg3", "msg2", "msg1", "msg0"]


def test_replay_caps_at_max_entries(tmp_path: Path):
    """Persistence is unbounded on disk; in-memory replay only loads max_entries."""
    log_path = tmp_path / "alerts.jsonl"
    a = AlertLog(persist_path=log_path)
    for i in range(20):
        a.record(f"k:{i}", "active", f"msg{i}", ())
    b = AlertLog(max_entries=5, persist_path=log_path)
    assert len(b) == 5
    # The TAIL of 5 should be loaded (newest entries kept).
    msgs = [r.message for r in reversed(b.recent())]
    assert msgs == ["msg15", "msg16", "msg17", "msg18", "msg19"]


def test_missing_file_is_fine(tmp_path: Path):
    """No file → empty log, no error."""
    log_path = tmp_path / "does_not_exist.jsonl"
    log = AlertLog(persist_path=log_path)
    assert len(log) == 0


def test_corrupted_lines_are_skipped(tmp_path: Path):
    """A malformed JSON line shouldn't crash replay — it's skipped."""
    log_path = tmp_path / "alerts.jsonl"
    # Write a mix of valid and invalid lines.
    log_path.write_text(
        json.dumps({"fired_at": 1.0, "key": "good:1", "kind": "good",
                    "status": "active", "message": "ok"}) + "\n"
        "this is not json\n"
        + json.dumps({"fired_at": 2.0, "key": "good:2", "kind": "good",
                      "status": "resolved", "message": "ok"}) + "\n",
        encoding="utf-8",
    )
    log = AlertLog(persist_path=log_path)
    keys = {r.key for r in log.recent()}
    assert keys == {"good:1", "good:2"}


def test_missing_required_field_skipped(tmp_path: Path):
    log_path = tmp_path / "alerts.jsonl"
    log_path.write_text(
        json.dumps({"fired_at": 1.0, "key": "k", "status": "active",
                    "message": "ok"}) + "\n",  # missing 'kind'
        encoding="utf-8",
    )
    log = AlertLog(persist_path=log_path)
    assert len(log) == 0


def test_empty_lines_skipped(tmp_path: Path):
    log_path = tmp_path / "alerts.jsonl"
    log_path.write_text(
        "\n"
        + json.dumps({"fired_at": 1.0, "key": "k", "kind": "k",
                      "status": "active", "message": "ok"}) + "\n"
        + "\n\n",
        encoding="utf-8",
    )
    log = AlertLog(persist_path=log_path)
    assert len(log) == 1


def test_parent_directory_created_via_record_when_missing(tmp_path: Path):
    """Persistence path with a missing parent dir — should not crash."""
    log_path = tmp_path / "deep" / "nested" / "alerts.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = AlertLog(persist_path=log_path)
    log.record("k:x", "active", "ok", ())
    assert log_path.exists()


def test_write_failure_does_not_crash_recording(tmp_path: Path, monkeypatch):
    """If the file becomes unwriteable, record() continues to update in-memory."""
    log_path = tmp_path / "alerts.jsonl"
    log = AlertLog(persist_path=log_path)
    # Sabotage file writes — patch Path.open inside the module.
    real_open = Path.open

    def _broken_open(self, *args, **kwargs):
        if "a" in (args[0] if args else kwargs.get("mode", "")):
            raise PermissionError("simulated")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _broken_open)
    log.record("k:x", "active", "ok", ())
    # In-memory still updated.
    assert len(log) == 1


def test_recent_filter_works_with_replayed_records(tmp_path: Path):
    """The kind-filter and limit on recent() should treat replayed records
    the same as in-memory ones.
    """
    log_path = tmp_path / "alerts.jsonl"
    a = AlertLog(persist_path=log_path)
    a.record("composite:BTC/USDT", "active", "1", ())
    a.record("liq:ETHUSDT", "active", "2", ())
    a.record("composite:ETH/USDT", "active", "3", ())
    b = AlertLog(persist_path=log_path)
    only_comp = b.recent(kind="composite")
    assert {r.key for r in only_comp} == {"composite:BTC/USDT", "composite:ETH/USDT"}
