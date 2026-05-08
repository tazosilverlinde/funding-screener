# Architecture

## Data flow

```
                     ┌────────────────────────────────────┐
                     │  Public REST APIs (no keys)        │
                     │  - fapi.binance.com                │
                     │  - contract.mexc.com               │
                     │  - api.coinpaprika.com             │
                     │  - stablecoins.llama.fi            │
                     │  - public Ethereum RPCs (×5 fallback)│
                     │  - api.telegram.org (egress only)  │
                     └─────────────────┬──────────────────┘
                                       │ httpx.AsyncClient (pooled, split timeouts)
                                       │
                     ┌─────────────────▼──────────────────┐
                     │  background.py                     │
                     │  daemon thread                     │
                     │  ├─ fast loop          (60s)       │  funding / contracts / vol
                     │  ├─ slow loop          (5m)        │  klines for top-N
                     │  ├─ market caps loop   (5m)        │  CoinPaprika top-1000
                     │  ├─ macro loop         (15m)       │  stablecoin supply
                     │  ├─ enrichment loop    (3m)        │  top-30 funding/OI/LS history
                     │  ├─ onchain loop       (15m)       │  ETH 24h netflow per token
                     │  ├─ macro flow loop    (6h)        │  ETH 7d daily flows (USDT/USDC/WBTC/WETH)
                     │  └─ alerts loop        (60s)       │  Telegram on threshold transitions
                     └─────────────────┬──────────────────┘
                                       │ thread-safe writes
                                       │
                     ┌─────────────────▼──────────────────┐
                     │  DataStore (in-memory)             │
                     │  binance / mexc snapshots          │
                     │  market_caps_usd                   │
                     │  enrichments[(exchange, symbol)]   │
                     │  stablecoin_supply                 │
                     │  onchain_flows / macro_daily_flows │
                     └─────────────────┬──────────────────┘
                                       │ thread-safe reads (snapshot copy)
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
┌───────▼────────┐         ┌───────────▼─────────────┐     ┌──────────▼────────────┐
│ usdt_usdc_arb  │         │ combined_high_funding   │     │   price_rise          │
│   (pure fn)    │         │  + composite score      │     │   + composite score   │
└───────┬────────┘         └───────────┬─────────────┘     └──────────┬────────────┘
        │                              │                              │
        └──────────────────────────────┼──────────────────────────────┘
                                       │
                     ┌─────────────────▼──────────────────┐
                     │  Streamlit pages 1-7 + landing     │
                     │  (auto-rerun every 30-60s)         │
                     └────────────────────────────────────┘
```

## Module map

```
streamlit_app.py             ← landing: macro banner + sector rotation + status
pages/
  1_Binance_USDT_USDC_Arb.py
  2_High_Funding.py          ← composite score + signal + L/S + OI Δ + vol-adj
  3_Binance_Price_Rise.py    ← composite score column added
  4_MEXC_Price_Rise.py       ← composite score column added
  5_Symbol_Detail.py         ← 90d funding chart, OI, L/S, kline, score breakdown
  6_Whale_Flows.py           ← 24h flows + whale subset (>$500K) + 7d macro
  7_Token_Unlocks.py         ← filter: window + min %

src/funding_screener/
  background.py              ← daemon thread, all loops, DataStore
  exchanges/
    base.py                  ← ExchangeClient Protocol
    binance.py               ← public REST: funding, contracts, klines, OI hist, L/S
    mexc.py                  ← public REST: funding, contracts, klines
  market_data.py             ← CoinPaprikaClient (top-N market caps)
  macro.py                   ← DefiLlamaClient (stablecoin supply)
  onchain.py                 ← Eth JSON-RPC (5-RPC fallback) + netflow + whale aggregation
  notifications.py           ← TelegramClient + alert state machine + evaluators
  models.py                  ← Pydantic frozen models
  config.py                  ← YAML loaders + http_client_kwargs (shared httpx config)
  sectors.py                 ← sector classification + sector_aggregates
  signals.py                 ← classify_signal + compute_composite_score + realized_volatility
  unlocks.py                 ← token unlock YAML parser
  streamlit_helpers.py       ← UI plumbing: boot, sidebar widgets, render_table, etc.
  screener/                  ← PURE FUNCTIONS over cached data — no I/O inside
    high_funding.py
    combined_high_funding.py
    usdt_usdc_arb.py
    price_rise.py

config/
  settings.yaml              ← thresholds, HTTP, refresh intervals
  fees.yaml                  ← maker/taker per exchange/quote/symbol
  exchange_wallets.yaml      ← CEX hot wallets (auto-discovery exclusion + flow source)
  non_whale_addresses.yaml   ← DEX routers, bridges, market makers (whale-class exclusion)
  eth_token_contracts.yaml   ← ERC-20 contract addresses per ticker
  symbol_sectors.yaml        ← curated sector buckets
  token_unlocks.yaml         ← manually-maintained unlock calendar
  alerts.yaml                ← Telegram thresholds + cooldowns

tests/                       ← 89 unit tests
scripts/
  run_screener.py            ← one-shot CLI for verification
  test_pages.py              ← AppTest harness for all 8 pages
```

## Composite signal score

```
score = clamp([-100, +100],
    -15 × clamp([-2, +2], funding_8h_norm)        # funding direction (±30)
  + (sign(streak_dir) × 15 if streak ≥ 3 else 7.5 if streak == 2 else 0)
  + 0.3 × clamp([-50, +50], oi_24h_pct) × sign(funding_direction_consistent)
  + (-10 if ls_global > 3 else +10 if ls_global < 0.4 else 0)
  + (±5 when smart money disagrees with retail)
  + 15 × clamp([-50M, +50M], onchain_net_usd) / 50M
) × (0.5 if |mark_index_spread| > 0.5% else 1.0)
```

Each input is independent — score is computed from whatever's available.

## Design conventions

- **Public endpoints only.** No API keys needed for the screener.
- **Pure screeners.** Anything in `src/funding_screener/screener/` must be a pure function over cached data — never call HTTP, never touch the DataStore. The background loop fetches; pages compose; screeners filter.
- **Streamlit imports** are confined to `streamlit_app.py`, `pages/`, and `streamlit_helpers.py`. The rest of `funding_screener` is UI-agnostic so screener logic can be tested without Streamlit installed.
- **Funding rate sign convention**: store the *signed* rate as a float (positive = longs pay shorts, negative = shorts pay longs).
- **Funding rates expressed in %** (not fractions). `0.01` means 0.01%, not 1%.
- **8h-normalized rate** = `raw_rate * 8 / interval_hours`. Always compute both.
- **Symbol naming**: keep each exchange's native form (Binance `BTCUSDT`, MEXC `BTC_USDT`). Only convert at the display layer.
- **Don't I/O in screeners.** Pure functions only.

## Refresh strategy

- Streamlit pages re-render every 30–60s via `streamlit-autorefresh`.
- Background loops fetch on independent cadences (table above).
- DataStore is locked per write/read; readers get a snapshot copy.
- Cooldown: 418/429/451 from any exchange triggers a per-client cooldown that's honoured on subsequent calls; UI surfaces it as a yellow banner.

## Out of scope

- Order placement / trading.
- Backtesting / historical PnL.
- WebSocket streams. Polling at this cadence is fine and avoids reconnection complexity.
- Persistent storage. The DataStore is in-memory and rebuilt every time the process starts.
