"""Re-label and re-score every stored filing (see finn/jobs/rescore.py).

    backend/.venv/bin/python scripts/rescore_filings.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from finn.config import REPO_ROOT
from finn.db.client import Db
from finn.jobs import rescore
from finn.llm.router import ACTUAL_COST_INR, DbStore, Router


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="work out the changes, write nothing")
    args = ap.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    db = Db.service()
    router = Router(store=DbStore(db))
    ctx = rescore.default_context(db, router)
    report = rescore.run(db, ctx, dry_run=args.dry_run)
    print("\n".join(report.lines()))
    calls = [c for c in router.store.calls if not c["cache_hit"]]
    print(
        f"llm calls: {len(calls)}, paid-equivalent Rs "
        f"{sum(c['cost_inr'] for c in calls):.2f}; actual cost Rs {ACTUAL_COST_INR:.2f} (free tier)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
