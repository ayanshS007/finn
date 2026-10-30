import json
import re
from datetime import date

import pytest

from finn.enrich import results_card as rc
from finn.enrich.pdf import PdfText
from finn.llm.router import LLMPending
from finn.parse.checks import pct_change
from finn.parse.results import results_facts
from finn.sources.http import SourceBlocked
from tests.llm_helpers import ScriptedRouter
from tests.test_results import FakeNSE, Fetcher, no_screener, only, raw_of, screener_company
from tests.xbrl_helpers import make_xbrl, raw_row

INFY_ROW = {
    "symbol": "INFY",
    "source_id": "106826465",
    "subject": "Infosys Limited has submitted to the Exchange, Financial Results for the quarter "
    "and half year ended September 30, 2026",
    "attachment_url": None,
    "filed_at": "2026-10-23T16:08:26+05:30",
}


def facts_fn(client, fetch, screener_fetch=no_screener):
    return lambda symbol, period_end=None: results_facts(
        symbol, client=client, fetch=fetch, screener_fetch=screener_fetch, period_end=period_end
    )


def no_pdf(url, pages=3):
    raise AssertionError("no PDF should be read on the XBRL path")


def infy_deps(fetch=None, **kw):
    fetch = fetch or Fetcher()
    return rc.Deps(results_facts=facts_fn(FakeNSE(raw_of("INFY")), fetch), pdf=no_pdf, **kw)


def lines_of(card):
    return {ln["metric"]: ln for ln in card.fragment["lines"]}


# -- the XBRL path ----------------------------------------------------------------------------


def test_infy_card_has_the_four_lines_with_code_computed_changes():
    card = rc.build_results_card(INFY_ROW, infy_deps())
    f = card.fragment
    assert (f["state"], f["source"], f["kind"], f["basis"]) == ("ok", "xbrl", "industry", "consolidated")
    assert (f["period"], f["period_label"]) == ("FY27Q2", "FY27 Q2 (Jul-Sep 2026)")
    assert f["source_url"].startswith("https://nsearchives.nseindia.com/corporate/xbrl/")
    assert [ln["metric"] for ln in f["lines"]] == ["revenue", "ebitda", "pbt", "pat"]
    rev = lines_of(card)["revenue"]
    assert rev["value_cr"] == 49244.0
    assert rev["qoq_pct"] == round(pct_change(49244.0, 48211.0), 2) == 2.14
    assert rev["yoy_pct"] == round(pct_change(49244.0, 44490.0), 2) == 10.69
    assert (rev["qoq_base_period"], rev["yoy_base_period"]) == ("FY27Q1", "FY26Q2")
    assert f["has_exceptional_items"] is False
    assert all(c["ok"] for c in f["checks"])
    assert {c["name"] for c in f["checks"]} >= {"arithmetic", "quote_match", "figures_present"}


def test_ebitda_line_carries_its_derived_flag_and_formula():
    ebitda = lines_of(rc.build_results_card(INFY_ROW, infy_deps()))["ebitda"]
    assert ebitda["derived"] is True and ebitda["value_cr"] == 11689.0
    assert ebitda["formula"] == (
        "ebitda = pbt + exceptional_items_added_back + finance_costs + d_and_a - other_income = "
        "11,063.00 + 0.00 + 115.00 + 1,359.00 - 848.00 = 11,689.00"
    )
    assert ebitda["has_exceptional_items"] is False
    assert lines_of(rc.build_results_card(INFY_ROW, infy_deps()))["revenue"]["derived"] is False


def test_the_facts_behind_the_card_are_returned_for_the_facts_table():
    card = rc.build_results_card(INFY_ROW, infy_deps())
    assert {f.period for f in card.facts} == {"FY27Q2", "FY27Q1", "FY26Q2"}
    assert all(f.quote_match >= 0.4 and f.basis == "consolidated" for f in card.facts)
    current = {f.metric for f in card.facts if f.period == "FY27Q2"}
    assert {"revenue", "ebitda", "pbt", "tax", "pat"} <= current
    # the card's keys name facts that are in the list
    keys = {"|".join(map(str, f.key)) for f in card.facts}
    assert set(card.fragment["fact_keys"]) <= keys


