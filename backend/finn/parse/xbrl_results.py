"""Parse the SEBI integrated-filing XBRL that NSE links from /api/integrated-filing-results.

One file holds one basis (consolidated or standalone), the quarter just reported, the
year-to-date period (H1, 9M or FY) and the balance sheet. The previous quarter and the same
quarter last year live in the earlier filings, which results_facts fetches. Three taxonomies:

* Ind AS (RevenueFromOperations, FinanceCosts, ...): most companies
* banking (InterestEarned, InterestExpended, OperatingProfitBeforeProvisionAndContingencies)
* NBFC (Ind AS with InterestEarned and no InterestExpended)

Values are plain rupees (unitRef INR) and go through finn.normalise.units.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from lxml import etree

from finn.db.models import Fact
from finn.normalise.periods import Period, PeriodError, period_from_range
from finn.normalise.quote import quote_match
from finn.normalise.units import UnknownUnit, detect_unit_xbrl, to_crore
from finn.sources.http import HttpClient, SourceBlocked, SourceError, shared_client

log = logging.getLogger(__name__)

XBRLI = "http://www.xbrl.org/2003/instance"
TTL_XBRL = 24 * 3600

# metric -> XBRL concepts tried in order
INDAS_METRICS = {
    "revenue": ("RevenueFromOperations",),
    "other_income": ("OtherIncome",),
    "finance_costs": ("FinanceCosts",),
    "d_and_a": (
        "DepreciationDepletionAndAmortisationExpense",
        "DepreciationAndAmortisationExpense",
    ),
    "pbt": ("ProfitBeforeTax",),
    "tax": ("TaxExpense",),
    "pat": ("ProfitLossForPeriod",),
}
BANK_METRICS = {
    "revenue": ("Income",),  # interest earned + other income
    "other_income": ("OtherIncome",),
    "pbt": ("ProfitLossFromOrdinaryActivitiesBeforeTax",),
    "tax": ("TaxExpense",),
    "pat": ("ProfitLossForThePeriod",),
    "ppop": ("OperatingProfitBeforeProvisionAndContingencies",),
}


class XbrlError(SourceError):
    """The file is not the XBRL we expect. Callers report it; they never fill the gap."""


@dataclass(frozen=True)
class XbrlFact:
    concept: str
    value: float  # as filed, in the unit of `unit` (rupees for INR facts)
    unit: str  # unit_original: always "rupee" for INR facts
    decimals: str | None
    raw: str  # the element as it appears in the file: the verbatim quote


@dataclass
class XbrlFiling:
    source_url: str
    text: str  # whole document, the source for quote_match
    basis: str  # consolidated | standalone
    kind: str  # indas | bank | nbfc
    facts: dict[tuple[str, Period], XbrlFact] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def periods(self) -> list[Period]:
        return sorted({p for _, p in self.facts}, key=lambda p: (p.end, p.kind))

    def get(self, concept: str, period: Period) -> XbrlFact | None:
        return self.facts.get((concept, period))

    def to_facts(self, symbol: str) -> list[Fact]:
        """Facts for every period (quarter and year-to-date) the filing carries."""
        out: list[Fact] = []
        for period in self.periods:
            out += self._period_facts(symbol, period)
        return out

    # -- one period ---------------------------------------------------------------------

    def _fact(self, symbol, metric, period, xf: XbrlFact) -> Fact:
        return Fact(
            ticker=symbol,
            metric=metric,
            value_cr=to_crore(xf.value, xf.unit),
            unit_original=xf.unit,
            period=period.label,
            period_kind=period.kind,
            basis=self.basis,
            source_url=self.source_url,
            quote=xf.raw,
            quote_match=quote_match(xf.raw, self.text),
        )

    def _derived(self, symbol, metric, period, parts: list[XbrlFact], signs, rule) -> Fact:
        crores = [to_crore(p.value, p.unit) for p in parts]
        value = round(sum(s * c for s, c in zip(signs, crores, strict=True)), 6)
        terms = " ".join(
            f"{'-' if s < 0 else '+'} {c:,.2f}" for s, c in zip(signs, crores, strict=True)
        ).lstrip("+ ")
        quote = " | ".join(p.raw for p in parts)
        return Fact(
            ticker=symbol,
            metric=metric,
            value_cr=value,
            unit_original=parts[0].unit,
            period=period.label,
            period_kind=period.kind,
            basis=self.basis,
            derived=True,
            formula=f"{rule} = {terms} = {value:,.2f}",
            source_url=self.source_url,
            quote=quote,
            quote_match=min(quote_match(p.raw, self.text) for p in parts),
        )

    def _period_facts(self, symbol: str, period: Period) -> list[Fact]:
        table = BANK_METRICS if self.kind == "bank" else INDAS_METRICS
        facts: list[Fact] = []
        for metric, concepts in table.items():
            xf = next((f for c in concepts if (f := self.get(c, period))), None)
            if xf is not None:
                facts.append(self._fact(symbol, metric, period, xf))

        if self.kind == "bank":
            earned, spent = self.get("InterestEarned", period), self.get("InterestExpended", period)
            if earned and spent:
                facts.append(
                    self._derived(
                        symbol,
                        "nii",
                        period,
                        [earned, spent],
                        [1, -1],
                        "nii = interest_earned - interest_expended",
                    )
                )
        elif self.kind == "nbfc":
            earned, costs = self.get("InterestEarned", period), self.get("FinanceCosts", period)
            if earned and costs:
                facts.append(
                    self._derived(
                        symbol,
                        "nii",
                        period,
                        [earned, costs],
                        [1, -1],
                        "nii = interest_earned - finance_costs",
                    )
                )
            before = self.get("ProfitBeforeExceptionalItemsAndTax", period)
            impair = self.get("ImpairmentOnFinancialInstruments", period)
            if before and impair:
                facts.append(
                    self._derived(
                        symbol,
                        "ppop",
                        period,
                        [before, impair],
                        [1, 1],
                        "ppop = pbt_before_exceptional + impairment",
                    )
                )
        return facts


def _decimal(text: str, concept: str) -> float:
    try:
        return float(Decimal(text.strip()))
    except InvalidOperation as exc:
        raise XbrlError(f"{concept}: {text!r} is not a number") from exc


def _raw_quote(el) -> str:
    """The element's opening tag and value as written in the file, minus the angle brackets."""
    prefix = el.prefix
    name = f"{prefix}:{etree.QName(el).localname}" if prefix else etree.QName(el).localname
    attrs = " ".join(f'{k}="{v}"' for k, v in el.attrib.items())
    return f"{name} {attrs}>{(el.text or '').strip()}"


