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


@dataclass(frozen=True)
class AlertFireRecord:
    """One row in the audit log — what fired, when, where, and to whom.

    `kind` derives from the alert key prefix (e.g. "composite", "liq_cascade",
    "funding_dev:over"). `delivered_to` lists which channels actually accepted
    the message (Telegram, email digest, etc.) — empty list means evaluator
    fired but every channel was unconfigured / errored.
    """
    fired_at: float                 # unix seconds
    key: str                        # full alert key (e.g. "composite:BTC/USDT")
    kind: str                       # category derived from key prefix
    status: str                     # "active" | "resolved"
    message: str
    delivered_to: tuple[str, ...]   # ("telegram",) etc.


class AlertLog:
    """Bounded in-memory audit log of alert fires, with optional append-only
    persistence to disk (Round 62).

    Producer: the alerts loop calls record() after a successful (or attempted)
    send. Consumer: the Alerts Log page reads recent() to render history.
    Bounded at `max_entries` (default 500) — far more than a user reads, but
    cheap memory-wise and gives a meaningful 24-72h tail of activity.

    Persistence (Round 62, opt-in):
      When persist_path is set, every record() also appends one JSON line to
      the file. On construction, the file is read and the last `max_entries`
      lines are replayed into the in-memory buffer so the audit log survives
      restarts. Append-only by design — never rotates, never deletes; users
      who want to trim can rotate the file externally (logrotate, etc.).
      Designed for ephemeral-disk environments: missing file is fine,
      corrupted lines are skipped, write errors don't crash recording.
    """

    def __init__(
        self, max_entries: int = 500, persist_path: Optional[Path] = None,
    ) -> None:
        self._max = max_entries
        self._buf: list[AlertFireRecord] = []
        self._persist_path = persist_path
        if persist_path is not None:
            self._replay_from_disk()

    def _replay_from_disk(self) -> None:
        """Load the tail of the on-disk log into the in-memory buffer.

        Idempotent: safe to call multiple times. Missing file is fine.
        Corrupted JSON lines are skipped with a debug log; we don't bail
        because one bad line shouldn't lose the rest.
        """
        import json
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            lines = self._persist_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            _log.warning("AlertLog: failed to read %s: %s", self._persist_path, exc)
            return
        # Walk only the tail (cap-sized) to avoid loading a huge file into
        # memory just to discard most of it.
        for line in lines[-self._max:]:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                self._buf.append(AlertFireRecord(
                    fired_at=float(d["fired_at"]),
                    key=str(d["key"]),
                    kind=str(d["kind"]),
                    status=str(d["status"]),
                    message=str(d["message"]),
                    delivered_to=tuple(d.get("delivered_to") or ()),
                ))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                _log.debug("AlertLog: skipping malformed line: %s", exc)

    def _append_to_disk(self, rec: AlertFireRecord) -> None:
        """Write one record as a JSON line. Best-effort — write errors are
        logged but don't propagate (alert recording must never fail).
        """
        import json
        if self._persist_path is None:
            return
        try:
            line = json.dumps({
                "fired_at": rec.fired_at,
                "key": rec.key,
                "kind": rec.kind,
                "status": rec.status,
                "message": rec.message,
                "delivered_to": list(rec.delivered_to),
            }, ensure_ascii=False)
            with self._persist_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:
            _log.warning("AlertLog: failed to append to %s: %s", self._persist_path, exc)

    def record(
        self, key: str, status: str, message: str, delivered_to: tuple[str, ...] = (),
    ) -> None:
        kind = key.split(":", 1)[0] if ":" in key else key
        rec = AlertFireRecord(
            fired_at=time.time(),
            key=key,
            kind=kind,
            status=status,
            message=message,
            delivered_to=delivered_to,
        )
        self._buf.append(rec)
        # Trim oldest when over cap. List append + slice is O(N) but N≤500 so it's fine.
        if len(self._buf) > self._max:
            self._buf = self._buf[-self._max:]
        self._append_to_disk(rec)

    def recent(self, limit: int = 50, kind: Optional[str] = None) -> list[AlertFireRecord]:
        """Return the most recent entries (newest first). Optionally filter by kind."""
        items = list(reversed(self._buf))
        if kind:
            items = [e for e in items if e.kind == kind]
        return items[:limit]

    def kinds(self) -> list[str]:
        """Distinct kinds currently in the log — used to populate filter UI."""
        seen: dict[str, None] = {}
        for e in self._buf:
            seen[e.kind] = None
        return list(seen.keys())

    def __len__(self) -> int:
        return len(self._buf)


