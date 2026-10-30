import httpx
import pytest

from finn.normalise.quote import quote_match
from finn.parse.checks import arithmetic_ok
from finn.parse.xbrl_results import XbrlError, fetch_xbrl, parse_xbrl
from finn.sources.http import DiskCache, HttpClient, SourceBlocked
from tests.xbrl_helpers import make_xbrl

URL = "https://nsearchives.nseindia.com/corporate/xbrl/{}.xml"


def load(fixtures, name):
    filing = parse_xbrl((fixtures / name).read_bytes(), URL.format(name))
    return filing, {f.metric: f for f in filing.to_facts("T") if f.period_kind == "Q"}


@pytest.mark.parametrize(
    "name, kind, expected",
    [
        (
            "xbrl_INFY_FY27Q2_consolidated.xml",
            "indas",
            {
                "revenue": 49244,
                "other_income": 848,
                "finance_costs": 115,
                "d_and_a": 1359,
                "pbt": 11063,
                "tax": 3225,
                "pat": 7838,
            },
        ),
        (
            "xbrl_GRAVITA_FY27Q2_consolidated.xml",
            "indas",
            {
                "revenue": 1427.35,
                "other_income": 18.0,
                "finance_costs": 13.88,
                "d_and_a": 13.89,
                "pbt": 124.21,
                "tax": 22.18,
                "pat": 102.03,
            },
        ),
    ],
)
def test_indas_fixture(fixtures, name, kind, expected):
    filing, q = load(fixtures, name)
    assert (filing.basis, filing.kind) == ("consolidated", kind)
    assert [p.label for p in filing.periods] == ["FY27H1", "FY27Q2"]  # quarter and year to date
    assert {m: round(q[m].value_cr, 2) for m in expected} == pytest.approx(expected)
    assert all(f.unit_original == "rupee" for f in q.values())
    assert {m for m, f in q.items() if f.derived} == {"ebitda"}  # the only line computed here
    assert q["ebitda"].has_exceptional_items is False  # both filings report a zero line
    assert all(f.has_exceptional_items is False for f in q.values())
    assert all(f.period == "FY27Q2" and f.basis == "consolidated" for f in q.values())
    assert all(f.source_url == URL.format(name) for f in q.values())
    assert arithmetic_ok(q["pbt"].value_cr, q["tax"].value_cr, q["pat"].value_cr)
    # the quote is the element as filed: all of it is in the source
    assert all(f.quote_match == 1.0 for f in q.values())
    assert "RevenueFromOperations" in q["revenue"].quote and "contextRef" in q["revenue"].quote


def test_hdfcbank_banking_taxonomy(fixtures):
    filing, q = load(fixtures, "xbrl_HDFCBANK_FY27Q2_consolidated.xml")
    assert (filing.basis, filing.kind) == ("consolidated", "bank")
    assert round(q["pbt"].value_cr, 2) == 27825.48
    assert round(q["tax"].value_cr, 2) == 7125.45
    assert round(q["pat"].value_cr, 2) == 20700.03
    assert arithmetic_ok(q["pbt"].value_cr, q["tax"].value_cr, q["pat"].value_cr)
    # operating profit before provisions is reported, interest margin is derived
    assert q["ppop"].value_cr == pytest.approx(32871.54) and not q["ppop"].derived
    nii = q["nii"]
    assert nii.value_cr == pytest.approx(44464.2) and nii.derived
    assert nii.formula == (
        "nii = interest_earned - interest_expended = 95,131.63 - 50,667.43 = 44,464.20"
    )
    assert q["revenue"].value_cr == pytest.approx(129358.69)  # interest earned + other income
    assert "finance_costs" not in q and "d_and_a" not in q  # not meaningful for a bank
    assert all(f.quote_match == 1.0 for f in q.values())


def test_nbfc_taxonomy(fixtures):
    filing, q = load(fixtures, "xbrl_BAJFINANCE_FY27Q2_consolidated.xml")
    assert filing.kind == "nbfc"
    assert q["nii"].value_cr == pytest.approx(13334.66) and q["nii"].derived
    assert q["ppop"].value_cr == pytest.approx(10699.56)
    assert "impairment" in q["ppop"].formula
    assert arithmetic_ok(q["pbt"].value_cr, q["tax"].value_cr, q["pat"].value_cr, other=37.3 / 10)


def test_standalone_filing_keeps_its_basis(fixtures):
    filing, q = load(fixtures, "xbrl_GRAVITA_FY27Q2_standalone.xml")
    assert filing.basis == "standalone"
    assert q["revenue"].value_cr == pytest.approx(786.91)
    assert all(f.basis == "standalone" for f in q.values())
    assert arithmetic_ok(q["pbt"].value_cr, q["tax"].value_cr, q["pat"].value_cr)


