# CLAUDE.md — funding_screener

This file is loaded by Claude Code when working in this repo. Read it before making changes.

## What this project is

A Python multi-page **read-only screener** for **perpetual-futures** funding rates and price moves on **Binance** and **MEXC**. Streamlit UI; pages live in `pages/`. All data comes from **public REST endpoints** — no API keys, no signed requests.

A single **background daemon thread** polls both exchanges and writes into a thread-safe `DataStore`. Pages render whatever is currently in the store, so navigation between pages is instant. Data refreshes happen on a fixed cadence regardless of what the user is doing.

### Pages

| # | Page | Filter |
|---|------|--------|
| 1 | Binance USDT/USDC arb | `(funding_diff − 4×maker_fee) > 0` for pairs that have both `BTCUSDT` and `BTCUSDC` perps. Long the lower-rate leg, short the higher-rate leg. |
| 2 | High funding (combined) | One row per base asset, Binance and MEXC USDT-perps side-by-side. Filtered by `max(\|bnb_8h\|, \|mxc_8h\|) > threshold`. Missing side displayed as `—`. |
| 3 | Binance price rise | Pairs whose close-to-close return over 1d / 7d / 30d exceeds the threshold (default 1000%). |
| 4 | MEXC price rise | Same as #3 for MEXC. |

> The previous **MEXC USDT/USDC arb** page was removed by user request — MEXC has USDC perps but the user explicitly wanted that page hidden. The screener function `screen_usdt_usdc_arb` still works for any exchange; only the page was deleted.

## Architecture

```
streamlit_app.py          ← landing page; calls boot() → starts background updater
pages/
  1_Binance_USDT_USDC_Arb.py
  2_High_Funding.py
  3_Binance_Price_Rise.py
  4_MEXC_Price_Rise.py
src/funding_screener/
  background.py           ← DataStore + daemon thread + fast/slow asyncio loops
  streamlit_helpers.py    ← UI helpers: boot(), sidebar_status(), freshness_banner()
  exchanges/
    base.py               ← ExchangeClient protocol
    binance.py            ← public REST client for fapi.binance.com
    mexc.py               ← public REST client for contract.mexc.com
  screener/               ← PURE FUNCTIONS over cached data — no I/O inside
    high_funding.py
    combined_high_funding.py
    usdt_usdc_arb.py
    price_rise.py
  models.py               ← Pydantic frozen models
  config.py               ← loads config/*.yaml
config/
  fees.yaml
  settings.yaml
scripts/
  run_screener.py         ← one-shot CLI used by the verify-screener-output skill
  test_pages.py           ← runs each page through streamlit.testing.v1.AppTest
tests/
  test_calculations.py
```

## Background updater

`src/funding_screener/background.py` owns:

- A daemon thread (`name="funding-bg"`) running its own asyncio loop.
- Two concurrent tasks:
  - **Fast loop (60s):** `fetch_funding_rows`, `fetch_contracts`, `fetch_24h_quote_volume` for both exchanges in parallel via `asyncio.gather(..., return_exceptions=True)`.
  - **Slow loop (300s):** daily klines for top-N volume symbols on each exchange (N = `row_limit × 25`, semaphore=8 for rate-limit safety).
- A thread-safe `DataStore` with separate `binance` and `mexc` `ExchangeSnapshot`s and freshness timestamps.
- `start_background()` is idempotent and starts the thread on first call. `streamlit_helpers.boot()` calls it via `st.cache_resource` so it runs exactly once per Streamlit process.

When a page renders:
1. `boot()` ensures the thread exists; if no data yet, it shows a spinner and waits up to 25s for the first fast-loop iteration.
2. The page calls `store.read_binance()` / `read_mexc()` to get a snapshot.
3. Pure screener functions transform the snapshot into output rows.
4. `streamlit_autorefresh` re-renders the page every 30–60s so freshness updates and any new data shows.

## Conventions

- **Public endpoints only.** Don't add auth.
- **Pure screeners.** Anything in `src/funding_screener/screener/` must be a pure function over data — never call HTTP, never touch the DataStore. The background loop fetches; pages compose; screeners filter.
- **Streamlit imports** are confined to `streamlit_app.py`, `pages/`, and `streamlit_helpers.py`. The rest of `funding_screener` is UI-agnostic so screener logic can be tested without Streamlit installed.
- **Funding rate sign convention**: store the *signed* rate as a float (positive = longs pay shorts, negative = shorts pay longs).
- **Funding rates expressed in %** (not fractions). `0.01` means 0.01%, not 1%.
- **8h-normalized rate** = `raw_rate * 8 / interval_hours`. Always compute both.
- **Maker fee**: per-symbol from MEXC's public `/contract/detail` (`makerFeeRate`). Binance has no public per-symbol fee endpoint, so we use a configurable default in `config/fees.yaml` (currently 0.02% futures maker — adjust if you have a VIP tier).
- **Symbol naming**: keep each exchange's native form (Binance `BTCUSDT`, MEXC `BTC_USDT`). Only convert at the display layer.

## What NOT to do

- Don't add a database. The DataStore is in-memory only.
- Don't add login / accounts / API key storage.
- Don't add trading actions.
- Don't put I/O inside screener functions.

## Running

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open http://localhost:8501. Sidebar lists the 4 pages.

## Adding a new screener page

Use the `/add-screener-page` skill — it scaffolds a page following these conventions (sidebar status, freshness banner, auto-rerun, pure screener wrapper, column_config formatting).

## Reference

The Java project at `C:\Users\user\Desktop\projects and bases\IdeaProjects\Arbitrage\src` was the source of the funding-arbitrage logic. Ported:

- `BinanceFundingRateAnalyzer.checkArbitrageBetweenTwoFuturesToCreateFutureOpeningOrders` → `screener/usdt_usdc_arb.py`
- `Symbol.openPositionIsProfitableBetweenUSDTUSDC` → ditto
- `Symbol.updateAvgDailyProfits` → embedded in the same screener
- `Binance.getFundingRates` (premiumIndex) → `BinanceClient.fetch_funding_rows`

We did NOT port the trading/portfolio-margin code — out of scope for a screener.
