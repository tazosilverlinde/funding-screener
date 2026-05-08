# Architecture

## Data flow

```
                                ┌────────────────────────────┐
                                │  Public REST APIs          │
                                │  - fapi.binance.com        │
                                │  - contract.mexc.com       │
                                └──────────────┬─────────────┘
                                               │ httpx.AsyncClient
                                               │
                                ┌──────────────▼─────────────┐
                                │  background.py             │
                                │  daemon thread             │
                                │  ├─ fast loop (60s)        │  funding, contracts, vol
                                │  └─ slow loop (5m)         │  klines for top-N
                                └──────────────┬─────────────┘
                                               │ thread-safe writes
                                               │
                                ┌──────────────▼─────────────┐
                                │  DataStore (in-memory)     │
                                │  binance: ExchangeSnapshot │
                                │  mexc:    ExchangeSnapshot │
                                └──────────────┬─────────────┘
                                               │ thread-safe reads (snapshot copy)
                                               │
                ┌──────────────────────────────┼──────────────────────────────┐
                │                              │                              │
        ┌───────▼────────┐         ┌───────────▼───────────┐         ┌────────▼────────┐
        │ usdt_usdc_arb  │         │ combined_high_funding │         │   price_rise    │
        │   (pure fn)    │         │      (pure fn)        │         │    (pure fn)    │
        └───────┬────────┘         └───────────┬───────────┘         └────────┬────────┘
                │                              │                              │
                └──────────────────────────────┼──────────────────────────────┘
                                               │
                                ┌──────────────▼─────────────┐
                                │      Streamlit pages       │
                                │  pages/1..4_*.py           │
                                │  + streamlit_app.py        │
                                └──────────────┬─────────────┘
                                               │ via streamlit-autorefresh
                                               │ (30–60s)
                                ┌──────────────▼─────────────┐
                                │           Browser           │
                                └────────────────────────────┘
```

## Module responsibilities

- **`background.py`** — `DataStore`, daemon thread, fast/slow asyncio loops, retry on partial errors. The only place that performs HTTP fetches.
- **`exchanges/{base,binance,mexc}.py`** — async REST clients. `ExchangeClient` Protocol defines the methods every exchange must support: `fetch_funding_rows`, `fetch_contracts`, `fetch_daily_klines`, `fetch_24h_quote_volume`. Returns Pydantic models.
- **`screener/*.py`** — pure functions: take cached data, return list of row models. **No I/O inside**.
- **`models.py`** — frozen Pydantic models (`FundingRow`, `ContractInfo`, `Kline`, `ArbRow`, `CombinedFundingRow`, `PriceRiseRow`).
- **`streamlit_helpers.py`** — `boot()`, `sidebar_status()`, `freshness_banner()`, `auto_rerun()`, `render_table()`. The only Streamlit-aware module inside `src/funding_screener/`.
- **`config.py`** — loads `config/*.yaml` once.

## Funding-rate math (cross-quote arbitrage)

```
funding_diff_per_period   = abs(rate_a - rate_b)            # signed values, abs of diff
funding_diff_8h_normalized = funding_diff_per_period * 8 / interval_hours
fees_total                = 4 × maker_fee                   # entry+exit on each leg
profitable_at_next_funding = funding_diff_per_period > fees_total
```

Direction (which leg longs / shorts) derived from the signs:

```
if rate_usdt > rate_usdc:  short USDT leg, long USDC leg
else:                       long USDT leg, short USDC leg
```

## Funding-rate math (combined high-funding page)

For each base asset present on either exchange's USDT perp:

```
binance_8h = binance.rate_per_period * 8 / binance.interval_hours
mexc_8h    = mexc.rate_per_period   * 8 / mexc.interval_hours
spread     = binance_8h - mexc_8h            # nullable, only when both sides present
include    = max(|binance_8h|, |mexc_8h|) > threshold
```

A row may have only one of the two sides — `None` for the missing exchange's columns.

## Out of scope

- Order placement / trading.
- Backtesting / historical PnL.
- WebSocket streams. Polling at this cadence is fine and avoids reconnection complexity.
- Persistent storage. The DataStore is in-memory and rebuilt every time the process starts.
