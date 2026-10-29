from finn.sources import screener


def test_transcript_pdf_is_the_one_linked_from_the_screener_fixture(fixtures):
    pdf = (fixtures / "transcript_INFY.pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and len(pdf) > 10_000
    html = (fixtures / "screener_INFY.html").read_text()
    company = screener.parse_company_page(html, "INFY", "fixture")
    assert company.concalls[0]["transcript_url"].endswith(
        "Pname=7982fc61-7765-4981-ab3d-6aec36ba2137.pdf"
    )


def test_pdf_text_extractable(fixtures):
    import pypdf

    reader = pypdf.PdfReader(fixtures / "transcript_INFY.pdf")
    text = reader.pages[0].extract_text()
    assert len(reader.pages) > 3 and "Infosys" in text
