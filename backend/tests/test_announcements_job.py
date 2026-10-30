from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from finn.enrich.pdf import PdfText
from finn.jobs import announcements as job
from finn.llm.router import LLMConfigError
from finn.sources.http import SourceBlocked, SourceError
from finn.sources.shapes import IST
from tests.llm_helpers import ScriptedRouter, pending

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=IST)
PDF_TEXT = (
    "Hindustan Aeronautics Limited. Intimation of change in Managing Director. The Board approved "
    "the appointment of Mr Rao as Chairman and Managing Director of the Company from today."
)
QUOTE = "approved the appointment of Mr Rao as Chairman and Managing Director of the Company"


def ann(source_id="1", symbol="INFY", category="Dividend", text="Board declared Interim Dividend of Rs. 7.70 per equity share",
        hours_ago=2, url="https://x/f.pdf"):  # fmt: skip
    return {
        "exchange": "NSE", "symbol": symbol, "company": f"{symbol} Limited", "category": category,
        "subcategory": None, "text": text, "attachment_url": url, "source_id": source_id,
        "published_at": (NOW - timedelta(hours=hours_ago)).isoformat(), "has_xbrl": False,
    }  # fmt: skip


def fake_pdf(url):
    return PdfText(url or "", PDF_TEXT, 2, "pdfplumber") if url else None


def replies(label=None, score=5, line="Board declared an interim dividend."):
    def reply(step, prompt):
        if step == "label":
            return label or {"category": "management_change", "tier": "cxo", "quote": QUOTE}
        if step == "grade":
            return {"score": score}
        return {"line": line}

    return reply


