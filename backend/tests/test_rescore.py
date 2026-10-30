"""Re-scoring the filings already stored: status from importance, order from recency."""

from datetime import timedelta

from finn.classify.rules import default_categories
from finn.enrich.cards import Card, CardBuilder
from finn.jobs import announcements as job
from finn.jobs import cards as cards_job
from finn.jobs import rescore
from tests.db_helpers import FakeSupabase
from tests.llm_helpers import ScriptedRouter
from tests.test_announcements_job import NOW, fake_pdf, replies

OLD = (NOW - timedelta(days=3)).isoformat()
RESULT_TEXT = "X has submitted to the Exchange, the financial results for the period ended September 30, 2026."  # fmt: skip
BOARD_NOTICE = "The meeting of the Board of Directors of the Company is scheduled on October 30, 2026, to consider and approve the unaudited financial results for the quarter ended September 30, 2026"  # fmt: skip


def row(sid, symbol="INFY", category=None, tier_name=None, status="drawer", materiality=0.0, raw="General Updates", subject="s", label_source="rules", extra=None, **cols):  # fmt: skip
    cats = default_categories()
    notes = {"source": "nse", "company": f"{symbol} Limited", "tier_name": tier_name, **(extra or {})}
    return {
        "id": f"id-{sid}", "symbol": symbol, "exchange": "NSE", "source_id": sid, "filed_at": OLD,
        "category_raw": raw, "subject": subject, "attachment_url": "u://f.pdf", "text_excerpt": None,
        "category": category, "tier": cats.tier_index(category, tier_name) if category else None,
        "label_source": label_source, "quote": None, "quote_match": None, "grader_score": None,
        "materiality": materiality, "status": status, "one_liner": None, "why_line": None,
        "card_json": notes, **cols,
    }  # fmt: skip


def stored():
    return [
        # old untiered results at 45 (recency halved it): a card now, tier filed
        row("r1", "MARUTI", "results", None, "oneliner", 45.0, "Outcome of Board Meeting", RESULT_TEXT, one_liner="Results filed."),  # fmt: skip
        # a board-meeting intimation of results was routine/unclassified: results.notice, drawer
        row("r2", "RELIANCE", "unclassified", None, "drawer", 0.0, "Analysts/Institutional Investor Meet/Con. Call Updates", BOARD_NOTICE, extra={"rule": "board_meeting_intimation"}),  # fmt: skip
        # a CXO change was a one-liner after decay; importance 85 makes it a card
        row("m1", "TRENT", "management_change", "cxo", "oneliner", 44.0, "Retirement", "Retirement of Mr Tata as the Chairman", one_liner="Chairman retires."),  # fmt: skip
        # a director change (40) was a drawer after decay; it is a one-liner now
        row("m2", "TRENT", "management_change", "director", "drawer", 20.0, "Appointment", "Appointment of Mr Dube as Non-Executive Director"),  # fmt: skip
        # label checks that failed stay in the drawer whatever the weight would say
        row("g1", "HAL", "management_change", "cxo", "drawer", 20.0, "Updates", "Change in CMD", label_source="llm", grader_score=2, extra={"drawer_reason": "grader score 2"}),  # fmt: skip
        # pending (no status yet) is left for the next announcement run
        row("p1", "ITC", None, None, None, None, "General Updates", "Winners of the Qorus awards", label_source=None),  # fmt: skip
        row("d1", "ITC", "dividend", None, "drawer", 20.0, "Dividend", "Board declared an interim dividend of Rs 7.70", ),  # fmt: skip
    ]


def make(rows=None, **kw):
    db = FakeSupabase(rows or stored())
    router = ScriptedRouter(replies(line="Chairman retires from the board."))
    ctx = job.Context(router=router, categories=default_categories(), now=NOW, pdf=fake_pdf, db=db, **kw)  # fmt: skip
    return db, router, ctx


def by_id(db):
    return {r["source_id"]: r for r in db.tables["filings"]}