class TelegramClient:
    """Minimal Telegram Bot API client with a sliding-window rate limiter
    (Round 61). Idempotent setup; safe to construct even when env vars
    aren't set (calls become no-ops).

    Rate limiter: Telegram's bot API allows ~30 msg/sec to a single chat
    before triggering 429 / temporary suspension on the bot account. With
    14 alert kinds × ~200 pairs × cooldown windows, a volatile market can
    plausibly burst 100+ messages in a minute. The default cap of 20/min
    keeps us well under the suspension threshold; overflow messages are
    DROPPED (logged as warnings) rather than queued — queueing creates
    backpressure that makes alerts go stale.
    """

    name = "Telegram"
    DEFAULT_MAX_PER_MINUTE: int = 20

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        max_per_minute: int | None = None,
    ) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http is None
        self._max_per_minute = (
            max_per_minute if max_per_minute is not None else self.DEFAULT_MAX_PER_MINUTE
        )
        # Sliding-window timestamps of recent sends. Pruned on every check.
        self._send_history: list[float] = []
        self._dropped_count: int = 0

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def configure_rate_limit(self, max_per_minute: int) -> None:
        """Allow alerts.yaml override at runtime — called from the alerts loop
        once per cycle so changes apply without restart.
        """
        self._max_per_minute = max(1, int(max_per_minute))

    def dropped_count(self) -> int:
        """Total messages dropped due to rate limiting since process start.
        Surfaced on the System Health page so operators see suppression."""
        return self._dropped_count

    def _check_rate_limit(self) -> bool:
        """Returns True if we're allowed to send right now. Side effect:
        prunes timestamps older than 60s from history.
        """
        now = time.monotonic()
        # Drop entries older than 60s (sliding window).
        self._send_history = [t for t in self._send_history if (now - t) < 60.0]
        if len(self._send_history) >= self._max_per_minute:
            return False
        self._send_history.append(now)
        return True

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def send(self, text: str) -> bool:
        """Returns True iff the message was actually delivered (or quietly skipped
        when not configured / rate-limited). Returns False on transport failure.

        Rate-limited messages are silently dropped (with a log line). Returning
        True for "rate-limited" matches the existing "skipped because
        unconfigured" semantics — both mean "didn't reach Telegram but no
        operational error".
        """
        if not self.is_configured():
            return True  # silently skipped — not a failure
        if not self._check_rate_limit():
            self._dropped_count += 1
            _log.warning(
                "Telegram rate limit hit (%d/min); dropping message. "
                "Lifetime dropped: %d",
                self._max_per_minute, self._dropped_count,
            )
            return True
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


