"""Save integrated-filing XBRL files to backend/tests/fixtures/ for the S2 parser tests.

Run from the repo root on a machine where NSE answers:

    backend/.venv/bin/python scripts/capture_xbrl_fixtures.py

For each (symbol, period end, basis) below it looks the row up in
/api/integrated-filing-results, downloads the linked XBRL file unmodified and saves it as
xbrl_<SYMBOL>_<period>_<basis>.xml. It also saves the GRAVITA row list for the offline
results_facts tests. Prints the source URL of every file so the fixtures README can cite it.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from finn.normalise.periods import quarter_for_end
from finn.parse.xbrl_results import fetch_xbrl
from finn.sources import nse
from finn.sources.http import SourceError

FIXTURES = Path(__file__).resolve().parents[1] / "backend" / "tests" / "fixtures"
# (symbol, period end, basis)
WANTED = [
    ("INFY", date(2026, 9, 30), "consolidated"),  # latest quarter
    ("INFY", date(2026, 6, 30), "consolidated"),  # previous quarter
    ("INFY", date(2025, 9, 30), "consolidated"),  # same quarter last year
    ("HDFCBANK", date(2026, 9, 30), "consolidated"),  # banking taxonomy
    ("GRAVITA", date(2026, 9, 30), "consolidated"),
    ("GRAVITA", date(2026, 9, 30), "standalone"),  # standalone-only test
    ("BAJFINANCE", date(2026, 9, 30), "consolidated"),  # NBFC taxonomy
]
ROW_LISTS = ("GRAVITA",)


def main() -> int:
    client = nse.NSEClient()
    rows_by_symbol: dict[str, list[dict]] = {}
    failed = 0
    for symbol, end, basis in WANTED:
        if symbol not in rows_by_symbol:
            rows_by_symbol[symbol] = nse.parse_integrated_results(
                client.integrated_results_raw(symbol, ttl=0)
            )
        row = next(
            (
                r
                for r in rows_by_symbol[symbol]
                if r["period_end"] == end.isoformat() and r["basis"] == basis
            ),
            None,
        )
        if row is None or not row["xbrl_url"]:
            print(f"MISSING {symbol} {end} {basis}: no integrated row with an XBRL link")
            failed += 1
            continue
        try:
            content = fetch_xbrl(row["xbrl_url"], client.http, ttl=0)
        except SourceError as exc:
            print(f"FAILED {symbol} {end} {basis}: {exc}")
            failed += 1
            continue
        name = f"xbrl_{symbol}_{quarter_for_end(end).label}_{basis}.xml"
        (FIXTURES / name).write_bytes(content)
        print(f"saved {name} ({len(content):,} bytes) <- {row['xbrl_url']}")
    for symbol in ROW_LISTS:
        raw = client.integrated_results_raw(symbol, ttl=0)
        name = f"nse_integrated_results_{symbol}.json"
        (FIXTURES / name).write_text(json.dumps(raw, indent=1, ensure_ascii=False))
        print(f"saved {name} <- /api/integrated-filing-results?symbol={symbol}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
