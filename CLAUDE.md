# CLAUDE.md — funding_screener

This file is loaded by Claude Code when working in this repo. Read it before making changes.

## What this project is

A Python multi-page **read-only screener** for **perpetual-futures** funding rates, on-chain whale flows, liquidations, and market signals on **Binance** and **MEXC**. Streamlit UI; pages live in `pages/`. All market data comes from **public REST endpoints + the Binance forceOrder WebSocket** — no API keys, no signed requests.

A single **background daemon thread** owns its own asyncio loop and runs 14 concurrent tasks that poll public endpoints and write into a thread-safe `DataStore`. Every task is wrapped in a **supervisor** that catches catastrophic failures and restarts with exponential backoff (Round 64) — operations are self-healing.

State is **in-memory by default**, with **opt-in persistence** for the four state surfaces that matter on restart: alert log, mutes, score history, watchlist overrides. The user's 2GB-per-process memory cap is enforced via auto-trim by default (Round 60) and surfaced via Page 12 (System Health).

### Pages (13)

| # | Page | What it shows |
|---|------|--------|
| 1 | Binance USDT/USDC arb | `(funding_diff − 4×maker_fee) > 0` opportunities within Binance |
| 2 | High funding (combined) | Densest signal table: Score + Δ 1h + σ 24h + Age + Quality + 3 sparkline columns + funding deviation z + 1d/7d % + Liq 24h + per-row TradingView access via the Detail link |
| 3 | Binance price rise | 1d/7d/30d returns above threshold, enriched with Quality + Age from the combined screener (R47) |
| 4 | MEXC price rise | Same as #3 for MEXC |
| 5 | Symbol Detail | Per-pair drill-down — **TradingView chart embed** (R70), funding history chart, OI/L-S 24h, 90-day price chart, auto-thesis expander (R37), score-history sparkline, funding-income calculator, liquidation 24h card + hourly histogram, sector peers comparison (R46) |
| 6 | Whale Flows | 24h on-chain netflow per token (Ethereum + BNB Chain, public JSON-RPC, no keys). Per-chain sections, conviction filter, 7-day macro flows |
| 7 | Token Unlocks | Manually-curated unlock calendar |
| 8 | Liquidations | Live tape from Binance forceOrder WebSocket. 24h aggregates, top long-cascades / short-squeezes |
| 9 | Daily Digest | Preview of the once-a-day digest (Telegram + email). Identical composer to the email |
| 10 | Cross-exchange Arb | Binance↔MEXC funding-spread screener |
| 11 | Alerts Log | Audit log (R24, persists R62), **mute controls** (R25, persist R66), **watchlist live overrides** (R68) |
| 12 | System Health | Operator dashboard: RSS bar + per-buffer breakdown + loop status with stall flags + WS health + recent errors + 24h alert fire counts + **signal hit-rate analytics** (R71-72) + cache freshness (R56) |

## Architecture

```
streamlit_app.py             ← landing: sentiment hero + best-opps + recent alerts +
                               score histogram + sector rotation + liq summary + perf
pages/                       ← 13 pages
src/funding_screener/
  background.py              ← DataStore + daemon thread + 14 asyncio loops (all supervised)
  streamlit_helpers.py       ← UI helpers
  process_memory.py          ← RSS + per-buffer estimates (R51-53)
  chains.py                  ← ChainConfig (Ethereum, BSC) for multi-chain whale flows
  liquidations.py            ← Binance forceOrder WS consumer + LiquidationsBuffer
  onchain.py                 ← EvmOnchainClient (multi-RPC fallback per chain)
  signals.py                 ← classify_signal, compute_composite_score,
                              compute_funding_deviation, classify_setup_quality,
                              estimate_funding_income, bucket_scores_for_histogram
                              (MYPY STRICT — R65)
  thesis.py                  ← compose_trade_thesis + format_thesis_for_telegram
                              (MYPY STRICT — R65)
  analytics.py               ← signal hit-rate, per-pair breakdown, decay curve (R71-72)
  highlights.py              ← landing-page digests + pick_best_opportunities
  digest.py                  ← compose_daily_digest + text/HTML formatters +
                              compose_system_status_line (R58)
  notifications.py           ← TelegramClient (rate-limited R61) + EmailClient +
                              14 alert evaluators + AlertLog (persists R62) +
                              suppress_overlapping_alerts (R63)
  score_history.py           ← in-memory composite-score samples (persists R67) +
                              score_volatility, signal_age_hours
  sectors.py                 ← sector classification + sector_aggregates + find_sector_peers
  unlocks.py                 ← load_upcoming_unlocks from YAML
  exchanges/
    binance.py               ← public REST client (funding, OI, L/S, klines)
    mexc.py                  ← public REST client
    coinpaprika.py           ← market caps
    defillama.py             ← stablecoin supply
  screener/                  ← PURE FUNCTIONS — no I/O, no DataStore access
    high_funding.py
    combined_high_funding.py ← biggest screener; produces CombinedFundingRow with score,
                              quality, deviation, sparkline data, per-pair fields
    cross_exchange_arb.py
    usdt_usdc_arb.py
    price_rise.py
  models.py                  ← Pydantic frozen models
  config.py                  ← loads config/*.yaml
config/
  alerts.yaml                ← 14 alert kinds, per-pair thresholds (R69),
                              watchlist (R48), overlap suppression (R63),
                              telegram rate-limit (R61), summary digest (R50),
                              memory_pressure + loop_stall + error_pattern (R52/55/57)
  exchange_wallets.yaml      ← per-chain CEX hot-wallet labels (ETH + BSC)
  eth_token_contracts.yaml   ← per-chain token addresses + decimals
  non_whale_addresses.yaml   ← per-chain DEX routers / bridges / market makers
  fees.yaml                  ← maker/taker fee defaults per quote
  settings.yaml              ← row_limit, threshold, http timeouts, digest hour
  symbol_sectors.yaml        ← symbol → sector mapping
  token_unlocks.yaml         ← unlock calendar
.github/workflows/test.yml   ← CI: mypy strict + pytest + page smoke on push/PR
mypy.ini                     ← strict on signals.py + thesis.py
scripts/
  run_screener.py            ← one-shot CLI
  test_pages.py              ← AppTest smoke on all 13 pages
tests/                       ← 722+ unit tests
```