def test_figures_for_the_why_line_are_written_by_code():
    card = rc.build_results_card(INFY_ROW, infy_deps())
    assert card.figures["revenue"] == "Rs 49,244 cr, +2.1% QoQ, +10.7% YoY"
    assert card.figures["pat"] == "Rs 7,838 cr, +0.8% QoQ, +6.3% YoY"


def test_bank_card_shows_nii_and_ppop_instead_of_ebitda():
    raw = raw_of("HDFCBANK", only(("30-SEP-2026", "Consolidated")))
    company = screener_company("HDFCBANK")
    row = {**INFY_ROW, "symbol": "HDFCBANK", "source_id": "b1",
           "subject": "HDFC Bank Limited has submitted to the Exchange, the financial results for the period ended September 30, 2026."}  # fmt: skip
    deps = rc.Deps(
        results_facts=facts_fn(FakeNSE(raw), Fetcher(), lambda s: company), pdf=no_pdf
    )
    card = rc.build_results_card(row, deps)
    f = card.fragment
    assert f["state"] == "ok" and f["kind"] == "lender"
    assert [ln["metric"] for ln in f["lines"]] == ["revenue", "nii", "ppop", "pbt", "pat"]
    nii = lines_of(card)["nii"]
    assert nii["value_cr"] == pytest.approx(44464.2) and nii["derived"] is True
    assert nii["formula"].startswith("nii = interest_earned - interest_expended")
    assert lines_of(card)["ppop"]["derived"] is False
    # the earlier quarters came from screener, which has no ppop: no base, so no change shown
    assert lines_of(card)["ppop"]["qoq_pct"] is None and lines_of(card)["nii"]["qoq_pct"] is not None
    assert f["has_exceptional_items"] is None  # a bank filing has no exceptional-items line
    qoq = next(c for c in f["checks"] if c["name"] == "qoq_base")
    assert qoq["ok"] is False  # said, not hidden; the state is still ok


# -- the checks: a card that cannot be checked shows no numbers ------------------------------------


class Patched(Fetcher):
    def __init__(self, edit):
        super().__init__()
        self.edit = edit

    def __call__(self, url):
        data = super().__call__(url)
        return self.edit(data) if "_1745058_" in url else data


def break_pat(data: bytes) -> bytes:
    text = data.decode()
    new, n = re.subn(
        r'(<in-capmkt:ProfitLossForPeriod contextRef="OneD"[^>]*>)(\d+)',
        lambda m: m.group(1) + str(int(m.group(2)) + 5 * 10**10),
        text,
    )
    assert n == 1
    return new.encode()


def test_failing_arithmetic_gives_check_source_and_no_numbers_and_no_facts():
    card = rc.build_results_card(INFY_ROW, infy_deps(Patched(break_pat)))
    f = card.fragment
    assert f["state"] == "check_source"
    arithmetic = next(c for c in f["checks"] if c["name"] == "arithmetic")
    assert arithmetic["ok"] is False
    assert "lines" not in f and "fact_keys" not in f and card.facts == [] and card.figures == {}
    assert "value_cr" not in json.dumps(f) and "yoy_pct" not in json.dumps(f)
    assert (f["period"], f["period_label"]) == ("FY27Q2", "FY27 Q2 (Jul-Sep 2026)")


def test_a_missing_figure_gives_check_source():
    def drop_d_and_a(data: bytes) -> bytes:
        text = data.decode()
        new, n = re.subn(r"<in-capmkt:DepreciationDepletionAndAmortisationExpense [^>]*>\d+<[^>]*>", "", text)
        assert n >= 1
        return new.encode()

    card = rc.build_results_card(INFY_ROW, infy_deps(Patched(drop_d_and_a)))
    missing = next(c for c in card.fragment["checks"] if c["name"] == "figures_present")
    assert card.fragment["state"] == "check_source" and missing["ok"] is False
    assert "ebitda" in missing["detail"] and card.facts == []


# -- exceptional items ---------------------------------------------------------------------------------

CTX = {"OneD": ("2026-07-01", "2026-09-30")}
CR = 10_000_000


