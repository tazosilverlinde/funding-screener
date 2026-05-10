# CLAUDE.md — funding_screener

This file is loaded by Claude Code when working in this repo. Read it before making changes.

## What this project is

A Python multi-page **read-only screener** for **perpetual-futures** funding rates, on-chain whale flows, liquidations, and market signals on **Binance** and **MEXC**. Streamlit UI; pages live in `pages/`. All data comes from **public REST endpoints + the public Binance forceOrder WebSocket** — no API keys, no signed requests.

A single **background daemon thread** owns its own asyncio loop and runs ~13 concurrent tasks that poll public endpoints and write into a thread-safe `DataStore`. Pages render whatever is currently in the store, so navigation between pages is instant. Data refreshes happen on a fixed cadence regardless of what the user is doing.

### Pages (12)

| # | Page | Filter |
|---|------|--------|
| 1 | Binance USDT/USDC arb | `(funding_diff − 4×maker_fee) > 0` for pairs that have both `BTCUSDT` and `BTCUSDC` perps. Long the lower-rate leg, short the higher-rate leg. |
| 2 | High funding (combined) | One row per (base, quote). Binance and MEXC USDT/USDC perps side-by-side. Filtered by `max(|bnb_8h|, |mxc_8h|) > threshold`. Has the densest column set: composite Score + Δ 1h + σ 24h + Age (h) + Quality + 3 sparkline columns + funding deviation z-score + 24h liquidation skew. Power-user filter sidebar (min |score|, quality buckets, age window, max σ). |
| 3 | Binance price rise | Pairs whose 1d/7d/30d return exceeds the threshold. |
| 4 | MEXC price rise | Same as #3 for MEXC. |
| 5 | Symbol Detail | Per-pair drill-down. Funding history chart, OI 24h chart, L/S 24h chart, 90-day price chart, volume profile, **auto-thesis expander** (📝), score-history sparkline, funding-income calculator, liquidation 24h card with hourly histogram. |
| 6 | Whale Flows | 24h on-chain netflow per token (Ethereum + BNB Chain via public JSON-RPC, no API keys). Per-chain sections, chain filter, whale-subset table with conviction filter, 7-day macro flows for stables + BTC + ETH on each chain. |
| 7 | Token Unlocks | Manually-curated unlock calendar from `config/token_unlocks.yaml`. |
| 8 | Liquidations | Live tape from Binance forceOrder WebSocket. 24h aggregates per symbol with bias, top long-cascades and top short-squeezes side-by-side. Window picker (1h / 4h / 12h / 24h). |
| 9 | Daily Digest | Preview of the once-a-day digest sent via Telegram + email. Same composer the email uses. |
| 10 | Cross-exchange Arb | Binance↔MEXC funding-spread screener. Same fee math as Page 1, but legs on different exchanges. |
| 11 | Alerts Log | In-app audit log of every alert that fired (in-memory ring buffer of 500). Mute controls (kind:foo, symbol:BTCUSDT) for in-session silencing of noisy kinds. |

## Architecture

```
streamlit_app.py             ← landing: sentiment hero + best-opps + alerts feed + sector rotation + liq summary + perf
pages/
  1_Binance_USDT_USDC_Arb.py
  2_High_Funding.py
  3_Binance_Price_Rise.py
  4_MEXC_Price_Rise.py
  5_Symbol_Detail.py
  6_Whale_Flows.py
  7_Token_Unlocks.py
  8_Liquidations.py
  9_Daily_Digest.py
  10_Cross_Exchange_Arb.py
  11_Alerts_Log.py
src/funding_screener/
  background.py              ← DataStore + daemon thread + 13 asyncio loops
  streamlit_helpers.py       ← UI helpers: boot(), sidebar_status(), freshness_banner(), filters
  chains.py                  ← ChainConfig (Ethereum, BSC) — drives multi-chain whale flows
  liquidations.py            ← Binance forceOrder WebSocket consumer + LiquidationsBuffer (24h ring)
  onchain.py                 ← EvmOnchainClient (chain-agnostic JSON-RPC, multi-RPC fallback)
  signals.py                 ← classify_signal, compute_composite_score, compute_funding_deviation,
                              estimate_funding_income, classify_setup_quality
  thesis.py                  ← compose_trade_thesis (auto-thesis composer)
                              + format_thesis_for_telegram (used in alert payloads)
  highlights.py              ← landing-page digests + pick_best_opportunities
  digest.py                  ← compose_daily_digest + format_digest_as_text/html
  notifications.py           ← TelegramClient + EmailClient + 11 alert evaluators + AlertLog + AlertState
  score_history.py           ← in-memory composite-score samples + score_volatility, signal_age_hours
  sectors.py                 ← sector classification + sector_aggregates
  unlocks.py                 ← load_upcoming_unlocks from YAML
  exchanges/
    base.py                  ← ExchangeClient protocol
    binance.py               ← public REST client (funding, OI, L/S, klines)
    mexc.py                  ← public REST client (funding, contracts, klines)
    coinpaprika.py           ← market caps
    defillama.py             ← stablecoin supply (macro)
  screener/                  ← PURE FUNCTIONS — no I/O, no DataStore access
    high_funding.py
    combined_high_funding.py ← biggest screener; produces CombinedFundingRow with
                              composite score, deviation, sparkline data, setup quality
    cross_exchange_arb.py
    usdt_usdc_arb.py
    price_rise.py
  models.py                  ← Pydantic frozen models (FundingRow, ContractInfo, CombinedFundingRow,
                              CrossExchangeArbRow, EnrichmentData, Kline, …)
  config.py                  ← loads config/*.yaml
config/
  alerts.yaml                ← Telegram + email alert thresholds (11 alert kinds)
  exchange_wallets.yaml      ← per-chain CEX hot-wallet labels (ETH + BSC)
  eth_token_contracts.yaml   ← per-chain token contract addresses + decimals
  non_whale_addresses.yaml   ← per-chain DEX routers / bridges / market makers (excluded from whale flows)
  fees.yaml                  ← maker/taker fee defaults per quote
  settings.yaml              ← row_limit, threshold, http timeouts, digest hour
  symbol_sectors.yaml        ← symbol → sector mapping for rotation summary
  token_unlocks.yaml         ← upcoming unlocks
scripts/
  run_screener.py            ← one-shot CLI (used by the verify-screener-output skill)
  test_pages.py              ← runs each page through streamlit.testing.v1.AppTest
tests/
  test_*.py                  ← 430+ unit tests (signals, alerts, screeners, digest, thesis, etc.)
```