def test_every_row_is_rescored_status_from_importance_and_old_results_are_relabelled():
    db, router, ctx = make()
    report = rescore.run(db, ctx)
    got = by_id(db)
    assert report.before == {"oneliner": 2, "drawer": 4, "pending": 1}
    assert report.after == {"card": 2, "oneliner": 1, "drawer": 3, "pending": 1}
    r1 = got["r1"]
    assert (r1["category"], r1["tier"], r1["card_json"]["tier_name"]) == ("results", 1, "filed")
    assert (r1["status"], r1["materiality"]) == ("card", 90.0)
    assert r1["why_line"] and r1["one_liner"] is None
    r2 = got["r2"]
    assert (r2["category"], r2["tier"], r2["status"]) == ("results", 2, "drawer")
    assert r2["materiality"] == 15.0 and r2["card_json"]["rule"] == "results_notice_board_meeting"
    m1, m2 = got["m1"], got["m2"]
    assert (m1["status"], m1["materiality"]) == ("card", 85.0) and m1["why_line"] and m1["one_liner"] is None  # fmt: skip
    assert (m2["status"], m2["materiality"]) == ("oneliner", 40.0) and m2["one_liner"]
    assert got["d1"]["status"] == "drawer" and got["d1"]["materiality"] == 25.0
    assert got["g1"]["status"] == "drawer" and got["g1"]["materiality"] == 20.0  # untouched
    assert got["p1"]["status"] is None
    assert sorted(report.relabelled) == [
        ("MARUTI", "results", "results.filed"),
        ("RELIANCE", "unclassified", "results.notice"),
    ]


def test_recency_is_kept_for_ordering_in_the_score_parts():
    db, _, ctx = make()
    rescore.run(db, ctx)
    parts = by_id(db)["m1"]["card_json"]["score_parts"]
    assert parts["score"] == 85.0 and parts["recency"] == 0.5 and parts["rank"] == 42.5
    assert by_id(db)["m1"]["card_json"]["why_template"].startswith("management_change.cxo 85 x holding 1.00 = 85")  # fmt: skip


def test_a_second_run_changes_nothing_and_calls_no_model():
    db, router, ctx = make()
    rescore.run(db, ctx)
    calls = len(router.calls)
    again = rescore.run(db, ctx)
    assert again.changed == 0 and len(router.calls) == calls and again.before == again.after


def test_dry_run_writes_nothing():
    db, _, ctx = make()
    before = [dict(r) for r in db.tables["filings"]]
    report = rescore.run(db, ctx, dry_run=True)
    assert db.tables["filings"] == before and report.after["card"] == 2


def test_a_rate_limited_text_leaves_the_status_right_and_the_template():
    from tests.llm_helpers import pending

    db = FakeSupabase(stored())
    ctx = job.Context(router=ScriptedRouter(lambda step, prompt: pending()), categories=default_categories(), now=NOW, pdf=fake_pdf, db=db)  # fmt: skip
    rescore.run(db, ctx)
    m1 = by_id(db)["m1"]
    assert m1["status"] == "card" and m1["why_line"] is None and "text_error" in m1["card_json"]


def test_needs_relabel_is_precise():
    cats = default_categories()
    assert rescore.needs_relabel(stored()[0]) and rescore.needs_relabel(stored()[1])
    others = stored()[2:]
    assert not any(rescore.needs_relabel(r) for r in others)
    # an LLM-labelled row whose rule now exists is relabelled, one the rules cannot place is not
    llm_notice = row("x", "RELIANCE", None, None, "drawer", None, "General Updates", BOARD_NOTICE, label_source="llm")  # fmt: skip
    assert rescore.needs_relabel(llm_notice)
    unplaced = row("y", "ITC", "press_release", None, "drawer", 15.0, "General Updates", "Winners of the Qorus awards", label_source="llm")  # fmt: skip
    assert not rescore.needs_relabel(unplaced) and cats.label("General Updates", "Winners of the Qorus awards") is None  # fmt: skip


def test_the_summary_lines_say_before_and_after():
    db, _, ctx = make()
    lines = rescore.run(db, ctx).lines()
    assert lines[0].startswith("before: card=0 oneliner=2 drawer=4 pending=1")
    assert lines[1].startswith("after:  card=2 oneliner=1 drawer=3 pending=1")