def format_alert_summary_digest(
    fire_records: list,
    interval_minutes: int,
    max_per_kind: int = 5,
) -> str:
    """Build a compact Telegram-Markdown digest from a list of AlertFireRecord.

    Round 50. Used by the periodic-summary loop to compress N raw fires into
    one batched message: header + one section per alert kind, each with up
    to `max_per_kind` symbols. Returns "" when there are no fires (caller
    can use truthiness as a "skip send" guard).

    Section format per kind:
        *🚀 composite (3):*  BTC/USDT (active +85), ETH/USDT (active +75), …

    Section heading uses the kind name AS-IS — caller can rely on the fact
    that 'composite' / 'liq_cascade' / 'fresh' etc. show up consistently
    across messages, which makes the digest greppable in chat.
    """
    if not fire_records:
        return ""

    # Group by kind; preserve insertion order so first-seen kind shows first.
    by_kind: dict[str, list] = {}
    for rec in fire_records:
        by_kind.setdefault(rec.kind, []).append(rec)

    lines: list[str] = []
    lines.append(
        f"📋 *Alert summary — last {interval_minutes} min* "
        f"({len(fire_records)} fire{'s' if len(fire_records) != 1 else ''})"
    )
    for kind, recs in by_kind.items():
        # Compact one-line per fire: "BASE/QUOTE (status)" or "SYMBOL (status)"
        # depending on key shape. We just strip the kind prefix off the key.
        compact_items: list[str] = []
        for r in recs[:max_per_kind]:
            # Key format: "kind:BASE/QUOTE" or "kind:SYMBOL" or "kind:BASE/QUOTE:DIRECTION"
            subject = r.key.split(":", 1)[1] if ":" in r.key else r.key
            status_short = "✅" if r.status == "resolved" else "🚨"
            compact_items.append(f"{subject} {status_short}")
        more = len(recs) - max_per_kind
        suffix = f", +{more} more" if more > 0 else ""
        lines.append(f"*{kind} ({len(recs)}):* {', '.join(compact_items)}{suffix}")
    return "\n".join(lines)


def parse_watchlist(raw: list | None) -> set[str]:
    """Normalize a raw YAML watchlist into an uppercase set of tickers.

    Tolerates None, non-string entries, and mixed case. Empty/None input → empty set.
    """
    if not raw:
        return set()
    out: set[str] = set()
    for s in raw:
        if isinstance(s, str) and s.strip():
            out.add(s.strip().upper())
    return out


def filter_rows_by_watchlist(rows, watchlist: set[str]) -> list:
    """Return only rows whose base_asset is in the watchlist (uppercase match).

    Empty watchlist → return rows unchanged (no filtering, all rows eligible).
    Used by the alerts loop to gate per-symbol evaluators on a user-curated
    list. Sector-rotation alerts intentionally bypass this filter.
    """
    if not watchlist:
        return list(rows or [])
    return [
        r for r in (rows or [])
        if (getattr(r, "base_asset", "") or "").upper() in watchlist
    ]


def filter_funding_rows_by_watchlist(funding_rows, watchlist: set[str]) -> list:
    """Filter raw FundingRow list (different shape from CombinedFundingRow)
    so the funding_rate threshold alert respects watchlist too.
    """
    if not watchlist:
        return list(funding_rows or [])
    return [
        r for r in (funding_rows or [])
        if (getattr(r, "base_asset", "") or "").upper() in watchlist
    ]


def filter_liq_stats_by_watchlist(
    liq_stats_by_symbol: dict, watchlist: set[str],
) -> dict:
    """Filter symbol-keyed liquidation stats. Symbol shape is e.g. 'BTCUSDT' —
    we strip the quote suffix to match the base-only watchlist. Case-insensitive
    on the input symbol so a defensive 'btcusdt' is normalized.
    """
    if not watchlist:
        return dict(liq_stats_by_symbol or {})
    out: dict = {}
    for sym, stats in (liq_stats_by_symbol or {}).items():
        sym_upper = (sym or "").upper()
        for q in ("USDT", "USDC", "BUSD"):
            if sym_upper.endswith(q):
                base = sym_upper[: -len(q)]
                if base in watchlist:
                    out[sym] = stats
                break
    return out


