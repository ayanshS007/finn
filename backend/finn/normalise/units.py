"""Detect the unit of a table or XBRL value and convert to Rs crore.

1 crore = 100 lakh = 10 million = 10,000,000 rupees. An unknown or ambiguous unit raises
UnknownUnit; nothing here ever guesses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# how many of the unit make one crore; converting divides, which is exact for these
UNITS_PER_CRORE = {"rupee": 10_000_000, "lakh": 100, "million": 10, "crore": 1}
UNITS = tuple(UNITS_PER_CRORE)


class UnknownUnit(ValueError):
    """The unit is missing, ambiguous or not one we convert."""


_SCALE_WORDS = {
    "lakh": re.compile(r"\b(?:lakhs?|lacs?)\b"),
    "crore": re.compile(r"\b(?:crores?|crs?)\b"),
    "million": re.compile(r"\b(?:millions?|mn|mln)\b"),
}
# Units we recognise but refuse to convert (stating them is better than silently mis-scaling).
_UNSUPPORTED = re.compile(r"\b(?:thousands?|billions?|bn|trillions?)\b|'000|\b000s\b|\bin 000\b")
_RUPEE = re.compile(
    r"\bin\s+(?:rs\.?|inr|rupees?)(?!\w)|\b(?:amounts?|figures?)\s+in\s+(?:rs|inr)\b"
)


def detect_unit(text: str) -> str:
    """Unit named in a header or caption: 'Rs. in lakhs' -> 'lakh', '(in Rs. Cr.)' -> 'crore'."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    if _UNSUPPORTED.search(low):
        raise UnknownUnit(f"unsupported unit in {text!r}")
    found = {unit for unit, rx in _SCALE_WORDS.items() if rx.search(low)}
    if len(found) > 1:
        raise UnknownUnit(f"ambiguous unit in {text!r}: {sorted(found)}")
    if found:
        return found.pop()
    if _RUPEE.search(low):
        return "rupee"
    raise UnknownUnit(f"no unit found in {text!r}")


def detect_unit_xbrl(measures: Iterable[str], decimals: str | int | None = None) -> str:
    """XBRL numeric facts: a unitRef whose measure is iso4217:INR holds plain rupees.

    `decimals` (e.g. -7 = rounded to the crore, -5 = to the lakh) only states precision, so it
    never changes the scale; it is validated and otherwise ignored. A non-INR unit raises.
    """
    measures = [m for m in measures if m]
    if len(measures) != 1 or measures[0].split(":")[-1].upper() != "INR":
        raise UnknownUnit(f"XBRL unit {list(measures)} is not a single INR measure")
    if decimals not in (None, "INF"):
        try:
            int(decimals)
        except (TypeError, ValueError) as exc:
            raise UnknownUnit(f"XBRL decimals {decimals!r} is not an integer") from exc
    return "rupee"


def to_crore(value: float, unit: str) -> float:
    if unit not in UNITS_PER_CRORE:
        raise UnknownUnit(f"unknown unit {unit!r}")
    return value / UNITS_PER_CRORE[unit]


def convert(value: float, header: str) -> tuple[float, str]:
    """(value in crore, unit_original) for a figure printed under `header`."""
    unit = detect_unit(header)
    return to_crore(value, unit), unit
