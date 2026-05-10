"""Tests for the digest formatters and EmailClient (Round 22)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from funding_screener.digest import (
    format_digest_as_html,
    format_digest_as_text,
)
from funding_screener.notifications import EmailClient


# ---------------- text formatter ----------------


def _sample_digest() -> dict:
    return {
        "market_overview": {
            "total_symbols": 100, "bullish": 12, "bearish": 8,
            "neutral": 80, "strong_bull": 3, "strong_bear": 2,
        },
        "top_longs": [
            {"symbol": "BTCUSDT", "score": 80, "label": "🚀 Strong bull",
             "funding_8h_pct": -1.2},
        ],
        "top_shorts": [
            {"symbol": "WIFUSDT", "score": -60, "label": "🔴 Bearish",
             "funding_8h_pct": 1.8},
        ],
        "top_squeezes": [
            {"symbol": "PEPEUSDT", "side_usd": 25_000_000, "other_usd": 1_000_000, "events_count": 60},
        ],
        "top_cascades": [
            {"symbol": "SOLUSDT", "side_usd": 40_000_000, "other_usd": 2_000_000, "events_count": 100},
        ],
        "whale_highlight": {"emoji": "🐋", "headline": "ARB whale withdrew $50M"},
        "upcoming_unlocks": [
            {"symbol": "APT", "days_until": 5, "date_str": "2026-05-15",
             "amount_usd": 200_000_000, "pct_of_supply": 2.5},
        ],
        "macro": {"emoji": "🟢", "headline": "Stablecoin supply expanding"},
        "sector_winners": [{"sector": "AI", "avg_score": 50, "row_count": 5}],
        "sector_losers": [{"sector": "Memes", "avg_score": -40, "row_count": 8}],
    }


def test_text_format_includes_all_sections_when_data_present():
    out = format_digest_as_text(_sample_digest())
    assert "Daily market digest" in out
    assert "100 symbols tracked" in out
    assert "Top long candidates:" in out
    assert "BTCUSDT" in out
    assert "Top short candidates:" in out
    assert "WIFUSDT" in out
    assert "Top short squeezes" in out
    assert "PEPEUSDT" in out
    assert "Top long cascades" in out
    assert "SOLUSDT" in out
    assert "Whale spotlight" in out
    assert "ARB whale withdrew $50M" in out
    assert "Upcoming unlocks" in out
    assert "APT" in out
    assert "Macro:" in out
    assert "Sector winners:" in out
    assert "Sector laggards:" in out


def test_text_format_omits_empty_sections():
    digest = {"market_overview": {"total_symbols": 0, "bullish": 0, "bearish": 0,
                                  "neutral": 0, "strong_bull": 0, "strong_bear": 0}}
    out = format_digest_as_text(digest)
    assert "Daily market digest" in out
    assert "Top long candidates:" not in out
    assert "Whale spotlight" not in out


def test_text_format_dollar_units():
    """Verify formatting picks B/M/K based on magnitude."""
    digest = {
        "market_overview": {"total_symbols": 1, "bullish": 0, "bearish": 0, "neutral": 1,
                            "strong_bull": 0, "strong_bear": 0},
        "top_squeezes": [
            {"symbol": "BTC", "side_usd": 2_000_000_000, "other_usd": 0, "events_count": 1},
            {"symbol": "ETH", "side_usd": 25_000_000, "other_usd": 0, "events_count": 1},
            {"symbol": "PEPE", "side_usd": 50_000, "other_usd": 0, "events_count": 1},
        ],
    }
    out = format_digest_as_text(digest)
    assert "$2.00B" in out
    assert "$25.0M" in out
    assert "$50K" in out


# ---------------- HTML formatter ----------------


def test_html_format_includes_all_sections():
    out = format_digest_as_html(_sample_digest())
    assert "<h2" in out
    assert "Daily market digest" in out
    assert "BTCUSDT" in out
    assert "WIFUSDT" in out
    assert "PEPEUSDT" in out
    assert "ARB whale" in out
    assert "APT" in out
    assert "🏆 Sector rotation" in out
    assert "Winners:" in out
    assert "Laggards:" in out


def test_html_format_uses_inline_styles_only():
    """Email clients strip <style>/<link> — every style must be inline."""
    out = format_digest_as_html(_sample_digest())
    assert "<style" not in out.lower()
    assert "<link" not in out.lower()
    # Verify at least some inline styling is present
    assert "style=" in out


def test_html_table_emits_rows():
    out = format_digest_as_html(_sample_digest())
    # Each section should produce a <table> or be a structured element
    assert out.count("<table") >= 4  # longs, shorts, squeezes, cascades, unlocks


# ---------------- EmailClient ----------------


def test_email_not_configured_when_env_empty(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    client = EmailClient()
    assert not client.is_configured()


def test_email_configured_when_all_env_set(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_TO", "you@example.com")
    client = EmailClient()
    assert client.is_configured()


def test_email_default_port_is_587(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.setenv(key, "x")
    monkeypatch.delenv("SMTP_PORT", raising=False)
    client = EmailClient()
    assert client._port == 587


def test_email_invalid_port_falls_back_to_587(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("SMTP_PORT", "not-a-number")
    client = EmailClient()
    assert client._port == 587


def test_email_from_falls_back_to_user(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("SMTP_USER", "alice@example.com")
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    client = EmailClient()
    assert client._email_from == "alice@example.com"


@pytest.mark.asyncio
async def test_send_skipped_when_not_configured(monkeypatch):
    """Quiet-skip when env not set — returns True (success / no-op)."""
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    client = EmailClient()
    ok = await client.send_message("subject", "plain", "<p>html</p>")
    assert ok is True


@pytest.mark.asyncio
async def test_send_uses_starttls_for_port_587(monkeypatch):
    """STARTTLS path — calls smtplib.SMTP and starttls()."""
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("SMTP_PORT", "587")
    client = EmailClient()
    fake_smtp = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = fake_smtp
    cm.__exit__.return_value = False
    with patch("funding_screener.notifications.smtplib.SMTP", return_value=cm) as smtp_class:
        ok = await client.send_message("subj", "plain", "<p>html</p>")
    assert ok is True
    smtp_class.assert_called_once()
    fake_smtp.starttls.assert_called_once()
    fake_smtp.login.assert_called_once()
    fake_smtp.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_uses_ssl_for_port_465(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("SMTP_PORT", "465")
    client = EmailClient()
    fake_smtp = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = fake_smtp
    cm.__exit__.return_value = False
    with patch("funding_screener.notifications.smtplib.SMTP_SSL", return_value=cm) as ssl_class:
        ok = await client.send_message("subj", "plain", "<p>html</p>")
    assert ok is True
    ssl_class.assert_called_once()
    # SSL path doesn't call starttls
    fake_smtp.starttls.assert_not_called()
    fake_smtp.login.assert_called_once()


@pytest.mark.asyncio
async def test_send_returns_false_on_smtp_exception(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("SMTP_PORT", "587")
    client = EmailClient()
    with patch(
        "funding_screener.notifications.smtplib.SMTP",
        side_effect=ConnectionRefusedError("no SMTP server"),
    ):
        ok = await client.send_message("subj", "plain", "<p>html</p>")
    assert ok is False


# ---------------- Round 58: compose_system_status_line + footer plumbing ----------------


def test_system_status_line_healthy_state():
    from funding_screener.digest import compose_system_status_line
    line = compose_system_status_line(
        rss_mb=412.0, budget_mb=2048.0,
        n_loops_total=14, n_loops_stalled=0,
        n_recent_errors=0, n_alerts_24h=47,
    )
    assert "🩺" in line
    assert "14 loops healthy" in line
    assert "STALLED" not in line
    assert "412MB" in line
    assert "2,048MB" in line
    assert "0 recent errors" in line
    assert "47 alerts last 24h" in line


def test_system_status_line_with_stall_and_pressure():
    from funding_screener.digest import compose_system_status_line
    line = compose_system_status_line(
        rss_mb=1700.0, budget_mb=2048.0,
        n_loops_total=14, n_loops_stalled=2,
        n_recent_errors=12, n_alerts_24h=89,
    )
    assert "12 loops healthy" in line
    assert "+ 2 STALLED" in line
    assert "1,700MB" in line
    assert "12 recent errors" in line


def test_system_status_line_unknown_rss():
    """When psutil isn't available, RSS should report as unknown rather than 0."""
    from funding_screener.digest import compose_system_status_line
    line = compose_system_status_line(
        rss_mb=None, budget_mb=2048.0,
        n_loops_total=14, n_loops_stalled=0,
        n_recent_errors=0, n_alerts_24h=0,
    )
    assert "RSS unknown" in line


