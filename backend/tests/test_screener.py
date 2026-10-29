import httpx
import pandas as pd
import pytest

from finn.sources import screener
from finn.sources.http import DiskCache, HttpClient, SourceError

URL = "https://www.screener.in/company/{}/consolidated/"


@pytest.fixture(scope="module")
def infy():
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "screener_INFY.html").read_text()
    return screener.parse_company_page(html, "INFY", URL.format("INFY"))


@pytest.fixture(scope="module")
def hdfc():
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "screener_HDFCBANK.html").read_text()
    return screener.parse_company_page(html, "HDFCBANK", URL.format("HDFCBANK"))


def test_header_and_about(infy):
    assert infy.name == "Infosys Ltd" and infy.basis == "consolidated"
    assert infy.about.startswith("Infosys Ltd provides consulting")
    assert infy.warnings == []
    assert infy.market_cap_cr and infy.market_cap_cr > 100_000


def test_five_tables_as_dataframes_with_units(infy):
    assert set(infy.tables) == {"quarterly", "profit_loss", "balance_sheet", "cash_flow", "ratios"}
    for name in ("quarterly", "profit_loss", "balance_sheet", "cash_flow"):
        t = infy.tables[name]
        assert isinstance(t.df, pd.DataFrame)
        assert t.unit == "Rs. Crores" and t.unit_original == "crore"
    assert infy.tables["ratios"].unit is None


def test_quarterly_values_are_floats(infy):
    q = infy.tables["quarterly"].df
    assert q.columns[0] == "Sep 2023" and q.columns[-1] == "Sep 2026" and len(q.columns) == 13
    assert q.loc["Sales", "Sep 2023"] == 38994.0
    assert q.loc["OPM %"].dropna().between(0, 100).all()
    assert (q.dtypes == "float64").all()
    assert "Raw PDF" not in q.index


def test_profit_loss_has_ttm_and_row_labels_clean(infy):
    pl = infy.tables["profit_loss"].df
    assert pl.columns[-1] == "TTM"
    assert not any(label.endswith("+") for label in pl.index)
    assert {"Sales", "Net Profit"} <= set(pl.index)


def test_annual_reports(infy):
    ars = infy.annual_reports
    assert len(ars) >= 10 and ars[0]["year"] == 2026
    assert ars[0]["url"].startswith("https://") and ars[0]["label"] == "Annual Report 2026"
    assert [a["year"] for a in ars] == sorted((a["year"] for a in ars), reverse=True)


def test_concalls(infy):
    c = infy.concalls
    assert len(c) >= 4 and c[0]["period"] == "Oct 2026"
    assert c[0]["transcript_url"].startswith("https://www.bseindia.com/stockinfo/AnnPdfOpen")
    assert all(x["transcript_url"] for x in c[:4])
    assert c[0]["ppt_url"] is None  # "PPT" chip without a link on this row
    assert c[0]["rec_url"].endswith(".mp3")


def test_concalls_with_ppt_link(hdfc):
    assert hdfc.concalls[0]["ppt_url"].endswith(".pdf")


def test_result_pdfs_are_absolute(infy):
    assert infy.result_pdfs["Sep 2026"].startswith("https://www.screener.in/company/source/")


def test_bank_format(hdfc):
    q = hdfc.tables["quarterly"].df
    assert {"Revenue", "Financing Profit", "Net Profit"} <= set(q.index)
    assert "Sales" not in q.index
    assert hdfc.tables["ratios"].df.shape[0] >= 1


def test_missing_documents_are_reported_clearly(fixtures):
    html = (fixtures / "screener_INFY.html").read_text()
    start = html.index('<section id="documents"')
    stripped = html[:start] + "</main></body></html>"
    c = screener.parse_company_page(stripped, "INFY", URL.format("INFY"))
    assert c.annual_reports == [] and c.concalls == []
    assert any("documents section missing" in w and "anonymous" in w for w in c.warnings)
    assert "quarterly" in c.tables


def test_login_wall_page_has_no_tables():
    c = screener.parse_company_page("<html><h1>Login</h1></html>", "X", URL.format("X"))
    assert c.tables == {} and len([w for w in c.warnings if w.startswith("table missing")]) == 5


def test_parse_number():
    assert screener.parse_number("1,23,456") == 123456.0
    assert screener.parse_number("25%") == 25.0
    assert screener.parse_number("₹ 4,65,283") == 465283.0
    assert screener.parse_number("") is None and screener.parse_number("Raw PDF") is None


def client_for(tmp_path, handler):
    return HttpClient(cache=DiskCache(tmp_path), transport=httpx.MockTransport(handler), spacing={})


def test_fetch_falls_back_to_standalone_on_404(tmp_path, fixtures):
    html = (fixtures / "screener_INFY.html").read_text()
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path.endswith("/consolidated/"):
            return httpx.Response(404)
        return httpx.Response(200, text=html)

    company = screener.fetch_company("INFY", http=client_for(tmp_path, handler), ttl=0)
    assert seen == ["/company/INFY/consolidated/", "/company/INFY/"]
    assert "quarterly" in company.tables


def test_fetch_falls_back_when_consolidated_page_has_no_tables(tmp_path, fixtures):
    html = (fixtures / "screener_INFY.html").read_text()

    def handler(req):
        if req.url.path.endswith("/consolidated/"):
            return httpx.Response(200, text="<html><h1>x</h1></html>")
        return httpx.Response(200, text=html)

    company = screener.fetch_company("INFY", http=client_for(tmp_path, handler), ttl=0)
    assert "quarterly" in company.tables


def test_fetch_raises_when_both_pages_missing(tmp_path):
    with pytest.raises(SourceError):
        screener.fetch_company(
            "NOPE", http=client_for(tmp_path, lambda r: httpx.Response(404)), ttl=0
        )


@pytest.mark.live
def test_live_screener_infy():
    c = screener.fetch_company("INFY", ttl=0)
    assert len(c.tables) == 5 and len(c.concalls) >= 4 and c.annual_reports


def test_inert_transcript_chips_are_reported():
    html = (
        '<section id="documents"><div class="documents concalls"><ul class="list-links">'
        '<li><div>Jul 2026</div><div class="button-chip">Transcript</div>'
        '<a class="button-chip" href="https://x/p.pdf">PPT</a></li></ul></div></section>'
    )
    c = screener.parse_company_page(html, "ITC", URL.format("ITC"))
    assert c.concalls == [
        {
            "period": "Jul 2026",
            "transcript_url": None,
            "ppt_url": "https://x/p.pdf",
            "rec_url": None,
        }
    ]
    assert any("none has a transcript link" in w for w in c.warnings)