def synthetic(exceptional_line, pbt=920, tax=230, pat=690):
    facts = [
        ("RevenueFromOperations", "OneD", 5000 * CR),
        ("OtherIncome", "OneD", 50 * CR),
        ("FinanceCosts", "OneD", 20 * CR),
        ("DepreciationDepletionAndAmortisationExpense", "OneD", 100 * CR),
        ("ProfitBeforeExceptionalItemsAndTax", "OneD", 1000 * CR),
        ("ExceptionalItemsBeforeTax", "OneD", exceptional_line * CR),
        ("ProfitBeforeTax", "OneD", pbt * CR),
        ("TaxExpense", "OneD", tax * CR),
        ("ProfitLossForPeriod", "OneD", pat * CR),
    ]
    url = "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_999001_X_WEB.xml"
    raw = {"data": [raw_row("30-SEP-2026", "consolidated", url, "20-Oct-2026 10:00:00")]}
    data = make_xbrl("consolidated", CTX, facts)

    def no_company(symbol):
        from finn.sources.http import SourceError

        raise SourceError("screener not available in this test")

    return rc.Deps(
        results_facts=facts_fn(FakeNSE(raw), lambda u: data, no_company), pdf=no_pdf
    )


ROW = {**INFY_ROW, "symbol": "TEST", "source_id": "t1", "subject": "Results for the quarter ended September 30, 2026"}  # fmt: skip


def test_an_exceptional_loss_is_flagged_and_excluded_from_ebitda():
    card = rc.build_results_card(ROW, synthetic(80))  # a loss of 80: pbt 920 after, 1000 before
    f = card.fragment
    assert f["state"] == "ok" and f["has_exceptional_items"] is True
    ebitda = lines_of(card)["ebitda"]
    assert ebitda["value_cr"] == 1070.0  # 920 + 80 + 20 + 100 - 50
    assert ebitda["has_exceptional_items"] is True and "+ 80.00 +" in ebitda["formula"]
    assert all(ln["has_exceptional_items"] is True for ln in f["lines"])
    # no earlier quarter to compare with: no change is shown, and the card says so
    assert lines_of(card)["revenue"]["qoq_pct"] is None
    assert next(c for c in f["checks"] if c["name"] == "yoy_base")["ok"] is False


def test_no_exceptional_items_means_false_not_null():
    card = rc.build_results_card(ROW, synthetic(0, pbt=1000, tax=250, pat=750))
    assert card.fragment["has_exceptional_items"] is False
    assert lines_of(card)["ebitda"]["value_cr"] == 1070.0


# -- no XBRL for the filing: the reader role reads the PDF table ---------------------------------------

PDF_TABLE = (
    "Infosys Limited. Statement of Unaudited Consolidated Financial Results for the quarter and "
    "half year ended September 30, 2026 (Rs. in crore)\n"
    "Particulars Quarter ended Sep 30 2026 Jun 30 2026 Sep 30 2025\n"
    "Revenue from operations 49,244 48,211 44,490\n"
    "Other income 848 800 700\n"
    "Finance costs 115 110 100\n"
    "Depreciation and amortisation expense 1,359 1,300 1,200\n"
    "Profit before tax 11,063 11,028 10,229\n"
    "Tax expense 3,225 3,200 2,900\n"
    "Net profit for the period 7,838 7,775 7,375\n"
)
PDF_ROWS = [
    {"metric": "revenue", "printed": "49,244", "quote": "Revenue from operations 49,244 48,211 44,490"},
    {"metric": "other_income", "printed": "848", "quote": "Other income 848 800 700"},
    {"metric": "finance_costs", "printed": "115", "quote": "Finance costs 115 110 100"},
    {"metric": "d_and_a", "printed": "1,359", "quote": "Depreciation and amortisation expense 1,359 1,300 1,200"},
    {"metric": "pbt", "printed": "11,063", "quote": "Profit before tax 11,063 11,028 10,229"},
    {"metric": "tax", "printed": "3,225", "quote": "Tax expense 3,225 3,200 2,900"},
    {"metric": "pat", "printed": "7,838", "quote": "Net profit for the period 7,838 7,775 7,375"},
]  # fmt: skip


