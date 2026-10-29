"""quote_match: how much of a quote is really in its source.

The share of the quote's 4-word runs that occur in the source, after NFKC, lowercasing and
turning punctuation and whitespace runs into single spaces. CLAUDE.md rule 3: below 0.4 the
label or claim is retried once and then dropped.
"""

from __future__ import annotations

import re
import unicodedata

RUN = 4
THRESHOLD = 0.4
_NON_WORD = re.compile(r"[\W_]+")


def tokens(text: str) -> list[str]:
    return _NON_WORD.sub(" ", unicodedata.normalize("NFKC", text or "").lower()).split()


def _runs(words: list[str]) -> list[tuple[str, ...]]:
    return [tuple(words[i : i + RUN]) for i in range(len(words) - RUN + 1)]


def quote_match(quote: str, source: str) -> float:
    """0.0 to 1.0. A quote shorter than 4 words counts as one run, found only as a whole."""
    q = tokens(quote)
    if not q:
        return 0.0
    s = tokens(source)
    if len(q) < RUN:
        n = len(q)
        return 1.0 if any(s[i : i + n] == q for i in range(len(s) - n + 1)) else 0.0
    have = set(_runs(s))
    runs = _runs(q)
    return sum(r in have for r in runs) / len(runs)


def passes(match: float) -> bool:
    return match >= THRESHOLD
