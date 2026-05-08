# funding_screener

Multi-page Streamlit screener for **Binance** and **MEXC** perpetual futures.

A background daemon thread polls public REST endpoints and writes into an in-memory cache; the pages just render from the cache, so navigation is instant and data freshness is consistent across pages.

## Pages

1. **Binance USDT/USDC arb** — pairs where shorting one quote and longing the other yields net positive funding after maker fees.
2. **High funding (combined)** — Binance and MEXC USDT perps side-by-side, one row per base asset, missing side shown as `—`. Filtered by `max(|binance_8h|, |mexc_8h|) > threshold`.
3. **Binance price rise** — pairs whose close-to-close return over 1d / 7d / 30d exceeds 1000% (configurable).
4. **MEXC price rise** — same as #3 for MEXC.

Public REST endpoints only. No API keys required.

## Quick start (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open http://localhost:8501.

## Background refresh cadence

- Funding rates / contracts / 24h volume — every **60 seconds**
- Daily klines for top-N volume symbols — every **5 minutes**

Pages auto-rerender every 30–60 seconds so freshness timestamps and new data appear without manual refresh.

## Configuration

`config/fees.yaml` — maker-fee defaults (Binance has no public per-symbol fee endpoint; override here if you're on a VIP tier).
`config/settings.yaml` — refresh intervals, row limits, threshold knobs.

## Project layout

See [`CLAUDE.md`](./CLAUDE.md) for the architecture, conventions, and how to add a page.

## License

Personal use.