def _build_thesis_block_for_row(row) -> str:
    """Compose the auto-thesis from a row's optional fields and format it as
    Telegram Markdown. Returns "" when no reasons or risks fire.

    Shared between every row-based alert evaluator that wants to enrich its
    Telegram message — composite, score-delta, funding-deviation, oi-surge.
    Uses getattr so older row shapes (e.g. test fixtures missing the new
    fields) still work without forcing a model migration.
    """
    from .thesis import compose_trade_thesis, format_thesis_for_telegram
    sym = row.binance_symbol or row.mexc_symbol or row.base_asset
    thesis = compose_trade_thesis(
        symbol=sym,
        composite_score=getattr(row, "composite_score", None),
        funding_8h_norm_pct=getattr(row, "binance_rate_8h_norm_percent", None)
                            or getattr(row, "mexc_rate_8h_norm_percent", None),
        mark_index_spread_pct=getattr(row, "binance_mark_index_spread_percent", None),
        oi_change_24h_pct=getattr(row, "binance_oi_change_24h_pct", None),
        ls_ratio_global=getattr(row, "binance_ls_ratio_global", None),
        ls_ratio_top=getattr(row, "binance_ls_ratio_top", None),
        funding_deviation_z=getattr(row, "funding_deviation_z", None),
        signal_age_hours=getattr(row, "signal_age_hours", None),
        score_stddev_24h=getattr(row, "composite_score_stddev_24h", None),
        setup_quality_label=getattr(row, "setup_quality_label", None),
    )
    return format_thesis_for_telegram(thesis)


def evaluate_composite_alerts(
    combined_rows,
    bull_threshold: int,
    bear_threshold: int,
    per_pair: Optional[dict[str, dict]] = None,
) -> list[tuple[str, str, str]]:
    """Composite-score alert evaluator. Round 38: embeds the auto-thesis in the
    message body so users get the bull/bear/risk breakdown without opening the app.

    Round 69: Per-pair threshold overrides. Some pairs have different normal
    score ranges (BTC tends to top out around +50; altcoins can hit +85
    easily). Global defaults are too noisy for high-vol pairs and too tight
    for low-vol ones. `per_pair` maps SYMBOL (e.g. 'BTCUSDT', 'WIFUSDT') to a
    dict {'bullish_threshold': int, 'bearish_threshold': int}; missing keys
    fall back to the global defaults.

    Lookup tries the row's binance_symbol first, then mexc_symbol, then
    base_asset — first match wins. This lets users key either by full
    futures symbol or just by base ticker.
    """
    per_pair = per_pair or {}
    out: list[tuple[str, str, str]] = []
    for r in combined_rows:
        if r.composite_score is None:
            continue
        key = f"composite:{r.base_asset}/{r.quote_asset}"
        score = r.composite_score
        sym = r.binance_symbol or r.mexc_symbol or r.base_asset

        # Resolve effective thresholds. First match wins; falls back to defaults.
        eff_bull = bull_threshold
        eff_bear = bear_threshold
        for candidate in (r.binance_symbol, r.mexc_symbol, r.base_asset):
            if candidate and candidate in per_pair:
                override = per_pair[candidate]
                eff_bull = int(override.get("bullish_threshold", bull_threshold))
                eff_bear = int(override.get("bearish_threshold", bear_threshold))
                break

        if score >= eff_bull or score <= eff_bear:
            head_emoji = "🚀" if score >= eff_bull else "💥"
            thesis_block = _build_thesis_block_for_row(r)
            header = (
                f"{head_emoji} *{sym}* — composite score `{score:+d}`\n"
                f"{r.composite_emoji} {r.composite_short}"
            )
            msg = header + (f"\n\n{thesis_block}" if thesis_block else "")
            out.append((key, "active", msg))
        else:
            out.append((key, "resolved", f"📉 {r.base_asset}/{r.quote_asset} composite score back to {score:+d}."))
    return out


