"""Indian fiscal periods. FY27 = April 2026 to March 2027; FY27Q2 = July to September 2026.

Labels: FY27Q1..Q4, FY27H1, FY27H2, FY27-9M, FY27. Anything that does not map cleanly to one
of these raises PeriodError; nothing here guesses.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from finn.db.models import Fact


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    fy: int  # calendar year in which the fiscal year ends: FY27 -> 2027
    kind: str  # Q | H1 | H2 | 9M | FY
    quarter: int | None = None  # 1-4 when kind == "Q"

    @property
    def label(self) -> str:
        yy = f"FY{self.fy % 100:02d}"
        return {
            "Q": f"{yy}Q{self.quarter}",
            "H1": f"{yy}H1",
            "H2": f"{yy}H2",
            "9M": f"{yy}-9M",
            "FY": yy,
        }[self.kind]

    @property
    def end(self) -> date:
        """Last day of the period."""
        if self.kind == "Q":
            return _quarter_end(self.fy, self.quarter)
        return {"H1": _quarter_end(self.fy, 2), "9M": _quarter_end(self.fy, 3)}.get(
            self.kind, _quarter_end(self.fy, 4)
        )

    def __str__(self) -> str:
        return self.label


def fiscal_year(d: date) -> int:
    return d.year + 1 if d.month >= 4 else d.year


def quarter_of(d: date) -> int:
    return {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]


def _quarter_end(fy: int, q: int) -> date:
    year, month = {1: (fy - 1, 6), 2: (fy - 1, 9), 3: (fy - 1, 12), 4: (fy, 3)}[q]
    return date(year, month, calendar.monthrange(year, month)[1])


def _is_quarter_end(d: date) -> bool:
    return d.month in (3, 6, 9, 12) and d.day == calendar.monthrange(d.year, d.month)[1]


def quarter_for_end(d: date) -> Period:
    """The fiscal quarter ending on date d; d must be a quarter end (30 Jun, 30 Sep, ...)."""
    if not _is_quarter_end(d):
        raise PeriodError(f"{d.isoformat()} is not the last day of a fiscal quarter")
    return Period(fiscal_year(d), "Q", quarter_of(d))


def shift_quarters(d: date, quarters: int) -> date:
    """Quarter-end date `quarters` quarters from quarter-end d (negative = earlier)."""
    quarter_for_end(d)
    months = d.year * 12 + (d.month - 1) + 3 * quarters
    year, month = divmod(months, 12)
    return date(year, month + 1, calendar.monthrange(year, month + 1)[1])


def period_from_range(start: date, end: date) -> Period:
    """Map an XBRL context duration or a printed start/end pair to a period."""
    if not _is_quarter_end(end):
        raise PeriodError(f"{start}..{end}: end is not a fiscal quarter end")
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if start.day != 1 or months not in (3, 6, 9, 12):
        raise PeriodError(f"{start}..{end}: not a whole 3, 6, 9 or 12 month span")
    fy, q = fiscal_year(end), quarter_of(end)
    if months == 3:
        return Period(fy, "Q", q)
    if start.month != 4 and not (months == 6 and (start.month, end.month) == (10, 3)):
        raise PeriodError(f"{start}..{end}: does not start at the fiscal year start")
    if months == 6:
        return Period(fy, "H1" if q == 2 else "H2")
    if months == 9 and q == 3:
        return Period(fy, "9M")
    if months == 12 and q == 4:
        return Period(fy, "FY")
    raise PeriodError(f"{start}..{end}: no fiscal period has this shape")


# -- parsing printed labels -------------------------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MONTHS["sept"] = 9
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

_FY_YEAR = r"(?:20)?(\d{2})(?:\s*[-/]\s*(?:20)?(\d{2}))?"
_SUB = r"(q[1-4]|[1-4]q|h[12]|[12]h|9m|nine\s+months?)"
_FY_FIRST = re.compile(rf"fy\s*{_FY_YEAR}(?:\s*[-_ ]?\s*{_SUB})?")
_SUB_FIRST = re.compile(rf"{_SUB}\s*[-_ ]?\s*(?:fy\s*)?{_FY_YEAR}")
_DURATION = re.compile(
    r"\b(?:(quarter|qtr)|(half[\s-]*year|half|six\s+months)|(nine\s+months)|"
    r"(year|twelve\s+months|annual))\b(?:\s+period)?\s+ended\b"
)
_DATE_NUM = re.compile(r"\b(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[-/. ,]*({_MONTH_RE})[-/. ,]*(\d{{4}})\b")
_DATE_MDY = re.compile(rf"\b({_MONTH_RE})[-/. ]+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b")
_MONTH_YEAR = re.compile(rf"\b({_MONTH_RE})[-/. ,]*(\d{{4}})\b")


def _fy_from_tokens(a: str, b: str | None, *, source: str) -> int:
    """'27' -> 2027, '26','27' (FY2026-27) -> 2027."""
    if b is not None:
        first, second = int(a), int(b)
        if second != (first + 1) % 100:
            raise PeriodError(f"{source!r}: {a}-{b} is not consecutive fiscal years")
        return 2000 + second
    return 2000 + int(a)


def _sub_kind(sub: str, fy: int) -> Period:
    sub = re.sub(r"\s+", "", sub)
    if sub.startswith("q") or sub.endswith("q"):
        return Period(fy, "Q", int(re.search(r"\d", sub).group()))
    if sub.startswith("h") or sub.endswith("h"):
        return Period(fy, f"H{re.search(r'[12]', sub).group()}")
    return Period(fy, "9M")


def _find_date(text: str) -> date | None:
    """The one date in the text; two different dates raise (a header naming both years)."""
    found: set[date] = set()
    try:
        for m in _DATE_ISO.finditer(text):
            found.add(date(int(m[1]), int(m[2]), int(m[3])))
        for m in _DATE_NUM.finditer(text):
            found.add(date(int(m[3]), int(m[2]), int(m[1])))
        for m in _DATE_DMY.finditer(text):
            found.add(date(int(m[3]), _MONTHS[m[2]], int(m[1])))
        for m in _DATE_MDY.finditer(text):
            found.add(date(int(m[3]), _MONTHS[m[1]], int(m[2])))
        if not found:
            for m in _MONTH_YEAR.finditer(text):
                year, month = int(m[2]), _MONTHS[m[1]]
                found.add(date(year, month, calendar.monthrange(year, month)[1]))
    except ValueError as exc:
        raise PeriodError(f"invalid date in {text!r}: {exc}") from exc
    if len(found) > 1:
        raise PeriodError(f"{text!r} names more than one date: {sorted(found)}")
    return found.pop() if found else None


def parse_period(text: str) -> Period:
    """'Q2 FY27', 'FY27Q2', 'Quarter ended 30-Sep-2026', 'Sep 2026', 'Year ended 31 March 2027'."""
    low = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not low:
        raise PeriodError("empty period")

    if m := _SUB_FIRST.fullmatch(low):
        if "fy" not in low and m[3] is None:  # "Q2 2026": calendar or fiscal year? do not guess
            raise PeriodError(f"{text!r}: say FY27 or 2026-27, not a bare year")
        return _sub_kind(m[1], _fy_from_tokens(m[2], m[3], source=text))
    if m := _FY_FIRST.fullmatch(low):
        fy = _fy_from_tokens(m[1], m[2], source=text)
        return _sub_kind(m[3], fy) if m[3] else Period(fy, "FY")
    if re.search(r"\bfy\s*\d", low) or re.search(r"\b(?:q|h)\d\b", low):
        raise PeriodError(f"cannot read a period from {text!r}")

    end = _find_date(low)
    if end is None:
        raise PeriodError(f"cannot read a period from {text!r}")
    duration = _DURATION.search(low)
    if duration is None:
        return quarter_for_end(end)  # a bare date or "Sep 2026" is the quarter ending then
    if not _is_quarter_end(end):
        raise PeriodError(f"{text!r}: {end.isoformat()} is not a fiscal quarter end")
    fy, q = fiscal_year(end), quarter_of(end)
    quarter, half, nine, year = duration.groups()
    if quarter:
        return Period(fy, "Q", q)
    if half and q in (2, 4):
        return Period(fy, "H1" if q == 2 else "H2")
    if nine and q == 3:
        return Period(fy, "9M")
    if year and q == 4:
        return Period(fy, "FY")
    raise PeriodError(f"{text!r}: duration does not fit a period ending {end.isoformat()}")


def period_label(text: str) -> str:
    return parse_period(text).label


_MONTH_NAME = {i: calendar.month_abbr[i] for i in range(1, 13)}


def period_display(period: Period | str) -> str:
    """How a card names a period: 'FY27 Q2 (Jul-Sep 2026)'. H1, 9M and FY read the same way."""
    p = parse_period(period) if isinstance(period, str) else period
    end = p.end
    months = {"Q": 3, "H1": 6, "H2": 6, "9M": 9, "FY": 12}[p.kind]
    start_month = (end.month - months) % 12 + 1
    start_year = end.year if start_month <= end.month else end.year - 1
    head = f"FY{p.fy % 100:02d}" + (f" Q{p.quarter}" if p.kind == "Q" else f" {p.kind}")
    if p.kind == "FY":
        head = f"FY{p.fy % 100:02d}"
    span = f"{_MONTH_NAME[start_month]}{'' if start_year == end.year else f' {start_year}'}"
    return f"{head} ({span}-{_MONTH_NAME[end.month]} {end.year})"


# -- derived periods ------------------------------------------------------------------------


def _num(x: float) -> str:
    return f"{x:,.2f}"


def _derive(whole: Fact, part: Fact, *, kind: str, target: str, rule: str) -> Fact:
    for f in (whole, part):
        if f.value_cr is None:
            raise PeriodError(f"cannot derive {target}: {f.period} {f.metric} has no value")
    if (whole.ticker, whole.metric, whole.segment, whole.basis) != (
        part.ticker,
        part.metric,
        part.segment,
        part.basis,
    ):
        raise PeriodError(f"cannot derive {target}: the two facts are not like for like")
    if whole.unit_original != part.unit_original:
        raise PeriodError(f"cannot derive {target}: units differ")
    fy_label = whole.period
    if not part.period.startswith(fy_label):
        raise PeriodError(f"cannot derive {target}: {part.period} is not within {fy_label}")
    value = round(whole.value_cr - part.value_cr, 6)
    return Fact(
        ticker=whole.ticker,
        metric=whole.metric,
        segment=whole.segment,
        value_cr=value,
        unit_original=whole.unit_original,
        period=fy_label + target,
        period_kind=kind,
        basis=whole.basis,
        derived=True,
        formula=(
            f"{rule}: {whole.metric} {whole.period} {_num(whole.value_cr)} - "
            f"{part.period} {_num(part.value_cr)} = {_num(value)}"
        ),
        source_url=f"{whole.source_url} | {part.source_url}",
        page=None,
        quote=f"{whole.quote} | {part.quote}",
        quote_match=min(whole.quote_match, part.quote_match),
        has_exceptional_items=_either(whole.has_exceptional_items, part.has_exceptional_items),
    )


def _either(a: bool | None, b: bool | None) -> bool | None:
    """Conservative flag for a difference: it may hold exceptional items if either input did."""
    return None if a is None and b is None else bool(a) or bool(b)


def derive_q4(fy: Fact, nine_m: Fact) -> Fact:
    """Q4 = FY - 9M, derived, formula stored."""
    if fy.period_kind != "FY" or nine_m.period_kind != "9M":
        raise PeriodError("derive_q4 needs an FY fact and a 9M fact")
    return _derive(fy, nine_m, kind="Q", target="Q4", rule="Q4 = FY - 9M")


def derive_h2(fy: Fact, h1: Fact) -> Fact:
    """H2 = FY - H1, derived, formula stored."""
    if fy.period_kind != "FY" or h1.period_kind != "H1":
        raise PeriodError("derive_h2 needs an FY fact and an H1 fact")
    return _derive(fy, h1, kind="H2", target="H2", rule="H2 = FY - H1")


def parse_date_text(text: str) -> date:
    """Helper for callers holding an NSE date such as '30-SEP-2026'."""
    d = _find_date(text.lower())
    if d is None:
        raise PeriodError(f"no date in {text!r}")
    return d


__all__ = [
    "Period",
    "PeriodError",
    "derive_h2",
    "derive_q4",
    "fiscal_year",
    "parse_date_text",
    "parse_period",
    "period_display",
    "period_from_range",
    "period_label",
    "quarter_for_end",
    "quarter_of",
    "shift_quarters",
]
