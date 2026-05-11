"""Tests for the Telegram rate limiter (Round 61)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_screener.notifications import TelegramClient


def _make_client(max_per_minute: int = 20, configured: bool = True, monkeypatch=None) -> TelegramClient:
    """Build a TelegramClient with mocked HTTP + optional env."""
    if monkeypatch:
        if configured:
            monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
            monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        else:
            monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
            monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    fake_http = AsyncMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = "ok"
    fake_http.post = AsyncMock(return_value=fake_response)
    return TelegramClient(http=fake_http, max_per_minute=max_per_minute)


# ---------------- basic limiter ----------------


@pytest.mark.asyncio
async def test_under_limit_sends_normally(monkeypatch):
    client = _make_client(max_per_minute=5, monkeypatch=monkeypatch)
    for i in range(5):
        ok = await client.send(f"msg {i}")
        assert ok is True
    assert client.dropped_count() == 0


@pytest.mark.asyncio
async def test_over_limit_drops_silently(monkeypatch):
    """Sends past the limit return True (quiet skip) but don't actually post."""
    client = _make_client(max_per_minute=3, monkeypatch=monkeypatch)
    # First 3 succeed.
    for i in range(3):
        ok = await client.send(f"msg {i}")
        assert ok is True
    # 4th should be dropped.
    ok = await client.send("overflow")
    assert ok is True  # quiet skip
    assert client.dropped_count() == 1


@pytest.mark.asyncio
async def test_dropped_does_not_call_http(monkeypatch):
    """When rate-limited, the underlying httpx.post is NOT called."""
    client = _make_client(max_per_minute=2, monkeypatch=monkeypatch)
    await client.send("a")
    await client.send("b")
    # Third dropped.
    await client.send("c")
    # post should have been called exactly twice.
    assert client._http.post.call_count == 2


# ---------------- window pruning ----------------


@pytest.mark.asyncio
async def test_window_prunes_old_timestamps(monkeypatch):
    """Sends older than 60s shouldn't count against the budget."""
    client = _make_client(max_per_minute=3, monkeypatch=monkeypatch)
    # Inject fake old timestamps directly.
    client._send_history = [time.monotonic() - 90.0] * 5
    # Even though history has 5 entries, all are >60s old — should prune
    # and allow new sends.
    for _ in range(3):
        ok = await client.send("fresh")
        assert ok is True
    assert client.dropped_count() == 0


# ---------------- configuration ----------------


def test_configure_rate_limit_updates_cap(monkeypatch):
    client = _make_client(max_per_minute=20, monkeypatch=monkeypatch)
    assert client._max_per_minute == 20
    client.configure_rate_limit(5)
    assert client._max_per_minute == 5


def test_configure_rate_limit_clamps_to_min_1(monkeypatch):
    """Defensive — 0 or negative isn't useful; clamp to 1."""
    client = _make_client(monkeypatch=monkeypatch)
    client.configure_rate_limit(0)
    assert client._max_per_minute == 1
    client.configure_rate_limit(-5)
    assert client._max_per_minute == 1


def test_default_max_per_minute_is_20(monkeypatch):
    """The default in alerts.yaml matches the class default; locked by test."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "x")
    client = TelegramClient()
    assert client._max_per_minute == 20
    assert TelegramClient.DEFAULT_MAX_PER_MINUTE == 20


# ---------------- unconfigured client ----------------


@pytest.mark.asyncio
async def test_unconfigured_client_doesnt_consume_quota(monkeypatch):
    """A client without env vars should NOT use up the rate limit on its
    silent no-ops — otherwise tests would burn the budget too.
    """
    client = _make_client(max_per_minute=2, configured=False, monkeypatch=monkeypatch)
    # 100 sends, all should "succeed" as no-ops.
    for _ in range(100):
        ok = await client.send("noop")
        assert ok is True
    # And no drops counted.
    assert client.dropped_count() == 0


# ---------------- dropped_count semantics ----------------


@pytest.mark.asyncio
async def test_dropped_count_is_cumulative(monkeypatch):
    client = _make_client(max_per_minute=1, monkeypatch=monkeypatch)
    # 1 sent + 4 dropped within the window.
    for _ in range(5):
        await client.send("msg")
    assert client.dropped_count() == 4


@pytest.mark.asyncio
async def test_dropped_count_does_not_reset_when_window_clears(monkeypatch):
    """Lifetime counter, not per-window. Window pruning frees the budget but
    dropped_count keeps climbing as a record of how much suppression there's been.
    """
    client = _make_client(max_per_minute=1, monkeypatch=monkeypatch)
    await client.send("a")
    await client.send("dropped 1")
    assert client.dropped_count() == 1
    # Simulate window passing.
    client._send_history = []
    await client.send("c")  # allowed
    await client.send("dropped 2")
    assert client.dropped_count() == 2
