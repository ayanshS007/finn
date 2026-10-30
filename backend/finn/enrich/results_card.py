"""The results card: a quarter's headline figures, built only from checked facts.

For a filing labelled results.filed the figures come from finn.parse.results.results_facts, the
integrated-filing XBRL path, and every number on the card is computed here in Python: the current
quarter, QoQ % and YoY % against the facts of the two earlier quarters, the derived flag and
formula, the exceptional-items flag and the basis. No model is involved.

Only when no XBRL exists for the filing's quarter does the `reader` model read the results table of
the attached PDF, and then it only copies printed numbers with a quote of the table row: code
parses the number, converts the unit and runs the same checks (CLAUDE.md rules 1-3, 6).

The card shows numbers only when the checks pass: every figure present, quote_match >= 0.4 for each,
and pat = pbt - tax within 1% (finn.parse.checks.arithmetic_ok). Otherwise card_json.state is
"check_source" and the card holds no figures at all (and no facts are written).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel

from finn.db.models import Fact
from finn.enrich.pdf import pdf_excerpt
from finn.llm.router import Router, default_router
from finn.normalise.periods import (
    PeriodError,
    parse_date_text,
    period_display,
    quarter_for_end,
    shift_quarters,
)
from finn.normalise.quote import THRESHOLD, quote_match
from finn.normalise.units import UnknownUnit, detect_unit, to_crore
from finn.parse.checks import arithmetic_ok, qoq, yoy
from finn.parse.results import results_facts

log = logging.getLogger(__name__)

STEP = "results_pdf"
PDF_PAGES = 8
CARD_METRICS = ("revenue", "ebitda", "pbt", "pat")
LENDER_CARD_METRICS = ("revenue", "nii", "ppop", "pbt", "pat")

# "quarter ended 30th September 2026", "for the period ended September 30, 2026", ...
_ENDED = re.compile(
    r"(?:quarter|period|half[- ]year|nine months|year)(?: and \w+(?: \w+)?)? ended(?: on)?[:\s]+"
    r"(\d{1,2}(?:st|nd|rd|th)?[\s.\-/]+(?:[A-Za-z]{3,9}|\d{1,2})[\s.,\-/]+\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})",
    re.IGNORECASE,
)


class CardPending(RuntimeError):
    """The card could not be built now (a source or model was unavailable); try again later."""


# -- the period of the filing ----------------------------------------------------------------


def period_end_from_text(text: str) -> date | None:
    """The quarter-end date a filing says its results are for, or None when it does not say."""
    for m in _ENDED.finditer(text or ""):
        try:
            end = parse_date_text(m.group(1))
            quarter_for_end(end)
        except PeriodError:
            continue
        return end
    return None


def period_end_from_filing_date(filed: date) -> date:
    """The latest quarter end on or before the filing date. Used only when the text names none."""
    for back in range(0, 400):
        d = date.fromordinal(filed.toordinal() - back)
        try:
            return quarter_for_end(d).end
        except PeriodError:
            continue
    raise PeriodError(f"no quarter end before {filed}")


def is_xbrl_fact(fact: Fact) -> bool:
    return "nsearchives.nseindia.com" in fact.source_url and "/xbrl/" in fact.source_url.lower()


# -- result containers -----------------------------------------------------------------------


@dataclass
class ResultsCard:
    fragment: dict  # merged into filings.card_json
    facts: list[Fact] = field(default_factory=list)  # for the facts table; empty unless state ok
    figures: dict[str, str] = field(default_factory=dict)  # code-written, for the why-line check

    @property
    def state(self) -> str:
        return self.fragment["state"]


@dataclass
class Deps:
    results_facts: Callable[..., list[Fact]] = results_facts
    pdf: Callable = pdf_excerpt
    router: Router | None = None
    duplicate_of: Callable[[str, str, str | None], str | None] = lambda *_: None


# -- the PDF fallback ------------------------------------------------------------------------


class PdfRow(BaseModel):
    metric: str
    printed: str  # the number as printed: no conversion, no arithmetic
    quote: str


class PdfTable(BaseModel):
    basis: str | None = None
    unit_header: str | None = None
    rows: list[PdfRow] = []


PDF_METRICS = {
    "industry": ("revenue", "other_income", "finance_costs", "d_and_a", "pbt", "tax", "pat",
                 "exceptional_items"),
    "lender": ("revenue", "interest_earned", "interest_expended", "ppop", "pbt", "tax", "pat"),
}  # fmt: skip


def _pdf_prompt(text: str, end: date, kind: str, nudge: str = "") -> str:
    names = ", ".join(PDF_METRICS[kind])
    quarter = f"{end.day} {end.strftime('%B %Y')}"
    return (
        "Read the financial results table in the text. Use only the provided text. Do not use "
        "outside knowledge. Copy numbers exactly as printed. Do not add, convert, round or "
        "compute anything.\n\n"
        f"Take the column for the three months (quarter) ended {quarter}. Prefer the consolidated "
        "table when both consolidated and standalone are given.\n\n"
        f"Rows wanted (metric names): {names}.\n"
        "revenue is total income or revenue from operations as the table calls it. For each row "
        "give the number printed in that column, with any minus sign or brackets, and a quote: the "
        "table row copied word for word from the text.\n\n"
        f"Text:\n{text[:40000]}\n\n"
        'Answer as JSON: {"basis": "consolidated" or "standalone", "unit_header": <the words '
        'that state the unit, copied from the text, e.g. "Rs. in lakhs">, "rows": [{"metric": '
        '<name>, "printed": <number as printed>, "quote": <row text>}]}. Leave out any row you '
        f"cannot find.{nudge}"
    )


_PRINTED = re.compile(r"^\(?\s*-?\s*[\d,]+(?:\.\d+)?\s*\)?$")


def parse_printed(printed: str) -> float | None:
    """'1,234.50' -> 1234.5, '(12.0)' and '-12.0' -> -12.0. Anything else: None."""
    text = printed.strip().replace("−", "-").replace("–", "-")
    if not _PRINTED.match(text):
        return None
    negative = text.startswith("(") or text.lstrip("(").lstrip().startswith("-")
    digits = re.sub(r"[^\d.]", "", text)
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def _number_in_quote(printed: str, quote: str) -> bool:
    core = re.sub(r"[^\d.,]", "", printed).strip(",")
    return bool(core) and core in quote


def facts_from_pdf(
    symbol: str, end: date, url: str, text: str, kind: str, router: Router
) -> tuple[list[Fact], list[str]]:
    """Facts for the quarter from a PDF table, and the reasons rows were dropped.

    A row survives only if its quote is in the text (quote_match >= 0.4), the printed number is
    inside the quote and parses, and the unit header is in the text and names one unit. One retry
    when anything is dropped; what still fails is dropped.
    """
    period = quarter_for_end(end).label
    problems: list[str] = []
    best: tuple[list[Fact], list[str]] = ([], ["no answer"])
    nudge = ""
    for _attempt in (1, 2):
        table = router.call(STEP, "reader", _pdf_prompt(text, end, kind, nudge), PdfTable).data
        facts, problems = _pdf_facts(symbol, period, url, text, kind, table)
        if len(facts) > len(best[0]) or not best[0]:
            best = (facts, problems)
        if not problems:
            break
        nudge = (
            "\n\nSome rows were rejected: each quote must be copied word for word from the text, "
            "the number must be inside its quote, and unit_header must be copied from the text."
        )
    return best


def _pdf_facts(symbol, period, url, text, kind, table: PdfTable) -> tuple[list[Fact], list[str]]:
    problems: list[str] = []
    basis = (table.basis or "").strip().lower()
    if basis not in ("consolidated", "standalone"):
        return [], ["basis not stated"]
    header = table.unit_header or ""
    if quote_match(header, text) < THRESHOLD:
        return [], ["unit header not in the text"]
    try:
        unit = detect_unit(header)
    except UnknownUnit as exc:
        return [], [f"unit: {exc}"]

    values: dict[str, tuple[float, str, float]] = {}  # metric -> (value in unit, quote, match)
    for row in table.rows:
        if row.metric not in PDF_METRICS[kind] or row.metric in values:
            continue
        match = quote_match(row.quote, text)
        value = parse_printed(row.printed)
        if match < THRESHOLD:
            problems.append(f"{row.metric}: quote_match {match:.2f}")
        elif value is None or not _number_in_quote(row.printed, row.quote):
            problems.append(f"{row.metric}: printed number not in its quote")
        else:
            values[row.metric] = (value, row.quote, match)

    def fact(metric, value, quote, match, derived=False, formula=None, exceptional=None) -> Fact:
        return Fact(
            ticker=symbol,
            metric=metric,
            value_cr=round(to_crore(value, unit), 6),
            unit_original=unit,
            period=period,
            period_kind="Q",
            basis=basis,
            derived=derived,
            formula=formula,
            source_url=url,
            quote=quote,
            quote_match=match,
            has_exceptional_items=exceptional,
        )

    exceptional_value = values.get("exceptional_items")
    flag = None if exceptional_value is None else exceptional_value[0] != 0
    out: list[Fact] = []
    for metric in PDF_METRICS[kind]:
        if metric in values and metric not in ("exceptional_items", "interest_earned",
                                               "interest_expended"):  # fmt: skip
            v, q, m = values[metric]
            out.append(fact(metric, v, q, m, exceptional=flag))

    if kind == "lender" and {"interest_earned", "interest_expended"} <= values.keys():
        (ie, qe, me), (ix, qx, mx) = values["interest_earned"], values["interest_expended"]
        nii = to_crore(ie - ix, unit)
        formula = (
            f"nii = interest_earned - interest_expended = {to_crore(ie, unit):,.2f} - "
            f"{to_crore(ix, unit):,.2f} = {nii:,.2f}"
        )
        out.append(fact("nii", ie - ix, f"{qe} | {qx}", min(me, mx), True, formula, flag))
    if kind == "industry" and {"pbt", "finance_costs", "d_and_a", "other_income"} <= values.keys():
        if exceptional_value is not None and exceptional_value[0] != 0:
            # the table does not say whether the line is a gain or a loss; EBITDA is left out
            problems.append("ebitda: nonzero exceptional items with no sign to read")
        else:
            parts = [values[m] for m in ("pbt", "finance_costs", "d_and_a", "other_income")]
            raw = parts[0][0] + parts[1][0] + parts[2][0] - parts[3][0]  # in the table's unit
            pbt, fin, dna, oth = (to_crore(p[0], unit) for p in parts)
            formula = (
                "ebitda = pbt + exceptional_items_added_back + finance_costs + d_and_a - "
                f"other_income = {pbt:,.2f} + 0.00 + {fin:,.2f} + {dna:,.2f} - {oth:,.2f} = "
                f"{to_crore(raw, unit):,.2f}"
            )
            quote = " | ".join(p[1] for p in parts)
            out.append(fact("ebitda", raw, quote, min(p[2] for p in parts), True, formula, flag))
    return out, problems


# -- the card --------------------------------------------------------------------------------


def _by_metric(facts: list[Fact], period: str) -> dict[str, Fact]:
    return {f.metric: f for f in facts if f.period == period and f.period_kind == "Q"}


def _money(v: float) -> str:
    return f"Rs {v:,.0f} cr"


def _pct(v: float) -> str:
    return f"{v:+.1f}%"


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def build_results_card(row: dict, deps: Deps | None = None) -> ResultsCard:
    """The results card for one filings row (symbol, subject, attachment_url, filed_at)."""
    deps = deps or Deps()
    symbol = row["symbol"]
    subject = row.get("subject") or ""
    url = row.get("attachment_url")

    pdf_text = ""
    end = period_end_from_text(subject)
    period_source = "subject"
    if end is None:
        excerpt = deps.pdf(url, PDF_PAGES) if url else None
        pdf_text = excerpt.text if excerpt else ""
        end = period_end_from_text(pdf_text)
        period_source = "attachment"
    if end is None:
        filed = row.get("filed_at")
        if not filed:
            raise CardPending("the filing names no period and has no date")
        end = period_end_from_filing_date(date.fromisoformat(str(filed)[:10]))
        period_source = "filing_date"
    cur, prev, year_ago = (quarter_for_end(shift_quarters(end, n)).label for n in (0, -1, -4))
    base = {
        "card_type": "results",
        "symbol": symbol,
        "period": cur,
        "period_label": period_display(cur),
        "period_source": period_source,
    }

    other = deps.duplicate_of(symbol, cur, row.get("source_id"))
    if other:
        return ResultsCard({**base, "state": "duplicate", "duplicate_of": other})

    facts = deps.results_facts(symbol, period_end=end)
    current = _by_metric(facts, cur)
    source = "xbrl"
    if not current or not all(is_xbrl_fact(f) for f in current.values()):
        # No XBRL for this filing's quarter (NSE has not listed it): read the PDF table instead.
        source = "pdf"
        if not pdf_text and url:
            excerpt = deps.pdf(url, PDF_PAGES)
            pdf_text = excerpt.text if excerpt else ""
        if not pdf_text or not url:
            return _check_source(base, "pdf", [_check("source", False, "no XBRL and no PDF text")])
        lender = bool(
            {f.metric for f in facts if f.period in (prev, year_ago)} & {"nii", "ppop"}
        ) or bool(re.search(r"interest earned|net interest income", pdf_text, re.IGNORECASE))
        router = deps.router or default_router()
        pdf_facts, problems = facts_from_pdf(
            symbol, end, url, pdf_text, "lender" if lender else "industry", router
        )
        facts = [f for f in facts if f.period != cur] + pdf_facts
        current = _by_metric(facts, cur)
        if problems:
            log.info("%s %s: pdf rows dropped: %s", symbol, cur, problems)

    lender = bool({"nii", "ppop"} & current.keys())
    wanted = LENDER_CARD_METRICS if lender else CARD_METRICS
    basis = next(iter(current.values())).basis if current else None
    # comparatives must be on the same basis as the current quarter
    previous = _by_metric([f for f in facts if f.basis == basis], prev)
    last_year = _by_metric([f for f in facts if f.basis == basis], year_ago)

    checks: list[dict] = [_check("source", True, source)]
    missing = [m for m in wanted if m not in current or current[m].value_cr is None]
    checks.append(
        _check("figures_present", not missing, "all present" if not missing else
               "missing: " + ", ".join(missing))  # fmt: skip
    )
    weakest = min((current[m].quote_match for m in wanted if m in current), default=0.0)
    checks.append(
        _check("quote_match", weakest >= THRESHOLD, f"lowest {weakest:.2f}, minimum {THRESHOLD}")
    )
    pbt, tax, pat = (current[m].value_cr if m in current else None for m in ("pbt", "tax", "pat"))
    arithmetic = arithmetic_ok(pbt, tax, pat)
    checks.append(_check("arithmetic", arithmetic, "pat = pbt - tax within 1%"))
    same_basis = len({f.basis for f in current.values()}) == 1
    checks.append(_check("one_basis", same_basis, basis or "none"))

    if missing or weakest < THRESHOLD or not arithmetic or not same_basis:
        return _check_source(base, source, checks, basis=basis)

    lines = []
    for metric in wanted:
        fact = current[metric]
        before, then = previous.get(metric), last_year.get(metric)
        lines.append(
            {
                "metric": metric,
                "value_cr": round(fact.value_cr, 4),
                "qoq_pct": _round(qoq(fact.value_cr, before.value_cr if before else None)),
                "yoy_pct": _round(yoy(fact.value_cr, then.value_cr if then else None)),
                "qoq_base_period": prev if before else None,
                "yoy_base_period": year_ago if then else None,
                "derived": fact.derived,
                "formula": fact.formula,
                "has_exceptional_items": fact.has_exceptional_items,
                "quote_match": round(fact.quote_match, 4),
            }
        )
    for label, key in (("QoQ", "qoq_pct"), ("YoY", "yoy_pct")):
        found = sum(1 for ln in lines if ln[key] is not None)
        checks.append(
            _check(f"{label.lower()}_base", found == len(lines), f"{found} of {len(lines)} lines")
        )

    flags = [ln["has_exceptional_items"] for ln in lines]
    exceptional = None if all(f is None for f in flags) else any(f for f in flags if f)
    fragment = {
        **base,
        "state": "ok",
        "source": source,
        "kind": "lender" if lender else "industry",
        "basis": basis,
        "source_url": current[wanted[0]].source_url,
        "has_exceptional_items": exceptional,
        "lines": lines,
        "checks": checks,
    }
    figures = {}
    for ln in lines:
        text = _money(ln["value_cr"])
        for label, key in (("QoQ", "qoq_pct"), ("YoY", "yoy_pct")):
            if ln[key] is not None:
                text += f", {_pct(ln[key])} {label}"
        figures[ln["metric"]] = text
    keep = [f for f in facts if f.period in (cur, prev, year_ago) and f.basis == basis]
    fragment["fact_keys"] = ["|".join(map(str, f.key)) for f in keep if f.period == cur]
    return ResultsCard(fragment, keep, figures)


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _check_source(base: dict, source: str, checks: list[dict], basis: str | None = None):
    """No figures at all: only which checks failed, by name."""
    return ResultsCard(
        {**base, "state": "check_source", "source": source, "basis": basis, "checks": checks}
    )
