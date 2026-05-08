---
name: add-screener-page
description: Scaffold a new Streamlit screener page in this funding_screener project. Use when the user asks to add a 7th page, a new exchange, or a new filter (e.g. "add a Bybit funding page", "add an open-interest screener"). Creates pages/N_*.py, the screener module under src/funding_screener/screener/, and a Pydantic row model — following project conventions.
---

# add-screener-page

You're adding a screener page to this project. Follow the conventions in `CLAUDE.md` and `ARCHITECTURE.md`.

## Steps

1. **Confirm the spec**. Ask the user (if not obvious from their message):
   - Which exchange(s)? Existing client (`binance`, `mexc`) or a new one?
   - What filter? (E.g., open interest > $X, mark vs index spread > Y%, …)
   - What columns to show?
   - Sort order?
   - Top-N cap (default 20 from `config/settings.yaml`)?
   - Refresh TTL (default 60s for funding-style, 300s for kline-style)?

2. **If a new exchange is needed**, create `src/funding_screener/exchanges/<name>.py` implementing the `ExchangeClient` protocol from `base.py`. Look at `binance.py` and `mexc.py` as references. Add to `exchanges/__init__.py`.

3. **Add a row model** to `src/funding_screener/models.py`. Pydantic `BaseModel`, frozen, fields typed. Match naming style of existing rows (`FundingRow`, `ArbRow`, `PriceRiseRow`).

4. **Add a screener function** in `src/funding_screener/screener/<name>.py`:
   ```python
   async def screen_<name>(client: ExchangeClient, ...) -> list[<RowModel>]:
       ...
   ```
   Pure: takes a client, returns a list. No Streamlit imports.

5. **Add the Streamlit page** at `pages/N_<Exchange>_<Title>.py`. Pattern (copy from existing pages):
   - `st.set_page_config(page_title=..., layout="wide")`
   - `@st.cache_data(ttl=...)` wrapper around `asyncio.run(screen_<name>(...))`
   - Sidebar: refresh button that calls `st.cache_data.clear()` + `st.rerun()`
   - Main: `st.dataframe(rows_df, use_container_width=True, hide_index=True)`

6. **Add a unit test** in `tests/test_<name>.py` that feeds fixture data into the screener function (no live HTTP) and asserts the expected rows come out.

7. **Update CLAUDE.md** "Pages" table.

## Don't

- Don't import `streamlit` in `src/funding_screener/...` *except* `streamlit_helpers.py`. That one module is the UI plumbing layer; everything else in the package stays UI-agnostic so screener logic can be unit-tested without Streamlit installed.
- Don't add API-key handling. Project is public-endpoints-only by design (see CLAUDE.md "What NOT to do").
- Don't mutate models — they're frozen for a reason.
- Don't add a database, file persistence, or Redis. Caching is `st.cache_data` only.

## Verification

After writing, run:
```
pytest tests/test_<name>.py -v
streamlit run streamlit_app.py
```
Then click through to the new page in the browser and confirm rows render. Don't claim it's done until you've seen the page render with non-empty data (or correctly empty if the filter is strict).
