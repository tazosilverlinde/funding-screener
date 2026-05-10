"""Telegram alerts + email digest delivery.

Telegram (per-event push):
    Free Telegram Bot API: requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars.
    Setup is documented in `config/alerts.yaml`.
    Each alert key is `(alert_type, symbol)`. State is in-memory; we only fire on
    transitions (off→on or on→off) and respect a per-key cooldown so oscillating
    metrics don't spam.

Email (daily digest, Round 22):
    Standard SMTP via stdlib smtplib. Requires SMTP_HOST, SMTP_PORT, SMTP_USER,
    SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO env vars. STARTTLS-style (port 587).
    Used only by the once-a-day digest loop, not per-event alerts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
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


class EmailClient:
    """SMTP client for the daily digest. Stdlib smtplib — no extra deps.

    Reads config from env vars at construction time:
        SMTP_HOST       e.g. "smtp.gmail.com"
        SMTP_PORT       e.g. "587" (STARTTLS) or "465" (SSL); defaults to 587
        SMTP_USER       SMTP auth username
        SMTP_PASSWORD   SMTP auth password (use an app password for Gmail)
        EMAIL_FROM      "From" header; defaults to SMTP_USER
        EMAIL_TO        recipient address (single, comma-separated for many)

    Constructing without env vars is safe — `is_configured()` returns False
    and `send_message()` becomes a no-op. That keeps this safe for users who
    only configured Telegram.
    """

    name = "Email"

    def __init__(self) -> None:
        self._host = os.getenv("SMTP_HOST", "").strip()
        try:
            self._port = int(os.getenv("SMTP_PORT", "587").strip() or 587)
        except ValueError:
            self._port = 587
        self._user = os.getenv("SMTP_USER", "").strip()
        self._password = os.getenv("SMTP_PASSWORD", "").strip()
        self._email_from = (os.getenv("EMAIL_FROM", "") or self._user).strip()
        self._email_to = os.getenv("EMAIL_TO", "").strip()

    def is_configured(self) -> bool:
        return bool(
            self._host and self._user and self._password and self._email_to
        )

    async def aclose(self) -> None:
        # Nothing to clean up — smtplib connections are per-call and short-lived.
        return None

    def _send_sync(self, subject: str, text_body: str, html_body: str) -> bool:
        """Blocking SMTP send. Run via asyncio.to_thread from the event loop."""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._email_from
        msg["To"] = self._email_to
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        try:
            if self._port == 465:
                # Implicit TLS variant.
                with smtplib.SMTP_SSL(self._host, self._port, timeout=30) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
                    smtp.starttls()
                    smtp.login(self._user, self._password)
                    smtp.send_message(msg)
            return True
        except Exception as exc:
            _log.warning("Email send failed: %s", exc)
            return False

    async def send_message(
        self, subject: str, text_body: str, html_body: str,
    ) -> bool:
        """Returns True on success or quiet-skip; False on SMTP error."""
        if not self.is_configured():
            return True  # silently skipped
        return await asyncio.to_thread(
            self._send_sync, subject, text_body, html_body,
        )


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


def evaluate_oi_surge_alerts(
    combined_rows,
    threshold_pct_24h: float = 50.0,
) -> list[tuple[str, str, str]]:
    """Open Interest 24h surge alert (Round 20).

    Fires when |OI Δ over 24h| crosses `threshold_pct_24h`. The direction is
    encoded in the message — surges UP usually mean fresh leverage entering
    (continuation potential, also liquidation risk if it goes wrong); surges
    DOWN mean rapid unwind (squeeze relief, capitulation, or liquidation
    cascade aftermath).

    Reads `binance_oi_change_24h_pct` from the combined screener rows so
    we don't refetch — the enrichment loop already populated it.

    Was configured in alerts.yaml since the alerts system shipped but had
    no evaluator until this round.
    """
    out: list[tuple[str, str, str]] = []
    for r in combined_rows:
        oi_pct = getattr(r, "binance_oi_change_24h_pct", None)
        if oi_pct is None:
            continue
        sym = r.binance_symbol or r.mexc_symbol or r.base_asset
        key = f"oi_surge:{r.base_asset}/{r.quote_asset}"
        if abs(oi_pct) >= threshold_pct_24h:
            if oi_pct > 0:
                emoji = "📈"
                direction = "Fresh leverage entering — continuation possible, but high-liq risk if move reverses"
            else:
                emoji = "📉"
                direction = "Rapid unwind — short squeeze relief, capitulation, or post-cascade settlement"
            msg = (
                f"{emoji} *{sym}* — OI Δ24h `{oi_pct:+.1f}%`\n"
                f"{direction}"
            )
            out.append((key, "active", msg))
        else:
            out.append((
                key, "resolved",
                f"📊 {sym} OI 24h Δ back to {oi_pct:+.1f}% (under {threshold_pct_24h:.0f}% threshold).",
            ))
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


def evaluate_liquidation_cascade_alerts(
    stats_by_symbol: dict[str, dict],
    cascade_threshold_usd: float = 50_000_000,
    single_threshold_usd: float = 10_000_000,
) -> list[tuple[str, str, str]]:
    """Liquidation-cascade + big-single-event alerts (Round 15).

    Two distinct signals on each symbol — tracked separately so they don't
    suppress one another:

      - cascade: total liquidated $ in the aggregation window exceeds
        `cascade_threshold_usd`. Direction (long/short dominant) is encoded
        in the message but NOT the key — a cascade is a cascade regardless of
        which side dominated; if it flips direction we'd want a fresh ping.
      - single: the biggest single liquidation in the window exceeds
        `single_threshold_usd`. Catches one whale getting blown out even
        when total is otherwise quiet.

    Caller passes the per-symbol stats dict from
    `LiquidationsBuffer.aggregate_all()`; we don't fetch anything here.
    """
    out: list[tuple[str, str, str]] = []
    for symbol, stats in (stats_by_symbol or {}).items():
        total = stats.get("total_usd", 0.0) or 0.0
        long_usd = stats.get("long_liq_usd", 0.0) or 0.0
        short_usd = stats.get("short_liq_usd", 0.0) or 0.0
        biggest = stats.get("biggest_single_usd", 0.0) or 0.0
        biggest_side = stats.get("biggest_single_side") or "—"

        cascade_key = f"liq_cascade:{symbol}"
        if total >= cascade_threshold_usd:
            if long_usd > short_usd:
                bias = "🔴 *Longs liquidated dominantly* — typically follows a sharp drop"
            elif short_usd > long_usd:
                bias = "🟢 *Shorts liquidated dominantly* — squeeze in progress"
            else:
                bias = "🟡 Balanced cascade"
            msg = (
                f"💥 *{symbol}* — liquidation cascade\n"
                f"Total: `${total / 1e6:,.1f}M`  |  Longs: `${long_usd / 1e6:,.1f}M`  "
                f"|  Shorts: `${short_usd / 1e6:,.1f}M`\n"
                f"{bias}"
            )
            out.append((cascade_key, "active", msg))
        else:
            out.append((
                cascade_key, "resolved",
                f"📊 {symbol} liquidation cascade subsided (${total / 1e6:.1f}M total).",
            ))

        single_key = f"liq_single:{symbol}"
        if biggest >= single_threshold_usd:
            who = "long" if biggest_side == "long" else "short"
            msg = (
                f"💣 *{symbol}* — single big liquidation\n"
                f"`${biggest / 1e6:,.1f}M` {who} position blown out\n"
                "One trader, possibly a fund, got force-closed."
            )
            out.append((single_key, "active", msg))
        else:
            out.append((single_key, "resolved", f"📊 {symbol} no large single-liq events."))
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
