import pytest
from pydantic import ValidationError

from finn.db.models import Claim, Fact, Verdict


def fact(**kw):
    base = dict(
        ticker="INFY",
        metric="revenue",
        value_cr=49244.0,
        unit_original="rupee",
        period="FY27Q2",
        period_kind="Q",
        basis="consolidated",
        source_url="u://x",
        quote="RevenueFromOperations contextRef",
        quote_match=1.0,
    )
    return Fact(**{**base, **kw})


def test_fact_round_trip_and_key():
    f = fact()
    assert f.derived is False and f.segment is None and f.page is None
    assert Fact.model_validate(f.model_dump()) == f
    assert f.key == ("INFY", "revenue", None, "FY27Q2", "Q", "consolidated")


@pytest.mark.parametrize(
    "kw",
    [
        {"metric": "gross_profit"},
        {"unit_original": "thousand"},
        {"basis": "proforma"},
        {"period": "Q2 FY27"},
        {"period": "FY27Q2", "period_kind": "FY"},  # label and kind disagree
        {"period": "FY27", "period_kind": "Q"},
        {"derived": True},  # derived without a formula
        {"quote_match": 1.2},
        {"extra_field": 1},
    ],
)
def test_fact_rejects_bad_input(kw):
    with pytest.raises(ValidationError):
        fact(**kw)


def test_fact_allows_null_value_and_derived_with_formula():
    assert fact(value_cr=None).value_cr is None
    assert fact(derived=True, formula="ebitda = pbt + ...").derived


def test_all_period_labels_accepted():
    for period, kind in [
        ("FY27Q4", "Q"),
        ("FY27H1", "H1"),
        ("FY27H2", "H2"),
        ("FY27-9M", "9M"),
        ("FY27", "FY"),
    ]:
        assert fact(period=period, period_kind=kind).period == period


def test_claim_contract():
    c = Claim(
        ticker="INFY",
        said_in="FY27Q1",
        speaker="CFO",
        metric="text",
        quote="we expect",
        quote_match=0.9,
    )
    assert c.target_low is None and c.by_period is None
    with pytest.raises(ValidationError):
        Claim(
            ticker="INFY",
            said_in="FY27Q1",
            metric="revenue",
            target_low=5,
            target_high=1,
            quote="x",
            quote_match=0.9,
        )
    with pytest.raises(ValidationError):
        Claim(ticker="INFY", said_in="FY27Q1", metric="margin", quote="x", quote_match=0.9)


def test_verdict_contract():
    assert Verdict(claim_id="c", actual_fact_id="f", verdict="delivered", reason="ok").verdict
    assert Verdict(claim_id="c", actual_fact_id=None, verdict="not_enough_evidence", reason="-")
    with pytest.raises(ValidationError):
        Verdict(claim_id="c", actual_fact_id=None, verdict="missed", reason="no fact")
    with pytest.raises(ValidationError):
        Verdict(claim_id="c", actual_fact_id="f", verdict="delivered", reason=" ".join(["w"] * 21))
