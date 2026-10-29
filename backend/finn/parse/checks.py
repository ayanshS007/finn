"""Arithmetic checks and growth helpers. Every derived percentage in the app is computed here."""

from __future__ import annotations

TOLERANCE = 0.01  # 1 percent


def arithmetic_ok(
    pbt: float | None,
    tax: float | None,
    pat: float | None,
    exceptional: float | None = None,
    other: float | None = None,
    tolerance: float = TOLERANCE,
) -> bool:
    """Does pat = pbt + exceptional - tax + other hold within `tolerance` of pat?

    * pbt is profit before tax as filed. When the filing's PBT is before exceptional items,
      pass the exceptional items (gain positive, loss negative) as `exceptional`; leave it None
      when PBT already includes them.
    * `other` is whatever else sits between PBT less tax and the profit for the period (share of
      associates, discontinued operations), signed.
    A missing input returns False: the check cannot confirm the numbers.
    """
    if pbt is None or tax is None or pat is None:
        return False
    expected = pbt + (exceptional or 0.0) - tax + (other or 0.0)
    scale = abs(pat) if pat else max(abs(pbt), abs(expected), 1e-9)
    return abs(expected - pat) <= tolerance * scale


def pct_change(new: float | None, old: float | None) -> float | None:
    """Percentage change from old to new; None when old is 0 or either is missing.

    The base is |old|, so a loss narrowing from -100 to -50 reads +50 (an improvement).
    """
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def qoq(current: float | None, previous_quarter: float | None) -> float | None:
    return pct_change(current, previous_quarter)


def yoy(current: float | None, same_quarter_last_year: float | None) -> float | None:
    return pct_change(current, same_quarter_last_year)


def margin_pct(numerator: float | None, revenue: float | None) -> float | None:
    if numerator is None or revenue is None or revenue == 0:
        return None
    return numerator / revenue * 100.0


def within(a: float | None, b: float | None, tolerance: float = TOLERANCE) -> bool:
    """Do two figures agree within `tolerance` of the larger magnitude?"""
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance * max(abs(a), abs(b), 1e-9)
