from datetime import datetime, timedelta

import pytest

from finn.score.materiality import (
    MaterialityError,
    holding_factor,
    load_materiality,
    recency_factor,
    score_filing,
    status_for,
    weight_for,
)
from finn.sources.shapes import IST

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=IST)

# The weights the product owner specified. The YAML must match this table exactly.
SPEC = {
    "results": 90,
    "litigation.insolvency": 90,
    "management_change.cxo": 85,
    "M&A": 80,
    "fund_raise.equity": 70,
    "credit_rating.downgrade": 70,
    "pledging.increase": 70,
    "litigation.regulatory": 60,
    "capex": 55,
    "credit_rating.upgrade": 50,
    "insider_trading": 50,
    "management_change.director": 40,
    "bonus_split": 40,
    "earnings_call.transcript": 35,
    "press_release": 30,
    "fund_raise.debt": 30,
    "pledging.release": 30,
    "dividend": 25,
    "earnings_call.notice": 15,
    "management_change.other_kmp": 10,
    "credit_rating.reaffirmed": 10,
    "postal_ballot": 10,
    "agm_egm": 10,
    "investor_meetings": 5,
    "unclassified": 0,
}


def test_weights_are_exactly_the_specified_ones_and_thresholds_are_60_and_30():
    cfg = load_materiality()
    assert cfg["weights"] == SPEC
    assert cfg["thresholds"] == {"card": 60, "oneliner": 30}
    assert (cfg["holding"]["min"], cfg["holding"]["max"], cfg["holding"]["default"]) == (
        0.8,
        1.2,
        1.0,
    )
    assert cfg["recency"]["window_hours"] == 24 and cfg["price_volume_bonus"] == 15


def test_postal_ballot_agm_and_egm_share_one_weight_of_10():
    assert weight_for("postal_ballot", None) == weight_for("agm_egm", None) == 10


def test_a_tiered_category_without_a_tier_cannot_be_scored():
    with pytest.raises(MaterialityError):
        weight_for("management_change", None)
    with pytest.raises(MaterialityError):
        weight_for("dividend", "cxo")


def test_score_is_weight_times_holding_times_recency_plus_bonus():
    s = score_filing("management_change", "cxo", "INFY", NOW - timedelta(hours=12), NOW,
                     holdings={"INFY": 20, "ITC": 10})  # fmt: skip
    # INFY is the largest holding: 1.2; 12 h of 24 h with a floor of 0.5: 0.75
    assert (s.weight, s.holding, s.recency) == (85, 1.2, 0.75)
    assert s.score == pytest.approx(85 * 1.2 * 0.75)
    with_bonus = score_filing("management_change", "cxo", "INFY", NOW - timedelta(hours=12), NOW,
                              price_volume_fired=True, holdings={"INFY": 20, "ITC": 10})  # fmt: skip
    assert with_bonus.score == pytest.approx(85 * 1.2 * 0.75 + 15) and with_bonus.bonus == 15


def test_fresh_filings_clear_the_thresholds_as_specified():
    def status(category, tier=None, hours=0, fired=False):
        s = score_filing(category, tier, "ITC", NOW - timedelta(hours=hours), NOW,
                         price_volume_fired=fired, holdings={})  # fmt: skip
        return status_for(s.score)

    assert status("results") == "card"
    assert status("litigation", "regulatory") == "card"  # exactly 60
    assert status("capex") == "oneliner"
    assert status("dividend") == "drawer"  # 25
    assert status("dividend", fired=True) == "oneliner"  # 25 + 15 = 40
    assert status("investor_meetings", fired=True) == "drawer"
    assert status("results", hours=48) == "oneliner"  # 90 x 0.5 = 45: old news is not a card


def test_status_boundaries_are_inclusive_at_60_and_30():
    assert [status_for(x) for x in (59.99, 60, 30, 29.99, 0)] == [
        "oneliner", "card", "oneliner", "drawer", "drawer",
    ]  # fmt: skip


def test_holding_factor_scales_0_8_to_1_2_and_defaults_to_1():
    holdings = {"A": 5, "B": 10, "C": 15}
    assert holding_factor("A", holdings) == 0.8
    assert holding_factor("B", holdings) == 1.0
    assert holding_factor("C", holdings) == 1.2
    assert holding_factor("Z", holdings) == 1.0  # not given
    assert holding_factor("A", {}) == 1.0  # no weights at all
    assert holding_factor("A", {"A": 7, "B": 7}) == 1.0  # nothing to scale


def test_recency_decays_over_24_hours_to_the_floor():
    f = lambda h: recency_factor(NOW - timedelta(hours=h), NOW)  # noqa: E731
    assert f(0) == 1.0 and f(12) == 0.75 and f(24) == 0.5 and f(100) == 0.5
    assert recency_factor(NOW + timedelta(hours=1), NOW) == 1.0  # clock skew never boosts
    assert recency_factor(None, NOW) == 0.5


def test_why_template_is_plain_code_with_the_same_numbers():
    s = score_filing("results", None, "ITC", NOW, NOW, price_volume_fired=True, holdings={})
    assert s.why_template() == "results 90 x holding 1.00 x recency 1.00 + 15 price/volume = 105"
