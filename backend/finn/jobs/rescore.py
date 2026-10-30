"""Re-label and re-score the filings already stored, after a change to the rules or the weights.

    python scripts/rescore_filings.py [--dry-run]

1. A row whose stored label no longer matches a definitive rule (or whose category is the old
   untiered `results`) is processed again through the announcement pipeline, so it gets the label,
   the tier, the score and the text a new filing would get.
2. Every other row keeps its label and gets its score recomputed from the stored parts (category,
   tier, symbol, filed_at): status comes from importance alone, recency only from the order.
   A row that went to the drawer because a check failed (quote, grader, duplicate) stays there.

No cards are built here (scripts/build_cards.py does that). Reads and writes go through supabase-py.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from finn.classify.rules import default_categories
from finn.config import load_portfolio
from finn.db.client import Db
from finn.jobs import announcements as job
from finn.llm.router import LLMError
from finn.score.materiality import MaterialityError, status_for
from finn.sources.shapes import IST

log = logging.getLogger("finn.jobs.rescore")

COLUMNS = (
    "id,symbol,exchange,source_id,filed_at,category_raw,subject,attachment_url,text_excerpt,"
    "category,tier,label_source,quote,quote_match,grader_score,materiality,status,one_liner,"
    "why_line,card_json"
)
CHECK_FAILED = ("drawer_reason",)  # card_json keys that keep a row in the drawer


def fetch_all(db: Db, page: int = 500) -> list[dict]:
    rows: list[dict] = []
    while True:
        chunk = (
            db.client.table("filings")
            .select(COLUMNS)
            .order("filed_at")
            .range(len(rows), len(rows) + page - 1)
            .execute()
            .data
        )
        rows += chunk
        if len(chunk) < page:
            return rows


def needs_relabel(row: dict) -> bool:
    """True when a definitive rule now labels the row differently, or its label predates tiers."""
    cats = default_categories()
    notes = row.get("card_json") or {}
    stored = (row["category"], notes.get("tier_name"))
    if row["category"] == "results" and stored[1] is None:
        return True
    hint = cats.label(row["category_raw"], row["subject"])
    if hint is None or hint.llm_review or hint.needs_tier:
        return False
    return (hint.category, hint.tier) != stored


def as_announcement(row: dict) -> dict:
    notes = row.get("card_json") or {}
    return {
        "exchange": row["exchange"],
        "symbol": row["symbol"],
        "company": notes.get("company"),
        "category": row["category_raw"],
        "subcategory": None,
        "text": row["subject"],
        "attachment_url": row["attachment_url"],
        "source_id": row["source_id"],
        "published_at": row["filed_at"],
        "has_xbrl": None,
    }


def rescored(row: dict, ctx: job.Context) -> dict:
    """The row with its materiality, status, score parts and text recomputed from stored parts."""
    notes = dict(row.get("card_json") or {})
    out = {**row, "card_json": notes}
    category, tier = row["category"], notes.get("tier_name")
    held = (
        row["status"] is None  # pending: the next announcement run labels it
        or category is None
        or any(notes.get(k) for k in CHECK_FAILED)
        or not default_categories().is_valid(category, tier)
    )
    if held:
        return out
    filed_at = job._when(row["filed_at"])
    try:
        score = job._score(out, category, tier, filed_at, ctx, notes)
    except MaterialityError as exc:
        out.update(status="drawer")
        notes["drawer_reason"] = str(exc)
        return out
    status = status_for(score.score)
    out["status"] = status
    if status == "card":
        out["one_liner"] = None
    elif status == "oneliner":
        out["why_line"] = None
    else:
        out["one_liner"] = out["why_line"] = None
    missing = (status == "card" and not out["why_line"]) or (
        status == "oneliner" and not out["one_liner"]
    )
    if missing:  # an existing line is kept: it was number-checked when it was written
        try:
            job._write_text(out, status, score, category, tier, row["subject"] or "", notes, ctx)
        except LLMError as exc:  # rate limited: the code-written why_template remains
            notes["text_error"] = str(exc)[:200]
    return out


@dataclass
class Report:
    before: Counter = field(default_factory=Counter)
    after: Counter = field(default_factory=Counter)
    relabelled: list[tuple[str, str, str]] = field(default_factory=list)  # symbol, was, now
    changed: int = 0

    def lines(self) -> list[str]:
        order = ("card", "oneliner", "drawer", "pending")
        fmt = lambda c: " ".join(f"{s}={c.get(s, 0)}" for s in order)  # noqa: E731
        out = [f"before: {fmt(self.before)}", f"after:  {fmt(self.after)}"]
        out.append(f"rows changed: {self.changed}; relabelled: {len(self.relabelled)}")
        out += [f"  relabel {s}: {a} -> {b}" for s, a, b in self.relabelled]
        return out


def _label(row: dict) -> str:
    tier = (row.get("card_json") or {}).get("tier_name")
    return f"{row['category'] or 'unlabelled'}{'.' + tier if tier else ''}"


def run(db: Db, ctx: job.Context, *, dry_run: bool = False, now: datetime | None = None) -> Report:
    ctx.now = now or ctx.now
    rows = fetch_all(db)
    report = Report(before=Counter(r["status"] or "pending" for r in rows))
    out_rows: list[dict] = []
    for row in rows:
        if needs_relabel(row):
            try:
                new = job.process(as_announcement(row), row["card_json"].get("source", "nse"), ctx)
            except LLMError as exc:
                log.warning("not relabelled %s: %s", row["source_id"], exc)
                new = rescored(row, ctx)
            else:
                new["id"] = row["id"]
                report.relabelled.append((row["symbol"], _label(row), _label(new)))
        else:
            new = rescored(row, ctx)
        out_rows.append(new)
        if _differs(row, new):
            report.changed += 1
    report.after = Counter(r["status"] or "pending" for r in out_rows)
    if not dry_run:
        changed = [n for o, n in zip(rows, out_rows, strict=True) if _differs(o, n)]
        for start in range(0, len(changed), job.FLUSH_EVERY):
            db.upsert_filings([_drop_id(r) for r in changed[start : start + job.FLUSH_EVERY]])
    return report


def _drop_id(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "id"}


def _differs(old: dict, new: dict) -> bool:
    keys = ("category", "tier", "status", "materiality", "one_liner", "why_line", "card_json",
            "label_source", "quote", "grader_score")  # fmt: skip
    return any(old.get(k) != new.get(k) for k in keys)


def default_context(db: Db | None, router) -> job.Context:
    now = datetime.now(IST)
    return job.Context(
        router=router,
        categories=default_categories(),
        now=now,
        holdings=load_portfolio().get("weights") or {},
        price_volume=job.price_volume_days(db, now - timedelta(days=30)),
        db=db,
        build_card=None,
    )