def evaluate_score_delta_alerts(combined_rows, abs_threshold: int) -> list[tuple[str, str, str]]:
    """Score-Δ-jump alert — fires when 1h score change is large in either direction.

    Catches momentum shifts BEFORE the absolute score crosses the extremes:
    a row that just went from +20 to +60 in 1h is more interesting than one
    that's been at +75 stable for hours. Round 39: also embeds the thesis so
    users see the case-for/case-against alongside the delta.
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
            thesis_block = _build_thesis_block_for_row(r)
            header = (
                f"{arrow} *{sym}* — composite score Δ `{delta:+d}` in last 1h\n"
                f"Current: `{r.composite_score:+d}` ({r.composite_emoji} {r.composite_short})\n"
                "Momentum shifting fast — check what triggered it."
            )
            msg = header + (f"\n\n{thesis_block}" if thesis_block else "")
            out.append((key, "active", msg))
        else:
            out.append((key, "resolved", f"📊 {r.base_asset}/{r.quote_asset} score Δ back below threshold."))
    return out


def suppress_overlapping_alerts(
    events: list[tuple[str, str, str]],
    overlap_groups: Optional[list[list[str]]] = None,
) -> list[tuple[str, str, str]]:
    """De-duplicate active alerts across overlapping kinds (Round 63).

    When a pair (BTC/USDT, ETH/USDT, ...) experiences a regime change like
    "score jumped from +20 to +75 in the last hour", THREE evaluators fire:

      - composite (score crossed +70)
      - score_delta (|Δ 1h| ≥ 25)
      - fresh_setup (Fresh bull + score ≥ 70)

    All three describe the SAME underlying event. Sending three Telegram
    messages for one regime change is alert fatigue.

    This helper takes the assembled events list and, within each `overlap_group`,
    keeps only the highest-priority kind for each subject (base/quote pair or
    symbol — extracted from the alert key). Order in `overlap_group` IS the
    priority: earlier kinds win over later.

    RESOLVED events are NEVER suppressed — each kind's state machine needs
    to resolve independently, otherwise stuck-active flags persist across
    regime changes.

    Default groups:
      - fresh_setup → composite → score_delta (regime-change kinds)
    Alerts outside any group (liq_cascade, funding_rate, oi_surge, whale_flow,
    sector_rotation, etc.) are independent — never suppressed.

    Input/output shape: list of (key, status, message) tuples.
    """
    if not events:
        return []
    if overlap_groups is None:
        overlap_groups = [["fresh", "composite", "score_delta"]]

    # Build a kind → group lookup for fast membership tests.
    kind_to_group: dict[str, int] = {}
    for group_idx, kinds in enumerate(overlap_groups):
        for kind in kinds:
            kind_to_group[kind] = group_idx
    # Within each group: kind → priority index (lower = higher priority).
    kind_priority: dict[str, int] = {}
    for kinds in overlap_groups:
        for prio, kind in enumerate(kinds):
            kind_priority[kind] = prio

    def _kind(key: str) -> str:
        return key.split(":", 1)[0] if ":" in key else key

    def _subject(key: str) -> str:
        """Extract the symbol/pair part of the key — what's AFTER the kind."""
        if ":" not in key:
            return key
        # Some keys have direction suffix like 'funding_dev:BTC/USDT:over'.
        # Subject is everything between first ':' and (if present) second ':'.
        parts = key.split(":", 2)
        return parts[1] if len(parts) >= 2 else key

    # Pass 1: collect active events per (group, subject) and find the
    # highest-priority kind for each.
    best_by_subject: dict[tuple[int, str], str] = {}
    for key, status, _msg in events:
        if status != "active":
            continue
        kind = _kind(key)
        if kind not in kind_to_group:
            continue
        group_idx = kind_to_group[kind]
        subj = _subject(key)
        slot = (group_idx, subj)
        if slot not in best_by_subject or kind_priority[kind] < kind_priority[best_by_subject[slot]]:
            best_by_subject[slot] = kind

    # Pass 2: emit events. Suppress active events in an overlap group whose
    # kind is NOT the best for their (group, subject). Pass everything else
    # through unchanged.
    out: list[tuple[str, str, str]] = []
    for key, status, msg in events:
        if status != "active":
            out.append((key, status, msg))
            continue
        kind = _kind(key)
        if kind not in kind_to_group:
            out.append((key, status, msg))
            continue
        slot = (kind_to_group[kind], _subject(key))
        if best_by_subject.get(slot) == kind:
            out.append((key, status, msg))
        # else: suppressed — same subject already covered by higher-priority kind
    return out