## Background updater

`src/funding_screener/background.py` owns ~13 concurrent asyncio tasks running in a single daemon thread:

- **Fast loop (60s):** funding rows + contracts + 24h volume for both exchanges
- **Slow loop (300s):** daily klines for top-N volume symbols
- **Market caps loop (1h):** CoinPaprika top-1000 market caps
- **Macro loop (1h):** DefiLlama stablecoin supply
- **Enrichment loop (180s):** OI history, L/S ratios, funding history (30 settled rates) for top-30 by abs(funding)
- **Onchain loop per chain (900s):** ETH + BSC whale flows via public JSON-RPC
- **Macro flow loop per chain (6h):** 7-day daily flows for stables + BTC + ETH wrappers
- **Score history loop (10min):** composite-score snapshots per (base, quote)
- **Alerts loop (60s):** evaluates 11 alert kinds, fires on transitions, records to AlertLog
- **Liquidation tape (long-running):** WebSocket consumer with auto-reconnect
- **Daily digest loop (60s tick, fires once per day at hour_utc):** Telegram + email

Per-loop wall-clock timings recorded in `loop_timings` and surfaced in the landing-page Performance expander.

## Composite score (signed -100..+100, positive = long bias)

Components (clamped, mark/idx >0.5% damps the total ×0.5):
- Funding rate (±30) — negative funding → positive score (carry favors longs)
- Streak 3+ same-sign (±15)
- OI 24h Δ × funding direction (±15) — OI rising while shorts pay = strong bull
- L/S ratio extremes (±10) — crowded long → contrarian short
- On-chain netflow (±15) — withdrawals → bullish
- Liquidation skew (±10) — shorts blown out → bullish
- Top-vs-retail L/S divergence (±5)

Setup quality (Round 34) synthesizes score + age + delta + σ into one bucket:
🚀 Fresh / 📈 Building / 🎯 Mature / ⏰ Late / ⚠️ Noisy.

## Alerts (11 kinds, Telegram + email)

Per-event Telegram (with auto-thesis embedded for the row-based ones):
- `composite` — score crosses ±70
- `composite_score_delta` — |Δ 1h| ≥ 25
- `fresh_setup` — newly enters Fresh bull/bear with |score| ≥ 70
- `funding_rate` — abs funding 8h-norm > threshold
- `funding_deviation` — z-score ±2.5σ vs 30-period mean
- `oi_surge` — |Δ 24h OI| > threshold
- `whale_flow` — 24h netflow exceeds USD threshold
- `liq_cascade` + `liq_single` — total liquidation cascade or single big event
- `sector_rotation` — sector avg score crosses ±30 across ≥ 3 tokens
- `new_listing` — fresh perp contract
- `token_unlock` — unlock within N days

State machine: each key fires on off→on transition, sends "resolved" on on→off, respects cooldown. AlertLog records every fire (kind, key, status, message, delivered channels). User can mute kinds or symbols in-app via Page 11.

Email digest: once a day at `digest.hour_utc`. Pure composer (`digest.compose_daily_digest`), text + HTML formatters, SMTP via stdlib smtplib (STARTTLS or SSL based on port).

## Conventions

- **Public endpoints only.** Don't add auth.
- **Pure screeners.** Anything in `src/funding_screener/screener/` is a pure function over data — never HTTP, never DataStore.
- **Streamlit imports** are confined to `streamlit_app.py`, `pages/`, and `streamlit_helpers.py`. The rest is UI-agnostic.
- **Funding rate sign convention**: signed float, positive = longs pay shorts.
- **Funding rates expressed in %** (not fractions). `0.01` means 0.01%, not 1%.
- **8h-normalized rate** = `raw_rate * 8 / interval_hours`. Always compute both.
- **Symbol naming**: keep each exchange's native form (Binance `BTCUSDT`, MEXC `BTC_USDT`).
- **In-memory only**: score history, alert log, mutes, liquidation buffer all wipe on restart. By design (see "What NOT to do").

## What NOT to do

- Don't add a database. Persistence is intentionally limited to YAML configs.
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

Open http://localhost:8501. Sidebar lists the 12 pages.

Optional env vars (all unconfigured = silently skipped):
```
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
BINANCE_ENABLED, MEXC_ENABLED  ← set to "false" to disable an exchange (geo-block fallback)
```

## Reference (historical)

The Java project at `C:\Users\user\Desktop\projects and bases\IdeaProjects\Arbitrage\src` was the source of the original funding-arbitrage logic. Ported `screener/usdt_usdc_arb.py` from the Java reference; trading/portfolio-margin code was NOT ported (out of scope).