def pdf_reply(rows=PDF_ROWS, header="Rs. in crore", basis="consolidated"):
    return {"basis": basis, "unit_header": header, "rows": rows}


def earlier_quarters_only(symbol, period_end=None):
    """What results_facts gives when NSE has not listed the quarter's XBRL yet."""
    facts = results_facts(
        symbol, client=FakeNSE(raw_of("INFY")), fetch=Fetcher(), screener_fetch=no_screener
    )
    return [f for f in facts if f.period != "FY27Q2"]


def pdf_deps(router, text=PDF_TABLE):
    return rc.Deps(
        results_facts=earlier_quarters_only,
        pdf=lambda url, pages=3: PdfText(url, text, 2, "pdfplumber"),
        router=router,
    )


PDF_ROW = {**INFY_ROW, "attachment_url": "https://nsearchives.nseindia.com/corporate/x.pdf"}


def test_pdf_fallback_uses_the_reader_and_code_computes_everything():
    router = ScriptedRouter([pdf_reply()])
    card = rc.build_results_card(PDF_ROW, pdf_deps(router))
    f = card.fragment
    assert f["state"] == "ok" and f["source"] == "pdf" and f["basis"] == "consolidated"
    step, role, prompt = router.calls[0]
    assert (step, role) == ("results_pdf", "reader")
    assert "Use only the provided text. Do not use outside knowledge." in prompt
    rev = lines_of(card)["revenue"]
    assert rev["value_cr"] == 49244.0 and rev["qoq_pct"] == 2.14  # against the XBRL quarter before
    ebitda = lines_of(card)["ebitda"]
    assert ebitda["value_cr"] == 11689.0 and ebitda["derived"] and ebitda["has_exceptional_items"] is None
    current = [x for x in card.facts if x.period == "FY27Q2"]
    assert current and all(x.quote_match >= 0.4 and x.source_url.endswith("x.pdf") for x in current)
    assert all(x.quote for x in current)


def test_pdf_unit_is_converted_by_code():
    lakh_table = PDF_TABLE.replace("(Rs. in crore)", "(Rs. in lakhs)")
    rows = [{**r, "printed": r["printed"].replace(",", "") + "00",
             "quote": r["quote"]} for r in PDF_ROWS]  # fmt: skip
    text = lakh_table
    for r, orig in zip(rows, PDF_ROWS, strict=True):
        text = text.replace(orig["quote"], orig["quote"].replace(orig["printed"], r["printed"], 1))
        r["quote"] = orig["quote"].replace(orig["printed"], r["printed"], 1)
    router = ScriptedRouter([pdf_reply(rows, header="Rs. in lakhs")])
    card = rc.build_results_card(PDF_ROW, pdf_deps(router, text))
    assert card.fragment["state"] == "ok"
    pbt = next(x for x in card.facts if x.metric == "pbt" and x.period == "FY27Q2")
    assert pbt.unit_original == "lakh" and pbt.value_cr == pytest.approx(1106300 / 100)


def test_a_pdf_quote_that_is_not_in_the_text_is_retried_once_then_dropped():
    bad = {**PDF_ROWS[0], "quote": "Total revenue for the quarter was a record high of the year"}
    router = ScriptedRouter([pdf_reply([bad, *PDF_ROWS[1:]])] * 2)
    card = rc.build_results_card(PDF_ROW, pdf_deps(router))
    assert len(router.calls) == 2  # one retry, no loop
    assert card.fragment["state"] == "check_source" and card.facts == []
    assert "revenue" in next(c for c in card.fragment["checks"] if c["name"] == "figures_present")["detail"]


def test_a_printed_number_that_is_not_in_its_quote_is_dropped():
    bad = {**PDF_ROWS[0], "printed": "51,000"}
    router = ScriptedRouter([pdf_reply([bad, *PDF_ROWS[1:]])] * 2)
    assert rc.build_results_card(PDF_ROW, pdf_deps(router)).fragment["state"] == "check_source"