## Background updater — 14 supervised tasks

Each task wrapped in `_supervised(coro_factory, name, store)` (R64) — fatal exceptions caught, restart with 1s→60s exponential backoff, per-task restart counter on Page 12.

| Task | Cadence | Purpose |
|---|---|---|
| `fast` | 60s | funding rows + contracts + 24h volume both exchanges |
| `slow` | 300s | daily klines top-N volume symbols |
| `market_caps` | 300s | CoinPaprika top-1000 |
| `enrichment` | 180s | OI history, L/S ratios, funding history for top-30 by abs(funding) |
| `macro` | 900s | DefiLlama stablecoin supply |
| `onchain.<chain>` × 2 | 900s | ETH + BSC whale flows |
| `macro_flow.<chain>` × 2 | 6h | 7-day daily flows for stables + BTC + ETH |
| `score_history` | 600s | composite-score snapshots per pair |
| `alerts` | 60s | 14 alert evaluators + suppression + state machine + Telegram |
| `liquidations` | (long-running WS) | forceOrder consumer with auto-reconnect |
| `daily_digest` | 60s tick | fires once at hour_utc → Telegram + email |
| `alerts_summary` | 60s tick | periodic batched alert summary (R50) |

Per-loop wall-clock timing recorded; loop-stall alert fires (R55) when any loop is overdue by 3× expected interval.

## Composite score (signed -100..+100, positive = long bias)

7 components, clamped, mark/idx >0.5% damps total ×0.5:
- Funding rate (±30) — negative funding → positive score
- Streak 3+ (±15)
- OI 24h Δ × funding direction (±15)
- L/S ratio extremes (±10)
- On-chain netflow (±15)
- Liquidation skew (±10) — shorts blown out → bullish
- Top-vs-retail L/S divergence (±5)

Setup quality classifier (R34) synthesizes score + age + Δ + σ → 🚀 Fresh / 📈 Building / 🎯 Mature / ⏰ Late / ⚠️ Noisy.

## Alerts — 14 kinds, multi-channel

Per-event Telegram (with auto-thesis embedded for row-based ones):

**Market signals (11):**
- `composite`, `score_delta`, `fresh_setup`, `funding_rate`, `funding_deviation`,
  `oi_surge`, `whale_flow`, `liq_cascade`, `liq_single`,
  `new_listing`, `token_unlock`, `sector_rotation`

**System health (3):**
- `memory_pressure` (R52) — RSS ≥ 75% of 2GB cap
- `loop_stall` (R55) — loop ≥3× overdue
- `error_pattern` (R57) — same source-prefix repeated ≥5× in 30min

State machine: each key fires on off→on transition, "resolved" on on→off, respects cooldown.

**Cross-cutting filters / controls:**
- **Watchlist** (R48 + R68 live overrides) — only watchlisted bases fire (except sector_rotation which always sees full universe)
- **Mute** (R25, persist R66) — `kind:foo` or `symbol:BTCUSDT` patterns
- **Overlap suppression** (R63) — `fresh > composite > score_delta` per pair
- **Telegram rate limit** (R61) — 20/min default, sliding window, drops overflow
- **Per-pair thresholds** (R69) — `composite_score.per_pair: {BTCUSDT: {bullish_threshold: 50}}`