def _error_category(msg: str) -> str:
    """Extract a coarse category from an error message for grouping (Round 57).

    The funding_screener convention is `<source>: <type>: <details>` where
    <source> is one of fast-loop, slow-loop, market-caps, enrichment,
    macro, onchain[chain], alerts: <evaluator>, etc. We take everything
    before the first ': ' as the category — captures meaningful grouping
    without falling for variable detail strings (which have IDs/values).

    Defensive: empty/None → "unknown"; messages without a colon → first
    50 chars.
    """
    if not msg:
        return "unknown"
    head = msg.split(": ", 1)[0]
    return head[:50] if head else "unknown"


def evaluate_error_pattern_alert(
    recent_errors,
    window_minutes: int = 30,
    repeat_threshold: int = 5,
) -> list[tuple[str, str, str]]:
    """Error-pattern alert (Round 57).

    Groups recent errors by category (everything before the first ': '),
    fires when ANY category has accumulated `repeat_threshold` or more
    occurrences within the last `window_minutes`. Single key per category
    so the state machine handles transitions — one message when a category
    goes hot, one when it clears.

    Why per-category vs simple count: a busy market produces transient RPC
    errors across many sources; aggregate count would always trip. A SUSTAINED
    error from a SPECIFIC source means real subsystem trouble. A score-history
    loop that crashes 10 times in a row is meaningfully different from 10
    different errors from 10 different evaluators.

    Inputs:
      recent_errors: list of (datetime_utc, message_str)
      window_minutes: lookback window
      repeat_threshold: per-category fire threshold

    Returns one (key, status, message) per category SEEN in the window.
    Categories not seen this window don't appear at all (no resolved
    flooding for categories that aren't even firing).
    """
    if not recent_errors:
        return []
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0, window_minutes))
    by_cat: dict[str, list[str]] = {}
    for ts, msg in recent_errors:
        try:
            if ts < cutoff:
                continue
        except TypeError:
            continue  # malformed timestamp, skip
        cat = _error_category(msg)
        by_cat.setdefault(cat, []).append(msg)

    out: list[tuple[str, str, str]] = []
    for cat, msgs in by_cat.items():
        key = f"error_pattern:{cat}"
        n = len(msgs)
        if n >= repeat_threshold:
            # Show the latest message in the alert so the user has a concrete
            # example, not just a count.
            latest = msgs[-1]
            preview = (latest[:200] + "…") if len(latest) > 200 else latest
            out.append((
                key, "active",
                f"⚠️ *Error pattern* — `{cat}` repeated `{n}` times in "
                f"the last {window_minutes} min.\n"
                f"Latest: `{preview}`\n"
                "Recurring same-source error suggests a sustained subsystem "
                "issue, not transient noise. Investigate that loop's recent activity."
            ))
        else:
            out.append((
                key, "resolved",
                f"📊 Error pattern `{cat}` back below threshold "
                f"({n} occurrences in last {window_minutes} min).",
            ))
    return out


