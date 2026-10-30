"""Pydantic contracts for the fact ledger (CLAUDE.md "Data contracts").

Fact is the only way a figure reaches the UI. Claim and Verdict follow the same contracts.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

Metric = Literal[
    "revenue",
    "ebitda",
    "pbt",
    "pat",
    "tax",
    "finance_costs",
    "d_and_a",
    "other_income",
    "nii",
    "ppop",
    "capex",
    "cfo",
    "fcf",
    "debt",
    "equity",
    "ebitda_margin",
    "pat_margin",
    "segment_revenue",
]
ClaimMetric = Literal[
    "revenue",
    "ebitda",
    "pbt",
    "pat",
    "tax",
    "finance_costs",
    "d_and_a",
    "other_income",
    "nii",
    "ppop",
    "capex",
    "cfo",
    "fcf",
    "debt",
    "equity",
    "ebitda_margin",
    "pat_margin",
    "segment_revenue",
    "text",
]
UnitOriginal = Literal["lakh", "crore", "million", "rupee"]
PeriodKind = Literal["Q", "H1", "H2", "9M", "FY"]
Basis = Literal["consolidated", "standalone"]
TargetUnit = Literal["%", "cr", "units", "text"]
VerdictLabel = Literal[
    "delivered", "missed", "moved_goalposts", "went_quiet", "not_enough_evidence"
]

# FY27Q2, FY27H1, FY27H2, FY27-9M, FY27
PERIOD_RE = re.compile(r"^FY\d{2}(Q[1-4]|H[12]|-9M)?$")
_SUFFIX_FOR_KIND = {"Q": r"Q[1-4]", "H1": "H1", "H2": "H2", "9M": "-9M", "FY": ""}


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    metric: Metric
    segment: str | None = None
    value_cr: float | None  # Rs crore; null when the source gives no figure
    unit_original: UnitOriginal
    period: str
    period_kind: PeriodKind
    basis: Basis
    derived: bool = False
    formula: str | None = None
    source_url: str
    page: int | None = None
    quote: str
    quote_match: float
    # True/False when the source reports an exceptional-items line for the period (True = nonzero);
    # None when it does not (screener nets them into other income). EBITDA always excludes them.
    has_exceptional_items: bool | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Fact:
        if not PERIOD_RE.match(self.period):
            raise ValueError(f"period {self.period!r} is not like FY27Q2")
        if not re.fullmatch(r"FY\d{2}" + _SUFFIX_FOR_KIND[self.period_kind], self.period):
            raise ValueError(
                f"period {self.period!r} does not match period_kind {self.period_kind}"
            )
        if self.derived and not self.formula:
            raise ValueError("a derived fact must store its formula")
        if not 0.0 <= self.quote_match <= 1.0:
            raise ValueError("quote_match must be between 0 and 1")
        return self

    @property
    def key(self) -> tuple:
        """The natural key used by the facts table's unique constraint."""
        return (self.ticker, self.metric, self.segment, self.period, self.period_kind, self.basis)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    said_in: str  # call period, e.g. FY27Q1
    speaker: str | None = None
    metric: ClaimMetric
    target_low: float | None = None
    target_high: float | None = None
    target_unit: TargetUnit | None = None
    by_period: str | None = None
    quote: str
    page: int | None = None
    quote_match: float

    @model_validator(mode="after")
    def _range(self) -> Claim:
        if not 0.0 <= self.quote_match <= 1.0:
            raise ValueError("quote_match must be between 0 and 1")
        low, high = self.target_low, self.target_high
        if low is not None and high is not None and low > high:
            raise ValueError("target_low is above target_high")
        return self


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    actual_fact_id: str | None  # null only when abstaining (not_enough_evidence) or went_quiet
    verdict: VerdictLabel
    reason: str  # max 20 words, no new numbers

    @model_validator(mode="after")
    def _checks(self) -> Verdict:
        if len(self.reason.split()) > 20:
            raise ValueError("reason is longer than 20 words")
        if self.actual_fact_id is None and self.verdict not in (
            "not_enough_evidence",
            "went_quiet",
        ):
            raise ValueError(f"verdict {self.verdict} needs an actual_fact_id")
        return self
