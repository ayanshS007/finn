"""The cards are built when the announcement job scores a filing (hook), and by the rebuild job."""

import pytest

from finn.db.models import Fact
from finn.enrich import cards
from finn.enrich.cards import Card, CardBuilder
from finn.enrich.pdf import PdfText
from finn.jobs import announcements as job
from finn.llm.router import LLMConfigError
from finn.sources.http import SourceBlocked
from tests.db_helpers import FakeSupabase
from tests.llm_helpers import ScriptedRouter
from tests.test_announcements_job import NOW, ann, fake_pdf, replies
from tests.test_results import FakeNSE, Fetcher, raw_of
from tests.test_results_card import INFY_ROW, facts_fn, no_pdf

RESULTS = dict(
    category="Outcome of Board Meeting",
    text="X has submitted the financial results for the period ended September 30, 2026.",
)
LINE = "Revenue rose to Rs 49,244 cr, up 10.7% YoY."


def a_fact():
    return Fact(ticker="INFY", metric="revenue", value_cr=49244.0, unit_original="rupee",
                period="FY27Q2", period_kind="Q", basis="consolidated", source_url="u://x",
                quote="RevenueFromOperations contextRef=OneD>492440000000", quote_match=1.0)  # fmt: skip


def ok_card(kind="results"):
    fragment = {"card_type": kind, "state": "ok", "period": "FY27Q2", "lines": [{"metric": "revenue", "value_cr": 49244.0}]}  # fmt: skip
    return Card(kind, fragment, [a_fact()], {"revenue": "Rs 49,244 cr, +10.7% YoY"})


def run(anns, build_card, db=None, router=None):
    router = router or ScriptedRouter(replies(line=LINE))
    summary = job.run_once(
        since=job.timedelta(hours=24), source="nse", symbols=["INFY", "HAL", "ITC"], db=db,
        router=router, now=NOW, fetchers={"nse": lambda s, a, b: anns}, pdf=fake_pdf,
        build_card=build_card,
    )  # fmt: skip
    return summary, router


def test_a_scored_results_filing_gets_its_card_its_facts_and_a_why_line_with_the_figures():
    db = FakeSupabase()
    seen = []

    def build(row, category, tier):
        seen.append((category, tier))
        return ok_card()

    summary, router = run([ann("1", symbol="INFY", **RESULTS)], build, db)
    row = summary.rows[0]
    assert seen == [("results", "filed")] and row["status"] == "card"
    notes = row["card_json"]
    assert notes["state"] == "ok" and notes["lines"][0]["value_cr"] == 49244.0
    assert notes["score_parts"] and notes["why_template"] and notes["tier_name"] == "filed"  # kept
    assert db.facts and db.facts[0].metric == "revenue"
    assert row["why_line"] == LINE  # the number check passed: both numbers came from the card
    why_prompt = router.calls[-1][2]
    assert "Rs 49,244 cr, +10.7% YoY" in why_prompt
    assert summary.cards["results:ok"] == 1 and "cards: results:ok=1" in "\n".join(summary.lines())


def test_a_why_line_with_a_number_the_card_does_not_hold_is_dropped():
    router = ScriptedRouter(replies(line="Revenue rose 12% to Rs 49,244 cr."))
    summary, _ = run([ann("1", symbol="INFY", **RESULTS)], lambda *a: ok_card(), FakeSupabase(), router)
    assert summary.rows[0]["why_line"] is None  # 12 is not in the figures; the template remains
    assert summary.rows[0]["card_json"]["why_template"]


def test_a_duplicate_results_filing_goes_to_the_drawer():
    dup = Card("results", {"card_type": "results", "state": "duplicate", "duplicate_of": "FIRST", "period": "FY27Q2"})  # fmt: skip
    summary, router = run([ann("2", symbol="INFY", **RESULTS)], lambda *a: dup, FakeSupabase())
    row = summary.rows[0]
    assert row["status"] == "drawer" and row["why_line"] is None
    assert row["card_json"]["drawer_reason"] == "duplicate of FIRST"
    assert row["card_json"]["duplicate_of"] == "FIRST"
    assert [c[0] for c in router.calls] == []  # no text written for a duplicate


def test_a_blocked_source_leaves_the_card_pending_and_the_filing_stored():
    def blocked(*a):
        raise SourceBlocked("NSE answered 403", 403)

    summary, _ = run([ann("1", symbol="INFY", **RESULTS)], blocked, FakeSupabase())
    row = summary.rows[0]
    assert row["status"] == "card" and "state" not in row["card_json"]
    assert "403" in row["card_json"]["card_error"] and summary.cards["card:pending"] == 1


def test_an_unexpected_error_in_a_card_does_not_lose_the_filing():
    def boom(*a):
        raise KeyError("metric")

    summary, _ = run([ann("1", symbol="INFY", **RESULTS)], boom, FakeSupabase())
    assert summary.rows[0]["status"] == "card" and "KeyError" in summary.rows[0]["card_json"]["card_error"]  # fmt: skip


