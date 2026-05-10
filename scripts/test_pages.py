"""Run each Streamlit page through AppTest and report any exceptions.

This actually executes the page's Python code (including HTTP fetches and screener logic)
without needing a browser. Catches Python errors that a plain curl health-check would miss.
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from streamlit.testing.v1 import AppTest  # noqa: E402

PAGES = [
    "streamlit_app.py",
    "pages/1_Binance_USDT_USDC_Arb.py",
    "pages/2_High_Funding.py",
    "pages/3_Binance_Price_Rise.py",
    "pages/4_MEXC_Price_Rise.py",
    "pages/5_Symbol_Detail.py",
    "pages/6_Whale_Flows.py",
    "pages/7_Token_Unlocks.py",
    "pages/8_Liquidations.py",
    "pages/9_Daily_Digest.py",
]


def run_one(rel_path: str, timeout_s: float = 90.0) -> tuple[bool, str]:
    full = _ROOT / rel_path
    at = AppTest.from_file(str(full), default_timeout=timeout_s)
    at.run()
    if at.exception:
        msgs = []
        for e in at.exception:
            msgs.append(f"{type(e).__name__}: {e.value}")
        return False, "; ".join(msgs)
    # Count surfaced widgets so we know it actually rendered.
    n_tables = len(at.dataframe)
    n_text = len(at.markdown) + len(at.title) + len(at.caption) + len(at.info) + len(at.warning)
    return True, f"OK — {n_tables} dataframe(s), {n_text} text element(s)"


def main() -> None:
    failures = 0
    for p in PAGES:
        ok, summary = run_one(p)
        marker = "OK " if ok else "FAIL"
        print(f"[{marker}] {p:42}  {summary}")
        if not ok:
            failures += 1
    print()
    print(f"{len(PAGES) - failures}/{len(PAGES)} pages passed")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
