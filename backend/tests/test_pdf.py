import pytest

from finn.enrich import pdf
from finn.sources.http import SourceError
from tests.conftest import FIXTURES


class FakeHttp:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.error:
            raise self.error
        return type("R", (), {"content": self.content})()


PDF_BYTES = (FIXTURES / "transcript_INFY.pdf").read_bytes()


def test_extracts_text_from_the_first_three_pages_only(tmp_path):
    http = FakeHttp(PDF_BYTES)
    got = pdf.pdf_excerpt("https://x/a.pdf", http=http, root=tmp_path)
    assert got.engine == "pdfplumber" and 1 <= got.pages <= 3 and len(got.text) > 200
    assert got.pages == min(3, got.pages)
    assert http.calls[0][1]["ttl"] == pdf.TTL_PDF  # the download is cached too


def test_extracted_text_is_cached_so_the_second_read_makes_no_request(tmp_path):
    http = FakeHttp(PDF_BYTES)
    first = pdf.pdf_excerpt("https://x/a.pdf", http=http, root=tmp_path)
    second = pdf.pdf_excerpt("https://x/a.pdf", http=http, root=tmp_path)
    assert len(http.calls) == 1 and second.cached and second.text == first.text


def test_pypdf_is_the_fallback_when_pdfplumber_fails(monkeypatch):
    def boom(data, pages):
        raise RuntimeError("broken")

    monkeypatch.setattr(pdf, "_extract_plumber", boom)
    text, pages, engine = pdf.extract_text(PDF_BYTES)
    assert engine == "pypdf" and len(text) > 200 and pages >= 1


def test_text_matches_between_engines_closely_enough_for_quotes():
    a = pdf._extract_plumber(PDF_BYTES, 1)[0]
    b = pdf._extract_pypdf(PDF_BYTES, 1)[0]
    from finn.normalise.quote import quote_match

    assert quote_match(" ".join(a.split()[:40]), b) > 0.6


def test_no_url_a_download_error_or_a_non_pdf_gives_none_not_invented_text(tmp_path):
    assert pdf.pdf_excerpt(None, root=tmp_path) is None
    assert (
        pdf.pdf_excerpt("https://x/a.pdf", http=FakeHttp(error=SourceError("403")), root=tmp_path)
        is None
    )
    assert (
        pdf.pdf_excerpt("https://x/a.pdf", http=FakeHttp(b"<html>blocked</html>"), root=tmp_path)
        is None
    )


def test_an_unreadable_pdf_returns_empty_text():
    text, pages, engine = pdf.extract_text(b"%PDF-1.4 not really")
    assert text == "" and engine == "none"


@pytest.mark.live
def test_live_nse_attachment():
    got = pdf.pdf_excerpt(
        "https://nsearchives.nseindia.com/corporate/HDFCBANK_27102026080232_SEIntimation_appt_of_AB.pdf"
    )
    assert got and "Chief Executive Officer" in got.text
