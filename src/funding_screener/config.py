"""Loads YAML config once at import. Read-only after that."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@lru_cache(maxsize=1)
def settings() -> dict:
    with (_CONFIG_DIR / "settings.yaml").open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def fees() -> dict:
    with (_CONFIG_DIR / "fees.yaml").open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def binance_default_maker_fee() -> float:
    return float(fees()["binance"]["futures_maker"])


def mexc_default_maker_fee() -> float:
    return float(fees()["mexc"]["futures_maker"])


def binance_maker_fee_for(symbol: str, quote: str) -> float:
    """Look up Binance maker fee with this priority:
    per-symbol override > per-quote override > default.
    """
    f = fees()["binance"]
    by_sym = f.get("futures_maker_by_symbol") or {}
    if symbol in by_sym:
        return float(by_sym[symbol])
    by_quote = f.get("futures_maker_by_quote") or {}
    if quote in by_quote:
        return float(by_quote[quote])
    return float(f["futures_maker"])


def binance_taker_fee_for(symbol: str, quote: str) -> float:
    f = fees()["binance"]
    by_sym = f.get("futures_taker_by_symbol") or {}
    if symbol in by_sym:
        return float(by_sym[symbol])
    by_quote = f.get("futures_taker_by_quote") or {}
    if quote in by_quote:
        return float(by_quote[quote])
    return float(f["futures_taker"])
