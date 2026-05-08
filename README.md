# funding_screener

Multi-page Streamlit screener for **Binance** and **MEXC** perpetual futures, plus on-chain flow tracking, token-unlock calendar, and Telegram alerts.

A background daemon thread polls public REST endpoints and writes into an in-memory cache; pages render from the cache, so navigation is instant and data freshness is consistent across the app.

Public APIs only — no API keys required for the screener itself. Telegram alerts need a free bot token (one-time setup).

## Pages

| # | Page | What it does |
|---|---|---|
| Landing | Stablecoin macro banner + sector rotation table + system status |
| 1 | Binance USDT/USDC arb | Pairs where shorting one quote and longing the other yields net positive funding after maker fees |
| 2 | High funding (combined) | Binance + MEXC funding side-by-side with composite signal score, OI Δ, L/S ratio, mark/index spread, funding streak, vol-adj funding, sector tag |
| 3 | Binance price rise | Pairs whose close-to-close return over 1d / 7d / 30d exceeds threshold (configurable, default 500%) — with composite score so you know if it's bullish setup or fragile squeeze |
| 4 | MEXC price rise | Same as #3 for MEXC |
| 5 | Symbol detail | Click any symbol on any page → full per-contract view (90-day funding chart, OI history, L/S ratio, kline chart, signal breakdown) |
| 6 | Exchange flows | 24h on-chain netflow per token + 24h whale-only subset (>$500K, excluding routine plumbing) + 7-day daily macro flows for stables/BTC/ETH |
| 7 | Token unlocks | Manually-maintained calendar with filters (this week / 30d / all + min % of supply) |

## Sidebar (every page)

- **Status** — funding/klines age, last error, refresh button
- **Maker fees** — Binance USDT (0.02%) vs USDC (0.00% promo) vs MEXC (per-contract)
- **Quick search** — type any ticker → jump to detail page
- **Watchlist** — paste tickers, toggle filter to narrow tables to those rows
- **Sector filter** — multi-select (layer1 / defi / meme / ai / gaming / …) on Page 2

## Composite signal score

A single signed `[-100, +100]` number per row that combines:

| Input | Range | Logic |
|---|---|---|
| Funding rate (8h-norm) | ±30 | Negative funding (shorts pay longs) → bullish |
| Streak (3+ same-sign) | ±15 | Persistence reinforces direction |
| OI 24h Δ × funding | ±15 | OI rising while shorts pay = strong bull |
| L/S ratio extremity | ±10 | Crowded long → contrarian short |
| Smart-vs-retail L/S | ±5 | Top traders against retail = small confirm |
| On-chain netflow | ±15 | Withdrawals exceed deposits → bullish |
| Mark/Index spread | ×0.5 damp | Big divergence reduces conviction |

Buckets: 🚀 Strong bull (+70+) → 🟢 Bullish → ↗ Mild bull → 🟡 Neutral → ↘ Mild bear → 🔴 Bearish → 💥 Strong bear (−70−).

## Background loops

| Loop | Cadence | What it fetches |
|---|---|---|
| Fast | 60s | Funding rates, contracts, 24h volumes (Binance + MEXC) |
| Slow | 5m | Daily klines for top-N volume symbols |
| Market caps | 5m | CoinPaprika top-1000 (USD market cap) |
| Macro (stablecoin supply) | 15m | DefiLlama USDT/USDC/DAI/etc supply + 1d/7d Δ |
| Enrichment | 3m | Top-30 funding symbols → funding history (streak), OI history, L/S ratio |
| On-chain (24h flows) | 15m | ERC-20 Transfer events for top-30 tokens via public ETH RPCs |
| Macro on-chain (7d) | 6h | Daily netflow for USDT/USDC/WBTC/WETH over the last 7 days |
| Alerts | 60s | Evaluate all alert thresholds, send Telegram on transitions |

## Quick start (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open http://localhost:8501.

## Configuration files

All under `config/`. Edit and restart Streamlit (or `git push` for cloud deploys with auto-redeploy):

| File | Purpose |
|---|---|
| `settings.yaml` | Refresh intervals, row limits, threshold knobs, HTTP timeouts/pool |
| `fees.yaml` | Maker/taker fees per exchange + per-quote/per-symbol overrides (USDC promo defaults to 0%) |
| `exchange_wallets.yaml` | Known CEX hot wallets (Binance, MEXC, OKX, Bybit, Coinbase, Kraken) |
| `non_whale_addresses.yaml` | DEX routers, bridges, market makers — excluded from "whale" classification |
| `eth_token_contracts.yaml` | ERC-20 contract addresses per ticker |
| `symbol_sectors.yaml` | Sector classifications (layer1/defi/meme/ai/etc) |
| `token_unlocks.yaml` | Manually-curated upcoming unlock events |
| `alerts.yaml` | Telegram alert thresholds + cooldowns |

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `BINANCE_ENABLED` | Set to `false` to skip every Binance call (e.g. on geo-blocked Streamlit Cloud) | `true` |
| `MEXC_ENABLED` | Same for MEXC | `true` |
| `TELEGRAM_BOT_TOKEN` | Bot token from `@BotFather` | — |
| `TELEGRAM_CHAT_ID` | Your numeric chat ID from `@userinfobot` | — |

## Deployment

- **Render.com (Frankfurt region)** — recommended. Free tier, no card needed, EU IP that Binance allows. Deploy from the included `render.yaml` blueprint.
- **Streamlit Community Cloud** — works with `BINANCE_ENABLED=false` (their AWS US IPs are geo-blocked by Binance with HTTP 451 — MEXC works fine).
- **Local** — works as-is on any IP that's not geo-blocked.

## Project layout

See [`CLAUDE.md`](./CLAUDE.md) for the architecture, conventions, and how to add a page.

## Tests

```powershell
python -m pytest tests/ -v          # 89 unit tests
python scripts/test_pages.py        # 8 page render tests
```

## License

Personal use.
