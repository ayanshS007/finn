"""Label a filing from its exchange category and subject with the regex rules in
config/categories.yaml. No model is involved: the answer is a category, a tier and the name of the
rule that matched, so every rule label can be traced.

A filing that no rule matches returns None; a rule can also say the category is certain but the tier
is not (`needs_tier`), or ask for the LLM to re-read it (`llm_review`). The job sends both to
finn.classify.llm_label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from finn.config import CONFIG_DIR

_MACRO = re.compile(r"<<(\w+)>>")


class RulesError(ValueError):
    """config/categories.yaml is malformed."""


@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    priority: int
    order: int
    tier: str | None
    llm_review: bool
    category_raw: re.Pattern | None
    subject: re.Pattern | None
    not_category_raw: re.Pattern | None

    def matches(self, category_raw: str, subject: str) -> bool:
        if self.category_raw and not self.category_raw.search(category_raw):
            return False
        if self.subject and not self.subject.search(subject):
            return False
        return not (self.not_category_raw and self.not_category_raw.search(category_raw))


@dataclass(frozen=True)
class RuleLabel:
    category: str
    tier: str | None
    rule: str
    llm_review: bool = False
    needs_tier: bool = False  # the category has tiers and the rule could not pick one


@dataclass(frozen=True)
class Categories:
    definitions: dict[str, str]
    tiers: dict[str, dict[str, str]]  # category -> tier -> description (empty: no tiers)
    rules: tuple[Rule, ...]
    # category -> pattern that disqualifies an LLM label whose quote matches it
    quote_vetoes: dict[str, re.Pattern] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return list(self.definitions)

    def is_valid(self, category: str, tier: str | None) -> bool:
        """A known category, and a tier exactly when the category has tiers."""
        if category not in self.definitions:
            return False
        tiers = self.tiers.get(category) or {}
        return (tier in tiers) if tiers else tier is None

    def tier_index(self, category: str, tier: str | None) -> int | None:
        """filings.tier is a smallint: the 1-based position of the tier in categories.yaml."""
        names = list(self.tiers.get(category) or {})
        return names.index(tier) + 1 if tier in names else None

    def label(self, category_raw: str | None, subject: str | None) -> RuleLabel | None:
        raw, text = category_raw or "", subject or ""
        for rule in self.rules:
            if rule.matches(raw, text):
                needs = bool(self.tiers.get(rule.category)) and rule.tier is None
                return RuleLabel(rule.category, rule.tier, rule.name, rule.llm_review, needs)
        return None


def _compile(pattern: str | None, macros: dict[str, str], where: str) -> re.Pattern | None:
    if pattern is None:
        return None
    try:
        expanded = _MACRO.sub(lambda m: macros[m.group(1)], pattern)
        return re.compile(expanded, re.IGNORECASE)
    except (KeyError, re.error) as exc:
        raise RulesError(f"{where}: bad pattern {pattern[:60]!r} ({exc})") from exc


def parse_categories(data: dict) -> Categories:
    macros = data.get("macros") or {}
    definitions: dict[str, str] = {}
    tiers: dict[str, dict[str, str]] = {}
    rules: list[Rule] = []
    vetoes: dict[str, re.Pattern] = {}
    for name, body in (data.get("categories") or {}).items():
        if not body.get("definition"):
            raise RulesError(f"{name}: missing definition")
        definitions[name] = body["definition"]
        tiers[name] = dict(body.get("tiers") or {})
        if body.get("quote_veto"):
            vetoes[name] = _compile(body["quote_veto"], macros, f"{name}/quote_veto")
        for raw in body.get("rules") or []:
            where = f"{name}/{raw.get('name')}"
            if not raw.get("name") or not (raw.get("category_raw") or raw.get("subject")):
                raise RulesError(f"{where}: a rule needs a name and a pattern")
            tier = raw.get("tier")
            if tier is not None and tier not in tiers[name]:
                raise RulesError(f"{where}: tier {tier!r} is not a tier of {name}")
            rules.append(
                Rule(
                    name=raw["name"],
                    category=name,
                    priority=int(raw.get("priority", 50)),
                    order=len(rules),
                    tier=tier,
                    llm_review=bool(raw.get("llm_review", False)),
                    category_raw=_compile(raw.get("category_raw"), macros, where),
                    subject=_compile(raw.get("subject"), macros, where),
                    not_category_raw=_compile(raw.get("not_category_raw"), macros, where),
                )
            )
    if len(definitions) != 17:
        raise RulesError(f"expected 17 categories, found {len(definitions)}")
    rules.sort(key=lambda r: (r.priority, r.order))
    return Categories(definitions, tiers, tuple(rules), vetoes)


def load_categories(path: Path | None = None) -> Categories:
    return parse_categories(yaml.safe_load((path or CONFIG_DIR / "categories.yaml").read_text()))


@lru_cache(maxsize=1)
def default_categories() -> Categories:
    return load_categories()


def label(category_raw: str | None, subject: str | None) -> RuleLabel | None:
    """Label with the default config. None when no rule matches."""
    return default_categories().label(category_raw, subject)
