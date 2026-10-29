import json
import logging
from dataclasses import replace
from datetime import date

import pytest

from finn.parse import results
from finn.parse.checks import arithmetic_ok, within
from finn.parse.results import results_facts
from finn.sources import screener
from finn.sources.http import SourceBlocked, SourceError
from tests.conftest import FIXTURES
from tests.xbrl_helpers import make_xbrl, raw_row

CRORE = 10_000_000


class FakeNSE:
    def __init__(self, raw: dict, comparison: dict | None = None):
        self.raw, self.comparison = raw, comparison

    def integrated_results_raw(self, symbol):
        return self.raw

    def results_comparison(self, symbol):
        return self.comparison


def raw_of(symbol, keep=lambda r: True):
    raw = json.loads((FIXTURES / f"nse_integrated_results_{symbol}.json").read_text())
    return {**raw, "data": [r for r in raw["data"] if keep(r)]}


def only(*pairs):
    """Keep integrated rows whose (qe_Date, consolidated) is in pairs."""
    return lambda r: (r["qe_Date"], r["consolidated"]) in pairs


FILES = {
    "1745058": "xbrl_INFY_FY27Q2_consolidated.xml",
    "1700136": "xbrl_INFY_FY27Q1_consolidated.xml",
    "1551001": "xbrl_INFY_FY26Q2_consolidated.xml",
    "1740913": "xbrl_HDFCBANK_FY27Q2_consolidated.xml",
    "1747256": "xbrl_GRAVITA_FY27Q2_consolidated.xml",
    "1747260": "xbrl_GRAVITA_FY27Q2_standalone.xml",
}


class Fetcher:
    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        for key, name in FILES.items():
            if f"_{key}_" in url:
                return (FIXTURES / name).read_bytes()
        raise AssertionError(f"unexpected download {url}")


def screener_company(symbol, **changes):
    html = (FIXTURES / f"screener_{symbol}.html").read_text()
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    return replace(screener.parse_company_page(html, symbol, url), **changes)


def no_screener(symbol):
    raise AssertionError("screener must not be called")


def by(facts):
    return {(f.period, f.metric): f for f in facts}


# -- XBRL path -------------------------------------------------------------------------------


def test_infy_three_periods_from_xbrl_only():
    fetch = Fetcher()
    facts = results_facts(
        "INFY", client=FakeNSE(raw_of("INFY")), fetch=fetch, screener_fetch=no_screener
    )
    got = by(facts)
    assert {f.period for f in facts} == {"FY27Q2", "FY27Q1", "FY26Q2"}
    assert len(fetch.urls) == 3
    assert {f.basis for f in facts} == {"consolidated"}
    assert got["FY27Q2", "revenue"].value_cr == 49244.0
    assert got["FY27Q1", "revenue"].value_cr == 48211.0
    assert got["FY26Q2", "revenue"].value_cr == 44490.0
    for period in ("FY27Q2", "FY27Q1", "FY26Q2"):
        assert arithmetic_ok(
            got[period, "pbt"].value_cr, got[period, "tax"].value_cr, got[period, "pat"].value_cr
        )
    assert all(
        f.source_url.startswith("https://nsearchives.nseindia.com/corporate/xbrl/") for f in facts
    )
    assert all(f.quote_match == 1.0 for f in facts)
    assert [f.period for f in facts][:1] == ["FY27Q2"]  # newest period first


def test_ebitda_is_derived_in_code_with_its_formula():
    facts = results_facts(
        "INFY", client=FakeNSE(raw_of("INFY")), fetch=Fetcher(), screener_fetch=no_screener
    )
    e = by(facts)["FY27Q2", "ebitda"]
    assert e.derived and e.value_cr == 11689.0  # 11063 + 115 + 1359 - 848
    assert e.formula == (
        "ebitda = pbt + finance_costs + d_and_a - other_income = "
        "11,063.00 + 115.00 + 1,359.00 - 848.00 = 11,689.00"
    )
    assert e.quote_match == 1.0 and e.basis == "consolidated"
    assert {"nii", "ppop"}.isdisjoint(m for _, m in by(facts))


