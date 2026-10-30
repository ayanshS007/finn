"""Rebuild results and new-appointee cards for stored filings (see finn/jobs/cards.py).

    backend/.venv/bin/python scripts/build_cards.py --symbol INFY --since 7d [--force] [--dry-run]

--symbol may be repeated; without it every portfolio symbol is used. A card is built from checked
data only: results from the XBRL path (or, with no XBRL, the PDF table read by the reader model
and checked in code), people from the filing's own text. Free tier only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from dotenv import load_dotenv

from finn.config import REPO_ROOT, portfolio_symbols
from finn.db.client import Db
from finn.enrich.cards import CardBuilder
from finn.jobs import cards, rescore
from finn.jobs.announcements import parse_duration
from finn.llm.router import ACTUAL_COST_INR, DbStore, Router
from finn.sources.shapes import IST


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", action="append", help="NSE symbol (repeatable); default: portfolio")
    ap.add_argument("--since", type=parse_duration, default=parse_duration("7d"))
    ap.add_argument("--force", action="store_true", help="rebuild cards that already exist")
    ap.add_argument("--dry-run", action="store_true", help="build, write nothing to Supabase")
    args = ap.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    db = Db.service()
    router = Router(store=DbStore(db))
    ctx = rescore.default_context(db, router)
    builder = CardBuilder(router=router, db=db)
    symbols = args.symbol or portfolio_symbols()
    rows = cards.select_rows(db, symbols, datetime.now(IST) - args.since)
    result = cards.rebuild(db, rows, ctx, builder, force=args.force, dry_run=args.dry_run)
    print("\n".join(result.lines()))
    calls = [c for c in router.store.calls if not c["cache_hit"]]
    print(
        f"llm calls: {len(calls)}, paid-equivalent Rs "
        f"{sum(c['cost_inr'] for c in calls):.2f}; actual cost Rs {ACTUAL_COST_INR:.2f} (free tier)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