def test_system_status_line_includes_pct():
    """Percent of budget should appear so the user sees relative pressure."""
    from funding_screener.digest import compose_system_status_line
    line = compose_system_status_line(
        rss_mb=1024.0, budget_mb=2048.0,
        n_loops_total=14, n_loops_stalled=0,
        n_recent_errors=0, n_alerts_24h=0,
    )
    assert "50%" in line  # 1024/2048


def test_compose_daily_digest_includes_system_status_when_passed():
    """When the caller passes system_status_line, it lands in the digest dict."""
    from funding_screener.digest import compose_daily_digest
    line = "🩺 System: 14 loops healthy · …"
    digest = compose_daily_digest(combined_rows=[], system_status_line=line)
    assert digest.get("system_status") == line


def test_compose_daily_digest_omits_system_status_when_none():
    """No status line passed → no key in dict (callers can use truthiness)."""
    from funding_screener.digest import compose_daily_digest
    digest = compose_daily_digest(combined_rows=[])
    assert "system_status" not in digest


def test_text_format_appends_system_status_at_end():
    """The footer is appended last so market signals stay first."""
    from funding_screener.digest import format_digest_as_text
    digest = {
        "market_overview": {
            "total_symbols": 1, "bullish": 0, "bearish": 0, "neutral": 1,
            "strong_bull": 0, "strong_bear": 0,
        },
        "system_status": "🩺 System: all good",
    }
    text = format_digest_as_text(digest)
    # Header first, system_status last.
    sys_idx = text.find("🩺 System")
    daily_idx = text.find("Daily market digest")
    assert daily_idx < sys_idx


def test_html_format_renders_system_status_inline():
    from funding_screener.digest import format_digest_as_html
    digest = {
        "market_overview": {
            "total_symbols": 1, "bullish": 0, "bearish": 0, "neutral": 1,
            "strong_bull": 0, "strong_bear": 0,
        },
        "system_status": "🩺 System: all good · 412MB/2048MB",
    }
    html = format_digest_as_html(digest)
    assert "🩺 System: all good" in html
    # Should be inline-styled (no <style>/<link>).
    assert "<style" not in html.lower()
    assert "<link" not in html.lower()
