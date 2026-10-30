"""Rebuild the results and people cards of filings already stored.

    python scripts/build_cards.py --symbol INFY --since 7d [--force] [--dry-run]

Picks the card/one-liner filings of the given symbols from the window whose category and tier have a
card (results.filed, management_change cxo or director), builds each card the way the announcement
job does, writes the facts and the row, and rewrites the why-line with the figures the code made.
A filing that already has a card is skipped unless --force. A source or model that is unavailable
leaves the card pending (card_json.card_error); nothing loops.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from finn.db.client import Db
from finn.enrich.cards import CardBuilder, apply_card, card_kind
from finn.enrich.results_card import CardPending
from finn.jobs import announcements as job
from finn.llm.router import LLMConfigError, LLMError
from finn.sources.http import SourceError

log = logging.getLogger("finn.jobs.cards")

BUILT = ("ok", "check_source", "duplicate")


@dataclass
class Result:
    states: Counter = field(default_factory=Counter)  # "results:ok", "people:ok", ...
    skipped: int = 0
    rows: list[dict] = field(default_factory=list)  # the rows written (or that would be)

    def lines(self) -> list[str]:
        return [
            "cards: " + (" ".join(f"{k}={v}" for k, v in sorted(self.states.items())) or "none"),
            f"skipped (already built or no card kind): {self.skipped}",
        ]


def select_rows(db: Db, symbols: list[str] | None, since: datetime) -> list[dict]:
    query = (
        db.client.table("filings")
        .select("*")
        .gte("filed_at", since.isoformat())
        .in_("status", ["card", "oneliner"])
        .order("filed_at")
    )
    if symbols:
        query = query.in_("symbol", symbols)
    return query.execute().data


def rebuild(
    db: Db | None,
    rows: list[dict],
    ctx: job.Context,
    builder: CardBuilder,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> Result:
    result = Result()
    for row in sorted(rows, key=lambda r: r.get("filed_at") or ""):
        notes = dict(row.get("card_json") or {})
        category, tier = row["category"], notes.get("tier_name")
        kind = card_kind(category, tier)
        if kind is None or (notes.get("state") in BUILT and not force):
            result.skipped += 1
            continue
        row = {**row, "card_json": notes}
        try:
            card = builder(row, category, tier)
            if card is None:
                result.skipped += 1
                continue
            if card.state == "duplicate":
                notes.update(card.fragment)
                notes["drawer_reason"] = f"duplicate of {card.fragment['duplicate_of']}"
                row["status"] = "drawer"
                row["one_liner"] = row["why_line"] = None
            else:
                apply_card(row, card, None if dry_run else db)
                notes = row["card_json"]
                notes.pop("card_error", None)
                if card.state == "ok":
                    _rewrite_text(row, category, tier, notes, card.figures, ctx)
            result.states[f"{kind}:{card.state}"] += 1
        except LLMConfigError:
            raise
        except (SourceError, LLMError, CardPending) as exc:
            log.warning("card pending for %s %s: %s", row["symbol"], row["source_id"], exc)
            notes["card_error"] = str(exc)[:200]
            result.states[f"{kind}:pending"] += 1
        row["card_json"] = notes
        result.rows.append(row)
        if db is not None and not dry_run:
            db.upsert_filings([{k: v for k, v in row.items() if k != "id"}])
    return result


def _rewrite_text(row, category, tier, notes, figures, ctx: job.Context) -> None:
    """The why-line (or one-liner) again, now with the figures the code made for the card."""
    score = job._score(row, category, tier, job._when(row["filed_at"]), ctx, notes)
    row["one_liner"] = row["why_line"] = None
    try:
        job._write_text(
            row, row["status"], score, category, tier, row["subject"] or "", notes, ctx, figures
        )
    except LLMError as exc:
        notes["text_error"] = str(exc)[:200]


def since_window(now: datetime, since: timedelta) -> datetime:
    return now - since
