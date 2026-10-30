"""Materiality score, per config/materiality.yaml.

    score = weight(category[.tier]) x holding factor (0.8-1.2) x recency (+ 15 if a price or volume
    rule fired the same day)

60 or more is a card, 30-59 a one-liner, below 30 the routine drawer. Pure functions: the clock
and the price/volume flag are arguments, so a score can be reproduced from the stored parts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from finn.config import CONFIG_DIR, load_portfolio


class MaterialityError(ValueError):
    """No weight for this category/tier combination."""


@dataclass(frozen=True)
class Score:
    score: float
    weight: float
    holding: float
    recency: float
    bonus: float
    key: str  # the weights key used, e.g. management_change.cxo

    def parts(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def why_template(self) -> str:
        """A plain-code 'why ranked' line: the same numbers, no model."""
        text = (
            f"{self.key} {self.weight:g} x holding {self.holding:.2f} x recency {self.recency:.2f}"
        )
        if self.bonus:
            text += f" + {self.bonus:g} price/volume"
        return f"{text} = {self.score:.0f}"


def load_materiality(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_DIR / "materiality.yaml").read_text())


@lru_cache(maxsize=1)
def default_config() -> dict:
    return load_materiality()


def weight_key(category: str, tier: str | None) -> str:
    return f"{category}.{tier}" if tier else category


def weight_for(category: str, tier: str | None, config: dict | None = None) -> float:
    weights = (config or default_config())["weights"]
    key = weight_key(category, tier)
    if key not in weights:
        raise MaterialityError(f"no weight for {key!r} in config/materiality.yaml")
    return float(weights[key])


def holding_factor(
    symbol: str, holdings: dict[str, float] | None = None, config: dict | None = None
) -> float:
    """0.8-1.2 scaled across the optional `weights` in config/portfolio.yaml; default 1.0."""
    cfg = (config or default_config())["holding"]
    if holdings is None:
        holdings = load_portfolio().get("weights") or {}
    if symbol not in holdings or len(set(holdings.values())) < 2:
        return float(cfg["default"])
    low, high = min(holdings.values()), max(holdings.values())
    share = (holdings[symbol] - low) / (high - low)
    return round(cfg["min"] + share * (cfg["max"] - cfg["min"]), 6)


def recency_factor(filed_at: datetime | None, now: datetime, config: dict | None = None) -> float:
    """1.0 now, falling linearly to the floor over the window, flat after. Unknown time: floor."""
    cfg = (config or default_config())["recency"]
    if filed_at is None:
        return float(cfg["floor"])
    age_h = max((now - filed_at).total_seconds() / 3600, 0.0)
    fade = min(age_h / cfg["window_hours"], 1.0)
    return round(1.0 - fade * (1.0 - cfg["floor"]), 6)


def score_filing(
    category: str,
    tier: str | None,
    symbol: str,
    filed_at: datetime | None,
    now: datetime,
    *,
    price_volume_fired: bool = False,
    holdings: dict[str, float] | None = None,
    config: dict | None = None,
) -> Score:
    cfg = config or default_config()
    weight = weight_for(category, tier, cfg)
    holding = holding_factor(symbol, holdings, cfg)
    recency = recency_factor(filed_at, now, cfg)
    bonus = float(cfg["price_volume_bonus"]) if price_volume_fired else 0.0
    return Score(
        round(weight * holding * recency + bonus, 4),
        weight,
        holding,
        recency,
        bonus,
        weight_key(category, tier),
    )


def status_for(score: float, config: dict | None = None) -> str:
    t = (config or default_config())["thresholds"]
    if score >= t["card"]:
        return "card"
    if score >= t["oneliner"]:
        return "oneliner"
    return "drawer"
