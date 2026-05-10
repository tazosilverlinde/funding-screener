"""Telegram alerts.

Free Telegram Bot API: requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars.
Setup is documented in `config/alerts.yaml`.

Each alert key is `(alert_type, symbol)`. State is in-memory; we only fire on
transitions (off→on or on→off) and respect a per-key cooldown so oscillating
metrics don't spam.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import yaml

_log = logging.getLogger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org"


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "alerts.yaml"


def load_alerts_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {"alerts": {"enabled": False}}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {"alerts": {"enabled": False}}


@dataclass
class AlertState:
    """In-memory state for de-duplication. Survives across alert-loop iterations."""
    active_keys: set[str] = field(default_factory=set)
    last_fired: dict[str, float] = field(default_factory=dict)  # key -> monotonic time

    def should_fire(self, key: str, cooldown_seconds: float) -> bool:
        """True if this alert isn't currently active AND cooldown has elapsed."""
        if key in self.active_keys:
            return False
        last = self.last_fired.get(key, 0.0)
        return (time.monotonic() - last) >= cooldown_seconds

    def mark_fired(self, key: str) -> None:
        self.active_keys.add(key)
        self.last_fired[key] = time.monotonic()

    def mark_resolved(self, key: str) -> None:
        self.active_keys.discard(key)