def evaluate_loop_stall_alert(
    last_loop_ran_at,
    expected_intervals_seconds: dict[str, float],
    stall_multiplier: float = 3.0,
) -> list[tuple[str, str, str]]:
    """Loop-stall alert (Round 55).

    For each loop in `expected_intervals_seconds`, checks whether more than
    `stall_multiplier × interval` seconds have elapsed since its last cycle
    completion. If so, fires an alert; otherwise resolves.

    Per-loop key (`loop_stall:<loop_name>`) — independent state machine per
    loop so a stalled fast loop doesn't suppress an alert for the macro loop.

    Loops that have NEVER recorded a cycle (not yet started, just deployed)
    are skipped — we can't tell stalled from not-yet-warmed-up.

    Loops that we don't have an expected interval for are also skipped (no
    alert vs false-positive trade-off).
    """
    import time as _t
    out: list[tuple[str, str, str]] = []
    if not last_loop_ran_at or not expected_intervals_seconds:
        return out
    now_utc_ts = _t.time()
    # last_loop_ran_at values are datetime; convert to unix seconds.
    for loop_name, interval_s in expected_intervals_seconds.items():
        ran_at = last_loop_ran_at.get(loop_name)
        if ran_at is None:
            continue
        try:
            ran_at_ts = ran_at.timestamp()
        except (AttributeError, TypeError):
            continue
        elapsed_s = now_utc_ts - ran_at_ts
        threshold_s = float(interval_s) * float(stall_multiplier)
        key = f"loop_stall:{loop_name}"
        if elapsed_s >= threshold_s:
            msg = (
                f"⚠️ *Loop stalled* — `{loop_name}` hasn't completed a cycle "
                f"in `{elapsed_s / 60:.1f} min` "
                f"(expected ~{interval_s:.0f}s; >{stall_multiplier:.1f}× overdue).\n"
                "Background task may be blocked on a slow RPC, network drop, "
                "or rate-limit cooldown. Investigate the loop's recent errors."
            )
            out.append((key, "active", msg))
        else:
            out.append((
                key, "resolved",
                f"📊 Loop `{loop_name}` running on schedule "
                f"(last cycle {elapsed_s:.0f}s ago).",
            ))
    return out


def evaluate_memory_pressure_alert(
    rss_mb: Optional[float],
    budget_mb: float = 2048.0,
    pct_threshold: float = 75.0,
) -> list[tuple[str, str, str]]:
    """Memory-pressure alert (Round 52).

    Fires when current process RSS crosses `pct_threshold`% of `budget_mb`.
    Single alert key (`memory_pressure`) — independent of the per-symbol
    state machine. AlertState handles the on/off transition so the user
    gets ONE message when crossing into pressure and ONE resolved message
    when it clears.

    Returns [] when rss_mb is None (psutil unavailable / read failed) so
    a missing sensor never spams.
    """
    if rss_mb is None or budget_mb <= 0:
        return []
    pct = (rss_mb / budget_mb) * 100.0
    key = "memory_pressure"
    if pct >= pct_threshold:
        msg = (
            f"⚠️ *Memory pressure* — process RSS at `{rss_mb:,.0f} MB` "
            f"({pct:.0f}% of {budget_mb:,.0f} MB budget)\n"
            f"Crossed the {pct_threshold:.0f}% pressure threshold. "
            "Investigate buffer growth (liquidations / score history / klines) "
            "before the process hits the hard cap."
        )
        return [(key, "active", msg)]
    return [(
        key, "resolved",
        f"📉 Memory pressure cleared — RSS back to {rss_mb:,.0f} MB ({pct:.0f}% of budget).",
    )]


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
            thesis_block = _build_thesis_block_for_row(r)
            header = f"{emoji} *{sym}* — OI Δ24h `{oi_pct:+.1f}%`\n{direction}"
            msg = header + (f"\n\n{thesis_block}" if thesis_block else "")
            out.append((key, "active", msg))
        else:
            out.append((
                key, "resolved",
                f"📊 {sym} OI 24h Δ back to {oi_pct:+.1f}% (under {threshold_pct_24h:.0f}% threshold).",
            ))
    return out


