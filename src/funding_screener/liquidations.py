"""Binance liquidation tape — public WebSocket consumer (Round 14).

Subscribes to `wss://fstream.binance.com/ws/!forceOrder@arr`, the all-symbols
stream of executed liquidation orders. No authentication, no rate limit on
this stream.

Each event from Binance:

    {
      "e": "forceOrder",
      "E": <event_time_ms>,
      "o": {
        "s": "BTCUSDT",
        "S": "SELL",          # SELL = a LONG was force-liquidated
        "ap": "9910",         # average fill price
        "q": "0.014",         # filled quantity
        "T": <trade_time_ms>,
        ...
      }
    }

Side encoding gotcha
====================
The "S" field is the SIDE OF THE LIQUIDATING ORDER, not the direction of the
position that got blown out. So:

  - S == "SELL" → exchange is selling to close a LONG position → long got liq
  - S == "BUY"  → exchange is buying to close a SHORT position → short got liq

We translate at parse time so the rest of the system reads the trader's
position direction directly.

In-memory only: we keep a deque per symbol of the last 24h of events, no DB.
On restart you lose the buffer; first event arrives within seconds and the
24h window fills up over the day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    _WEBSOCKETS_AVAILABLE = False

_log = logging.getLogger(__name__)

_BINANCE_FORCE_ORDER_WSS = "wss://fstream.binance.com/ws/!forceOrder@arr"

# Window: liquidations older than this are dropped from the buffer.
_WINDOW_SECONDS = 24 * 3600
# Hard cap per symbol so a flood (e.g. major liquidation cascade) can't
# unbounded-grow the buffer. ~50K is plenty for a 24h window even on BTCUSDT.
_MAX_PER_SYMBOL = 50_000


@dataclass(frozen=True)
class LiquidationEvent:
    symbol: str
    side_liquidated: str    # "long" or "short" — the position that got blown out
    price_usd: float
    qty: float
    notional_usd: float     # price × qty
    timestamp: float        # Unix seconds (event_time_ms / 1000)


def parse_force_order_message(raw: dict) -> Optional[LiquidationEvent]:
    """Decode a Binance forceOrder JSON dict to LiquidationEvent.

    Returns None for malformed payloads — better to drop a single bad event
    than crash the WebSocket loop. Logs at debug level so noise doesn't
    pollute production logs.
    """
    try:
        if raw.get("e") != "forceOrder":
            return None
        order = raw.get("o") or {}
        symbol = order.get("s")
        side_raw = order.get("S")
        avg_price = float(order.get("ap", 0) or 0)
        qty = float(order.get("q", 0) or 0)
        ts_ms = int(order.get("T", 0) or raw.get("E", 0) or 0)
        if not symbol or avg_price <= 0 or qty <= 0 or ts_ms <= 0:
            return None
        # SELL means exchange sold to close a long. BUY means it bought to close a short.
        side_liq = "long" if side_raw == "SELL" else "short"
        return LiquidationEvent(
            symbol=symbol,
            side_liquidated=side_liq,
            price_usd=avg_price,
            qty=qty,
            notional_usd=avg_price * qty,
            timestamp=ts_ms / 1000.0,
        )
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        _log.debug("liquidations: skipping malformed payload: %s", e)
        return None


class LiquidationsBuffer:
    """Thread-safe rolling 24h buffer of LiquidationEvents per symbol.

    Producers (the WebSocket loop) call `add()`; consumers (Streamlit pages)
    call `aggregate(symbol)` or `aggregate_all()`. Every read prunes events
    older than the window — no separate cleanup task needed.
    """

    def __init__(self, window_seconds: int = _WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._lock = asyncio.Lock()
        # Plain dict; we only mutate from the WS task and from sync reads.
        self._buf: dict[str, deque[LiquidationEvent]] = defaultdict(deque)
        self._total_events_seen = 0
        self._last_event_at: Optional[float] = None

    def add(self, ev: LiquidationEvent) -> None:
        """Append one event. O(1) amortised — pruning runs lazily on read."""
        dq = self._buf[ev.symbol]
        dq.append(ev)
        if len(dq) > _MAX_PER_SYMBOL:
            dq.popleft()
        self._total_events_seen += 1
        self._last_event_at = ev.timestamp

    def _prune(self, dq: deque[LiquidationEvent], cutoff: float) -> None:
        while dq and dq[0].timestamp < cutoff:
            dq.popleft()

    def aggregate(self, symbol: str, window_seconds: Optional[int] = None) -> dict:
        """Per-symbol stats over the last `window_seconds` (default = full window).

        Returns: {
            "long_liq_usd", "short_liq_usd", "total_usd",
            "long_count", "short_count", "biggest_single_usd",
            "biggest_single_side", "events_count",
        }
        """
        window = window_seconds or self._window
        cutoff = time.time() - window
        dq = self._buf.get(symbol)
        if not dq:
            return _empty_stats()
        self._prune(dq, cutoff)
        return _aggregate_deque(dq)

    def aggregate_all(self, window_seconds: Optional[int] = None) -> dict[str, dict]:
        """Stats for every symbol that has at least one event in the window."""
        window = window_seconds or self._window
        cutoff = time.time() - window
        out: dict[str, dict] = {}
        for symbol, dq in list(self._buf.items()):
            self._prune(dq, cutoff)
            if not dq:
                continue
            out[symbol] = _aggregate_deque(dq)
        return out

    def total_events_seen(self) -> int:
        """Lifetime count since process start — used for health/debug."""
        return self._total_events_seen

    def last_event_at(self) -> Optional[float]:
        """Unix-seconds timestamp of the most recent event we saw."""
        return self._last_event_at


def _empty_stats() -> dict:
    return {
        "long_liq_usd": 0.0,
        "short_liq_usd": 0.0,
        "total_usd": 0.0,
        "long_count": 0,
        "short_count": 0,
        "biggest_single_usd": 0.0,
        "biggest_single_side": None,
        "events_count": 0,
    }


def _aggregate_deque(dq: deque[LiquidationEvent]) -> dict:
    long_usd = 0.0
    short_usd = 0.0
    long_count = 0
    short_count = 0
    biggest = 0.0
    biggest_side: Optional[str] = None
    for ev in dq:
        if ev.side_liquidated == "long":
            long_usd += ev.notional_usd
            long_count += 1
        else:
            short_usd += ev.notional_usd
            short_count += 1
        if ev.notional_usd > biggest:
            biggest = ev.notional_usd
            biggest_side = ev.side_liquidated
    return {
        "long_liq_usd": long_usd,
        "short_liq_usd": short_usd,
        "total_usd": long_usd + short_usd,
        "long_count": long_count,
        "short_count": short_count,
        "biggest_single_usd": biggest,
        "biggest_single_side": biggest_side,
        "events_count": long_count + short_count,
    }


async def consume_binance_liquidations(
    buffer: LiquidationsBuffer,
    *,
    url: str = _BINANCE_FORCE_ORDER_WSS,
    initial_backoff_s: float = 2.0,
    max_backoff_s: float = 60.0,
) -> None:
    """Long-running coroutine — connects to the forceOrder stream and feeds
    every event into `buffer`. Auto-reconnects with exponential backoff on
    any connection drop or WebSocket error.

    Designed to be spawned as an asyncio task in the background runner; never
    returns under normal operation. Cancel the task to stop.
    """
    if not _WEBSOCKETS_AVAILABLE:
        _log.warning("liquidations: `websockets` package not installed — loop disabled")
        return
    backoff = initial_backoff_s
    while True:
        try:
            _log.info("liquidations: connecting to %s", url)
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                _log.info("liquidations: connected, streaming")
                backoff = initial_backoff_s  # reset backoff on successful connect
                async for raw_msg in ws:
                    try:
                        payload = json.loads(raw_msg)
                    except (TypeError, ValueError):
                        continue
                    ev = parse_force_order_message(payload)
                    if ev is not None:
                        buffer.add(ev)
        except ConnectionClosed as e:
            _log.warning("liquidations: connection closed (%s) — reconnecting in %.1fs", e, backoff)
        except asyncio.CancelledError:
            _log.info("liquidations: task cancelled, exiting")
            raise
        except Exception as e:
            _log.warning("liquidations: WS error %s: %s — reconnecting in %.1fs", type(e).__name__, e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff_s)