def test_bank_gets_nii_and_ppop_not_ebitda_and_screener_fills_other_periods():
    raw = raw_of("HDFCBANK", only(("30-SEP-2026", "Consolidated")))
    company = screener_company("HDFCBANK")
    facts = results_facts(
        "HDFCBANK", client=FakeNSE(raw), fetch=Fetcher(), screener_fetch=lambda s: company
    )
    got = by(facts)
    assert got["FY27Q2", "nii"].value_cr == pytest.approx(44464.2)
    assert got["FY27Q2", "ppop"].value_cr == pytest.approx(32871.54)
    assert all(m != "ebitda" for _, m in got)
    # prior quarters came from screener: nii = Revenue - Interest, no ppop (not on that page)
    assert got["FY27Q1", "nii"].source_url == company.url and got["FY27Q1", "nii"].derived
    assert ("FY27Q1", "ppop") not in got and ("FY27Q1", "revenue") not in got
    assert ("FY26Q2", "pat") in got


def test_revision_supersedes_original():
    rows = [
        raw_row(
            "30-SEP-2026",
            "consolidated",
            "https://x/INTEGRATED_FILING_INDAS_111111_a.xml",
            "23-Oct-2026 20:00:00",
        ),
        raw_row(
            "30-SEP-2026",
            "consolidated",
            "https://x/INTEGRATED_FILING_INDAS_222222_b.xml",
            "25-Oct-2026 10:00:00",
            sub="Revision",
        ),
    ]
    picked = results._pick_row(
        results.nse.parse_integrated_results({"data": rows}), date(2026, 9, 30), "consolidated"
    )
    assert picked["xbrl_url"].endswith("222222_b.xml") and picked["revision"] is True


def test_consolidated_is_preferred_when_both_exist():
    raw = raw_of("GRAVITA", only(("30-SEP-2026", "Consolidated"), ("30-SEP-2026", "Standalone")))
    company = screener_company("INFY")
    facts = results_facts(
        "GRAVITA",
        client=FakeNSE(raw),
        fetch=Fetcher(),
        screener_fetch=lambda s: replace(company, tables={}, basis="consolidated"),
    )
    q2 = [f for f in facts if f.period == "FY27Q2"]
    assert {f.basis for f in q2} == {"consolidated"}
    assert by(q2)["FY27Q2", "revenue"].value_cr == pytest.approx(1427.35)


def test_standalone_only_filing(caplog):
    raw = raw_of("GRAVITA", only(("30-SEP-2026", "Standalone")))

    def down(symbol):
        raise SourceError("screener down", 500)

    with caplog.at_level(logging.WARNING):
        facts = results_facts("GRAVITA", client=FakeNSE(raw), fetch=Fetcher(), screener_fetch=down)
    assert {f.period for f in facts} == {"FY27Q2"}
    assert {f.basis for f in facts} == {"standalone"}
    got = by(facts)
    assert got["FY27Q2", "revenue"].value_cr == pytest.approx(786.91)
    assert got["FY27Q2", "ebitda"].value_cr == pytest.approx(59.64 + 6.51 + 5.92 - 8.35)
    assert "FY27Q1" in caplog.text and "FY26Q2" in caplog.text  # the gaps are reported


def test_nse_failure_is_reported_not_papered_over():
    class Blocked:
        def integrated_results_raw(self, symbol):
            raise SourceBlocked("NSE refused", 403)

    with pytest.raises(SourceBlocked):
        results_facts("INFY", client=Blocked(), fetch=Fetcher(), screener_fetch=no_screener)


# -- screener path ----------------------------------------------------------------------------


def test_screener_fills_a_period_the_xbrl_list_lacks():
    raw = raw_of("INFY", lambda r: r["qe_Date"] != "30-SEP-2025")
    company = screener_company("INFY")
    facts = results_facts(
        "INFY", client=FakeNSE(raw), fetch=Fetcher(), screener_fetch=lambda s: company
    )
    got = by(facts)
    yoy = got["FY26Q2", "revenue"]
    assert yoy.value_cr == 44490.0 and yoy.source_url == company.url
    assert yoy.unit_original == "crore" and yoy.basis == "consolidated"
    assert yoy.quote.startswith("Sales ") and "44,490" in yoy.quote  # the row as printed
    assert yoy.quote_match == 1.0
    assert ("FY26Q2", "tax") not in got  # screener only has a rounded Tax %, so no tax figure
    assert got["FY26Q2", "ebitda"].value_cr == 10535.0  # same as the XBRL-based figure


