"""Per-symbol source coverage checks. Pure: fetchers are passed in, so tests need no network."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from finn.sources.http import SourceError
from finn.sources.screener import SECTIONS, ScreenerCompany

MIN_PRICE_ROWS = 60
MIN_TRANSCRIPTS = 4
MIN_ANNUAL_REPORTS = 1
SMALLCAP_MAX_MCAP_CR = 25_000  # heuristic ceiling for "small-cap"


@dataclass
class CoverageRow:
    symbol: str
    price_rows: int = 0
    tables: list[str] = field(default_factory=list)
    transcripts: int = 0
    annual_reports: int = 0
    nse_announcements: int | None = None  # None = unreachable
    market_cap_cr: float | None = None
    latest_result_period: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def checks(self) -> dict[str, bool]:
        return {
            "prices": self.price_rows >= MIN_PRICE_ROWS,
            "screener tables": set(self.tables) == set(SECTIONS.values()),
            "transcripts": self.transcripts >= MIN_TRANSCRIPTS,
            "annual reports": self.annual_reports >= MIN_ANNUAL_REPORTS,
            "NSE announcements": self.nse_announcements is not None,
        }

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def failed_checks(self) -> list[str]:
        return [name for name, ok in self.checks.items() if not ok]


def check_symbol(
    symbol: str,
    prices: pd.DataFrame,
    fetch_company: Callable[[str], ScreenerCompany],
    fetch_announcements: Callable[[str], list[dict]],
    fetch_results: Callable[[str], list[dict]] | None = None,
) -> CoverageRow:
    row = CoverageRow(symbol, price_rows=int((prices["symbol"] == symbol).sum()))
    try:
        company = fetch_company(symbol)
        row.tables = sorted(company.tables)
        row.transcripts = sum(1 for c in company.concalls if c["transcript_url"])
        row.annual_reports = len(company.annual_reports)
        row.market_cap_cr = company.market_cap_cr
        row.notes += company.warnings
    except SourceError as exc:
        row.notes.append(f"screener: {exc}")
    try:
        row.nse_announcements = len(fetch_announcements(symbol))
    except SourceError as exc:
        row.notes.append(f"NSE announcements: {exc}")
    if fetch_results:
        try:
            ends = [r["period_end"] for r in fetch_results(symbol) if r["period_end"]]
            row.latest_result_period = max(ends, default=None)
        except SourceError as exc:
            row.notes.append(f"NSE results: {exc}")
    return row


def pick_replacement(rows: list[CoverageRow]) -> CoverageRow | None:
    """Among small-caps passing every check, the one with the most recent NSE filings."""
    ok = [
        r
        for r in rows
        if r.passed and r.market_cap_cr is not None and r.market_cap_cr <= SMALLCAP_MAX_MCAP_CR
    ]
    return max(ok, key=lambda r: r.nse_announcements or 0, default=None)


def render_markdown(
    rows: list[CoverageRow],
    *,
    generated: str,
    pending: list[str] = (),
    candidates: list[CoverageRow] = (),
    chosen: str | None = None,
    window_days: int = 30,
) -> str:
    def line(r: CoverageRow, with_mcap: bool = False) -> str:
        mark = lambda ok: "ok" if ok else "**no**"  # noqa: E731
        c = r.checks
        cells = [
            r.symbol,
            f"{r.price_rows} ({mark(c['prices'])})",
            f"{len(r.tables)}/{len(SECTIONS)} ({mark(c['screener tables'])})",
            f"{r.transcripts} ({mark(c['transcripts'])})",
            f"{r.annual_reports} ({mark(c['annual reports'])})",
            "unreachable" if r.nse_announcements is None else str(r.nse_announcements),
        ]
        if with_mcap:
            cells.append(f"{r.market_cap_cr:,.0f}" if r.market_cap_cr else "-")
        else:
            cells.append(r.latest_result_period or "-")
        cells.append("PASS" if r.passed else "FAIL: " + ", ".join(r.failed_checks))
        cells.append("; ".join(r.notes) or "")
        return "| " + " | ".join(cells) + " |"

    head = (
        "| Symbol | Price rows | Screener tables | Transcripts | Annual reports | "
        f"NSE announcements ({window_days}d) | {{extra}} | Result | Notes |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    passed = sum(r.passed for r in rows)
    out = [
        "# Source coverage",
        "",
        f"Generated {generated} by `scripts/coverage.py`. "
        f"Pass = prices >= {MIN_PRICE_ROWS} rows, all {len(SECTIONS)} screener tables parse, "
        f">= {MIN_TRANSCRIPTS} transcripts linked, >= {MIN_ANNUAL_REPORTS} annual report, "
        "NSE announcements reachable.",
        "",
        f"**{passed}/{len(rows)} portfolio symbols pass.**",
        "",
        head.format(extra="Latest NSE results period") + "\n".join(line(r) for r in rows),
    ]
    for symbol in pending:
        out.append(f"| {symbol} | - | - | - | - | - | - | PENDING: not a real symbol yet | |")
    if candidates:
        out += [
            "",
            "## Small-cap replacement candidates",
            "",
            f"Checked with the same rules; market cap ceiling Rs {SMALLCAP_MAX_MCAP_CR:,} crore. "
            + (f"**Chosen: {chosen}.**" if chosen else "None passed."),
            "",
            head.format(extra="Market cap (Rs cr)") + "\n".join(line(r, True) for r in candidates),
        ]
    return "\n".join(out) + "\n"
