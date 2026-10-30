"""Card builders for the filings the brief shows as more than a line: results and new people.

`CardBuilder` is what the announcement job and scripts/build_cards.py call. It picks the builder
from the filing's category and tier, returns a Card (a card_json fragment, the facts to write and
the code-made figures a why-line may quote). It never invents anything: a card that cannot be
checked says so in its state instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from finn.config import portfolio_names
from finn.db.client import Db
from finn.db.models import Fact
from finn.enrich import people_card, results_card
from finn.enrich.pdf import pdf_excerpt
from finn.llm.router import Router, Store, default_router
from finn.parse.results import results_facts

log = logging.getLogger(__name__)

# (category, tier) -> card kind
CARD_KINDS = {
    ("results", "filed"): "results",
    ("management_change", "cxo"): "people",
    ("management_change", "director"): "people",
}


@dataclass
class Card:
    kind: str
    fragment: dict
    facts: list[Fact] = field(default_factory=list)
    figures: dict[str, str] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.fragment["state"]


def card_kind(category: str | None, tier: str | None) -> str | None:
    return CARD_KINDS.get((category, tier))


class CardBuilder:
    """Builds a card for one filings row. Network and model access are injected for tests."""

    def __init__(
        self,
        router: Router | None = None,
        db: Db | None = None,
        store: Store | None = None,
        pdf: Callable = pdf_excerpt,
        facts_fn: Callable = results_facts,
        tavily: people_card.Tavily | None = None,
    ):
        self.router = router or default_router()
        self.db = db
        self.pdf = pdf
        self.facts_fn = facts_fn
        self.tavily = tavily or people_card.Tavily(store=store or self.router.store)
        self._built: dict[tuple[str, str], str] = {}  # (symbol, period) -> source_id, this run

    def __call__(self, row: dict, category: str, tier: str | None) -> Card | None:
        kind = card_kind(category, tier)
        if kind == "results":
            deps = results_card.Deps(
                results_facts=self.facts_fn,
                pdf=self.pdf,
                router=self.router,
                duplicate_of=self.duplicate_of,
            )
            built = results_card.build_results_card(row, deps)
            if built.state != "duplicate":
                self._built[(row["symbol"], built.fragment["period"])] = row["source_id"]
            return Card(kind, built.fragment, built.facts, built.figures)
        if kind == "people":
            company = (row.get("card_json") or {}).get("company") or portfolio_names().get(
                row["symbol"], row["symbol"]
            )
            deps = people_card.Deps(router=self.router, tavily=self.tavily, pdf=self.pdf)
            built = people_card.build_people_card(row, company, deps)
            return Card(kind, built.fragment)
        return None

    def duplicate_of(self, symbol: str, period: str, own_source_id: str | None) -> str | None:
        """source_id of the filing that already holds this company's card for the period."""
        mine = self._built.get((symbol, period))
        if mine and mine != own_source_id:
            return mine
        if self.db is None:
            return None
        rows = (
            self.db.client.table("filings")
            .select("source_id,filed_at")
            .eq("symbol", symbol)
            .eq("card_json->>card_type", "results")
            .eq("card_json->>period", period)
            .in_("card_json->>state", ["ok", "check_source"])
            .order("filed_at")
            .execute()
            .data
        )
        others = [r["source_id"] for r in rows if r["source_id"] != own_source_id]
        return others[0] if others else None


def apply_card(row: dict, card: Card, db: Db | None) -> None:
    """Merge the card into the row's card_json and write its facts (raises if that write fails:
    a card must never show figures that are not in the facts table)."""
    if card.facts and db is not None:
        db.upsert_facts(card.facts)
    row["card_json"] = {**(row.get("card_json") or {}), **card.fragment}