def test_screener_of_other_basis_is_dropped(caplog):
    raw = raw_of("INFY", lambda r: r["qe_Date"] != "30-SEP-2025")
    company = screener_company("INFY", basis="standalone")
    with caplog.at_level(logging.WARNING):
        facts = results_facts(
            "INFY", client=FakeNSE(raw), fetch=Fetcher(), screener_fetch=lambda s: company
        )
    assert "FY26Q2" not in {f.period for f in facts}
    assert "screener is standalone, wanted consolidated" in caplog.text


def test_no_integrated_rows_falls_back_to_screener_for_all_three():
    company = screener_company("INFY")
    facts = results_facts(
        "INFY", client=FakeNSE({"data": []}), fetch=Fetcher(), screener_fetch=lambda s: company
    )
    assert {f.period for f in facts} == {"FY27Q2", "FY27Q1", "FY26Q2"}
    assert {f.source_url for f in facts} == {company.url}


def test_xbrl_and_screener_agree_for_infy_latest_quarter(capsys):
    """Cross-check: every comparable line within 1 percent. The gaps are printed (pytest -s)."""
    xbrl = by(
        results_facts(
            "INFY", client=FakeNSE(raw_of("INFY")), fetch=Fetcher(), screener_fetch=no_screener
        )
    )
    company = screener_company("INFY")
    scr = by(results.add_ebitda(results._screener_quarter("INFY", date(2026, 9, 30), company)))
    rows = []
    for metric in ("revenue", "ebitda", "other_income", "finance_costs", "d_and_a", "pbt", "pat"):
        a, b = xbrl["FY27Q2", metric].value_cr, scr["FY27Q2", metric].value_cr
        gap = abs(a - b) / max(abs(a), abs(b)) * 100
        rows.append(f"{metric:14}{a:>12,.2f}{b:>12,.2f}{gap:>8.3f}%")
        assert within(a, b), f"{metric}: XBRL {a} vs screener {b}"
    print("\nINFY FY27Q2   metric      XBRL        screener     gap\n" + "\n".join(rows))
    # screener's PAT is the profit for the period, which is what XBRL's ProfitLossForPeriod is
    assert xbrl["FY27Q2", "pat"].value_cr == scr["FY27Q2", "pat"].value_cr


# -- legacy path and the unit/period cases ----------------------------------------------------


def legacy_payload(symbol):
    return json.loads((FIXTURES / f"nse_results_comparision_{symbol}.json").read_text())


def test_lakh_header_legacy_figures_convert_to_crore():
    facts = by(results._legacy_quarter("INFY", date(2024, 12, 31), legacy_payload("INFY")))
    rev = facts["FY25Q3", "revenue"]
    assert rev.unit_original == "lakh" and rev.value_cr == 34915.0  # 3,491,500 lakh
    assert facts["FY25Q3", "pbt"].value_cr == 8844.0 and facts["FY25Q3", "tax"].value_cr == 2486.0
    assert arithmetic_ok(8844.0, 2486.0, facts["FY25Q3", "pat"].value_cr)
    assert rev.basis == "standalone" and rev.quote == '"re_net_sale": "3491500"'
    assert rev.quote_match == 1.0


def test_legacy_is_standalone_not_consolidated():
    """The reason legacy facts are labelled standalone: INFY's Dec 2024 sales differ from the
    consolidated figure on screener (41,764)."""
    legacy = by(results._legacy_quarter("INFY", date(2024, 12, 31), legacy_payload("INFY")))
    consolidated = screener_company("INFY").tables["quarterly"].df.loc["Sales", "Dec 2024"]
    assert consolidated == 41764.0 and legacy["FY25Q3", "revenue"].value_cr != consolidated