def test_pdf_arithmetic_failure_is_check_source():
    wrong = {**PDF_ROWS[6], "printed": "9,999", "quote": "Net profit for the period 9,999 7,775 7,375"}
    text = PDF_TABLE.replace("Net profit for the period 7,838", "Net profit for the period 9,999")
    router = ScriptedRouter([pdf_reply([*PDF_ROWS[:6], wrong])])
    card = rc.build_results_card(PDF_ROW, pdf_deps(router, text))
    assert card.fragment["state"] == "check_source"
    assert next(c for c in card.fragment["checks"] if c["name"] == "arithmetic")["ok"] is False


def test_pdf_with_an_unnamed_unit_or_basis_is_check_source():
    for reply in (pdf_reply(header="amounts as stated"), pdf_reply(basis="group")):
        router = ScriptedRouter([reply] * 2)
        assert rc.build_results_card(PDF_ROW, pdf_deps(router)).fragment["state"] == "check_source"


def test_no_xbrl_and_no_pdf_text_is_check_source_without_a_model_call():
    router = ScriptedRouter([])
    deps = rc.Deps(results_facts=earlier_quarters_only, pdf=lambda u, p=3: None, router=router)
    card = rc.build_results_card(PDF_ROW, deps)
    assert card.fragment["state"] == "check_source" and router.calls == []


def test_a_rate_limited_reader_is_pending_not_a_guess():
    router = ScriptedRouter([LLMPending("rate limited")])
    with pytest.raises(LLMPending):
        rc.build_results_card(PDF_ROW, pdf_deps(router))


# -- the period, duplicates, blocked sources -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("submitted the financial results for the period ended September 30, 2026.", date(2026, 9, 30)),
        ("results for the quarter and half year ended 30th September 2026", date(2026, 9, 30)),
        ("Quarter ended 30.09.2026 30.06.2026 30.09.2025", date(2026, 9, 30)),
        ("audited results for the year ended March 31, 2027", date(2027, 3, 31)),
        ("Outcome of Board Meeting held today", None),
        ("quarter ended 15 August 2026", None),  # not a quarter end
    ],
)
def test_period_end_from_text(text, expected):
    assert rc.period_end_from_text(text) == expected


def test_period_from_the_filing_date_when_the_text_names_none():
    assert rc.period_end_from_filing_date(date(2026, 10, 27)) == date(2026, 9, 30)
    assert rc.period_end_from_filing_date(date(2026, 5, 30)) == date(2026, 3, 31)
    row = {**INFY_ROW, "subject": "Outcome of Board Meeting held today"}
    card = rc.build_results_card(row, infy_deps())
    assert card.fragment["period"] == "FY27Q2" and card.fragment["period_source"] == "filing_date"


def test_period_is_read_from_the_attachment_when_the_subject_has_none():
    row = {**INFY_ROW, "subject": "Outcome of Board Meeting", "attachment_url": "u://x.pdf"}
    deps = infy_deps()
    deps.pdf = lambda url, pages=3: PdfText(url, PDF_TABLE, 2, "pdfplumber")
    card = rc.build_results_card(row, deps)
    assert card.fragment["period_source"] == "attachment" and card.fragment["state"] == "ok"


def test_a_second_filing_for_the_same_period_collapses_into_the_first():
    called = []
    deps = infy_deps(duplicate_of=lambda sym, period, own: "FIRST-1")
    deps.results_facts = lambda *a, **k: called.append(1)
    card = rc.build_results_card(INFY_ROW, deps)
    assert card.fragment["state"] == "duplicate" and card.fragment["duplicate_of"] == "FIRST-1"
    assert card.facts == [] and called == []  # nothing fetched for a duplicate


def test_a_blocked_source_raises_and_is_not_papered_over():
    def blocked(symbol, period_end=None):
        raise SourceBlocked("NSE answered 403", 403)

    with pytest.raises(SourceBlocked):
        rc.build_results_card(INFY_ROW, rc.Deps(results_facts=blocked, pdf=no_pdf))


@pytest.mark.parametrize(
    "printed, value",
    [("1,234.50", 1234.5), ("(12.0)", -12.0), ("-12.0", -12.0), ("\u221212", -12.0), ("7", 7.0),
     ("NA", None), ("12 cr", None), ("", None), ("1.2.3", None)],
)  # fmt: skip
def test_parse_printed(printed, value):
    assert rc.parse_printed(printed) == value
