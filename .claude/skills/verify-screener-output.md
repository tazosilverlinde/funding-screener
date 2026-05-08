---
name: verify-screener-output
description: Run any of the six screeners once against live exchange data and print the top results to the terminal. Use when the user asks "what's the screener showing right now", "is page 3 working", "test the funding arb logic", or wants to debug a specific row without launching the Streamlit UI.
---

# verify-screener-output

A fast feedback loop: run a screener once, no UI, print the output.

## Steps

1. Ask the user which screener if not stated:
   - `binance-arb` (Page 1)
   - `mexc-arb` (Page 2)
   - `binance-high-funding` (Page 3)
   - `mexc-high-funding` (Page 4)
   - `binance-rise` (Page 5)
   - `mexc-rise` (Page 6)

2. Run a one-shot via the `scripts/run_screener.py` helper:
   ```
   python scripts/run_screener.py <screener-name>
   ```
   If `scripts/run_screener.py` doesn't exist yet, create it. It should:
   - Take a screener name as argv[1]
   - Build the right exchange client and call the screener function
   - Pretty-print the top 20 rows with `pandas.DataFrame.to_string()` or `rich.table.Table`
   - Exit non-zero if zero rows AND that's unexpected (e.g., page 3 should always have results)

3. Show the user the output. Flag anything suspicious:
   - All-zero funding rates → API call probably failed silently
   - Symbol with NaN price → kline fetch issue
   - Rows in wrong sort order → bug in the screener
   - Unexpectedly empty → check the threshold knobs in `config/settings.yaml`

## When to use this vs. the Streamlit UI

- Use this when iterating on screener logic and want a tight loop.
- Use Streamlit when verifying the user-facing UI (formatting, refresh button, page nav).
- A passing test in `tests/test_*.py` proves the calc is right against fixtures; this skill proves it works against *live* market data.
