"""Background data updater.

A single daemon thread runs an asyncio event loop with two concurrent tasks:

  - "fast" loop: funding rates, contracts, 24h volumes — every 60s
  - "slow" loop: daily klines for top-N volume symbols on each exchange — every 5m

All results land in a thread-safe `DataStore`. Streamlit pages read from it via
`get_store()` and render whatever is currently cached; freshness timestamps tell
them how stale the data is.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import is_binance_enabled, is_mexc_enabled, settings
from .exchanges.binance import BinanceClient
from .exchanges.mexc import MexcClient
from .market_data import CoinPaprikaClient
from .models import ContractInfo, EnrichmentData, FundingRow, Kline
from .signals import compute_funding_streak

log = logging.getLogger(__name__)


@dataclass
class ExchangeSnapshot:
    funding: list[FundingRow] = field(default_factory=list)
    contracts: list[ContractInfo] = field(default_factory=list)
    volumes: dict[str, float] = field(default_factory=dict)
    klines: dict[str, list[Kline]] = field(default_factory=dict)


class DataStore:
    """Thread-safe in-memory cache shared by every Streamlit page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.binance = ExchangeSnapshot()
        self.mexc = ExchangeSnapshot()
        self.market_caps_usd: dict[str, float] = {}  # base_asset upper → USD mcap
        self.enrichments: dict[tuple[str, str], EnrichmentData] = {}  # (exchange, symbol) → enrichment
        self.last_fast_at: Optional[datetime] = None  # funding/contracts/volumes
        self.last_slow_at: Optional[datetime] = None  # klines
        self.last_market_caps_at: Optional[datetime] = None
        self.last_enrichment_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.bg_started_at: Optional[datetime] = None

    # ---- writers (called from background thread only) ----

    def update_fast(
        self,
        binance_funding: list[FundingRow] | None,
        binance_contracts: list[ContractInfo] | None,
        binance_volumes: dict[str, float] | None,
        mexc_funding: list[FundingRow] | None,
        mexc_contracts: list[ContractInfo] | None,
        mexc_volumes: dict[str, float] | None,
    ) -> None:
        with self._lock:
            if binance_funding is not None:
                self.binance.funding = binance_funding
            if binance_contracts is not None:
                self.binance.contracts = binance_contracts
            if binance_volumes is not None:
                self.binance.volumes = binance_volumes
            if mexc_funding is not None:
                self.mexc.funding = mexc_funding
            if mexc_contracts is not None:
                self.mexc.contracts = mexc_contracts
            if mexc_volumes is not None:
                self.mexc.volumes = mexc_volumes
            self.last_fast_at = datetime.now(timezone.utc)

    def update_klines(self, exchange: str, klines: dict[str, list[Kline]]) -> None:
        with self._lock:
            if exchange == "binance":
                self.binance.klines = klines
            elif exchange == "mexc":
                self.mexc.klines = klines
            self.last_slow_at = datetime.now(timezone.utc)

    def update_market_caps(self, mcaps: dict[str, float]) -> None:
        with self._lock:
            self.market_caps_usd = mcaps
            self.last_market_caps_at = datetime.now(timezone.utc)

    def read_market_caps(self) -> dict[str, float]:
        with self._lock:
            return dict(self.market_caps_usd)

    def update_enrichments(self, items: list[EnrichmentData]) -> None:
        with self._lock:
            for it in items:
                self.enrichments[(it.exchange, it.symbol)] = it
            self.last_enrichment_at = datetime.now(timezone.utc)

    def read_enrichments(self) -> dict[tuple[str, str], EnrichmentData]:
        with self._lock:
            return dict(self.enrichments)

    def record_error(self, msg: str) -> None:
        with self._lock:
            self.last_error = msg

    # ---- readers (thread-safe; called from Streamlit page renders) ----

    def read_binance(self) -> ExchangeSnapshot:
        with self._lock:
            return ExchangeSnapshot(
                funding=list(self.binance.funding),
                contracts=list(self.binance.contracts),
                volumes=dict(self.binance.volumes),
                klines=dict(self.binance.klines),
            )

    def read_mexc(self) -> ExchangeSnapshot:
        with self._lock:
            return ExchangeSnapshot(
                funding=list(self.mexc.funding),
                contracts=list(self.mexc.contracts),
                volumes=dict(self.mexc.volumes),
                klines=dict(self.mexc.klines),
            )

    def freshness(self) -> tuple[Optional[datetime], Optional[datetime], Optional[str]]:
        with self._lock:
            return self.last_fast_at, self.last_slow_at, self.last_error

    def market_caps_freshness(self) -> Optional[datetime]:
        with self._lock:
            return self.last_market_caps_at


_store: DataStore = DataStore()
_thread: Optional[threading.Thread] = None
_started = threading.Event()
_runner_state: dict = {}  # holds the live clients for sidebar status display


def get_store() -> DataStore:
    return _store