def parse_xbrl(content: bytes | str, source_url: str, basis: str | None = None) -> XbrlFiling:
    """Parse one integrated-filing XBRL document. `basis` (from NSE's row) is cross-checked."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise XbrlError(f"{source_url}: not XML ({exc})") from exc
    if etree.QName(root).localname != "xbrl" or etree.QName(root).namespace != XBRLI:
        raise XbrlError(f"{source_url}: root element is not xbrli:xbrl")

    contexts: dict[str, tuple[date, date] | None] = {}  # id -> duration of a plain context
    units: dict[str, list[str]] = {}
    for el in root:
        if not isinstance(el.tag, str):
            continue
        q = etree.QName(el)
        if q.namespace != XBRLI:
            continue
        if q.localname == "context":
            period = el.find(f"{{{XBRLI}}}period")
            start, end = (period.find(f"{{{XBRLI}}}{t}") for t in ("startDate", "endDate"))
            dimensional = el.find(f"{{{XBRLI}}}scenario") is not None or (
                el.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}segment") is not None
            )
            if dimensional or start is None or end is None:
                contexts[el.get("id")] = None  # dimensional or an instant: not a headline figure
            else:
                contexts[el.get("id")] = (
                    date.fromisoformat(start.text.strip()),
                    date.fromisoformat(end.text.strip()),
                )
        elif q.localname == "unit":
            units[el.get("id")] = [m.text.strip() for m in el.iter(f"{{{XBRLI}}}measure")]

    filing = XbrlFiling(source_url, data.decode("utf-8", errors="replace"), "", "indas")
    period_cache: dict[str, Period | None] = {}
    for el in root:
        if not isinstance(el.tag, str) or etree.QName(el).namespace == XBRLI:
            continue
        ctx_id = el.get("contextRef")
        if ctx_id is None or ctx_id not in contexts or contexts[ctx_id] is None:
            continue
        concept = etree.QName(el).localname
        text = (el.text or "").strip()
        if el.get("unitRef") is None:
            filing.texts.setdefault(concept, text)
            continue
        if ctx_id not in period_cache:
            try:
                period_cache[ctx_id] = period_from_range(*contexts[ctx_id])
            except PeriodError as exc:
                filing.skipped.append(f"context {ctx_id}: {exc}")
                period_cache[ctx_id] = None
        period = period_cache[ctx_id]
        if period is None or el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
            continue
        measures = units.get(el.get("unitRef"), [])
        if len(measures) != 1:  # per-share, ratios: not money, and not ours
            continue
        try:
            unit = detect_unit_xbrl(measures, el.get("decimals"))
        except UnknownUnit:
            continue  # a non-INR money fact would be a problem only if we map it; none are
        fact = XbrlFact(concept, _decimal(text, concept), unit, el.get("decimals"), _raw_quote(el))
        old = filing.facts.get((concept, period))
        if old is not None and old.value != fact.value:
            raise XbrlError(f"{source_url}: {concept} {period} has two different values")
        filing.facts[(concept, period)] = fact

    nature = filing.texts.get("NatureOfReportStandaloneConsolidated", "").strip().lower()
    if nature not in ("consolidated", "standalone"):
        raise XbrlError(f"{source_url}: NatureOfReportStandaloneConsolidated is {nature!r}")
    if basis and basis != nature:
        raise XbrlError(f"{source_url}: NSE says {basis} but the file says {nature}")
    filing.basis = nature

    concepts = {c for c, _ in filing.facts}
    if "InterestExpended" in concepts:
        filing.kind = "bank"
    elif "InterestEarned" in concepts:
        filing.kind = "nbfc"
    if not any(
        c in concepts
        for table in (INDAS_METRICS, BANK_METRICS)
        for cs in table.values()
        for c in cs
    ):
        raise XbrlError(f"{source_url}: none of the expected result concepts are present")
    return filing


def fetch_xbrl(url: str, http: HttpClient | None = None, ttl: float = TTL_XBRL) -> bytes:
    """Download an XBRL file from nsearchives. A block page raises SourceBlocked."""
    http = http or shared_client()
    resp = http.get(url, ttl=ttl, headers={"Referer": "https://www.nseindia.com/"})
    head = resp.content[:200].lstrip().lower()
    if not (head.startswith(b"<?xml") or head.startswith(b"<xbrli:") or head.startswith(b"<xbrl")):
        raise SourceBlocked(f"{url}: expected XBRL, got {resp.content[:60]!r}", resp.status)
    return resp.content


def parse_results(
    content: bytes | str, source_url: str, symbol: str, basis: str | None = None
) -> list[Fact]:
    """Convenience: Facts for every period in one filing."""
    return parse_xbrl(content, source_url, basis).to_facts(symbol)