class FakeDb:
    """Just enough of the supabase-py chain for the job: select().in_().execute() and upserts."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.upserted: list[dict] = []
        self.client = self

    def table(self, name):
        assert name in ("filings", "rules", "alerts")
        return FakeQuery(self.existing if name == "filings" else [])

    def upsert_filings(self, rows):
        self.upserted += rows
        return len(rows)


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_):
        return self

    def in_(self, column, values):
        self.rows = [r for r in self.rows if r[column] in values]
        return self

    def gte(self, *_):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


def run(anns, router=None, db=None, source="nse", symbols=None, pdf=fake_pdf):
    router = router or ScriptedRouter(replies())
    summary = job.run_once(
        since=timedelta(hours=24), source=source, symbols=symbols or ["INFY", "HAL", "ITC"], db=db,
        router=router, now=NOW, fetchers={source: lambda s, a, b: anns}, pdf=pdf,
    )  # fmt: skip
    return summary, router


# -- the row ----------------------------------------------------------------------------------


def test_a_rule_labelled_filing_uses_no_model_for_its_label_and_gets_a_one_liner():
    summary, router = run([ann()])
    [row] = summary.rows
    assert (row["category"], row["label_source"], row["card_json"]["rule"]) == (
        "dividend",
        "rules",
        "dividend_in_text",
    )
    assert row["quote"] is None and row["grader_score"] is None and row["text_excerpt"] is None
    # dividend 25 x holding 1.0 x recency (2 h of 24: 0.9583) = 23.96: below 30, so the drawer
    assert row["status"] == "drawer" and row["one_liner"] is None
    assert [c[0] for c in router.calls] == []  # no model call at all
    assert (
        summary.by_status["drawer"] == 1 and summary.rules_labelled == 1 and summary.llm_calls == 0
    )


def test_a_30_to_59_score_gets_a_one_liner_and_a_60_plus_score_gets_a_why_line():
    capex = ann(
        "2", text="Board approves capex of Rs 400 crore for capacity expansion", category="Updates"
    )
    results = ann("3", category="Outcome of Board Meeting", text="X has submitted the financial results for the period ended September 30, 2026.")  # fmt: skip
    summary, router = run(
        [capex, results], ScriptedRouter(replies(line="Board approves a capacity expansion."))
    )
    by = {r["source_id"]: r for r in summary.rows}
    assert by["2"]["status"] == "oneliner" and by["2"]["one_liner"] and by["2"]["why_line"] is None
    assert by["3"]["status"] == "card" and by["3"]["why_line"] and by["3"]["one_liner"] is None
    assert [c[0] for c in router.calls] == ["oneliner", "why_line"]
    assert by["3"]["materiality"] == pytest.approx(90 * (1 - 2 / 24 * 0.5))
    assert by["3"]["card_json"]["why_template"].startswith("results 90 x holding 1.00")


def test_every_column_is_present_and_tier_is_the_smallint_position():
    cxo = ann(
        "4",
        symbol="HAL",
        category="Change in Director(s)",
        text="Change in Managing Director and Chief Executive Officer",
    )
    [row] = run([cxo])[0].rows
    assert set(row) == {
        "symbol", "exchange", "source_id", "filed_at", "category_raw", "subject", "attachment_url",
        "text_excerpt", "category", "tier", "label_source", "quote", "quote_match", "grader_score",
        "materiality", "status", "one_liner", "why_line", "card_json",
    }  # fmt: skip
    assert (row["category"], row["tier"], row["card_json"]["tier_name"]) == (
        "management_change",
        1,
        "cxo",
    )


# -- the LLM path -----------------------------------------------------------------------------


def test_llm_label_with_a_good_quote_and_a_passing_grade_is_scored_normally():
    generic = ann(
        "5",
        symbol="HAL",
        category="General Updates",
        text="HAL has informed the Exchange about an update",
    )
    summary, router = run([generic])
    [row] = summary.rows
    assert (row["category"], row["tier"], row["label_source"]) == ("management_change", 1, "llm")
    assert row["quote"] == QUOTE and row["quote_match"] >= 0.99 and row["grader_score"] == 5
    assert row["text_excerpt"] == PDF_TEXT and row["status"] == "card"
    assert [c[0] for c in router.calls] == ["label", "grade", "why_line"]
    assert summary.llm_labelled == 1


def test_grader_score_1_to_3_sends_the_label_to_the_drawer_with_its_score_kept():
    generic = ann("6", symbol="HAL", category="General Updates", text="HAL update")
    summary, _ = run([generic], ScriptedRouter(replies(score=3)))
    [row] = summary.rows
    assert row["status"] == "drawer" and row["grader_score"] == 3
    assert row["card_json"]["drawer_reason"] == "grader score 3" and row["why_line"] is None
    assert row["materiality"] > 0  # the score is still shown, for the drawer's sort order


def test_quote_that_is_not_in_the_text_retries_once_and_then_the_drawer():
    bad = {
        "category": "M&A",
        "tier": None,
        "quote": "a completely invented sentence about buying a rival company",
    }
    generic = ann("7", symbol="HAL", category="General Updates", text="HAL update")
    summary, router = run([generic], ScriptedRouter(replies(label=bad)))
    [row] = summary.rows
    assert [c[0] for c in router.calls] == ["label", "label"]  # no grading of a failed label
    assert row["status"] == "drawer" and row["category"] is None and row["grader_score"] is None
    assert "label check failed" in row["card_json"]["drawer_reason"]
    assert row["quote_match"] < 0.4


def test_a_press_release_catch_all_that_the_model_cannot_place_stays_a_press_release():
    pr = ann(
        "8", category="Press Release", text="a press release dated October 29, 2026, titled Awards"
    )
    unclassified = {"category": "unclassified", "tier": None, "quote": QUOTE}
    [row] = run([pr], ScriptedRouter(replies(label=unclassified)))[0].rows
    assert row["category"] == "press_release" and row["label_source"] == "llm"


def test_a_press_release_can_be_moved_to_a_specific_category():
    pr = ann("9", category="Press Release", text="a press release titled Capacity")
    capex = {"category": "capex", "tier": None, "quote": QUOTE}
    [row] = run([pr], ScriptedRouter(replies(label=capex)))[0].rows
    assert row["category"] == "capex"


def test_a_category_with_no_tier_in_the_text_asks_the_model_for_the_tier():
    cm = ann(
        "10",
        symbol="HAL",
        category="Change in Management",
        text="HAL has informed the Exchange about change in Management",
    )
    summary, router = run([cm], ScriptedRouter(replies(label={"category": "management_change", "tier": "other_kmp", "quote": QUOTE})))  # fmt: skip
    [row] = summary.rows
    assert (row["category"], row["card_json"]["tier_name"], row["label_source"]) == (
        "management_change",
        "other_kmp",
        "llm",
    )
    assert row["status"] == "drawer"  # other KMP: 10 points


# -- rate limits: pending, never a loop ------------------------------------------------------


def test_a_rate_limited_filing_is_stored_pending_and_the_run_carries_on():
    router = ScriptedRouter(
        lambda step, prompt: pending() if step == "label" else replies()(step, prompt)
    )
    generic = ann("11", symbol="HAL", category="General Updates", text="HAL update")
    capex = ann("12", text="Board approves capex for capacity expansion", category="Updates")
    summary, _ = run([generic, capex], router)
    by = {r["source_id"]: r for r in summary.rows}
    assert (
        by["11"]["status"] is None
        and "pending" in by["11"]["card_json"]
        and by["11"]["category"] is None
    )
    assert by["12"]["status"] == "oneliner"  # the rules-labelled filing is unaffected
    assert summary.by_status["pending"] == 1 and len(router.calls) == 2  # one label try, no loop


def test_pending_rows_are_retried_on_the_next_run_but_done_rows_are_skipped():
    existing = [{"source_id": "11", "status": None}, {"source_id": "12", "status": "oneliner"}]
    generic = ann("11", symbol="HAL", category="General Updates", text="HAL update")
    capex = ann("12", text="Board approves capex for capacity expansion", category="Updates")
    db = FakeDb(existing)
    summary, _ = run([generic, capex], db=db)
    assert [r["source_id"] for r in summary.rows] == ["11"] and summary.skipped_existing == 1
    assert [r["source_id"] for r in db.upserted] == ["11"]


def test_a_missing_api_key_stops_the_run_instead_of_marking_everything_pending():
    router = ScriptedRouter([LLMConfigError("GROQ_API_KEY is not set")])
    generic = ann("13", symbol="HAL", category="General Updates", text="HAL update")
    with pytest.raises(LLMConfigError):
        run([generic], router)


def test_one_unexpected_error_does_not_stop_the_run():
    def pdf_that_breaks_once(url):
        if url == "https://x/boom.pdf":
            raise RuntimeError("disk full")
        return fake_pdf(url)

    broken = ann(
        "14", symbol="HAL", category="General Updates", text="HAL update", url="https://x/boom.pdf"
    )
    ok = ann("15", text="Board approves capex for capacity expansion", category="Updates")
    summary, _ = run([broken, ok], pdf=pdf_that_breaks_once)
    assert summary.errors == 1 and [r["source_id"] for r in summary.rows] == ["15"]


def test_an_unreadable_time_keeps_the_filing_undated_at_the_recency_floor():
    odd = ann("16", text="Board approves capex for capacity expansion", category="Updates")
    odd["published_at"] = "not a date"
    [row] = run([odd])[0].rows
    assert row["filed_at"] is None and row["card_json"]["score_parts"]["recency"] == 0.5


# -- fetch, dedupe, window, order ---------------------------------------------------------------


def test_dedupes_by_source_id_and_drops_other_symbols_and_old_filings():
    rows = [
        ann("1"),
        ann("1"),
        ann("2", symbol="TCS"),
        ann("3", hours_ago=30),
        ann("4", hours_ago=1),
    ]
    summary, _ = run(rows)
    assert summary.fetched == 5 and summary.in_window == 2
    assert sorted(r["source_id"] for r in summary.rows) == ["1", "4"]


def test_source_order_is_nse_then_rss_then_bse_when_one_fails():
    calls = []

    def failing(name):
        def fetch(symbols, a, b):
            calls.append(name)
            raise SourceBlocked(f"{name} blocked")

        return fetch

    fetchers = {
        "nse": failing("nse"),
        "rss": lambda s, a, b: (calls.append("rss"), [ann()])[1],
        "bse": failing("bse"),
    }
    name, rows = job.fetch_announcements(None, ["INFY"], NOW, NOW, fetchers)
    assert (name, calls) == ("rss", ["nse", "rss"]) and len(rows) == 1
    fetchers["rss"] = failing("rss")
    with pytest.raises(SourceError, match="every source failed"):
        job.fetch_announcements(None, ["INFY"], NOW, NOW, fetchers)
    assert calls[-3:] == ["nse", "rss", "bse"]


def test_an_explicit_source_does_not_fall_back():
    fetchers = {
        "nse": lambda s, a, b: (_ for _ in ()).throw(SourceBlocked("blocked")),
        "rss": lambda s, a, b: [ann()],
    }
    with pytest.raises(SourceError):
        job.fetch_announcements("nse", ["INFY"], NOW, NOW, fetchers)


def test_bse_category_and_subcategory_are_joined_for_the_rules():
    assert (
        job.category_raw_of(
            {"category": "Company Update", "subcategory": "Earnings Call Transcript"}
        )
        == "Company Update | Earnings Call Transcript"
    )
    assert job.category_raw_of({"category": "Dividend", "subcategory": None}) == "Dividend"
    assert job.category_raw_of({"category": None, "subcategory": None}) is None


def test_fixtures_replay_labels_itself_and_writes_nothing(caplog):
    rows = job.fetch_fixtures(["INFY"], NOW, NOW)
    assert rows and all(r["symbol"] == "INFY" for r in rows)
    assert "REPLAY MODE" in caplog.text


def test_parse_duration():
    assert job.parse_duration("24h") == timedelta(hours=24)
    assert job.parse_duration("3d") == timedelta(days=3) and job.parse_duration("15m") == timedelta(
        minutes=15
    )
    with pytest.raises(Exception):  # noqa: B017
        job.parse_duration("soon")


# -- summary --------------------------------------------------------------------------------------


def test_summary_counts_by_category_status_and_llm_cost_with_actual_cost_zero():
    capex = ann("2", text="Board approves capex for capacity expansion", category="Updates")
    generic = ann("3", symbol="HAL", category="General Updates", text="HAL update")
    summary, _ = run([capex, generic, ann("4")])
    assert summary.by_status == {"oneliner": 1, "card": 1, "drawer": 1}
    assert summary.llm_calls == 4 and summary.paid_equivalent_inr == pytest.approx(0.004)
    lines = "\n".join(summary.lines())
    assert "actual cost Rs 0.00 (free tier)" in lines and "paid-equivalent" in lines
    assert "| management_change | 0 | 0 |" not in summary.table()
    assert "| capex | 0 | 1 | 0 | 0 | 1 |" in summary.table()


def test_dry_run_writes_nothing_and_a_db_run_upserts_every_row_once():
    db = FakeDb()
    summary, _ = run([ann("1"), ann("2", text="Board approves capex", category="Updates")], db=db)
    assert sorted(r["source_id"] for r in db.upserted) == ["1", "2"] and len(summary.rows) == 2
    db = FakeDb()
    run([ann("1")], db=None)
    assert db.upserted == []
