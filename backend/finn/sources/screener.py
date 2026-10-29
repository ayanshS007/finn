"""screener.in company pages: tables as DataFrames, about text, annual reports and concalls."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup, Tag

from finn.sources.http import HttpClient, SourceError, shared_client

BASE = "https://www.screener.in/company"
TTL_PAGE = 24 * 3600

# page section id -> table name
SECTIONS = {
    "quarters": "quarterly",
    "profit-loss": "profit_loss",
    "balance-sheet": "balance_sheet",
    "cash-flow": "cash_flow",
    "ratios": "ratios",
}
UNIT_ORIGINAL = {"rs. crores": "crore", "rs. lakhs": "lakh", "rs. millions": "million"}


@dataclass
class ScreenerTable:
    df: pd.DataFrame  # index: row label; columns: period labels as printed ("Sep 2026", "TTM")
    unit: str | None  # as printed on the page, e.g. "Rs. Crores"; None where none is stated
    unit_original: str | None  # crore | lakh | million | None


@dataclass
class ScreenerCompany:
    symbol: str
    url: str
    name: str | None
    basis: str | None  # consolidated | standalone
    about: str | None
    top_ratios: dict[str, str]
    tables: dict[str, ScreenerTable]
    annual_reports: list[dict]  # {year, label, url, source}
    concalls: list[dict]  # {period, transcript_url, ppt_url, rec_url}
    result_pdfs: dict[str, str]  # period label -> results PDF url ("Raw PDF" row)
    warnings: list[str] = field(default_factory=list)

    @property
    def market_cap_cr(self) -> float | None:
        return parse_number(self.top_ratios.get("Market Cap", "").replace("Cr.", ""))


def fetch_company(
    symbol: str, *, ttl: float = TTL_PAGE, http: HttpClient | None = None
) -> ScreenerCompany:
    """Consolidated page first; standalone when it is missing or has no quarterly table."""
    http = http or shared_client()
    best: ScreenerCompany | None = None
    last_error: SourceError | None = None
    for url in (f"{BASE}/{symbol}/consolidated/", f"{BASE}/{symbol}/"):
        try:
            resp = http.get(url, ttl=ttl)
        except SourceError as exc:
            if exc.status != 404:
                raise
            last_error = exc
            continue
        company = parse_company_page(resp.text, symbol, resp.url)
        if "quarterly" in company.tables:
            return company
        best = best or company
    if best is None:
        raise last_error or SourceError(f"screener: no page for {symbol}")
    return best


def parse_number(text: str | None) -> float | None:
    """'38,994' -> 38994.0; '25%' -> 25.0; '₹ 4,65,283' -> 465283.0; blanks -> None."""
    if text is None:
        return None
    cleaned = re.sub(r"[,\s%₹]|Rs\.?", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _unit(section: Tag) -> tuple[str | None, str | None, str | None]:
    """(basis, unit, unit_original) from the 'Consolidated Figures in Rs. Crores' caption."""
    for p in section.find_all("p", class_="sub"):
        caption = _text(p)
        unit = re.search(r"Figures in (.+?)(?:\s*/|$)", caption)
        basis = re.match(r"(Consolidated|Standalone)", caption)
        if unit or basis:
            u = unit.group(1).strip() if unit else None
            return (
                basis.group(1).lower() if basis else None,
                u,
                UNIT_ORIGINAL.get((u or "").lower()),
            )
    return None, None, None


def _parse_table(table: Tag) -> tuple[pd.DataFrame, dict[str, str]]:
    headers = [_text(th) for th in table.select("thead th")][1:]
    rows: dict[str, list[float | None]] = {}
    links: dict[str, str] = {}
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        label = re.sub(r"\s*\+$", "", _text(cells[0])) if cells else ""
        if not label:
            continue
        if label == "Raw PDF":
            for header, td in zip(headers, cells[1:], strict=False):
                if (a := td.find("a", href=True)) is not None:
                    links[header] = a["href"]
            continue
        values = [parse_number(_text(td)) for td in cells[1:]]
        values += [None] * (len(headers) - len(values))
        rows[label] = values[: len(headers)]
    df = pd.DataFrame.from_dict(rows, orient="index", columns=headers, dtype="float64")
    return df.dropna(how="all"), links


def parse_company_page(html: str, symbol: str, url: str) -> ScreenerCompany:
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []

    tables: dict[str, ScreenerTable] = {}
    result_pdfs: dict[str, str] = {}
    basis = None
    for section_id, name in SECTIONS.items():
        section = soup.find("section", id=section_id)
        table = section.find("table") if section else None
        if table is None:
            warnings.append(f"table missing: {name}")
            continue
        section_basis, unit, unit_original = _unit(section)
        basis = basis or section_basis
        df, links = _parse_table(table)
        if df.empty:
            warnings.append(f"table empty: {name}")
            continue
        tables[name] = ScreenerTable(df, unit, unit_original)
        if name == "quarterly":
            result_pdfs = {k: urljoin(url, v) for k, v in links.items()}
    if basis is None:
        basis = "consolidated" if "/consolidated" in url else "standalone"

    annual_reports, concalls = _parse_documents(soup, warnings)
    about = soup.select_one(".company-profile .about")
    top = {
        _text(li.select_one(".name")): _text(li.select_one(".value"))
        for li in soup.select("#top-ratios li")
        if li.select_one(".name")
    }
    return ScreenerCompany(
        symbol=symbol,
        url=url,
        name=_text(soup.find("h1")) or None,
        basis=basis,
        about=_text(about) or None,
        top_ratios=top,
        tables=tables,
        annual_reports=annual_reports,
        concalls=concalls,
        result_pdfs=result_pdfs,
        warnings=warnings,
    )


def _parse_documents(soup: BeautifulSoup, warnings: list[str]) -> tuple[list[dict], list[dict]]:
    docs = soup.find("section", id="documents")
    if docs is None:
        warnings.append(
            "documents section missing: screener may hide document links from anonymous requests"
        )
        return [], []

    annual: list[dict] = []
    for a in docs.select(".annual-reports li > a[href]"):
        label = _text(a)
        year = re.search(r"\b(19|20)\d{2}\b", label)
        source = a.find_next("span", class_="ink-600")
        annual.append(
            {
                "year": int(year.group(0)) if year else None,
                "label": label,
                "url": a["href"],
                "source": _text(source).removeprefix("from ") or None,
            }
        )
    if not annual:
        warnings.append("no annual report links in documents section")

    concalls: list[dict] = []
    for li in docs.select(".concalls li"):
        period = _text(li.find("div"))
        links = {_text(a): a["href"] for a in li.find_all("a", href=True)}
        concalls.append(
            {
                "period": period,
                "transcript_url": links.get("Transcript"),
                "ppt_url": links.get("PPT"),
                "rec_url": links.get("REC"),
            }
        )
    if not concalls:
        warnings.append("no concall rows in documents section")
    elif not any(c["transcript_url"] for c in concalls):
        warnings.append(
            f"{len(concalls)} concall rows but none has a transcript link "
            "(screener shows an inert Transcript chip)"
        )
    return annual, concalls