def test_a_missing_api_key_still_stops_the_run():
    def no_key(*a):
        raise LLMConfigError("GEMINI_API_KEY is not set")

    with pytest.raises(LLMConfigError):
        run([ann("1", symbol="INFY", **RESULTS)], no_key, FakeSupabase())


def test_if_the_facts_cannot_be_written_the_card_shows_no_numbers():
    db = FakeSupabase()
    db.fail_facts = True
    summary, _ = run([ann("1", symbol="INFY", **RESULTS)], lambda *a: ok_card(), db)
    notes = summary.rows[0]["card_json"]
    assert "lines" not in notes and "state" not in notes and "facts table" in notes["card_error"]


def test_a_management_change_cxo_or_director_gets_a_people_card_and_a_secretary_does_not():
    seen = []

    def build(row, category, tier):
        seen.append((category, tier))
        return Card("people", {"card_type": "people", "state": "ok", "people": []})

    cxo = ann("1", symbol="HAL", category="Updates", text="Appointment of Mr Rao as Chairman and Managing Director of the Company")  # fmt: skip
    director = ann("2", symbol="HAL", category="Change in Director(s)", text="Change in Director(s) of the company")  # fmt: skip
    secretary = ann("3", symbol="HAL", category="Resignation", text="Resignation of Company Secretary")  # fmt: skip
    dividend = ann("4", symbol="ITC", category="Dividend")
    summary, _ = run([cxo, director, secretary, dividend], build, FakeSupabase())
    assert sorted(seen) == [("management_change", "cxo"), ("management_change", "director")]
    assert summary.cards["people:ok"] == 2


def test_no_card_is_built_for_a_filing_that_went_to_the_drawer():
    seen = []
    # grader score 2: the label goes to the drawer, and no card is built for it
    router = ScriptedRouter(replies(score=2))
    summary, _ = run(
        [ann("1", symbol="HAL", category="General Updates", text="Update on the management of the Company")],
        lambda *a: seen.append(a),
        FakeSupabase(),
        router,
    )
    assert summary.rows[0]["status"] == "drawer" and seen == []


# -- the CardBuilder ---------------------------------------------------------------------------------------


def builder(**kw):
    deps = dict(router=ScriptedRouter([]), facts_fn=facts_fn(FakeNSE(raw_of("INFY")), Fetcher()), pdf=no_pdf)  # fmt: skip
    return CardBuilder(**{**deps, **kw})


def test_the_builder_picks_the_card_from_category_and_tier():
    b = builder()
    card = b({**INFY_ROW}, "results", "filed")
    assert card.kind == "results" and card.state == "ok" and card.facts and card.figures
    assert b({**INFY_ROW}, "results", "notice") is None
    assert b({**INFY_ROW}, "management_change", "other_kmp") is None
    assert b({**INFY_ROW}, "dividend", None) is None
    assert cards.card_kind("management_change", "cxo") == "people"


def test_the_second_filing_for_a_period_is_a_duplicate_of_the_first_within_a_run():
    b = builder()
    first = b({**INFY_ROW, "source_id": "A"}, "results", "filed")
    second = b({**INFY_ROW, "source_id": "B"}, "results", "filed")
    assert first.state == "ok"
    assert second.state == "duplicate" and second.fragment["duplicate_of"] == "A"
    assert b({**INFY_ROW, "source_id": "A"}, "results", "filed").state == "ok"  # a rebuild of A


def test_the_builder_finds_an_earlier_card_in_the_database():
    db = FakeSupabase([{
        "symbol": "INFY", "source_id": "OLD", "filed_at": "2026-10-23T10:00:00+05:30",
        "card_json": {"card_type": "results", "period": "FY27Q2", "state": "ok"},
    }])  # fmt: skip
    card = builder(db=db)({**INFY_ROW, "source_id": "NEW"}, "results", "filed")
    assert card.state == "duplicate" and card.fragment["duplicate_of"] == "OLD"
    pending = FakeSupabase([{**db.tables["filings"][0], "card_json": {"card_type": "results", "period": "FY27Q2", "state": "pending"}}])  # fmt: skip
    assert builder(db=pending)({**INFY_ROW, "source_id": "NEW"}, "results", "filed").state == "ok"


def test_people_card_company_name_comes_from_the_row_or_the_portfolio():
    seen = {}

    class Tav:
        def linkedin(self, name, company):
            seen["company"] = company

    b = builder(tavily=Tav(), pdf=lambda url, pages=3: PdfText(url, "Mr Rao has been appointed as Managing Director of the Company.", 1, "pdfplumber"))  # fmt: skip
    b.router = ScriptedRouter([{"people": []}] * 2)
    card = b({"symbol": "HAL", "subject": "s", "attachment_url": "u", "card_json": {"company": "Hindustan Aeronautics Limited"}}, "management_change", "cxo")  # fmt: skip
    assert card.kind == "people" and card.state == "check_source"


def test_dry_run_and_facts_apply():
    db = FakeSupabase()
    row = {"card_json": {"source": "nse"}}
    cards.apply_card(row, ok_card(), None)  # no db: nothing written
    assert row["card_json"]["state"] == "ok" and row["card_json"]["source"] == "nse"
    cards.apply_card(row, ok_card(), db)
    assert len(db.facts) == 1