def test_prior_period_filings_parse_the_same_way(fixtures):
    _, q1 = load(fixtures, "xbrl_INFY_FY27Q1_consolidated.xml")
    _, yoy = load(fixtures, "xbrl_INFY_FY26Q2_consolidated.xml")
    assert q1["revenue"].period == "FY27Q1" and q1["revenue"].value_cr == 48211.0
    assert yoy["revenue"].period == "FY26Q2" and yoy["revenue"].value_cr == 44490.0
    for q in (q1, yoy):
        assert arithmetic_ok(q["pbt"].value_cr, q["tax"].value_cr, q["pat"].value_cr)


def test_year_to_date_facts_are_labelled_by_kind(fixtures):
    filing, _ = load(fixtures, "xbrl_INFY_FY27Q2_consolidated.xml")
    h1 = {f.metric: f for f in filing.to_facts("INFY") if f.period_kind == "H1"}
    assert h1["revenue"].period == "FY27H1" and h1["revenue"].value_cr == 97454.0


def test_basis_mismatch_with_nse_row_raises(fixtures):
    data = (fixtures / "xbrl_GRAVITA_FY27Q2_standalone.xml").read_bytes()
    with pytest.raises(XbrlError, match="NSE says consolidated"):
        parse_xbrl(data, "u://x", basis="consolidated")


CTX = {"OneD": ("2026-07-01", "2026-09-30")}


def test_synthetic_filing_and_unit_scale():
    data = make_xbrl("consolidated", CTX, [("RevenueFromOperations", "OneD", 1_000_000_000)])
    f = parse_xbrl(data, "u://x").to_facts("T")[0]
    assert f.value_cr == 100.0 and f.unit_original == "rupee"


def test_non_inr_money_is_not_used():
    data = make_xbrl(
        "consolidated", CTX, [("RevenueFromOperations", "OneD", 5)], unit="iso4217:USD"
    )
    with pytest.raises(XbrlError, match="none of the expected"):
        parse_xbrl(data, "u://x")


def test_unmappable_context_is_skipped_not_guessed():
    ctxs = {"OneD": ("2026-07-01", "2026-09-30"), "Odd": ("2026-05-01", "2026-09-30")}
    data = make_xbrl(
        "consolidated",
        ctxs,
        [("RevenueFromOperations", "OneD", 10**9), ("RevenueFromOperations", "Odd", 5 * 10**9)],
    )
    filing = parse_xbrl(data, "u://x")
    assert [p.label for p in filing.periods] == ["FY27Q2"] and len(filing.skipped) == 1


@pytest.mark.parametrize(
    "data, message",
    [
        (b"<html>blocked</html>", "not xbrli:xbrl"),
        (b"<xbrli:xbrl", "not XML"),
        (b"<?xml version='1.0'?><root/>", "not xbrli:xbrl"),
        (
            make_xbrl("consolidated", CTX, [("RevenueFromOperations", "OneD", 1)], nature="Other"),
            "NatureOfReportStandaloneConsolidated",
        ),
        (
            make_xbrl(
                "consolidated",
                CTX,
                [("RevenueFromOperations", "OneD", 1), ("RevenueFromOperations", "OneD", 2)],
            ),
            "two different values",
        ),
        (make_xbrl("consolidated", CTX, []), "none of the expected"),
    ],
)
def test_bad_files_raise(data, message):
    with pytest.raises(XbrlError, match=message):
        parse_xbrl(data, "u://x")


def test_quote_is_verbatim_from_the_file(fixtures):
    raw = (fixtures / "xbrl_INFY_FY27Q2_consolidated.xml").read_text(encoding="utf-8")
    _, q = load(fixtures, "xbrl_INFY_FY27Q2_consolidated.xml")
    assert "492440000000" in q["revenue"].quote
    assert quote_match(q["revenue"].quote, raw) == 1.0
    assert quote_match(q["revenue"].quote.replace("492440000000", "999999999999"), raw) < 1.0


def client(tmp_path, body: bytes, status=200):
    return HttpClient(
        cache=DiskCache(tmp_path),
        spacing={},
        transport=httpx.MockTransport(lambda req: httpx.Response(status, content=body)),
    )


def test_fetch_xbrl_returns_the_document_and_rejects_block_pages(tmp_path):
    xml = make_xbrl("consolidated", CTX, [("RevenueFromOperations", "OneD", 1)])
    assert fetch_xbrl("https://nsearchives.nseindia.com/a.xml", client(tmp_path, xml), ttl=0) == xml
    with pytest.raises(SourceBlocked):
        fetch_xbrl(
            "https://nsearchives.nseindia.com/a.xml",
            client(tmp_path, b"<html><body>Access Denied</body></html>"),
            ttl=0,
        )


