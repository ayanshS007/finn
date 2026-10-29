from datetime import date

import pytest

from finn.db.models import Fact
from finn.normalise.periods import (
    PeriodError,
    derive_h2,
    derive_q4,
    fiscal_year,
    parse_period,
    period_from_range,
    quarter_for_end,
    shift_quarters,
)


@pytest.mark.parametrize(
    "text, label",
    [
        ("Quarter ended 30-Sep-2026", "FY27Q2"),
        ("Quarter ended 30/09/2026", "FY27Q2"),
        ("QUARTER ENDED 30-SEP-2026", "FY27Q2"),
        ("Q2 FY27", "FY27Q2"),
        ("Q2FY27", "FY27Q2"),
        ("2Q FY27", "FY27Q2"),
        ("Q2 FY2027", "FY27Q2"),
        ("Q2 2026-27", "FY27Q2"),
        ("FY27Q2", "FY27Q2"),
        ("FY 2026-27 Q2", "FY27Q2"),
        ("Sep 2026", "FY27Q2"),
        ("September 2026", "FY27Q2"),
        ("30th September 2026", "FY27Q2"),
        ("2026-09-30", "FY27Q2"),
        ("30-SEP-2026", "FY27Q2"),
        # the fiscal year starts in April: January to March belong to the year that ends
        ("Jun 2026", "FY27Q1"),
        ("Dec 2026", "FY27Q3"),
        ("Mar 2027", "FY27Q4"),
        ("Quarter ended 31-Mar-2027", "FY27Q4"),
        ("Q4 FY27", "FY27Q4"),
        ("Mar 2026", "FY26Q4"),
        ("H1 FY27", "FY27H1"),
        ("Half year ended 30-Sep-2026", "FY27H1"),
        ("Half year ended 31 March 2027", "FY27H2"),
        ("Nine months ended 31-Dec-2026", "FY27-9M"),
        ("9M FY27", "FY27-9M"),
        ("Year ended 31st March 2027", "FY27"),
        ("FY2026-27", "FY27"),
        ("FY27", "FY27"),
    ],
)
def test_parse_period(text, label):
    assert parse_period(text).label == label


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello",
        "Aug 2026",  # not a quarter end
        "Quarter ended 31-Aug-2026",
        "30-Sep-2026 to 31-Oct-2026 and more",  # nothing sensible
        "31-Feb-2026",
        "Year ended 30-Sep-2026",  # a year cannot end in September
        "Nine months ended 30-Sep-2026",
        "Q5 FY27",
        "Q2 2026",  # bare year: calendar or fiscal? never guess
        "FY26-28",
        "Q2 FY27 results",
    ],
)
def test_parse_period_refuses_to_guess(text):
    with pytest.raises(PeriodError):
        parse_period(text)


def test_xbrl_context_ranges():
    assert period_from_range(date(2026, 7, 1), date(2026, 9, 30)).label == "FY27Q2"
    assert period_from_range(date(2026, 1, 1), date(2026, 3, 31)).label == "FY26Q4"
    assert period_from_range(date(2026, 4, 1), date(2026, 9, 30)).label == "FY27H1"
    assert period_from_range(date(2026, 4, 1), date(2026, 12, 31)).label == "FY27-9M"
    assert period_from_range(date(2025, 4, 1), date(2026, 3, 31)).label == "FY26"
    assert period_from_range(date(2026, 10, 1), date(2027, 3, 31)).label == "FY27H2"
    for start, end in [
        (date(2026, 7, 2), date(2026, 9, 30)),  # does not start on the 1st
        (date(2026, 7, 1), date(2026, 10, 31)),  # not a quarter end
        (date(2026, 6, 1), date(2026, 11, 30)),  # 6 months but not a fiscal half
        (date(2026, 1, 1), date(2026, 12, 31)),  # calendar year
    ]:
        with pytest.raises(PeriodError):
            period_from_range(start, end)


def test_fiscal_year_and_shifts():
    assert fiscal_year(date(2026, 3, 31)) == 2026 and fiscal_year(date(2026, 4, 1)) == 2027
    assert quarter_for_end(date(2024, 3, 31)).label == "FY24Q4"  # leap year
    assert shift_quarters(date(2026, 9, 30), -1) == date(2026, 6, 30)
    assert shift_quarters(date(2026, 3, 31), 1) == date(2026, 6, 30)  # across a year end
    assert shift_quarters(date(2026, 9, 30), -4) == date(2025, 9, 30)
    assert shift_quarters(date(2027, 3, 31), -1) == date(2026, 12, 31)
    with pytest.raises(PeriodError):
        quarter_for_end(date(2026, 9, 29))


def fact(period, kind, value, metric="revenue", url="u://x"):
    return Fact(
        ticker="TEST",
        metric=metric,
        value_cr=value,
        unit_original="crore",
        period=period,
        period_kind=kind,
        basis="consolidated",
        source_url=url,
        quote=f"q {period} {value}",
        quote_match=1.0,
    )


def test_q4_is_fy_minus_9m_and_marked_derived():
    q4 = derive_q4(fact("FY26", "FY", 178_650.0, url="u://fy"), fact("FY26-9M", "9M", 132_248.0))
    assert q4.value_cr == 46_402.0
    assert (q4.period, q4.period_kind, q4.derived) == ("FY26Q4", "Q", True)
    assert q4.formula == "Q4 = FY - 9M: revenue FY26 178,650.00 - FY26-9M 132,248.00 = 46,402.00"
    assert "u://fy" in q4.source_url


def test_h2_is_fy_minus_h1_and_marked_derived():
    h2 = derive_h2(fact("FY27", "FY", 100.0), fact("FY27H1", "H1", 40.25))
    assert (h2.value_cr, h2.period, h2.period_kind, h2.derived) == (59.75, "FY27H2", "H2", True)
    assert h2.formula.startswith("H2 = FY - H1:")


def test_derivation_refuses_mismatched_inputs():
    fy = fact("FY26", "FY", 10.0)
    with pytest.raises(PeriodError):
        derive_q4(fy, fact("FY25-9M", "9M", 5.0))  # different fiscal year
    with pytest.raises(PeriodError):
        derive_q4(fy, fact("FY26-9M", "9M", 5.0, metric="pat"))  # different metric
    with pytest.raises(PeriodError):
        derive_q4(fy, fact("FY26-9M", "9M", None))  # no value to subtract
    with pytest.raises(PeriodError):
        derive_q4(fy, fact("FY26H1", "H1", 5.0))  # not a 9M fact
