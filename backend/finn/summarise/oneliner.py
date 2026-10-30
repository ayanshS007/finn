"""One-liners (score 30-59) and why-ranked lines (score 60+), written by the small model.

The model sees the subject, the category and any figures the code computed, and nothing else. It
never produces a figure: the number check (CLAUDE.md rule 10) requires every number in the text to
appear in its inputs, or the text is dropped (one retry first, then None; the card keeps the
code-written why_template from finn.score.materiality).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from finn.llm.router import Router, default_router

log = logging.getLogger(__name__)

ONE_LINER = "oneliner"
WHY_LINE = "why_line"
MAX_WORDS = 30

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# spelled-out numbers are numbers too; "one" is left out (it is mostly a pronoun)
_NUMBER_WORDS = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|half|halved|doubl(?:e|es|ed|ing)|tripl(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)


def _value(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def numbers_in(text: str) -> set[Decimal]:
    """Every number in the text as a value, so 7.70 equals 7.7 and 1,234 equals 1234."""
    return {v for t in _NUMBER.findall(text or "") if (v := _value(t)) is not None}


def unsupported_numbers(text: str, *inputs: str) -> list[str]:
    """Numbers (digits or spelled out) in `text` that appear in none of the inputs."""
    source = "\n".join(i for i in inputs if i)
    allowed = numbers_in(source)
    bad = [t for t in _NUMBER.findall(text) if (v := _value(t)) is not None and v not in allowed]
    have_words = {w.lower() for w in _NUMBER_WORDS.findall(source)}
    bad += [w for w in _NUMBER_WORDS.findall(text) if w.lower() not in have_words]
    return bad


def number_check(text: str, *inputs: str) -> bool:
    return not unsupported_numbers(text, *inputs)


class LineOut(BaseModel):
    line: str


@dataclass(frozen=True)
class Written:
    text: str | None  # None: dropped by the number check, or the model gave nothing usable
    dropped_numbers: tuple[str, ...] = ()


def _figures_block(figures: dict[str, str] | None) -> str:
    if not figures:
        return "(none)"
    return "\n".join(f"- {label}: {value}" for label, value in figures.items())


def one_liner_prompt(subject, category, tier, figures, nudge="") -> str:
    return (
        "Write one plain sentence of at most 25 words saying what this stock exchange filing "
        "is about. Use only the provided text. Do not use outside knowledge. Do not add any "
        "number that is not in the text below. Leave out phrases like 'has informed the "
        "Exchange'.\n\n"
        f"Category: {category}{' / ' + tier if tier else ''}\n"
        f"Subject: {subject}\n"
        f"Figures computed by code:\n{_figures_block(figures)}\n\n"
        f'Answer as JSON: {{"line": <sentence>}}.{nudge}'
    )


def why_line_prompt(subject, category, tier, parts, figures, nudge="") -> str:
    facts = (
        f"base weight {parts['weight']:g}; holding factor {parts['holding']:.2f}; "
        f"price/volume bonus {parts['bonus']:g}; score {parts['score']:.0f} "
        f"(recency factor {parts['recency']:.2f} only orders the list)"
    )
    return (
        "Write one plain sentence of at most 25 words saying why this stock exchange filing ranks "
        "as important. Name the kind of event and the reason it matters. Use only the provided "
        "text. Do not use outside knowledge. Do not add any number that is not in the text "
        "below.\n\n"
        f"Category: {category}{' / ' + tier if tier else ''}\n"
        f"Subject: {subject}\n"
        f"Ranking inputs: {facts}\n"
        f"Figures computed by code:\n{_figures_block(figures)}\n\n"
        f'Answer as JSON: {{"line": <sentence>}}.{nudge}'
    )


def _write(step, role, build, inputs: list[str], router: Router) -> Written:
    dropped: tuple[str, ...] = ()
    nudge = ""
    for _ in (1, 2):
        line = router.call(step, role, build(nudge), LineOut).data.line.strip()
        words = len(line.split())
        bad = unsupported_numbers(line, *inputs)
        if line and not bad and words <= MAX_WORDS:
            return Written(line)
        dropped = tuple(bad)
        log.info("%s rejected (numbers not in inputs: %s; %d words)", step, bad, words)
        nudge = (
            "\n\nYour last sentence was rejected: it must be under 25 words and contain no "
            "number that is not in the text above. Write it again without those numbers."
        )
    return Written(None, dropped)


def write_one_liner(
    subject: str,
    category: str,
    tier: str | None = None,
    figures: dict[str, str] | None = None,
    *,
    router: Router | None = None,
) -> Written:
    router = router or default_router()
    inputs = [subject, category, tier or "", *(figures or {}).keys(), *(figures or {}).values()]
    return _write(
        ONE_LINER,
        "small",
        lambda nudge: one_liner_prompt(subject, category, tier, figures, nudge),
        inputs,
        router,
    )


def write_why_line(
    subject: str,
    category: str,
    tier: str | None,
    parts: dict,
    figures: dict[str, str] | None = None,
    *,
    router: Router | None = None,
) -> Written:
    router = router or default_router()
    ranking = (
        f"{parts['weight']:g} {parts['holding']:.2f} {parts['recency']:.2f} "
        f"{parts['bonus']:g} {parts['score']:.0f}"
    )
    inputs = [subject, category, tier or "", ranking, *(figures or {}).values()]
    return _write(
        WHY_LINE,
        "small",
        lambda nudge: why_line_prompt(subject, category, tier, parts, figures, nudge),
        inputs,
        router,
    )
