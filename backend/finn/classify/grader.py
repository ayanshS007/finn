"""A separate small-model call that scores an LLM label 1-5.

It sees only the quote and the chosen category's definition (and the tier's description when a tier
was chosen), nothing else: not the subject, not the model that labelled, not the first answer. A
score of 1-3 sends the filing to the routine drawer (CLAUDE.md rule 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from finn.classify.rules import Categories, default_categories
from finn.llm.router import LLMOutputError, Router, default_router

log = logging.getLogger(__name__)

STEP = "grade"
PASS_SCORE = 4


class GradeOut(BaseModel):
    score: int = Field(ge=1, le=5)


@dataclass(frozen=True)
class Grade:
    score: int | None  # None when the grader itself failed
    passed: bool


def build_prompt(categories: Categories, category: str, tier: str | None, quote: str) -> str:
    definition = categories.definitions[category]
    if tier:
        definition += f" Tier {tier}: {categories.tiers[category][tier]}"
    return (
        "You grade labels on stock exchange filings. Use only the provided text. "
        "Do not use outside knowledge.\n\n"
        f"Category: {category}\nDefinition: {definition}\n\n"
        f'Quote from the filing: "{quote}"\n\n'
        "How well does the quote alone show that the filing belongs to this category? "
        "5 = clearly and specifically; 4 = fairly clearly; 3 = could be; 2 = weak; "
        '1 = not at all. Answer as JSON: {"score": <1-5>}.'
    )


def grade_label(
    category: str,
    tier: str | None,
    quote: str,
    *,
    router: Router | None = None,
    categories: Categories | None = None,
) -> Grade:
    """Score a label. A grader that cannot answer yields score None, which is not a pass."""
    router = router or default_router()
    categories = categories or default_categories()
    prompt = build_prompt(categories, category, tier, quote)
    try:
        score = router.call(STEP, "small", prompt, GradeOut).data.score
    except LLMOutputError:
        log.warning("grader returned no usable score; the label stays unverified")
        return Grade(None, False)
    return Grade(score, score >= PASS_SCORE)