**Email digest** — once a day at `digest.hour_utc`. Pure composer + text/HTML formatters + SMTP via stdlib smtplib. System status footer (R58).

**Audit:** `AlertLog` (R24) records every fire; persists to JSON-lines via `ALERT_LOG_PATH` (R62).

## Persistence — 4 surfaces, all opt-in via env

| Env var | What persists | Round |
|---|---|---|
| `ALERT_LOG_PATH` | Audit history (append-only JSON-lines) | R62 |
| `ALERT_MUTES_PATH` | User-set kind/symbol silencing (JSON) | R66 |
| `SCORE_HISTORY_PATH` | Composite-score samples (atomic JSON) | R67 |
| `WATCHLIST_OVERRIDES_PATH` | Live additions / removals (JSON) | R68 |

Unset = in-memory only (default). All loads are defensive (corrupted/missing files → empty state, never crash startup). Writes are best-effort (failures logged but don't propagate). Score history uses `.tmp` + `os.replace` for atomic write.

## Memory budget

User imposed a **hard 2GB RAM cap per process**. Observability + enforcement stack (R51-54):

| Layer | What |
|---|---|
| R51 — See | RSS bar on Page 12 + landing perf expander, color-coded against 2GB |
| R52 — Notify | Telegram alert when RSS ≥ 75% |
| R53 — Diagnose | Per-buffer breakdown table (score history, liquidations, klines, etc.) |
| R54 — Self-protect | **`auto_trim: true` by default** (R60) — shrinks score history + liq buffer to 12h when pressured |

When designing new background buffers, reason about peak memory: `entries × bytes/entry × symbols`. Prefer bounded ring buffers over unbounded lists; trim on read.

## Signal-quality analytics (R71-72)

Pure-function module `analytics.py` over the persistent score history:

- `find_threshold_crossings(samples, threshold)` — indices where score crossed
- `compute_signal_hit_rate(samples_by_pair, threshold, follow_up_hours)` — aggregate sustain rate
- `compute_per_pair_hit_rate(...)` — same per pair, sorted best-first
- `compute_decay_curve(...)` — sustain rate at multiple follow-up windows

Surfaced on Page 12 Section 6. Answers "is +70 actually predictive on this universe?" with empirical data.

## CI / type safety

`.github/workflows/test.yml` runs on push & PR:
1. `mypy` on signals.py + thesis.py (strict mode)
2. `pytest tests/` (722 unit tests)
3. `python scripts/test_pages.py` (13 page smokes via AppTest)

Binance/MEXC disabled in CI env to avoid geo-block on GitHub Actions runners.

## Conventions

- **Public endpoints only.** No auth, no signed requests, no API key storage.
- **Pure screeners.** Anything in `src/funding_screener/screener/` is a pure function — no HTTP, no DataStore.
- **Streamlit imports** confined to `streamlit_app.py`, `pages/`, `streamlit_helpers.py`.
- **Funding rate sign:** positive = longs pay shorts.
- **Funding rates expressed in %** (not fractions). `0.01` means 0.01%.
- **8h-normalized rate** = `raw_rate * 8 / interval_hours`. Always compute both.
- **Symbol naming:** keep each exchange's native form (Binance `BTCUSDT`, MEXC `BTC_USDT`).
- **In-memory state can be lost** on restart; opt-in persistence env vars cover the four state surfaces that matter.

## What NOT to do

- Don't add a database. Persistence is JSON files via env vars only.
- Don't add login / accounts / API key storage.
- Don't add trading actions.
- Don't put I/O inside screener functions.
- Don't spawn a process that could exceed 2GB RAM (user-imposed hard rule — see memory budget above).
- Don't bypass the supervisor pattern when adding new background tasks.

## Running

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open http://localhost:8501. Sidebar lists the 13 pages.

Optional env vars (all unconfigured = in-memory only or silently skipped):
```
# Telegram (per-event alerts + daily digest delivery)
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Email digest (SMTP for daily digest)
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO

# Persistence (R62/R66/R67/R68)
ALERT_LOG_PATH=/var/lib/funding_screener/alerts.jsonl
ALERT_MUTES_PATH=/var/lib/funding_screener/mutes.json
SCORE_HISTORY_PATH=/var/lib/funding_screener/scores.json
WATCHLIST_OVERRIDES_PATH=/var/lib/funding_screener/wl_overrides.json

# Geo-block fallback (set to "false" to disable an exchange)
BINANCE_ENABLED, MEXC_ENABLED
```

## Reference (historical)

Original funding-arbitrage logic was ported from `C:\Users\user\Desktop\projects and bases\IdeaProjects\Arbitrage\src` (Java reference). Trading/portfolio-margin code was NOT ported — out of scope for a screener.
