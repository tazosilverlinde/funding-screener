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
from .macro import DefiLlamaClient
from .market_data import CoinPaprikaClient
from .models import ContractInfo, EnrichmentData, FundingRow, Kline
from .notifications import (
    AlertState,
    TelegramClient,
    evaluate_composite_alerts,
    evaluate_funding_alerts,
    evaluate_new_listing_alerts,
    evaluate_score_delta_alerts,
    evaluate_unlock_alerts,
    evaluate_whale_flow_alerts,
    load_alerts_config,
)
from .onchain import (
    EthOnchainClient,
    EvmOnchainClient,
    classify_netflow_signal,
    compute_daily_netflow_history,
    compute_token_netflow,
    filter_tokens_traded_on_exchanges,
    load_eth_token_contracts,
    load_exchange_wallets,
    load_non_whale_addresses,
    _BLOCKS_PER_24H,
)
from .score_history import trim_old as _trim_history
from .screener.combined_high_funding import screen_combined_high_funding
from .signals import compute_funding_streak
from .unlocks import load_upcoming_unlocks

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
        self.stablecoin_supply: dict[str, dict[str, float]] = {}  # macro: USDT/USDC/TOTAL supply
        # Per-token 24h exchange netflow, keyed by chain name (e.g. "ethereum",
        # "bsc"). Each chain's loop writes its own slice. read_onchain_flows()
        # flattens to one cross-chain list — every row has a "chain" field.
        self.onchain_flows_by_chain: dict[str, list[dict]] = {}
        self.last_onchain_by_chain: dict[str, datetime] = {}
        # 7-day daily exchange netflow for stables + BTC + ETH (USDT/USDC/WBTC/WETH).
        # Shape: {symbol: list[{"date": ..., "deposits_usd": ..., "withdrawals_usd": ..., "net_usd": ...}]}
        self.macro_daily_flows: dict[str, list[dict]] = {}
        # Composite-score history per (base_asset, quote_asset), trimmed to last 24h.
        # Each value is a list of (timestamp_utc, score_int) tuples in chronological order.
        self.score_history: dict[tuple[str, str], list[tuple[datetime, int]]] = {}
        # Per-loop cycle durations in seconds (last 50). Drives the perf expander.
        self.loop_timings: dict[str, list[float]] = {}
        self.last_fast_at: Optional[datetime] = None  # funding/contracts/volumes
        self.last_slow_at: Optional[datetime] = None  # klines
        self.last_market_caps_at: Optional[datetime] = None
        self.last_enrichment_at: Optional[datetime] = None
        self.last_macro_at: Optional[datetime] = None
        self.last_macro_flows_at: Optional[datetime] = None
        self.last_score_snapshot_at: Optional[datetime] = None
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

    def update_stablecoin_supply(self, supply: dict[str, dict[str, float]]) -> None:
        with self._lock:
            self.stablecoin_supply = supply
            self.last_macro_at = datetime.now(timezone.utc)

    def read_stablecoin_supply(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {k: dict(v) for k, v in self.stablecoin_supply.items()}

    def update_onchain_flows(self, chain: str, flows: list[dict]) -> None:
        """Replace the chain's slice. Each flow row is expected to carry the
        chain name in its "chain" field — we don't add it here.
        """
        with self._lock:
            self.onchain_flows_by_chain[chain] = list(flows)
            self.last_onchain_by_chain[chain] = datetime.now(timezone.utc)

    def read_onchain_flows(self) -> tuple[list[dict], Optional[datetime]]:
        """Return flat cross-chain list (each row has a "chain" field) and the
        oldest timestamp across chains so the freshness banner reflects the
        slowest one. None when no chain has ever reported.
        """
        with self._lock:
            combined: list[dict] = []
            for flows in self.onchain_flows_by_chain.values():
                combined.extend(flows)
            timestamps = list(self.last_onchain_by_chain.values())
            oldest = min(timestamps) if timestamps else None
        return combined, oldest

    def read_onchain_flows_by_chain(self) -> dict[str, tuple[list[dict], Optional[datetime]]]:
        """Return {chain_name: (flows, last_at)} so the page can show per-chain
        freshness or filter by chain.
        """
        with self._lock:
            return {
                chain: (list(flows), self.last_onchain_by_chain.get(chain))
                for chain, flows in self.onchain_flows_by_chain.items()
            }

    def update_macro_daily_flows(self, data: dict[str, list[dict]]) -> None:
        with self._lock:
            self.macro_daily_flows = data
            self.last_macro_flows_at = datetime.now(timezone.utc)

    def read_macro_daily_flows(self) -> tuple[dict[str, list[dict]], Optional[datetime]]:
        with self._lock:
            return ({k: list(v) for k, v in self.macro_daily_flows.items()},
                    self.last_macro_flows_at)

    def snapshot_scores(self, scores_by_key: dict[tuple[str, str], int]) -> None:
        """Append a (now, score) entry per (base, quote) and trim to 24h."""
        now = datetime.now(timezone.utc)
        with self._lock:
            for key, score in scores_by_key.items():
                history = self.score_history.setdefault(key, [])
                history.append((now, int(score)))
                # Trim in-place to keep the buffer bounded.
                self.score_history[key] = _trim_history(history, now)
            # Drop stale (key, []) pairs that may exist from a previous bigger universe.
            self.score_history = {k: v for k, v in self.score_history.items() if v}
            self.last_score_snapshot_at = now

    def read_score_histories(self) -> dict[tuple[str, str], list[tuple[datetime, int]]]:
        with self._lock:
            return {k: list(v) for k, v in self.score_history.items()}

    def read_score_history(self, key: tuple[str, str]) -> list[tuple[datetime, int]]:
        with self._lock:
            return list(self.score_history.get(key, []))

    def record_loop_duration(self, loop_name: str, duration_s: float) -> None:
        """Append one cycle's wall-clock duration; keep last 50 per loop."""
        with self._lock:
            buf = self.loop_timings.setdefault(loop_name, [])
            buf.append(duration_s)
            if len(buf) > 50:
                buf.pop(0)

    def read_loop_stats(self) -> dict[str, dict]:
        """Return {loop_name: {samples, avg_s, p50_s, p95_s, last_s}} for every
        loop that has emitted at least one timing.
        """
        with self._lock:
            out: dict[str, dict] = {}
            for name, durations in self.loop_timings.items():
                if not durations:
                    continue
                sorted_d = sorted(durations)
                n = len(sorted_d)
                out[name] = {
                    "samples": n,
                    "avg_s": sum(sorted_d) / n,
                    "p50_s": sorted_d[n // 2],
                    "p95_s": sorted_d[min(n - 1, int(n * 0.95))],
                    "last_s": durations[-1],
                }
        return out

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
    import time as _t
    interval = 60
    while True:
        cycle_start = _t.monotonic()
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
                store.record_error(f"fast-loop partial errors: {_describe_errors(errs)}")
            else:
                store.record_error("")
        except Exception as e:
            log.exception("fast loop error")
            store.record_error(f"fast-loop fatal: {type(e).__name__}: {e}")
        store.record_loop_duration("fast", _t.monotonic() - cycle_start)
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

    import time as _t
    while True:
        cycle_start = _t.monotonic()
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
        store.record_loop_duration("slow", _t.monotonic() - cycle_start)
        await asyncio.sleep(interval)


async def _market_caps_loop(store: DataStore, mcap_client: CoinPaprikaClient) -> None:
    """Top-N USD market caps from CoinPaprika — every 5 minutes (one call per cycle)."""
    import time as _t
    interval = 300
    while True:
        cycle_start = _t.monotonic()
        try:
            mcaps = await mcap_client.fetch_market_caps_top_n(top_n=1000)
            if mcaps:
                store.update_market_caps(mcaps)
        except Exception as e:
            log.exception("market caps loop error")
            store.record_error(f"market-caps: {type(e).__name__}: {e}")
        store.record_loop_duration("market_caps", _t.monotonic() - cycle_start)
        await asyncio.sleep(interval)


async def _macro_loop(store: DataStore, llama: DefiLlamaClient) -> None:
    """Stablecoin supply from DefiLlama — every 15 minutes.

    These move slowly (USDT issuance is daily-scale) so a long interval is fine.
    """
    import time as _t
    interval = 900
    while True:
        cycle_start = _t.monotonic()
        try:
            supply = await llama.fetch_stablecoin_supply()
            if supply:
                store.update_stablecoin_supply(supply)
        except Exception as e:
            log.exception("macro loop error")
            store.record_error(f"macro: {type(e).__name__}: {e}")
        store.record_loop_duration("macro", _t.monotonic() - cycle_start)
        await asyncio.sleep(interval)


async def _alerts_loop(store: DataStore, telegram: TelegramClient) -> None:
    """Telegram alerts on transition. Idempotent — safe to run unconfigured.

    Reads `config/alerts.yaml` once per iteration so a config change applies
    on the next cycle without restart.
    """
    interval = 60
    state = AlertState()
    seen_symbols: set[str] = set()  # for new-listing detection
    first_iter = True
    while True:
        try:
            cfg = (load_alerts_config() or {}).get("alerts") or {}
            if not cfg.get("enabled", True) or not telegram.is_configured():
                # Still iterate so a re-enabling flag takes effect, just don't fire.
                await asyncio.sleep(interval)
                continue
            cooldown_s = float(cfg.get("cooldown_minutes", 240)) * 60.0

            bnb = store.read_binance()
            mxc = store.read_mexc()
            enrichments = store.read_enrichments()
            onchain_flows, _ = store.read_onchain_flows()

            events: list[tuple[str, str, str]] = []

            # Composite — uses already-computed rows from the screener.
            try:
                screener_threshold = 0.0  # don't pre-filter for alert evaluation
                onchain_by_base = {f["token"]: f.get("net_usd", 0.0) for f in onchain_flows}
                histories_for_alerts = store.read_score_histories()
                combined_rows = screen_combined_high_funding(
                    bnb.funding, mxc.funding,
                    bnb.contracts, mxc.contracts,
                    enrichments,
                    threshold_percent=screener_threshold,
                    binance_volumes=bnb.volumes, mexc_volumes=mxc.volumes,
                    min_volume_usd_per_side=0.0,
                    onchain_netflow_by_base=onchain_by_base,
                    score_histories=histories_for_alerts,
                )
                if cfg.get("composite_score", {}).get("enabled", True):
                    cs = cfg["composite_score"]
                    events.extend(evaluate_composite_alerts(
                        combined_rows,
                        bull_threshold=int(cs.get("bullish_threshold", 70)),
                        bear_threshold=int(cs.get("bearish_threshold", -70)),
                    ))
                if cfg.get("composite_score_delta", {}).get("enabled", True):
                    sd = cfg["composite_score_delta"]
                    events.extend(evaluate_score_delta_alerts(
                        combined_rows,
                        abs_threshold=int(sd.get("abs_threshold", 25)),
                    ))
            except Exception as e:
                log.warning("alerts: composite evaluator failed: %s", e)

            # Funding rate threshold.
            if cfg.get("funding_rate", {}).get("enabled", True):
                thr = float(cfg["funding_rate"].get("threshold_pct_8h", 2.0))
                events.extend(evaluate_funding_alerts(list(bnb.funding) + list(mxc.funding), thr))

            # Whale flows.
            if cfg.get("whale_flow", {}).get("enabled", True):
                thr_usd = float(cfg["whale_flow"].get("threshold_usd", 20_000_000))
                events.extend(evaluate_whale_flow_alerts(onchain_flows, thr_usd))

            # New listings — skip the FIRST iteration so we don't fire one alert per existing contract.
            if cfg.get("new_listing", {}).get("enabled", True):
                listing_events, current = evaluate_new_listing_alerts(bnb.contracts, mxc.contracts, seen_symbols)
                if not first_iter:
                    events.extend(listing_events)
                seen_symbols = current

            # Token unlocks — read from YAML directly.
            if cfg.get("token_unlock", {}).get("enabled", True):
                tradable = {c.base_asset.upper() for c in bnb.contracts} | {c.base_asset.upper() for c in mxc.contracts}
                upcoming = load_upcoming_unlocks(tradable_symbols=tradable)
                days = int(cfg["token_unlock"].get("days_ahead", 3))
                events.extend(evaluate_unlock_alerts(upcoming, days))

            # Apply state machine: fire only on off→on transitions, send "resolved"
            # only for previously-active keys.
            for key, status, msg in events:
                if status == "active":
                    if state.should_fire(key, cooldown_s):
                        ok = await telegram.send(msg)
                        if ok:
                            state.mark_fired(key)
                elif status == "resolved":
                    if key in state.active_keys:
                        await telegram.send(msg)
                        state.mark_resolved(key)

        except Exception as e:
            log.exception("alerts loop error")
            store.record_error(f"alerts: {type(e).__name__}: {e}")
        first_iter = False
        await asyncio.sleep(interval)


async def _score_history_loop(store: DataStore) -> None:
    """Snapshot composite scores for every (base, quote) every 10 minutes.

    Drives the "Score Δ 1h" column on Page 2 and the "Top movers" section on
    the landing page. In-memory only — process restart wipes history; signal
    recovers within one snapshot cycle.
    """
    import time as _t
    interval = 600  # 10 min — fast enough to catch hourly movement, slow enough to be cheap
    # Wait for fast loop to populate funding before first snapshot.
    while True:
        bnb = store.read_binance()
        mxc = store.read_mexc()
        if bnb.funding or mxc.funding:
            break
        await asyncio.sleep(2)

    while True:
        cycle_start = _t.monotonic()
        try:
            bnb = store.read_binance()
            mxc = store.read_mexc()
            enrichments = store.read_enrichments()
            onchain_flows, _ = store.read_onchain_flows()
            onchain_by_base = {f["token"]: f.get("net_usd", 0.0) for f in onchain_flows}
            combined_klines: dict = {}
            combined_klines.update(bnb.klines)
            combined_klines.update(mxc.klines)
            # Threshold = 0 means we evaluate every (base, quote) pair, not just
            # the high-funding ones. We need history for everything to surface
            # mid-pack movers ("from 5 → 35 in 1h" matters more than "+85 → +90").
            rows = screen_combined_high_funding(
                bnb.funding, mxc.funding,
                bnb.contracts, mxc.contracts,
                enrichments,
                threshold_percent=0.0,
                binance_volumes=bnb.volumes,
                mexc_volumes=mxc.volumes,
                min_volume_usd_per_side=0.0,
                onchain_netflow_by_base=onchain_by_base,
                klines_by_symbol=combined_klines,
            )
            scores: dict[tuple[str, str], int] = {}
            for r in rows:
                if r.composite_score is None:
                    continue
                scores[(r.base_asset, r.quote_asset)] = r.composite_score
            if scores:
                store.snapshot_scores(scores)
        except Exception as e:
            log.exception("score history loop error")
            store.record_error(f"score-history: {type(e).__name__}: {e}")
        store.record_loop_duration("score_history", _t.monotonic() - cycle_start)
        await asyncio.sleep(interval)


async def _macro_flow_loop(store: DataStore, eth_client: EthOnchainClient) -> None:
    """7-day daily exchange flows for stables + BTC + ETH — every 6 hours.

    Tokens: USDT, USDC, WBTC (BTC proxy), WETH (ETH proxy).
    32 RPC calls per cycle (4 tokens × 7 days × 2 dirs ÷ pacing). Sequential to
    avoid hammering free public RPCs.
    """
    interval = 6 * 3600  # 6h
    macro_tokens = ["USDT", "USDC", "WBTC", "WETH"]
    exchange_wallets = load_exchange_wallets()
    contracts = load_eth_token_contracts()
    if not exchange_wallets or not contracts:
        log.warning("Macro flow loop disabled — wallet or token YAML missing")
        return

    # Wait until fast loop has prices.
    while True:
        bnb = store.read_binance()
        if bnb.funding:
            break
        await asyncio.sleep(2)

    import time as _t
    while True:
        cycle_start = _t.monotonic()
        try:
            bnb = store.read_binance()
            mxc = store.read_mexc()
            price_map: dict[str, float] = {}
            for r in bnb.funding:
                if r.quote_asset == "USDT" and r.mark_price:
                    price_map.setdefault(r.base_asset.upper(), r.mark_price)
            for r in mxc.funding:
                if r.quote_asset == "USDT" and r.mark_price:
                    price_map.setdefault(r.base_asset.upper(), r.mark_price)
            # Stables ≈ 1.0; WETH gets BTC=...wait, ETH price.
            price_map.setdefault("USDT", 1.0)
            price_map.setdefault("USDC", 1.0)
            # WBTC tracks BTC, WETH tracks ETH.
            price_map.setdefault("WBTC", price_map.get("BTC", 0.0))
            price_map.setdefault("WETH", price_map.get("ETH", 0.0))

            macro: dict[str, list[dict]] = {}
            for sym in macro_tokens:
                if sym not in contracts:
                    continue
                token = contracts[sym]
                price = price_map.get(sym)
                if price is None or price <= 0:
                    continue
                history = await compute_daily_netflow_history(
                    eth_client, token, exchange_wallets, days=7, price_usd=price,
                )
                if history:
                    macro[sym] = history
                # Pause between tokens to be RPC-friendly.
                await asyncio.sleep(1.0)
            if macro:
                store.update_macro_daily_flows(macro)
        except Exception as e:
            log.exception("macro flow loop error")
            store.record_error(f"macro-flow: {type(e).__name__}: {e}")
        store.record_loop_duration("macro_flow", _t.monotonic() - cycle_start)
        await asyncio.sleep(interval)


async def _onchain_loop(store: DataStore, client: EvmOnchainClient) -> None:
    """On-chain exchange-flow scanner for one EVM chain — every 15 minutes.

    Filters our token universe down to tokens that are (a) listed under this
    chain in `config/eth_token_contracts.yaml`, AND (b) have a Binance or MEXC
    futures contract. For each one, fetches 24h Transfer events to/from
    labeled exchange wallets and computes USD-denominated netflow.

    Multi-chain: one task per chain, each with its own EvmOnchainClient. The
    chain identity flows from `client.chain.name` — used to load the right
    YAML slices, label flows, and key per-chain timing/freshness.
    """
    chain = client.chain
    chain_name = chain.name
    timing_key = f"onchain.{chain_name}"
    interval = 900
    exchange_wallets = load_exchange_wallets(chain_name)
    contracts_by_symbol = load_eth_token_contracts(chain_name)
    non_whale_addresses = load_non_whale_addresses(chain_name)
    if not exchange_wallets or not contracts_by_symbol:
        log.warning("Onchain loop disabled for %s — wallet or token YAML is empty", chain_name)
        return

    # Wait for the fast loop to populate funding/contracts (we need price + universe).
    while True:
        bnb = store.read_binance()
        mxc = store.read_mexc()
        if bnb.contracts or mxc.contracts:
            break
        await asyncio.sleep(2)

    import time as _t
    while True:
        cycle_start = _t.monotonic()
        try:
            bnb = store.read_binance()
            mxc = store.read_mexc()
            bnb_bases = {c.base_asset.upper() for c in bnb.contracts}
            mxc_bases = {c.base_asset.upper() for c in mxc.contracts}
            tradable_tokens = filter_tokens_traded_on_exchanges(
                contracts_by_symbol, bnb_bases, mxc_bases,
            )

            # Build symbol → mark price (USD) lookup. Prefer Binance USDT perp,
            # fall back to MEXC USDT perp, then to Binance USDC. BSC pegged
            # tokens reuse the underlying mainnet symbol's price (BTCB → BTC,
            # ETH-on-BSC → ETH) — see explicit mappings below.
            price_map: dict[str, float] = {}
            for r in bnb.funding:
                if r.quote_asset == "USDT" and r.mark_price:
                    price_map.setdefault(r.base_asset.upper(), r.mark_price)
            for r in mxc.funding:
                if r.quote_asset == "USDT" and r.mark_price:
                    price_map.setdefault(r.base_asset.upper(), r.mark_price)
            for r in bnb.funding:
                if r.quote_asset == "USDC" and r.mark_price:
                    price_map.setdefault(r.base_asset.upper(), r.mark_price)
            # Stables ≈ $1; pegged proxies follow their underlying.
            price_map.setdefault("USDT", 1.0)
            price_map.setdefault("USDC", 1.0)
            price_map.setdefault("BTCB", price_map.get("BTC", 0.0))  # BSC BTC proxy

            # Strictly sequential — each token does 2 RPC calls inside; parallelising
            # at this layer too would burn through the public RPC rate limits.
            results: list = []
            for token in tradable_tokens:
                try:
                    res = await compute_token_netflow(
                        client,
                        token,
                        exchange_wallets,
                        blocks_back=chain.blocks_per_24h,
                        price_usd=price_map.get(token.symbol),
                        non_whale_addresses=non_whale_addresses,
                        whale_threshold_usd=500_000.0,
                    )
                    results.append(res)
                except Exception as e:
                    log.warning("Onchain compute failed for %s/%s: %s", chain_name, token.symbol, e)
                # Pace token-to-token to be RPC-friendly.
                await asyncio.sleep(0.5)
            flows: list[dict] = []
            for r in results:
                if isinstance(r, dict) and r:
                    emoji, short = classify_netflow_signal(
                        r["net_usd"],
                        r["deposits_usd"] + r["withdrawals_usd"],
                    )
                    r["signal_emoji"] = emoji
                    r["signal_short"] = short
                    r["chain"] = chain_name  # tag for cross-chain page rendering
                    flows.append(r)
            flows.sort(key=lambda r: r["net_usd"], reverse=True)
            store.update_onchain_flows(chain_name, flows)
        except Exception as e:
            log.exception("onchain loop error (%s)", chain_name)
            store.record_error(f"onchain[{chain_name}]: {type(e).__name__}: {e}")
        store.record_loop_duration(timing_key, _t.monotonic() - cycle_start)
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

    import time as _t
    while True:
        cycle_start = _t.monotonic()
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
                    # 30 funding rates ≈ 10 days of history at the typical 8h
                    # cadence — enough sample for compute_funding_deviation to
                    # produce a stable z-score (default min_samples=10).
                    results = await asyncio.gather(
                        binance.fetch_funding_rate_history(row.symbol, limit=30),
                        binance.fetch_open_interest_history(row.symbol, period="1h", limit=24),
                        binance.fetch_long_short_ratio_global(row.symbol, period="1h", limit=1),
                        binance.fetch_long_short_ratio_top(row.symbol, period="1h", limit=1),
                        return_exceptions=True,
                    )
                history = results[0] if isinstance(results[0], list) else []
                oi_hist = results[1] if isinstance(results[1], list) else []
                ls_g = results[2] if not isinstance(results[2], Exception) else None
                ls_t = results[3] if not isinstance(results[3], Exception) else None
                count, direction = compute_funding_streak(history)
                spread = None
                if row.mark_price is not None and row.index_price and row.index_price > 0:
                    spread = (row.mark_price - row.index_price) / row.index_price * 100.0
                oi_now, oi_1h, oi_24h = _oi_metrics_from_hist(oi_hist)
                return EnrichmentData(
                    exchange="Binance",
                    symbol=row.symbol,
                    prev_funding_rates_percent=history,
                    funding_streak_count=count,
                    funding_streak_direction=direction,
                    mark_index_spread_percent=spread,
                    oi_usd=oi_now,
                    oi_change_1h_pct=oi_1h,
                    oi_change_24h_pct=oi_24h,
                    ls_ratio_global=ls_g,
                    ls_ratio_top=ls_t,
                    fetched_at=datetime.now(timezone.utc),
                )

            async def _enrich_mexc(row: FundingRow) -> Optional[EnrichmentData]:
                async with sem:
                    try:
                        history = await mexc.fetch_funding_rate_history(row.symbol, limit=30)
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
        store.record_loop_duration("enrichment", _t.monotonic() - cycle_start)
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


def _oi_metrics_from_hist(hist: list) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Extract (current_oi_usd, change_1h_pct, change_24h_pct) from /openInterestHist.

    History is ordered oldest -> newest. Returns Nones if data is too sparse.
    """
    if not hist:
        return (None, None, None)
    try:
        cur = float(hist[-1].get("sumOpenInterestValue", 0) or 0)
    except (TypeError, ValueError):
        return (None, None, None)
    if cur <= 0:
        return (None, None, None)
    change_1h: Optional[float] = None
    change_24h: Optional[float] = None
    if len(hist) >= 2:
        try:
            prev = float(hist[-2].get("sumOpenInterestValue", 0) or 0)
            if prev > 0:
                change_1h = (cur / prev - 1.0) * 100.0
        except (TypeError, ValueError):
            pass
    if len(hist) >= 24:
        try:
            old = float(hist[0].get("sumOpenInterestValue", 0) or 0)
            if old > 0:
                change_24h = (cur / old - 1.0) * 100.0
        except (TypeError, ValueError):
            pass
    return (cur, change_1h, change_24h)


def _describe_errors(errs: list) -> str:
    """Render up to 3 exceptions with type + repr so empty-str exceptions
    (e.g. bare TimeoutError()) still produce an actionable message."""
    out = []
    for e in errs[:3]:
        s = repr(e) if str(e) else type(e).__name__ + "()"
        out.append(f"{type(e).__name__}: {s}")
    return " | ".join(out)


def _runner() -> None:
    """Entry-point for the daemon thread. Owns its own event loop and clients."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    binance = BinanceClient()
    mexc = MexcClient()
    mcap_client = CoinPaprikaClient()
    llama = DefiLlamaClient()
    # One EVM client per chain — each owns its own RPC fallback list. Macro
    # flow keeps using the ETH client (stables/BTC/ETH liquidity is biggest there).
    from .chains import ALL_CHAINS
    chain_clients: dict[str, EvmOnchainClient] = {
        name: EvmOnchainClient(cfg) for name, cfg in ALL_CHAINS.items()
    }
    eth_chain = chain_clients["ethereum"]
    telegram = TelegramClient()
    _runner_state["binance"] = binance
    _runner_state["mexc"] = mexc
    try:
        _store.bg_started_at = datetime.now(timezone.utc)
        loop.create_task(_fast_loop(_store, binance, mexc))
        loop.create_task(_slow_loop(_store, binance, mexc))
        loop.create_task(_market_caps_loop(_store, mcap_client))
        loop.create_task(_enrichment_loop(_store, binance, mexc))
        loop.create_task(_macro_loop(_store, llama))
        # Onchain task per chain — runs in parallel; each chain has its own RPC
        # fallback list so a slow BSC node won't block ETH.
        for chain_name, client in chain_clients.items():
            loop.create_task(_onchain_loop(_store, client))
        loop.create_task(_macro_flow_loop(_store, eth_chain))
        loop.create_task(_score_history_loop(_store))
        loop.create_task(_alerts_loop(_store, telegram))
        loop.run_forever()
    finally:
        try:
            close_calls = [
                binance.aclose(), mexc.aclose(),
                mcap_client.aclose(), llama.aclose(),
                telegram.aclose(),
            ]
            close_calls.extend(c.aclose() for c in chain_clients.values())
            loop.run_until_complete(
                asyncio.gather(*close_calls, return_exceptions=True)
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