class TelegramClient:
    """Minimal Telegram Bot API client. Idempotent setup; safe to construct
    even when env vars aren't set (calls become no-ops)."""

    name = "Telegram"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http is None

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def send(self, text: str) -> bool:
        """Returns True iff the message was actually delivered (or quietly skipped
        when not configured). Returns False on transport failure."""
        if not self.is_configured():
            return True  # silently skipped — not a failure
        url = f"{_TELEGRAM_BASE}/bot{self._token}/sendMessage"
        try:
            r = await self._http.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code != 200:
                _log.warning("Telegram send failed %d: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            _log.warning("Telegram send exception: %s", exc)
            return False


# ---------------- alert evaluators ----------------


def evaluate_funding_alerts(funding_rows, threshold_pct: float) -> list[tuple[str, str, str]]:
    """Return list of (alert_key, status, message) for funding-rate violations.

    status is "active" (currently breaching) or "resolved" (back below threshold).
    The caller decides whether to fire based on AlertState.
    """
    out: list[tuple[str, str, str]] = []
    for r in funding_rows:
        rate = abs(r.rate_8h_norm_percent)
        key = f"funding:{r.exchange}:{r.symbol}"
        if rate > threshold_pct:
            direction = "negative (shorts pay longs)" if r.rate_8h_norm_percent < 0 else "positive (longs pay shorts)"
            msg = (
                f"🚨 *{r.symbol}* on {r.exchange}\n"
                f"Funding rate: `{r.rate_8h_norm_percent:+.4f}% / 8h` — {direction}\n"
                f"Above the {threshold_pct:.1f}% / 8h alert threshold."
            )
            out.append((key, "active", msg))
        else:
            # Resolved transition possible; the loop will check.
            out.append((key, "resolved", f"✅ {r.symbol} funding back to normal ({r.rate_8h_norm_percent:+.4f}%/8h)."))
    return out


def evaluate_composite_alerts(combined_rows, bull_threshold: int, bear_threshold: int) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for r in combined_rows:
        if r.composite_score is None:
            continue
        key = f"composite:{r.base_asset}/{r.quote_asset}"
        score = r.composite_score
        if score >= bull_threshold:
            sym = r.binance_symbol or r.mexc_symbol or r.base_asset
            msg = (
                f"🚀 *{sym}* — composite score `{score:+d}`\n"
                f"{r.composite_emoji} {r.composite_short}\n"
                f"```\n{r.composite_breakdown}\n```"
            )
            out.append((key, "active", msg))
        elif score <= bear_threshold:
            sym = r.binance_symbol or r.mexc_symbol or r.base_asset
            msg = (
                f"💥 *{sym}* — composite score `{score:+d}`\n"
                f"{r.composite_emoji} {r.composite_short}\n"
                f"```\n{r.composite_breakdown}\n```"
            )
            out.append((key, "active", msg))
        else:
            out.append((key, "resolved", f"📉 {r.base_asset}/{r.quote_asset} composite score back to {score:+d}."))
    return out


def evaluate_score_delta_alerts(combined_rows, abs_threshold: int) -> list[tuple[str, str, str]]:
    """Score-Δ-jump alert — fires when 1h score change is large in either direction.

    Catches momentum shifts BEFORE the absolute score crosses the extremes:
    a row that just went from +20 to +60 in 1h is more interesting than one
    that's been at +75 stable for hours.
    """
    out: list[tuple[str, str, str]] = []
    for r in combined_rows:
        delta = getattr(r, "composite_score_delta_1h", None)
        if delta is None:
            continue
        key = f"score_delta:{r.base_asset}/{r.quote_asset}"
        if abs(delta) >= abs_threshold:
            sym = r.binance_symbol or r.mexc_symbol or r.base_asset
            arrow = "🚀 surging" if delta > 0 else "💥 plunging"
            msg = (
                f"{arrow} *{sym}* — composite score Δ `{delta:+d}` in last 1h\n"
                f"Current: `{r.composite_score:+d}` ({r.composite_emoji} {r.composite_short})\n"
                "Momentum shifting fast — check what triggered it."
            )
            out.append((key, "active", msg))
        else:
            out.append((key, "resolved", f"📊 {r.base_asset}/{r.quote_asset} score Δ back below threshold."))
    return out


def evaluate_funding_deviation_alerts(
    combined_rows,
    z_threshold: float = 2.5,
) -> list[tuple[str, str, str]]:
    """Funding-rate-deviation alert — fires on extreme overshoots/undershoots.

    Builds on the funding_deviation_z field added in Round 12. The alert key
    is per (base, quote, direction) so an "extreme overshoot" alert and an
    "extreme undershoot" alert on the same pair are tracked independently —
    a flip from one to the other should re-fire, not be suppressed.

    Direction is encoded in the key so AlertState's resolved-transition logic
    correctly fires both "no longer overshooting" and "now undershooting" on
    a same-row regime flip rather than swallowing it.
    """
    out: list[tuple[str, str, str]] = []
    for r in combined_rows:
        z = getattr(r, "funding_deviation_z", None)
        if z is None:
            continue
        sym = r.binance_symbol or r.mexc_symbol or r.base_asset
        # One key per direction — we track overshoot and undershoot separately.
        for direction, predicate, emoji, label in (
            ("over", z >= z_threshold, "🔥", "extreme overshoot"),
            ("under", z <= -z_threshold, "❄", "extreme undershoot"),
        ):
            key = f"funding_dev:{r.base_asset}/{r.quote_asset}:{direction}"
            if predicate:
                bias = (
                    "Mean-revert candidate (short-funding-side, **fading**)"
                    if direction == "over"
                    else "Mean-revert candidate (long-funding-side, **squeeze setup**)"
                )
                msg = (
                    f"{emoji} *{sym}* — funding {label}\n"
                    f"z-score: `{z:+.1f}σ` vs ~30-period mean\n"
                    f"{bias}"
                )
                out.append((key, "active", msg))
            else:
                out.append((
                    key, "resolved",
                    f"📊 {sym} funding deviation back within ±{z_threshold:.1f}σ ({z:+.1f}σ).",
                ))
    return out


def evaluate_whale_flow_alerts(onchain_flows: list[dict], threshold_usd: float) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for flow in onchain_flows:
        token = flow.get("token", "?")
        net = flow.get("net_usd", 0.0) or 0.0
        key = f"whale:{token}"
        if abs(net) >= threshold_usd:
            direction = "withdrawn FROM exchanges (accumulation)" if net > 0 else "deposited TO exchanges (distribution)"
            top_ex = max(
                (flow.get("by_exchange") or {}).items(),
                key=lambda x: abs(x[1].get("deposits_usd", 0)) + abs(x[1].get("withdrawals_usd", 0)),
                default=("—", {}),
            )[0]
            msg = (
                f"🐳 *{token}* — net `${net:+,.0f}` {direction}\n"
                f"Top exchange: {top_ex.title()}\n"
                f"Deposits ${flow.get('deposits_usd', 0):,.0f} / Withdrawals ${flow.get('withdrawals_usd', 0):,.0f} (24h)"
            )
            out.append((key, "active", msg))
        else:
            out.append((key, "resolved", f"🐳 {token} whale flow back below ${threshold_usd:,.0f}."))
    return out


def evaluate_new_listing_alerts(
    binance_contracts, mexc_contracts, seen_symbols: set[str]
) -> tuple[list[tuple[str, str, str]], set[str]]:
    """Compare current symbol set to `seen_symbols`. Returns (events, updated_set)."""
    current: set[str] = set()
    for c in list(binance_contracts) + list(mexc_contracts):
        current.add(f"{c.exchange}:{c.symbol}")
    new = current - seen_symbols
    out: list[tuple[str, str, str]] = []
    for full in sorted(new):
        exchange, sym = full.split(":", 1)
        msg = f"🆕 New perp listing on *{exchange}*: `{sym}`"
        out.append((f"new_listing:{full}", "active", msg))
    return out, current


def evaluate_unlock_alerts(unlocks_events, days_ahead: int) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for ev in unlocks_events:
        if ev.days_until > days_ahead or ev.days_until < 0:
            continue
        key = f"unlock:{ev.symbol}:{ev.date_str}"
        msg = (
            f"⏳ *{ev.symbol}* unlock in `{ev.days_until}d` ({ev.date_str})\n"
            f"Type: {ev.unlock_type}\n"
            f"Tokens: {ev.amount_tokens:,.0f}"
        )
        if ev.pct_of_supply is not None:
            msg += f" ({ev.pct_of_supply:.2f}% of supply)"
        if ev.notes:
            msg += f"\n_{ev.notes}_"
        out.append((key, "active", msg))
    return out