def evaluate_sector_rotation_alerts(
    sector_aggregate_rows,
    threshold: int = 30,
    min_token_count: int = 3,
) -> list[tuple[str, str, str]]:
    """Sector-rotation alert (Round 42).

    Fires when a sector's average composite score crosses ±`threshold` AND
    the sector has at least `min_token_count` tracked tokens (to avoid noise
    from a 1-2 token "sector"). Catches broad rotations — when DeFi flips
    bullish across 8 tokens at once, that's a stronger signal than any single
    token's score crossing.

    Input: list of dicts produced by sectors.sector_aggregates(combined_rows).
    Each entry has: sector, avg_score, row_count, bullish_count, bearish_count,
    sample_symbols.

    Direction-aware keys: "sector_rot:DeFi:up" and ":down" tracked separately
    so a sector flipping from bullish to bearish fires twice (resolved-up and
    active-down) rather than being swallowed.
    """
    out: list[tuple[str, str, str]] = []
    for s in sector_aggregate_rows or []:
        sector = s.get("sector")
        avg = s.get("avg_score")
        n = s.get("row_count", 0) or 0
        if not sector or avg is None or n < min_token_count:
            continue
        bull_n = s.get("bullish_count", 0) or 0
        bear_n = s.get("bearish_count", 0) or 0
        sample = s.get("sample_symbols") or []
        sample_str = ", ".join(sample[:3]) if sample else ""

        for direction, predicate, emoji, label in (
            ("up", avg >= threshold, "🚀", "bullish"),
            ("down", avg <= -threshold, "💥", "bearish"),
        ):
            key = f"sector_rot:{sector}:{direction}"
            if predicate:
                msg = (
                    f"{emoji} *{sector}* sector turns {label}\n"
                    f"Avg composite score `{avg:+.1f}` across {n} tokens "
                    f"(🟢 {bull_n} bullish · 🔴 {bear_n} bearish)\n"
                    + (f"Sample symbols: {sample_str}" if sample_str else "")
                )
                out.append((key, "active", msg.strip()))
            else:
                out.append((
                    key, "resolved",
                    f"📊 {sector} sector back within ±{threshold} (avg {avg:+.1f}).",
                ))
    return out


def evaluate_fresh_setup_alerts(
    combined_rows,
    min_abs_score: int = 70,
) -> list[tuple[str, str, str]]:
    """Fresh-setup alert (Round 41).

    Fires when a row's setup_quality_label is in the "Fresh bull" or
    "Fresh bear" bucket AND its absolute composite score meets the threshold.
    This complements the broader composite_score alert by specifically
    catching setups in their first hour with high conviction — the moment
    of asymmetric edge before the move accumulates.

    Why a separate alert kind (instead of just filtering composite_score
    by quality): the regime semantics differ. composite_score fires once on
    crossing ±threshold and resolves when score returns; fresh_setup fires
    once on entering Fresh-with-conviction and resolves when EITHER the
    quality drops out of Fresh OR the score retreats. They have independent
    cooldowns and audit-log entries.
    """
    out: list[tuple[str, str, str]] = []
    for r in combined_rows:
        score = getattr(r, "composite_score", None)
        label = getattr(r, "setup_quality_label", None)
        if score is None or not label:
            continue
        key = f"fresh:{r.base_asset}/{r.quote_asset}"
        is_fresh_with_conviction = (
            "Fresh" in label and abs(score) >= min_abs_score
        )
        if is_fresh_with_conviction:
            sym = r.binance_symbol or r.mexc_symbol or r.base_asset
            direction = "long" if score > 0 else "short"
            head_emoji = "🚀" if score > 0 else "💥"
            thesis_block = _build_thesis_block_for_row(r)
            header = (
                f"{head_emoji} *{sym}* — fresh {direction} setup\n"
                f"score `{score:+d}` · quality {label}\n"
                "Just lit up — likely still un-priced. Composite alert may follow if the move "
                "sustains; this fires earlier on the freshness window."
            )
            msg = header + (f"\n\n{thesis_block}" if thesis_block else "")
            out.append((key, "active", msg))
        else:
            sym = r.binance_symbol or r.mexc_symbol or r.base_asset
            out.append((
                key, "resolved",
                f"📊 {sym} fresh-setup window cleared "
                f"(quality={label}, score={score:+d}).",
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
        # Build the thesis ONCE per row (same for both direction keys).
        thesis_block = _build_thesis_block_for_row(r)
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
                header = (
                    f"{emoji} *{sym}* — funding {label}\n"
                    f"z-score: `{z:+.1f}σ` vs ~30-period mean\n"
                    f"{bias}"
                )
                msg = header + (f"\n\n{thesis_block}" if thesis_block else "")
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