def test_legacy_bank_format_and_its_limits():
    facts = by(results._legacy_quarter("HDFCBANK", date(2024, 12, 31), legacy_payload("HDFCBANK")))
    assert facts["FY25Q3", "nii"].value_cr == pytest.approx(30653.25)  # 76,006.88 - 45,353.63
    assert facts["FY25Q3", "ppop"].value_cr == pytest.approx(25000.40)
    assert (
        results._legacy_quarter("INFY", date(2025, 3, 31), legacy_payload("INFY")) == []
    )  # frozen


def test_legacy_not_used_for_a_consolidated_company():
    class NoLegacy(FakeNSE):
        def results_comparison(self, symbol):
            raise AssertionError("legacy results-comparision must not be used")

    results_facts(
        "INFY",
        client=NoLegacy({"data": []}),
        fetch=Fetcher(),
        screener_fetch=lambda s: screener_company("INFY"),
    )


def test_crore_header_screener_table():
    facts = by(results._screener_quarter("INFY", date(2026, 9, 30), screener_company("INFY")))
    assert facts["FY27Q2", "revenue"].value_cr == 49244.0
    assert facts["FY27Q2", "revenue"].unit_original == "crore"
    assert facts["FY27Q2", "pat"].value_cr == 7838.0


def xbrl(contexts, facts):
    return make_xbrl("consolidated", contexts, facts)


def test_q4_is_fy_minus_9m_when_the_filing_has_no_quarter_column():
    metrics = {
        "RevenueFromOperations": 178_650,
        "OtherIncome": 2_000,
        "FinanceCosts": 400,
        "DepreciationDepletionAndAmortisationExpense": 5_000,
        "ProfitBeforeTax": 40_000,
        "TaxExpense": 11_000,
        "ProfitLossForPeriod": 29_000,
    }
    nine = {k: v * 0.74 for k, v in metrics.items()}
    mar = xbrl(
        {"FY": ("2025-04-01", "2026-03-31")}, [(k, "FY", v * CRORE) for k, v in metrics.items()]
    )
    dec = xbrl(
        {"NM": ("2025-04-01", "2025-12-31")}, [(k, "NM", round(v * CRORE)) for k, v in nine.items()]
    )
    files = {
        "https://x/INTEGRATED_FILING_INDAS_333333_m.xml": mar,
        "https://x/INTEGRATED_FILING_INDAS_444444_d.xml": dec,
    }
    raw = {
        "data": [
            raw_row(
                "31-MAR-2026",
                "consolidated",
                "https://x/INTEGRATED_FILING_INDAS_333333_m.xml",
                "23-Apr-2026 20:00:00",
            ),
            raw_row(
                "31-DEC-2025",
                "consolidated",
                "https://x/INTEGRATED_FILING_INDAS_444444_d.xml",
                "14-Jan-2026 20:00:00",
            ),
        ]
    }
    rows = [r for r in results.nse.parse_integrated_results(raw)]
    q4 = by(results._xbrl_quarter("T", date(2026, 3, 31), rows, "consolidated", files.__getitem__))
    rev = q4["FY26Q4", "revenue"]
    assert rev.derived and rev.value_cr == pytest.approx(178_650 - 132_201)
    assert rev.formula.startswith("Q4 = FY - 9M: revenue FY26 178,650.00 - FY26-9M 132,201.00")
    assert rev.period_kind == "Q" and "|" in rev.source_url  # both source files cited
    full = results.add_ebitda(list(q4.values()))
    assert by(full)["FY26Q4", "ebitda"].derived


def test_q4_without_a_december_filing_is_left_out():
    mar = xbrl({"FY": ("2025-04-01", "2026-03-31")}, [("RevenueFromOperations", "FY", 10**10)])
    rows = results.nse.parse_integrated_results(
        {
            "data": [
                raw_row(
                    "31-MAR-2026",
                    "consolidated",
                    "https://x/INTEGRATED_FILING_INDAS_333333_m.xml",
                    "23-Apr-2026 20:00:00",
                )
            ]
        }
    )
    assert results._xbrl_quarter("T", date(2026, 3, 31), rows, "consolidated", lambda u: mar) == []