def _runner_clients() -> tuple[Optional[BinanceClient], Optional[MexcClient]]:
    """Public read of the daemon thread's live clients (read-only; for UI status)."""
    return _runner_state.get("binance"), _runner_state.get("mexc")


# ---- background loops ----


async def _fast_loop(store: DataStore, binance: BinanceClient, mexc: MexcClient) -> None:
    """Funding rates, contracts, 24h volumes — every 60s.

    Skip an exchange entirely when:
      - its env flag (BINANCE_ENABLED / MEXC_ENABLED) is false, OR
      - the client is in cooldown (after 418/429/451)
    Either condition silences error noise from a known-blocked exchange.
    """
    interval = 60
    while True:
        try:
            bnb_on = is_binance_enabled() and not binance.is_cooled_down()
            mxc_on = is_mexc_enabled() and not mexc.is_cooled_down()
            tasks: list = []
            if bnb_on:
                tasks += [
                    binance.fetch_funding_rows(),
                    binance.fetch_contracts(),
                    binance.fetch_24h_quote_volume(),
                ]
            if mxc_on:
                tasks += [
                    mexc.fetch_funding_rows(),
                    mexc.fetch_contracts(),
                    mexc.fetch_24h_quote_volume(),
                ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Walk the results in the order tasks were queued.
            it = iter(results)
            bnb_funding = bnb_contracts = bnb_volumes = None
            mxc_funding = mxc_contracts = mxc_volumes = None
            if bnb_on:
                bnb_funding = _ok(next(it))
                bnb_contracts = _ok(next(it))
                bnb_volumes = _ok(next(it))
            if mxc_on:
                mxc_funding = _ok(next(it))
                mxc_contracts = _ok(next(it))
                mxc_volumes = _ok(next(it))
            store.update_fast(
                binance_funding=bnb_funding,
                binance_contracts=bnb_contracts,
                binance_volumes=bnb_volumes,
                mexc_funding=mxc_funding,
                mexc_contracts=mxc_contracts,
                mexc_volumes=mxc_volumes,
            )
            errs = [r for r in results if isinstance(r, Exception)]
            if errs:
                store.record_error(f"fast-loop partial errors: {errs[0]}")
            else:
                store.record_error("")
        except Exception as e:
            log.exception("fast loop error")
            store.record_error(f"fast-loop fatal: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


async def _slow_loop(store: DataStore, binance: BinanceClient, mexc: MexcClient) -> None:
    """Daily klines for top-N symbols by volume — every 5 minutes."""
    interval = 300
    cfg = settings()
    pcfg = cfg["price_rise"]
    candidate_cap = int(cfg["row_limit"]) * int(pcfg.get("kline_candidate_multiplier", 5))
    days_to_fetch = int(pcfg.get("kline_history_days", 1500))
    min_vol = float(pcfg["min_24h_quote_volume"])

    # Wait until the fast loop has populated contracts/volumes at least once.
    while True:
        bnb = store.read_binance()
        mxc = store.read_mexc()
        if bnb.contracts or mxc.contracts:
            break
        await asyncio.sleep(2)

    while True:
        try:
            tasks = []
            if is_binance_enabled() and not binance.is_cooled_down():
                tasks.append(_refresh_klines(store, "binance", binance, candidate_cap, days_to_fetch, min_vol))
            if is_mexc_enabled() and not mexc.is_cooled_down():
                tasks.append(_refresh_klines(store, "mexc", mexc, candidate_cap, days_to_fetch, min_vol))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            log.exception("slow loop error")
            store.record_error(f"slow-loop fatal: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


async def _market_caps_loop(store: DataStore, mcap_client: CoinPaprikaClient) -> None:
    """Top-N USD market caps from CoinPaprika — every 5 minutes (one call per cycle)."""
    interval = 300
    while True:
        try:
            mcaps = await mcap_client.fetch_market_caps_top_n(top_n=1000)
            if mcaps:
                store.update_market_caps(mcaps)
        except Exception as e:
            log.exception("market caps loop error")
            store.record_error(f"market-caps: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


async def _enrichment_loop(store: DataStore, binance: BinanceClient, mexc: MexcClient) -> None:
    """Pull funding-rate history + compute streaks for the top-N flagged symbols.

    Runs every 180s. Only enriches the top 30 symbols by abs(rate_8h_norm) on
    each exchange — keeps the API call count modest (60 total per cycle).
    """
    interval = 180
    top_n = 30

    # Wait until the fast loop has populated funding data.
    while True:
        bnb = store.read_binance()
        mxc = store.read_mexc()
        if bnb.funding or mxc.funding:
            break
        await asyncio.sleep(2)

    while True:
        try:
            bnb_on = is_binance_enabled() and not binance.is_cooled_down()
            mxc_on = is_mexc_enabled() and not mexc.is_cooled_down()
            bnb_snap = store.read_binance()
            mxc_snap = store.read_mexc()

            # Pick top-N by abs(8h-normalized rate) on each side.
            bnb_top = sorted(
                bnb_snap.funding,
                key=lambda r: abs(r.rate_8h_norm_percent),
                reverse=True,
            )[:top_n] if bnb_on else []
            mxc_top = sorted(
                mxc_snap.funding,
                key=lambda r: abs(r.rate_8h_norm_percent),
                reverse=True,
            )[:top_n] if mxc_on else []

            sem = asyncio.Semaphore(4)

            async def _enrich_binance(row: FundingRow) -> Optional[EnrichmentData]:
                async with sem:
                    try:
                        history = await binance.fetch_funding_rate_history(row.symbol, limit=4)
                    except Exception:
                        history = []
                count, direction = compute_funding_streak(history)
                spread = None
                if row.mark_price is not None and row.index_price and row.index_price > 0:
                    spread = (row.mark_price - row.index_price) / row.index_price * 100.0
                return EnrichmentData(
                    exchange="Binance",
                    symbol=row.symbol,
                    prev_funding_rates_percent=history,
                    funding_streak_count=count,
                    funding_streak_direction=direction,
                    mark_index_spread_percent=spread,
                    fetched_at=datetime.now(timezone.utc),
                )

            async def _enrich_mexc(row: FundingRow) -> Optional[EnrichmentData]:
                async with sem:
                    try:
                        history = await mexc.fetch_funding_rate_history(row.symbol, limit=4)
                    except Exception:
                        history = []
                count, direction = compute_funding_streak(history)
                # MEXC funding endpoint doesn't expose mark/index → spread is N/A.
                return EnrichmentData(
                    exchange="MEXC",
                    symbol=row.symbol,
                    prev_funding_rates_percent=history,
                    funding_streak_count=count,
                    funding_streak_direction=direction,
                    mark_index_spread_percent=None,
                    fetched_at=datetime.now(timezone.utc),
                )

            bnb_results = await asyncio.gather(*[_enrich_binance(r) for r in bnb_top], return_exceptions=True)
            mxc_results = await asyncio.gather(*[_enrich_mexc(r) for r in mxc_top], return_exceptions=True)

            ok = [
                r for r in (list(bnb_results) + list(mxc_results))
                if isinstance(r, EnrichmentData)
            ]
            if ok:
                store.update_enrichments(ok)
        except Exception as e:
            log.exception("enrichment loop error")
            store.record_error(f"enrichment: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


async def _refresh_klines(
    store: DataStore,
    exchange: str,
    client,
    candidate_cap: int,
    days: int,
    min_vol: float,
) -> None:
    snap = store.read_binance() if exchange == "binance" else store.read_mexc()
    eligible: list[ContractInfo] = []
    for c in snap.contracts:
        if c.status != "TRADING":
            continue
        if c.quote_asset not in ("USDT", "USDC"):
            continue
        v = snap.volumes.get(c.symbol, 0.0)
        if min_vol > 0 and v < min_vol:
            continue
        eligible.append(c)
    eligible.sort(key=lambda c: snap.volumes.get(c.symbol, 0.0), reverse=True)
    eligible = eligible[:candidate_cap]

    # Concurrency=4 to keep burst weight under Binance's 2400/min IP limit.
    sem = asyncio.Semaphore(4)

    async def _one(c: ContractInfo):
        async with sem:
            try:
                return c.symbol, await client.fetch_daily_klines(c.symbol, days)
            except Exception:
                return c.symbol, []

    pairs = await asyncio.gather(*[_one(c) for c in eligible])
    klines = {sym: rows for sym, rows in pairs if rows}
    store.update_klines(exchange, klines)


def _ok(result):
    return None if isinstance(result, Exception) else result


def _runner() -> None:
    """Entry-point for the daemon thread. Owns its own event loop and clients."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    binance = BinanceClient()
    mexc = MexcClient()
    mcap_client = CoinPaprikaClient()
    _runner_state["binance"] = binance
    _runner_state["mexc"] = mexc
    try:
        _store.bg_started_at = datetime.now(timezone.utc)
        loop.create_task(_fast_loop(_store, binance, mexc))
        loop.create_task(_slow_loop(_store, binance, mexc))
        loop.create_task(_market_caps_loop(_store, mcap_client))
        loop.create_task(_enrichment_loop(_store, binance, mexc))
        loop.run_forever()
    finally:
        try:
            loop.run_until_complete(
                asyncio.gather(binance.aclose(), mexc.aclose(), mcap_client.aclose(), return_exceptions=True)
            )
        except Exception:
            pass
        loop.close()


def start_background() -> DataStore:
    """Idempotent. Safe to call from any number of Streamlit reruns."""
    global _thread
    if _started.is_set():
        return _store
    _started.set()
    _thread = threading.Thread(target=_runner, daemon=True, name="funding-bg")
    _thread.start()
    return _store


def wait_for_initial_data(timeout_s: float = 25.0) -> bool:
    """Blocks until the fast loop's first iteration finishes, or the timeout elapses."""
    start = time.time()
    while time.time() - start < timeout_s:
        last_fast, _, _ = _store.freshness()
        if last_fast is not None:
            return True
        time.sleep(0.4)
    return False
