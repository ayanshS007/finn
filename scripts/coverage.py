"""Check every portfolio symbol against every source and write docs/COVERAGE.md.

    backend/.venv/bin/python scripts/coverage.py [--candidates KAYNES,CDSL,...]

SMALLCAP_TBD is reported as PENDING. With --candidates, each candidate is checked the same way and
the best passing small-cap is named; this script never edits config/portfolio.yaml itself.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from finn.config import load_portfolio
from finn.sources import nse, prices, screener
from finn.sources.coverage import check_symbol, pick_replacement, render_markdown
from finn.sources.http import SourceError

DOC = Path(__file__).resolve().parents[1] / "docs" / "COVERAGE.md"
WINDOW_DAYS = 30
PLACEHOLDER = "SMALLCAP_TBD"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="", help="comma-separated NSE symbols")
    args = ap.parse_args()

    symbols = load_portfolio()["portfolio"]
    real = [s for s in symbols if s != PLACEHOLDER]
    candidates = [c.strip().upper() for c in args.candidates.split(",") if c.strip()]

    today = date.today()
    frame = prices.fetch_prices(real + candidates)
    fetch_company = screener.fetch_company
    fetch_ann = lambda s: nse.announcements(s, today - timedelta(days=WINDOW_DAYS), today)  # noqa: E731
    fetch_results = nse.financial_results

    def run(symbol: str, with_results: bool):
        print(f"checking {symbol} ...", flush=True)
        return check_symbol(
            symbol, frame, fetch_company, fetch_ann, fetch_results if with_results else None
        )

    rows = [run(s, True) for s in real]
    cand_rows = [run(s, False) for s in candidates]
    # Once a candidate has been written into portfolio.yaml it stays the choice.
    chosen = next((r for r in cand_rows if r.symbol in real), None) or pick_replacement(cand_rows)

    DOC.write_text(
        render_markdown(
            rows,
            generated=datetime.now().strftime("%Y-%m-%d %H:%M IST"),
            pending=[s for s in symbols if s == PLACEHOLDER],
            candidates=cand_rows,
            chosen=chosen.symbol if chosen else None,
            window_days=WINDOW_DAYS,
        )
    )
    print(f"wrote {DOC}")
    if chosen:
        print(f"small-cap pick: {chosen.symbol}")
    failed = [r.symbol for r in rows if not r.passed]
    if failed:
        print(f"FAILING: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed or PLACEHOLDER in symbols else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SourceError as exc:
        print(f"source error: {exc}", file=sys.stderr)
        sys.exit(2)