# -- exceptional items: EBITDA excludes them -------------------------------------------------


def exceptional_filing(before, exceptional, after, **extra):
    """One quarter: other income 50, finance costs 20, D&A 100 (Rs crore x 1e7 = rupees)."""
    cr = 10_000_000
    facts = [
        ("RevenueFromOperations", "OneD", 5000 * cr),
        ("OtherIncome", "OneD", 50 * cr),
        ("FinanceCosts", "OneD", 20 * cr),
        ("DepreciationDepletionAndAmortisationExpense", "OneD", 100 * cr),
        ("ProfitBeforeExceptionalItemsAndTax", "OneD", before * cr),
        ("ExceptionalItemsBeforeTax", "OneD", exceptional * cr),
        ("ProfitBeforeTax", "OneD", after * cr),
    ]
    return parse_xbrl(make_xbrl("consolidated", CTX, facts, **extra), "u://x").to_facts("T")


@pytest.mark.parametrize(
    "before, line, after, added_back",
    [
        (1000, 80, 920, 80),  # a loss of 80, filed as a positive expense
        (1000, -80, 920, 80),  # the same loss for a filer who signs the line the other way
        (1000, 60, 1060, -60),  # a gain of 60, filed as a positive income
        (1000, -60, 1060, -60),  # the same gain, signed the other way
    ],
)
def test_ebitda_adds_back_a_loss_and_subtracts_a_gain(before, line, after, added_back):
    facts = {f.metric: f for f in exceptional_filing(before, line, after) if f.period_kind == "Q"}
    e = facts["ebitda"]
    # operating profit before exceptional items: 1000 + 20 + 100 - 50 = 1,070 either way
    assert e.value_cr == pytest.approx(1070.0)
    assert e.value_cr == pytest.approx(facts["pbt"].value_cr + added_back + 20 + 100 - 50)
    assert e.derived and e.has_exceptional_items is True
    assert e.formula == (
        "ebitda = pbt + exceptional_items_added_back + finance_costs + d_and_a - other_income = "
        f"{after:,.2f} + {added_back:,.2f} + 20.00 + 100.00 - 50.00 = 1,070.00"
    )
    assert "ExceptionalItemsBeforeTax" in e.quote and e.quote_match == 1.0
    assert all(f.has_exceptional_items is True for f in facts.values())  # every fact is flagged


def test_zero_exceptional_line_is_flagged_false_and_keeps_the_plain_value():
    facts = {f.metric: f for f in exceptional_filing(1000, 0, 1000)}
    assert facts["ebitda"].value_cr == pytest.approx(1070.0)
    assert facts["ebitda"].has_exceptional_items is False
    assert "+ 0.00 +" in facts["ebitda"].formula


def test_filing_without_the_line_has_no_flag():
    facts = [
        ("OtherIncome", "OneD", 5 * 10**7),
        ("FinanceCosts", "OneD", 2 * 10**7),
        ("DepreciationDepletionAndAmortisationExpense", "OneD", 10**8),
        ("ProfitBeforeTax", "OneD", 10**9),
    ]
    got = parse_xbrl(make_xbrl("consolidated", CTX, facts), "u://x").to_facts("T")
    assert all(f.has_exceptional_items is None for f in got)
    assert all(f.metric != "ebitda" for f in got)  # the line is needed to say it is excluded


def test_exceptional_line_that_the_profit_lines_do_not_confirm_leaves_ebitda_out():
    filing = parse_xbrl(
        make_xbrl(
            "consolidated",
            CTX,
            [
                ("RevenueFromOperations", "OneD", 5000 * 10**7),
                ("OtherIncome", "OneD", 50 * 10**7),
                ("FinanceCosts", "OneD", 20 * 10**7),
                ("DepreciationDepletionAndAmortisationExpense", "OneD", 100 * 10**7),
                ("ProfitBeforeExceptionalItemsAndTax", "OneD", 1000 * 10**7),
                ("ExceptionalItemsBeforeTax", "OneD", 80 * 10**7),
                ("ProfitBeforeTax", "OneD", 990 * 10**7),  # a gap of 10, not 80
            ],
        ),
        "u://x",
    )
    got = {f.metric: f for f in filing.to_facts("T")}
    assert "ebitda" not in got and got["pbt"].has_exceptional_items is True
    assert filing.skipped and "exceptional" in filing.skipped[0]
