import pandas as pd

from finn.sources import coverage
from finn.sources.coverage import CoverageRow, check_symbol, pick_replacement, render_markdown
from finn.sources.http import SourceBlocked, SourceError
from finn.sources.screener import SECTIONS, ScreenerCompany, ScreenerTable


def company(tables=5, transcripts=4, reports=1, mcap="₹ 5,000 Cr."):
    names = list(SECTIONS.values())[:tables]
    return ScreenerCompany(
        symbol="X",
        url="u",
        name="X",
        basis="consolidated",
        about="a",
        top_ratios={"Market Cap": mcap},
        tables={n: ScreenerTable(pd.DataFrame(), None, None) for n in names},
        annual_reports=[{"year": 2026}] * reports,
        concalls=[{"transcript_url": "t"}] * transcripts,
        result_pdfs={},
        warnings=[],
    )


def frame(symbol, n):
    return pd.DataFrame({"symbol": [symbol] * n})


def test_all_checks_pass():
    row = check_symbol("X", frame("X", 60), lambda s: company(), lambda s: [{}])
    assert row.passed and row.failed_checks == []


def test_each_threshold_is_enforced():
    base = dict(prices=frame("X", 60), fetch_announcements=lambda s: [{}])
    cases = {
        "prices": dict(prices=frame("X", 59), fetch_company=lambda s: company()),
        "screener tables": dict(fetch_company=lambda s: company(tables=4)),
        "transcripts": dict(fetch_company=lambda s: company(transcripts=3)),
        "annual reports": dict(fetch_company=lambda s: company(reports=0)),
    }
    for failed, kw in cases.items():
        row = check_symbol("X", **{**base, **kw})
        assert row.failed_checks == [failed], failed


def test_source_errors_become_failures_with_notes_not_exceptions():
    def boom(_):
        raise SourceBlocked("HTTP 403")

    row = check_symbol("X", frame("X", 80), lambda s: company(), boom)
    assert row.failed_checks == ["NSE announcements"]
    assert any("NSE announcements" in n for n in row.notes)

    def no_page(_):
        raise SourceError("HTTP 404", 404)

    row = check_symbol("X", frame("X", 80), no_page, lambda s: [])
    assert {"screener tables", "transcripts", "annual reports"} <= set(row.failed_checks)
    assert row.nse_announcements == 0  # reachable but quiet


def test_latest_result_period():
    row = check_symbol(
        "X",
        frame("X", 60),
        lambda s: company(),
        lambda s: [],
        lambda s: [
            {"period_end": "2026-06-30"},
            {"period_end": "2026-09-30"},
            {"period_end": None},
        ],
    )
    assert row.latest_result_period == "2026-09-30"


def test_pick_replacement_prefers_active_small_cap_that_passes():
    def mk(sym, mcap, ann, ok=True):
        return CoverageRow(sym, 80, list(SECTIONS.values()) if ok else [], 5, 2, ann, mcap)

    rows = [
        mk("BIG", coverage.SMALLCAP_MAX_MCAP_CR + 1, 99),
        mk("BROKEN", 1000, 80, ok=False),
        mk("QUIET", 1000, 5),
        mk("BUSY", 9000, 40),
    ]
    assert pick_replacement(rows).symbol == "BUSY"
    assert pick_replacement(rows[:2]) is None


def test_markdown_table_has_pass_fail_column():
    ok = CoverageRow("A", 80, list(SECTIONS.values()), 5, 2, 7, 1000.0, "2026-09-30")
    bad = CoverageRow("B", 10, [], 0, 0, None, None, None, ["screener: HTTP 404"])
    md = render_markdown(
        [ok, bad], generated="t", pending=["SMALLCAP_TBD"], candidates=[ok], chosen="A"
    )
    assert "| A | 80 (ok) | 5/5 (ok) | 5 (ok) | 2 (ok) | 7 | 2026-09-30 | PASS |" in md
    assert "| B |" in md and "FAIL: prices, screener tables" in md and "unreachable" in md
    assert "PENDING" in md and "**Chosen: A.**" in md and "1/2 portfolio symbols pass" in md
