"""LLM labelling for the filings the rules cannot settle.

Used only when the rules return nothing, return a category without its tier, or flag the filing
for review (every press_release catch-all). The small model sees the exchange category, the
subject and the first pages of the attachment, and answers {category, tier, quote}. The quote must
be copied from that text: quote_match >= 0.4 against it, else one retry, else the filing goes to
the drawer (CLAUDE.md rules 3 and 5). The model never sees or produces a figure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

from finn.classify.rules import Categories, RuleLabel, default_categories
from finn.llm.router import Router, default_router
from finn.normalise.quote import THRESHOLD, quote_match

log = logging.getLogger(__name__)

STEP = "label"
EXCERPT_CHARS = 5000  # keeps one call well under the free-tier token-per-minute limit
# Press releases about results are covered by the results filing itself; a second 90-point card
# for the same quarter would just be noise.
NOT_FOR_PRESS_RELEASE = {"results"}


class LabelOut(BaseModel):
    category: str
    tier: str | None = None
    quote: str


@dataclass(frozen=True)
class LlmLabel:
    ok: bool  # category valid and quote found in the text
    category: str | None
    tier: str | None
    quote: str | None
    quote_match: float
    reason: str | None = None  # why it is not ok
    attempts: int = 1


def _catalogue(categories: Categories, allowed: list[str]) -> str:
    lines = []
    for name in allowed:
        tiers = categories.tiers.get(name) or {}
        line = f"- {name}: {categories.definitions[name]}"
        if tiers:
            line += " Tier is required, one of: " + "; ".join(
                f"{t} ({d})" for t, d in tiers.items()
            )
        lines.append(line)
    return "\n".join(lines)


def build_prompt(
    categories: Categories,
    category_raw: str | None,
    subject: str,
    excerpt: str,
    hint: RuleLabel | None,
    allowed: list[str],
    nudge: str = "",
) -> str:
    known = ""
    if hint and hint.needs_tier:
        known = (
            f"\nThe category is already known to be {hint.category}; pick its tier (and keep the "
            "category unless the text clearly shows otherwise)."
        )
    return (
        "Label a stock exchange filing with one category. Use only the provided text. "
        "Do not use outside knowledge.\n\n"
        f"Categories:\n{_catalogue(categories, allowed)}\n\n"
        "If no category fits, answer unclassified. Answer unclassified rather than guess."
        f"{known}\n\n"
        f"Exchange category: {category_raw or '(none)'}\n"
        f"Subject: {subject or '(none)'}\n"
        f"Attachment text (first pages):\n{excerpt[:EXCERPT_CHARS] or '(not available)'}\n\n"
        'Answer as JSON: {"category": <name>, "tier": <tier name or null>, "quote": <text>}. '
        "The quote must be copied word for word from the subject or attachment text above, one "
        "or two sentences that show why you chose this label. Do not paraphrase it."
        f"{nudge}"
    )


def label_with_llm(
    category_raw: str | None,
    subject: str | None,
    excerpt: str | None,
    hint: RuleLabel | None = None,
    *,
    router: Router | None = None,
    categories: Categories | None = None,
) -> LlmLabel:
    """Label one filing. Raises finn.llm.router.LLMPending when both providers are rate limited."""
    router = router or default_router()
    categories = categories or default_categories()
    subject, excerpt = subject or "", excerpt or ""
    source = f"{subject}\n{excerpt}"
    allowed = [
        c for c in categories.names if not (hint and hint.llm_review and c in NOT_FOR_PRESS_RELEASE)
    ]

    last = LlmLabel(False, None, None, None, 0.0, "no answer")
    nudge = ""
    for attempt in (1, 2):
        prompt = build_prompt(categories, category_raw, subject, excerpt, hint, allowed, nudge)
        out: LabelOut = router.call(STEP, "small", prompt, LabelOut).data
        category = out.category.strip()
        tier = (out.tier or "").strip() or None
        if category not in allowed:
            last = LlmLabel(False, None, None, out.quote, 0.0, f"unknown category {category!r}")
        else:
            if not categories.tiers.get(category):
                tier = None  # a tier on a category without tiers is noise, not a failure
            match = quote_match(out.quote, source)
            if not categories.is_valid(category, tier):
                last = LlmLabel(False, category, tier, out.quote, match, f"bad tier {tier!r}")
            elif match < THRESHOLD:
                last = LlmLabel(False, category, tier, out.quote, match, f"quote_match {match:.2f}")
            else:
                return LlmLabel(True, category, tier, out.quote, match, None, attempt)
        log.info("label attempt %d failed: %s", attempt, last.reason)
        nudge = (
            "\n\nYour previous answer was rejected. Choose the category and tier exactly as "
            "listed, and copy the quote exactly from the text above."
        )
    return LlmLabel(False, last.category, last.tier, last.quote, last.quote_match, last.reason, 2)
