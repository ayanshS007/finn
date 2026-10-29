"""results_facts(symbol): the Facts behind every results card and the Promise Ledger.

Returns Facts for the current quarter, the previous quarter and the same quarter last year,
all on one basis (consolidated when the current quarter has it, else standalone). Source order
per period:

1. integrated-filing XBRL (finn.parse.xbrl_results), one filing per quarter, Mar 2025 onwards;
2. the screener.in quarterly table when the XBRL filing for that period is not listed;
3. NSE /api/results-comparision, only for periods up to Dec 2024 and only on a standalone basis
   (it is frozen there and its figures are standalone: see docs/BUILD_LOG.md).

A source that blocks or changes shape raises; it is never papered over with another source.
A period that no source has is logged and left out. EBITDA is derived in code from the other
lines; banks and NBFCs get nii and ppop instead.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
from collections.abc import Callable
from datetime import date

from finn.db.models import Fact
from finn.normalise.periods import (
    Period,
    PeriodError,
    derive_q4,
    parse_date_text,
    parse_period,
    quarter_for_end,
    shift_quarters,
)
from finn.normalise.quote import quote_match
from finn.normalise.units import detect_unit, to_crore
from finn.parse.xbrl_results import XbrlFiling, fetch_xbrl, parse_xbrl
from finn.sources import nse, screener
from finn.sources.http import SourceError

log = logging.getLogger(__name__)

LEGACY_LAST_PERIOD = date(2024, 12, 31)
METRIC_ORDER = [
    "revenue",
    "ebitda",
    "nii",
    "ppop",
    "other_income",
    "finance_costs",
    "d_and_a",
    "pbt",
    "tax",
    "pat",
]

SCREENER_ROWS = {
    "Sales": "revenue",
    "Other Income": "other_income",
    "Interest": "finance_costs",
    "Depreciation": "d_and_a",
    "Profit before tax": "pbt",
    "Net Profit": "pat",
}
# results-comparision field -> metric (all in lakh)
LEGACY_FIELDS = {
    "re_net_sale": "revenue",
    "re_oth_inc_new": "other_income",
    "re_int_new": "finance_costs",
    "re_depr_und_exp": "d_and_a",
    "re_pro_loss_bef_tax": "pbt",
    "re_tax": "tax",
    "re_net_profit": "pat",
}
LEGACY_BANK_FIELDS = {
    "re_oth_inc": "other_income",
    "re_pro_loss_bef_tax": "pbt",
    "re_tax": "tax",
    "re_net_profit": "pat",
    "re_oper_exp_bef_pro_cont": "ppop",  # NSE's name is misleading: it is operating profit
}
LEGACY_URL = "https://www.nseindia.com/api/results-comparision?symbol={}"


class ResultsClient:
    """What results_facts needs from NSE; NSEClient satisfies it, tests pass a fake."""

    def integrated_results_raw(self, symbol: str) -> dict: ...
    def results_comparison(self, symbol: str) -> dict: ...


# -- source 1: XBRL ---------------------------------------------------------------------------


def _pick_row(rows: list[dict], end: date, basis: str) -> dict | None:
    """Latest filing for that period and basis; a revision supersedes the original."""
    mine = [r for r in rows if r["period_end"] == end.isoformat() and r["basis"] == basis]
    return max(mine, key=lambda r: r["filed_at"] or "", default=None)


def _filing(row: dict, fetch: Callable[[str], bytes]) -> XbrlFiling:
    return parse_xbrl(fetch(row["xbrl_url"]), row["xbrl_url"], row["basis"])


def _xbrl_quarter(
    symbol: str, end: date, rows: list[dict], basis: str, fetch: Callable[[str], bytes]
) -> list[Fact]:
    period = quarter_for_end(end)
    row = _pick_row(rows, end, basis)
    if row is None:
        return []
    facts = _filing(row, fetch).to_facts(symbol)
    quarter = [f for f in facts if f.period == period.label]
    if quarter:
        return quarter
    if period.quarter != 4:
        return []
    # Fourth quarter without its own column: Q4 = FY - 9M, the 9M figures coming from Dec.
    dec_row = _pick_row(rows, shift_quarters(end, -1), basis)
    if dec_row is None:
        return []
    nine_m = {
        f.metric: f
        for f in _filing(dec_row, fetch).to_facts(symbol)
        if f.period_kind == "9M" and f.period == f"FY{period.fy % 100:02d}-9M"
    }
    out = []
    for f in facts:
        if f.period_kind == "FY" and f.metric in nine_m and f.metric != "ppop":
            out.append(derive_q4(f, nine_m[f.metric]))
    return out


# -- source 2: screener -----------------------------------------------------------------------


def _screener_column(end: date) -> str:
    return f"{calendar.month_abbr[end.month]} {end.year}"


def _screener_quarter(symbol: str, end: date, company: screener.ScreenerCompany) -> list[Fact]:
    table = company.tables.get("quarterly")
    label = _screener_column(end)
    if table is None or label not in table.df.columns:
        return []
    unit = detect_unit(table.unit or "")
    period = quarter_for_end(end)
    df = table.df
    lender = "Financing Profit" in df.index

    def fact(metric, value, row_labels, derived=False, formula=None) -> Fact:
        quote = " | ".join(table.row_text[r] for r in row_labels)
        return Fact(
            ticker=symbol,
            metric=metric,
            value_cr=round(to_crore(value, unit), 6),
            unit_original=unit,
            period=period.label,
            period_kind="Q",
            basis=company.basis,
            derived=derived,
            formula=formula,
            source_url=company.url,
            quote=quote,
            quote_match=min(quote_match(table.row_text[r], company.page_text) for r in row_labels),
        )

    def value(row: str) -> float | None:
        if row not in df.index:
            return None
        v = df.at[row, label]
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    out: list[Fact] = []
    if lender:
        wanted = {"Other Income": "other_income", "Profit before tax": "pbt", "Net Profit": "pat"}
        revenue, interest = value("Revenue"), value("Interest")
        if revenue is not None and interest is not None:
            nii = to_crore(revenue, unit) - to_crore(interest, unit)
            out.append(
                fact(
                    "nii",
                    revenue - interest,
                    ["Revenue", "Interest"],
                    derived=True,
                    formula=f"nii = Revenue - Interest = {to_crore(revenue, unit):,.2f} - "
                    f"{to_crore(interest, unit):,.2f} = {nii:,.2f}",
                )
            )
    else:
        wanted = SCREENER_ROWS
    for row, metric in wanted.items():
        if (v := value(row)) is not None:
            out.append(fact(metric, v, [row]))
    return out


# -- source 3: legacy results-comparision -----------------------------------------------------


def _legacy_row_facts(symbol: str, period: Period, row: dict, bank: bool) -> list[Fact]:
    source = json.dumps(row)
    out: list[Fact] = []

    def add(metric: str, lakh: float, quote: str, **extra) -> None:
        out.append(
            Fact(
                ticker=symbol,
                metric=metric,
                value_cr=round(to_crore(lakh, "lakh"), 6),
                unit_original="lakh",
                period=period.label,
                period_kind="Q",
                basis="standalone",
                source_url=LEGACY_URL.format(symbol),
                quote=quote,
                quote_match=quote_match(quote, source),
                **extra,
            )
        )

    for field, metric in (LEGACY_BANK_FIELDS if bank else LEGACY_FIELDS).items():
        if row.get(field) not in (None, "", "-"):
            add(metric, float(row[field]), f'"{field}": "{row[field]}"')
    if bank and row.get("re_int_earned") and row.get("re_int_expd"):
        earned, spent = float(row["re_int_earned"]), float(row["re_int_expd"])
        nii = to_crore(earned - spent, "lakh")
        add(
            "nii",
            earned - spent,
            f'"re_int_earned": "{row["re_int_earned"]}", "re_int_expd": "{row["re_int_expd"]}"',
            derived=True,
            formula=(
                f"nii = interest_earned - interest_expended = {to_crore(earned, 'lakh'):,.2f} - "
                f"{to_crore(spent, 'lakh'):,.2f} = {nii:,.2f}"
            ),
        )
    return out


def _legacy_quarter(symbol: str, end: date, payload: dict) -> list[Fact]:
    bank = payload.get("bankNonBnking") == "B"
    period = quarter_for_end(end)
    for row in payload.get("resCmpData", []):
        try:
            row_end = parse_date_text(row["re_to_dt"])
            row_start = parse_date_text(row["re_from_dt"])
        except (KeyError, PeriodError):
            continue
        if row_end == end and (row_end.month - row_start.month) % 12 == 2:  # a 3-month column
            return _legacy_row_facts(symbol, period, row, bank)
    return []


# -- derived lines ----------------------------------------------------------------------------


def add_ebitda(facts: list[Fact]) -> list[Fact]:
    """ebitda = pbt + finance_costs + d_and_a - other_income, per period and basis (not lenders)."""
    out = list(facts)
    groups: dict[tuple, dict[str, Fact]] = {}
    for f in facts:
        groups.setdefault((f.ticker, f.period, f.period_kind, f.basis, f.segment), {})[f.metric] = f
    for (ticker, period, kind, basis, segment), by in groups.items():
        need = ("pbt", "finance_costs", "d_and_a", "other_income")
        if "ebitda" in by or {"nii", "ppop"} & by.keys() or any(by.get(m) is None for m in need):
            continue
        parts = [by[m] for m in need]
        if any(p.value_cr is None for p in parts):
            continue
        pbt, fin, dna, oi = (p.value_cr for p in parts)
        value = round(pbt + fin + dna - oi, 6)
        out.append(
            Fact(
                ticker=ticker,
                metric="ebitda",
                segment=segment,
                value_cr=value,
                unit_original=by["pbt"].unit_original,
                period=period,
                period_kind=kind,
                basis=basis,
                derived=True,
                formula=f"ebitda = pbt + finance_costs + d_and_a - other_income = "
                f"{pbt:,.2f} + {fin:,.2f} + {dna:,.2f} - {oi:,.2f} = {value:,.2f}",
                source_url=by["pbt"].source_url,
                quote=" | ".join(p.quote for p in parts),
                quote_match=min(p.quote_match for p in parts),
            )
        )
    return out


# -- entry point ------------------------------------------------------------------------------


def results_facts(
    symbol: str,
    *,
    client: ResultsClient | None = None,
    fetch: Callable[[str], bytes] | None = None,
    screener_fetch: Callable[[str], screener.ScreenerCompany] | None = None,
) -> list[Fact]:
    """Facts for the current quarter, the previous quarter and the same quarter last year."""
    client = client or nse.NSEClient()
    fetch = fetch or fetch_xbrl
    screener_fetch = screener_fetch or screener.fetch_company

    rows = [
        r
        for r in nse.parse_integrated_results(client.integrated_results_raw(symbol))
        if r["xbrl_url"] and r["period_end"]
    ]
    company_cache: list[screener.ScreenerCompany] = []

    def company() -> screener.ScreenerCompany:
        if not company_cache:
            company_cache.append(screener_fetch(symbol))
        return company_cache[0]

    if rows:
        current_end = date.fromisoformat(max(r["period_end"] for r in rows))
        basis = (
            "consolidated"
            if any(
                r["basis"] == "consolidated" and r["period_end"] == current_end.isoformat()
                for r in rows
            )
            else "standalone"
        )
    else:
        columns = company().tables["quarterly"].df.columns
        current_end = parse_period(columns[-1]).end
        basis = company().basis
    log.info("%s: current quarter %s, basis %s", symbol, quarter_for_end(current_end), basis)

    facts: list[Fact] = []
    for offset in (0, -1, -4):
        end = shift_quarters(current_end, offset)
        got = _xbrl_quarter(symbol, end, rows, basis, fetch)
        if not got:
            try:
                got = _screener_quarter(symbol, end, company())
            except SourceError as exc:
                if offset == 0:
                    raise
                log.warning("%s %s: screener unavailable (%s)", symbol, end, exc)
            if got and got[0].basis != basis:
                log.warning(
                    "%s %s: screener is %s, wanted %s; dropped", symbol, end, got[0].basis, basis
                )
                got = []
        if not got and end <= LEGACY_LAST_PERIOD and basis == "standalone":
            got = _legacy_quarter(symbol, end, client.results_comparison(symbol))
        if not got:
            log.warning("%s: no source has %s on a %s basis", symbol, quarter_for_end(end), basis)
        facts += got
    facts = add_ebitda(facts)
    facts.sort(
        key=lambda f: (
            -_end_ordinal(f.period),
            METRIC_ORDER.index(f.metric) if f.metric in METRIC_ORDER else 99,
        )
    )
    return facts


def _end_ordinal(label: str) -> int:
    return parse_period(label).end.toordinal()
