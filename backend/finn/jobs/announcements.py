"""Announcement job: fetch, dedupe, label, check, grade, score and upsert filings.

    python -m finn.jobs.announcements --since 24h [--every 15m] [--source nse|rss|bse|fixtures]
                                      [--dry-run]

Per filing: the regex rules label it first (no model). Only a filing the rules cannot place, or
whose tier they cannot pick, or a press_release catch-all, goes to the small model, which must quote
the text (quote_match >= 0.4, one retry, else the drawer); a second small-model call grades the
label (1-3 goes to the drawer). The materiality score then decides card / one-liner / drawer, and a
card gets a why_line, a one-liner a one-liner, both behind the number check.

Free tier only: a rate-limited call fails over once and then leaves the filing *pending* (status
NULL); the next run picks it up again. Nothing loops. The actual cost of a run is always zero.

Default source order is nse, then rss, then bse when the one before it fails. `--source fixtures`
is the labelled replay mode: it reads the committed test fixtures and never writes to Supabase.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from finn.classify import grader, llm_label
from finn.classify.rules import Categories, RuleLabel, default_categories
from finn.config import REPO_ROOT, load_portfolio, portfolio_names, portfolio_symbols
from finn.db.client import Db
from finn.enrich.cards import Card, CardBuilder, apply_card, card_kind
from finn.enrich.pdf import pdf_excerpt
from finn.enrich.results_card import CardPending
from finn.llm.router import (
    ACTUAL_COST_INR,
    DbStore,
    LLMConfigError,
    LLMError,
    LLMOutputError,
    MemoryStore,
    Router,
)
from finn.score.materiality import MaterialityError, Score, score_filing, status_for
from finn.sources import bse, nse, nse_rss
from finn.sources.http import SourceError
from finn.sources.shapes import IST
from finn.summarise import oneliner

log = logging.getLogger("finn.jobs.announcements")

SOURCE_ORDER = ("nse", "rss", "bse")
FIXTURE_DIR = REPO_ROOT / "backend" / "tests" / "fixtures"
FLUSH_EVERY = 10  # rows per Supabase upsert, so a long run keeps its progress
EXCERPT_STORED = 3000  # characters of attachment text kept in filings.text_excerpt
STATUSES = ("card", "oneliner", "drawer", "pending")


# -- small helpers -----------------------------------------------------------------------------


def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"{text!r}: use a number and m, h or d (15m, 24h, 3d)")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def category_raw_of(ann: dict) -> str | None:
    """NSE gives one category; BSE a category and a subcategory, joined for the rules."""
    parts = [p for p in (ann.get("category"), ann.get("subcategory")) if p]
    return " | ".join(parts) or None


def _when(iso: str | None) -> datetime | None:
    """Parse an ISO time with offset. Unknown or malformed gives None: the filing is still kept."""
    try:
        when = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        log.warning("unreadable time %r; treating the filing as undated", iso)
        return None
    return when if when is None or when.tzinfo else when.replace(tzinfo=IST)


# -- sources -----------------------------------------------------------------------------------


def fetch_nse(symbols: list[str], since: datetime, now: datetime) -> list[dict]:
    client = nse.NSEClient()
    out: list[dict] = []
    for symbol in symbols:
        out += client.announcements(symbol, since.date(), now.date(), ttl=0)
    return out


def fetch_rss(symbols: list[str], since: datetime, now: datetime) -> list[dict]:
    rows = nse_rss.fetch(symbols, portfolio_names(), ttl=0)
    return [r for r in rows if r["symbol"] in symbols]


def fetch_bse(symbols: list[str], since: datetime, now: datetime) -> list[dict]:
    out: list[dict] = []
    for symbol in symbols:
        code = bse.scrip_code(symbol)
        out += bse.announcements(code, since.date(), now.date(), symbol=symbol, ttl=0)
    return out


def fetch_fixtures(symbols: list[str], since: datetime, now: datetime) -> list[dict]:
    """REPLAY MODE: committed test fixtures, never live data, never written to Supabase."""
    log.warning("REPLAY MODE: reading test fixtures, not a live source; nothing is written")
    out: list[dict] = []
    for path in sorted(FIXTURE_DIR.glob("nse_announcements_*.json")):
        out += nse.parse_announcements(json.loads(path.read_text()))
    out += bse.parse_announcements(
        json.loads((FIXTURE_DIR / "bse_announcements_INFY.json").read_text()), "INFY"
    )
    return [r for r in out if r["symbol"] in symbols]


FETCHERS = {"nse": fetch_nse, "rss": fetch_rss, "bse": fetch_bse, "fixtures": fetch_fixtures}


def fetch_announcements(
    source: str | None, symbols: list[str], since: datetime, now: datetime, fetchers=None
) -> tuple[str, list[dict]]:
    """Fetch from one source, or down the default order on failure. Returns (source, rows)."""
    fetchers = fetchers or FETCHERS
    order = (source,) if source else SOURCE_ORDER
    failures: list[str] = []
    for name in order:
        try:
            return name, fetchers[name](symbols, since, now)
        except SourceError as exc:  # blocked or reshaped: say so and try the next one
            log.error("source %s failed: %s", name, exc)
            failures.append(f"{name}: {exc}")
    raise SourceError("every source failed: " + "; ".join(failures))


# -- the pipeline ------------------------------------------------------------------------------


@dataclass
class Context:
    router: Router
    categories: Categories
    now: datetime
    holdings: dict[str, float] | None = None
    price_volume: set[tuple[str, str]] = field(default_factory=set)  # (symbol, IST date)
    pdf: Callable = pdf_excerpt
    db: Db | None = None
    # (row, category, tier) -> Card | None; None builds no cards
    build_card: Callable[[dict, str, str | None], Card | None] | None = None


def process(ann: dict, source: str, ctx: Context) -> dict:
    """One announcement -> one filings row (every column present, so bulk upserts are uniform)."""
    cats = ctx.categories
    raw, subject = category_raw_of(ann), ann.get("text") or ""
    filed_at = _when(ann.get("published_at"))
    notes: dict = {"source": source, "company": ann.get("company")}

    row = {
        "symbol": ann["symbol"],
        "exchange": ann["exchange"],
        "source_id": ann["source_id"],
        "filed_at": filed_at.isoformat() if filed_at else None,
        "category_raw": raw,
        "subject": subject or None,
        "attachment_url": ann.get("attachment_url"),
        "text_excerpt": None,
        "category": None,
        "tier": None,
        "label_source": None,
        "quote": None,
        "quote_match": None,
        "grader_score": None,
        "materiality": None,
        "status": None,
        "one_liner": None,
        "why_line": None,
        "card_json": notes,
    }

    def finish(category, tier, status, reason=None):
        row.update(category=category, tier=cats.tier_index(category, tier), status=status)
        notes["tier_name"] = tier
        if reason:
            notes["drawer_reason"] = reason
        return row

    hint = cats.label(raw, subject)
    drawer_reason: str | None = None
    category, tier = (hint.category, hint.tier) if hint else (None, None)
    if hint:
        notes["rule"] = hint.rule
        row["label_source"] = "rules"

    if hint is None or hint.llm_review or hint.needs_tier:
        try:
            category, tier, drawer_reason = _llm_label(ann, raw, subject, hint, row, notes, ctx)
        except LLMConfigError:
            raise
        except LLMError as exc:  # rate limited on both providers, or the API is unwell
            log.warning("pending %s: %s", ann["source_id"], exc)
            notes["pending"] = str(exc)[:200]
            row["label_source"] = None
            return finish(None, None, None)

    if category is None or not cats.is_valid(category, tier):
        return finish(category, tier, "drawer", drawer_reason or "no valid category and tier")

    try:
        score = _score(row, category, tier, filed_at, ctx, notes)
    except MaterialityError as exc:
        return finish(category, tier, "drawer", str(exc))
    status = "drawer" if drawer_reason else status_for(score.score)
    finish(category, tier, status, drawer_reason)

    card: Card | None = None
    if status in ("card", "oneliner") and ctx.build_card and card_kind(category, tier):
        card = _build_card(row, category, tier, notes, ctx)
        if card is not None and card.state == "duplicate":
            return finish(category, tier, "drawer", f"duplicate of {card.fragment['duplicate_of']}")

    if status in ("card", "oneliner"):
        try:
            figures = card.figures if card else None
            _write_text(row, status, score, category, tier, subject, notes, ctx, figures)
        except LLMConfigError:
            raise
        except LLMError as exc:  # leave the text empty; the code-written why_template remains
            log.warning("no text for %s: %s", ann["source_id"], exc)
            notes["text_error"] = str(exc)[:200]
    return row


def _llm_label(ann, raw, subject, hint: RuleLabel | None, row, notes, ctx: Context):
    """Returns (category, tier, drawer_reason). Raises LLMPending/LLMError for 'pending'."""
    excerpt = ctx.pdf(ann.get("attachment_url"))
    text = excerpt.text if excerpt else ""
    row["text_excerpt"] = text[:EXCERPT_STORED] or None
    notes["pdf"] = {"pages": excerpt.pages, "engine": excerpt.engine} if excerpt else None

    try:
        label = llm_label.label_with_llm(
            raw, subject, text, hint, router=ctx.router, categories=ctx.categories
        )
    except LLMOutputError:
        label = llm_label.LlmLabel(False, None, None, None, 0.0, "no usable JSON")
    row["quote"], row["quote_match"] = label.quote, round(label.quote_match, 4)
    notes["label_attempts"] = label.attempts

    if not label.ok:
        # The catch-all keeps its rule label; anything else is category unknown. Drawer either way.
        row["label_source"] = "rules" if hint else "llm"
        keep = (hint.category, hint.tier) if hint else (None, None)
        return (*keep, f"label check failed: {label.reason}")

    row["label_source"] = "llm"
    category, tier = label.category, label.tier
    if label.note:
        notes["label_note"] = label.note
    if hint and hint.llm_review and category == "unclassified":
        category, tier = hint.category, hint.tier  # the exchange called it a press release
    grade = grader.grade_label(
        category, tier, label.quote, router=ctx.router, categories=ctx.categories
    )
    row["grader_score"] = grade.score
    notes["llm_label"] = {"category": label.category, "tier": label.tier}
    if not grade.passed:
        return category, tier, f"grader score {grade.score}"
    return category, tier, None


def _score(row, category, tier, filed_at, ctx: Context, notes) -> Score:
    fired = (
        (row["symbol"], filed_at.astimezone(IST).date().isoformat()) in ctx.price_volume
        if filed_at
        else False
    )
    score = score_filing(
        category,
        tier,
        row["symbol"],
        filed_at,
        ctx.now,
        price_volume_fired=fired,
        holdings=ctx.holdings,
    )
    row["materiality"] = score.score
    notes["score_parts"] = score.parts()
    notes["why_template"] = score.why_template()
    return score


def _build_card(row, category, tier, notes, ctx: Context) -> Card | None:
    """Build the results or people card. A source or model that is unavailable leaves the card
    pending (card_json.card_error); the filing is still stored and build_cards.py can retry."""
    try:
        card = ctx.build_card(row, category, tier)
        if card is not None:
            apply_card(row, card, ctx.db)  # may raise: the facts must be stored with the card
            notes.update(row["card_json"])
            row["card_json"] = notes
        return card
    except LLMConfigError:
        raise
    except (SourceError, LLMError, CardPending) as exc:
        log.warning("card pending for %s %s: %s", row["symbol"], row["source_id"], exc)
        notes["card_error"] = str(exc)[:200]
    except Exception as exc:  # a card must never lose the filing it belongs to
        log.exception("card failed for %s %s", row["symbol"], row["source_id"])
        notes["card_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return None


def _write_text(
    row, status, score: Score, category, tier, subject, notes, ctx: Context, figures=None
):
    if status == "card":
        out = oneliner.write_why_line(
            subject, category, tier, score.parts(), figures, router=ctx.router
        )
        row["why_line"] = out.text
    else:
        out = oneliner.write_one_liner(subject, category, tier, figures, router=ctx.router)
        row["one_liner"] = out.text
    if out.text is None:
        notes["text_dropped_numbers"] = list(out.dropped_numbers)  # failed the number check


# -- database ----------------------------------------------------------------------------------


def already_done(db: Db | None, ids: list[str]) -> set[str]:
    """source_ids already stored with a status. Pending rows (status NULL) are retried."""
    if db is None or not ids:
        return set()
    done: set[str] = set()
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        rows = (
            db.client.table("filings")
            .select("source_id,status")
            .in_("source_id", chunk)
            .execute()
            .data
        )
        done |= {r["source_id"] for r in rows if r["status"] is not None}
    return done


def price_volume_days(db: Db | None, since: datetime) -> set[tuple[str, str]]:
    """(symbol, IST date) pairs on which a price_move or volume_spike rule fired. Empty until S5
    writes alerts; a read failure is logged and treated as none."""
    if db is None:
        return set()
    try:
        kinds = {
            r["id"]: r["kind"]
            for r in db.client.table("rules").select("id,kind").execute().data
            if r["kind"] in ("price_move", "volume_spike")
        }
        alerts = (
            db.client.table("alerts")
            .select("symbol,rule_id,created_at")
            .gte("created_at", since.isoformat())
            .execute()
            .data
        )
    except Exception as exc:
        log.warning(
            "price/volume alerts not read (%s); no same-day bonus this run", type(exc).__name__
        )
        return set()
    return {
        (a["symbol"], datetime.fromisoformat(a["created_at"]).astimezone(IST).date().isoformat())
        for a in alerts
        if a["rule_id"] in kinds
    }


# -- run ---------------------------------------------------------------------------------------


@dataclass
class Summary:
    source: str
    fetched: int = 0
    in_window: int = 0
    skipped_existing: int = 0
    skipped_other_symbol: int = 0
    errors: int = 0
    by_status: Counter = field(default_factory=Counter)
    by_category: Counter = field(default_factory=Counter)  # (category, status)
    cards: Counter = field(default_factory=Counter)  # "results:ok", "people:check_source", ...
    rules_labelled: int = 0
    llm_labelled: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    paid_equivalent_inr: float = 0.0
    rows: list[dict] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"source={self.source} fetched={self.fetched} in_window={self.in_window} "
            f"skipped_existing={self.skipped_existing} errors={self.errors}",
            "status: " + " ".join(f"{s}={self.by_status.get(s, 0)}" for s in STATUSES),
            f"labels: rules={self.rules_labelled} llm={self.llm_labelled}",
            "cards: " + (" ".join(f"{k}={v}" for k, v in sorted(self.cards.items())) or "none"),
            f"llm calls: {self.llm_calls} ({self.cache_hits} cache hits), paid-equivalent "
            f"Rs {self.paid_equivalent_inr:.2f}; actual cost Rs {ACTUAL_COST_INR:.2f} (free tier)",
        ]
        return out

    def table(self) -> str:
        """Markdown table of category x status counts, for logs and the PR description."""
        cats = sorted({c for c, _ in self.by_category})
        head = (
            "| category | "
            + " | ".join(STATUSES)
            + " | total |\n|---|"
            + "---|" * (len(STATUSES) + 1)
        )
        rows = []
        for c in cats:
            counts = [self.by_category.get((c, s), 0) for s in STATUSES]
            rows.append(f"| {c} | " + " | ".join(map(str, counts)) + f" | {sum(counts)} |")
        return "\n".join([head, *rows])


def run_once(
    *,
    since: timedelta,
    source: str | None,
    symbols: list[str] | None = None,
    db: Db | None = None,
    router: Router | None = None,
    now: datetime | None = None,
    fetchers=None,
    pdf=pdf_excerpt,
    cards: bool = True,
    build_card=None,
) -> Summary:
    now = now or datetime.now(IST)
    symbols = symbols or portfolio_symbols()
    window_start = now - since
    name, fetched = fetch_announcements(source, symbols, window_start, now, fetchers)
    summary = Summary(source=name, fetched=len(fetched))
    log.info("fetched %d announcements from %s", len(fetched), name)

    # dedupe by source_id and keep the window
    seen: dict[str, dict] = {}
    for ann in fetched:
        if not ann.get("symbol") or ann["symbol"] not in symbols or not ann.get("source_id"):
            summary.skipped_other_symbol += 1
            continue
        when = _when(ann.get("published_at"))
        if when is not None and when < window_start:
            continue
        seen.setdefault(ann["source_id"], ann)
    summary.in_window = len(seen)
    done = already_done(db, list(seen))
    todo = [a for sid, a in seen.items() if sid not in done]
    summary.skipped_existing = len(seen) - len(todo)
    todo.sort(key=lambda a: a.get("published_at") or "")

    router = router or Router(store=DbStore(db) if db else MemoryStore())
    store = router.store
    calls_before = len(store.calls)
    ctx = Context(
        router=router,
        categories=default_categories(),
        now=now,
        holdings=load_portfolio().get("weights") or {},
        price_volume=price_volume_days(db, window_start - timedelta(days=1)),
        pdf=pdf,
        db=db,
        build_card=build_card or (CardBuilder(router=router, db=db) if cards else None),
    )

    unsaved: list[dict] = []

    def flush() -> None:
        if db is not None and unsaved:
            db.upsert_filings(unsaved)
            unsaved.clear()

    try:
        for ann in todo:
            try:
                row = process(ann, name, ctx)
            except LLMConfigError:
                raise
            except Exception:  # one bad filing must not stop the run
                log.exception("could not process %s %s", ann.get("symbol"), ann.get("source_id"))
                summary.errors += 1
                continue
            summary.rows.append(row)
            unsaved.append(row)
            state = row["card_json"].get("state") or (
                "pending" if row["card_json"].get("card_error") else None
            )
            if state:
                summary.cards[f"{row['card_json'].get('card_type', 'card')}:{state}"] += 1
            status = row["status"] or "pending"
            summary.by_status[status] += 1
            summary.by_category[
                (
                    row["category"] or ("(pending)" if status == "pending" else "(unlabelled)"),
                    status,
                )
            ] += 1
            if row["label_source"] == "rules":
                summary.rules_labelled += 1
            elif row["label_source"] == "llm":
                summary.llm_labelled += 1
            if len(unsaved) >= FLUSH_EVERY:
                flush()
    finally:
        flush()  # what is done is kept even if the run stops early

    new_calls = store.calls[calls_before:]
    summary.llm_calls = sum(1 for c in new_calls if not c["cache_hit"])
    summary.cache_hits = sum(1 for c in new_calls if c["cache_hit"])
    summary.paid_equivalent_inr = round(sum(c["cost_inr"] for c in new_calls), 4)

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m finn.jobs.announcements")
    parser.add_argument("--since", type=parse_duration, default=timedelta(hours=24))
    parser.add_argument("--every", type=parse_duration, help="repeat at this interval (e.g. 15m)")
    parser.add_argument(
        "--source", choices=sorted(FETCHERS), help="default: nse, then rss, then bse"
    )
    parser.add_argument("--symbol", action="append", help="only this symbol (repeatable)")
    parser.add_argument(
        "--dry-run", action="store_true", help="classify but do not write to Supabase"
    )
    parser.add_argument("--report", type=Path, help="write the summary and table here (markdown)")
    args = parser.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")  # the laptop keeps its keys here; real environment wins
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "hpack"):  # request lines carry URLs; keep logs readable
        logging.getLogger(noisy).setLevel(logging.WARNING)

    replay = args.source == "fixtures"
    db = None if (args.dry_run or replay) else Db.service()
    if db is None:
        log.warning("not writing to Supabase (%s)", "replay mode" if replay else "--dry-run")

    while True:
        try:
            summary = run_once(
                since=args.since, source=args.source, db=db, symbols=args.symbol
            )
        except SourceError as exc:
            log.error("%s", exc)  # stop and report; fixtures are never a stand-in for a live source
            return 1
        for line in summary.lines():
            log.info(line)
        log.info("\n%s", summary.table())
        if args.report:
            args.report.write_text("\n".join(summary.lines()) + "\n\n" + summary.table() + "\n")
        if not args.every:
            return 0
        time.sleep(args.every.total_seconds())


if __name__ == "__main__":
    sys.exit(main())
