"""Tests for sector classification + symbol-search helpers (the resolver
logic; the sidebar widget itself isn't unit-testable without Streamlit)."""

from __future__ import annotations

import pytest

from funding_screener.sectors import (
    all_sectors,
    sector_for,
    symbols_in_sector,
)


def test_known_layer1_assignment():
    assert sector_for("BTC") == "layer1"
    assert sector_for("ETH") == "layer1"
    assert sector_for("SOL") == "layer1"


def test_known_meme_assignment():
    assert sector_for("PEPE") == "meme"
    assert sector_for("WIF") == "meme"


def test_known_defi_assignment():
    assert sector_for("UNI") == "defi"
    assert sector_for("AAVE") == "defi"


def test_unknown_returns_none():
    assert sector_for("DEFINITELYNOTATOKEN") is None
    assert sector_for("") is None
    assert sector_for(None) is None


def test_case_insensitive_lookup():
    assert sector_for("btc") == sector_for("BTC")
    assert sector_for("Pepe") == sector_for("PEPE")


def test_all_sectors_returns_sorted_list():
    s = all_sectors()
    assert isinstance(s, list)
    assert s == sorted(s)
    # Smoke check: at least the major buckets are present.
    for required in ["layer1", "defi", "meme"]:
        assert required in s


def test_symbols_in_sector_returns_uppercase_set():
    layer1 = symbols_in_sector("layer1")
    assert isinstance(layer1, set)
    assert "BTC" in layer1
    assert all(s == s.upper() for s in layer1)


def test_symbols_in_unknown_sector_is_empty():
    assert symbols_in_sector("nonexistent_sector_xyz") == set()


def test_http_client_kwargs_returns_useful_config():
    """http_client_kwargs() should return a dict that httpx.AsyncClient accepts
    and that has the expected timeout / pool sizing."""
    import httpx
    from funding_screener.config import http_client_kwargs

    kwargs = http_client_kwargs()
    assert "timeout" in kwargs
    assert "limits" in kwargs
    # Should construct without errors.
    client = httpx.AsyncClient(**kwargs)
    # Timeout: read >= 15s (sensible production value), connect ~5s
    timeout = kwargs["timeout"]
    assert timeout.read is None or timeout.read >= 15
    assert timeout.connect is None or timeout.connect <= 10
    import asyncio
    asyncio.get_event_loop().run_until_complete(client.aclose())