# -- build_cards: rebuild from the stored rows -------------------------------------------------------------


def results_rows():
    first = row("a", "INFY", "results", "filed", "card", 90.0, "Outcome of Board Meeting",
                "Infosys has submitted to the Exchange, Financial Results for the quarter and half year ended September 30, 2026")  # fmt: skip
    first["filed_at"] = (NOW - timedelta(hours=5)).isoformat()
    second = {**first, "id": "id-b", "source_id": "b", "category_raw": "General Updates"}
    second["filed_at"] = (NOW - timedelta(hours=4)).isoformat()
    second["card_json"] = dict(first["card_json"])
    return [second, first]  # out of order on purpose


def infy_builder(db, **kw):
    from tests.test_results import FakeNSE, Fetcher, raw_of
    from tests.test_results_card import facts_fn, no_pdf

    router = ScriptedRouter(replies(line="Revenue rose to Rs 49,244 cr, up 10.7% YoY."))
    ctx = job.Context(router=router, categories=default_categories(), now=NOW, db=db)
    builder = CardBuilder(router=router, db=db, pdf=no_pdf, facts_fn=facts_fn(FakeNSE(raw_of("INFY")), Fetcher()), **kw)  # fmt: skip
    return ctx, builder


def test_rebuild_builds_the_earliest_filing_and_collapses_the_later_one():
    db = FakeSupabase(results_rows())
    ctx, builder = infy_builder(db)
    result = cards_job.rebuild(db, db.tables["filings"], ctx, builder)
    got = by_id(db)
    assert result.states == {"results:ok": 1, "results:duplicate": 1}
    a, b = got["a"], got["b"]
    assert a["card_json"]["state"] == "ok" and a["status"] == "card"
    assert a["why_line"] == "Revenue rose to Rs 49,244 cr, up 10.7% YoY."  # figures from the card
    assert b["status"] == "drawer" and b["card_json"]["duplicate_of"] == "a"
    assert b["card_json"]["drawer_reason"] == "duplicate of a" and b["why_line"] is None
    assert len(db.facts) == 24  # three quarters of INFY facts, once


def test_rebuild_skips_a_card_that_exists_unless_forced_and_never_calls_twice():
    db = FakeSupabase(results_rows())
    ctx, builder = infy_builder(db)
    cards_job.rebuild(db, db.tables["filings"], ctx, builder)
    again = cards_job.rebuild(db, [r for r in db.tables["filings"] if r["status"] != "drawer"], ctx, builder)  # fmt: skip
    assert again.states == {} and again.skipped == 1
    forced = cards_job.rebuild(db, [r for r in db.tables["filings"] if r["status"] != "drawer"], ctx, builder, force=True)  # fmt: skip
    assert forced.states == {"results:ok": 1}


def test_rebuild_marks_a_blocked_source_pending_and_goes_on():
    from finn.sources.http import SourceBlocked

    def blocked(symbol, period_end=None):
        raise SourceBlocked("NSE answered 403", 403)

    db = FakeSupabase(results_rows())
    ctx, builder = infy_builder(db)
    builder.facts_fn = blocked
    result = cards_job.rebuild(db, db.tables["filings"], ctx, builder)
    assert result.states["results:pending"] == 2
    assert "403" in by_id(db)["a"]["card_json"]["card_error"] and by_id(db)["a"]["status"] == "card"


def test_rebuild_dry_run_writes_nothing():
    db = FakeSupabase(results_rows())
    ctx, builder = infy_builder(db)
    snapshot = [dict(r) for r in db.tables["filings"]]
    cards_job.rebuild(db, db.tables["filings"], ctx, builder, dry_run=True)
    assert db.tables["filings"] == snapshot and db.facts == []


def test_select_rows_asks_for_the_window_and_the_symbols():
    db = FakeSupabase(results_rows() + [row("z", "ITC", "results", "filed", "card", 90.0)])
    rows = cards_job.select_rows(db, ["INFY"], NOW - timedelta(days=7))
    assert {r["symbol"] for r in rows} == {"INFY"} and len(rows) == 2


def test_card_dataclass_state_property():
    assert Card("results", {"state": "ok"}).state == "ok"
