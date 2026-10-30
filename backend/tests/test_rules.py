import json

import pytest
import yaml

from finn.classify.rules import RulesError, label, load_categories, parse_categories
from finn.config import CONFIG_DIR
from finn.score.materiality import default_config
from finn.sources import nse
from tests.conftest import FIXTURES


def got(category_raw, subject):
    r = label(category_raw, subject)
    return None if r is None else (r.category, r.tier)


def test_there_are_17_categories_each_with_a_definition_and_every_tier_has_a_weight():
    cats = load_categories()
    assert len(cats.names) == 17 and all(cats.definitions.values())
    weights = default_config()["weights"]
    for name in cats.names:
        tiers = cats.tiers[name]
        keys = [f"{name}.{t}" for t in tiers] if tiers else [name]
        assert all(k in weights for k in keys), (name, keys)
    # and no weight points at a category or tier that does not exist
    for key in weights:
        name, _, tier = key.partition(".")
        assert name in cats.names and (tier in cats.tiers[name] if tier else True)


def test_the_spec_tiers_exist():
    t = load_categories().tiers
    assert set(t["management_change"]) == {"cxo", "director", "other_kmp"}
    assert set(t["fund_raise"]) == {"equity", "debt"}
    assert set(t["credit_rating"]) == {"downgrade", "upgrade", "reaffirmed"}
    assert set(t["pledging"]) == {"increase", "release"}
    assert set(t["litigation"]) == {"insolvency", "regulatory"}


# -- management change: the company secretary / CFO split --------------------------------------


@pytest.mark.parametrize(
    "category, subject, expected",
    [
        (
            "Appointment",
            "X has informed the Exchange regarding Appointment of Ms Rao as Company Secretary and Compliance Officer",
            "other_kmp",
        ),
        (
            "Resignation",
            "X has informed the Exchange about resignation of Company Secretary",
            "other_kmp",
        ),
        ("Updates", "Cessation of Mr Shah as Compliance Officer of the Company", "other_kmp"),
        ("Appointment", "Appointment of Mr Iyer as Chief Financial Officer of the Company", "cxo"),
        ("Resignation", "Resignation of Chief Financial Officer", "cxo"),
        (
            "General Updates",
            "Change in Managing Director and Chief Executive Officer of the Bank",
            "cxo",
        ),
        ("Retirement", "Retirement of Mr Noel N Tata as the Chairman", "cxo"),
        ("Appointment", "Appointment of Mr Rao as CEO", "cxo"),
        # both appear: the more material tier wins
        (
            "Appointment",
            "Appointment of Ms Das as Company Secretary and Chief Financial Officer",
            "cxo",
        ),
        (
            "Appointment",
            "Appointment of Mrs Merchant as Non-Executive Independent Director",
            "director",
        ),
        ("Change in Director(s)", "Change in Director(s) of the company.", "director"),
        # chair of a committee is a director, not the chair of the board
        (
            "Appointment",
            "Mr Rao, Independent Director, appointed Chairman of the Audit Committee",
            "director",
        ),
        # "chief compliance officer" is not a CXO
        ("Appointment", "Appointment of Chief Compliance Officer", "other_kmp"),
    ],
)
def test_management_change_tiers(category, subject, expected):
    assert got(category, subject) == ("management_change", expected)


def test_change_in_management_with_no_role_needs_the_tier_from_the_filing():
    r = label(
        "Change in Management",
        "Infosys Limited has informed the Exchange about change in Management",
    )
    assert (r.category, r.tier, r.needs_tier) == ("management_change", None, True)


def test_nse_fixture_filings():
    rows = nse.parse_announcements(
        json.loads((FIXTURES / "nse_announcements_mgmt_change.json").read_text())
    )
    by_cat = {r["category"]: label(r["category"], r["text"]) for r in rows}
    assert by_cat["Change in Management"].needs_tier
    assert (by_cat["ESOP/ESOS/ESPS"].category, by_cat["ESOP/ESOS/ESPS"].rule) == (
        "unclassified",
        "esop_exchange_category",
    )


# -- the other tiered categories ----------------------------------------------------------------


@pytest.mark.parametrize(
    "category, subject, expected",
    [
        (
            "Fund Raising",
            "Board approves raising funds through QIP of equity shares",
            ("fund_raise", "equity"),
        ),
        (
            "Updates",
            "Approval of preferential issue of warrants to promoters",
            ("fund_raise", "equity"),
        ),
        (
            "Outcome of Board Meeting",
            "Approved buyback of equity shares at Rs 100",
            ("fund_raise", "equity"),
        ),
        (
            "Updates",
            "Issue of non-convertible debentures aggregating to Rs 500 crore",
            ("fund_raise", "debt"),
        ),
        ("Fund Raising", "Board considered fund raising", ("fund_raise", None)),
        (
            "Credit Rating",
            "CRISIL downgrades the rating of the company to AA from AA+",
            ("credit_rating", "downgrade"),
        ),
        ("Updates", "ICRA upgraded the long term rating to AAA", ("credit_rating", "upgrade")),
        (
            "Credit Rating",
            "CARE Ratings reaffirmed the credit rating AA+ with stable outlook",
            ("credit_rating", "reaffirmed"),
        ),
        ("Credit Rating", "Credit rating update", ("credit_rating", None)),
        (
            "Encumbrance",
            "Creation of pledge on 2,00,000 shares by the promoter",
            ("pledging", "increase"),
        ),
        ("Encumbrance", "Release of pledge on shares held by promoter", ("pledging", "release")),
        ("Encumbrance", "Disclosure of encumbrance", ("pledging", None)),
        (
            "Updates",
            "Initiation of Corporate Insolvency Resolution Process against the subsidiary",
            ("litigation", "insolvency"),
        ),
        (
            "Updates",
            "SEBI passed an adjudication order imposing penalty on the company",
            ("litigation", "regulatory"),
        ),
        (
            "Updates",
            "Receipt of show cause notice from the Income Tax department",
            ("litigation", "regulatory"),
        ),
    ],
)
def test_tiered_categories(category, subject, expected):
    assert got(category, subject) == expected


def test_an_nclat_update_filed_under_the_insolvency_category_is_not_labelled_insolvent():
    """Polycab's real filing: the exchange category says CIRP but the company is not the debtor."""
    r = label(
        "Corporate Insolvency Resolution Process",
        "Polycab India Limited has informed the Exchange on the update of NCLAT order dated October 27, 2026",
    )
    assert (r.category, r.tier, r.needs_tier) == ("litigation", None, True)


# -- everything else, and what must stay out ----------------------------------------------------


@pytest.mark.parametrize(
    "category, subject, expected",
    [
        (
            "Outcome of Board Meeting",
            "X has submitted to the Exchange, the financial results for the period ended September 30, 2026.",
            "results",
        ),
        ("Financial Results", "Integrated filing", "results"),
        ("Result | Financial Results", "Financial results", "results"),
        (
            "General Updates",
            "X has informed the Exchange about the financial results for the quarter and half year ended 30th September 2026.",
            "results",
        ),
        (
            "Acquisition",
            "X has informed the Exchange about acquisition of 51% stake in Y Ltd",
            "M&A",
        ),
        ("Updates", "Scheme of arrangement for merger of Z with the company", "M&A"),
        ("Dividend", "Board declared an Interim Dividend of Rs. 7.70 per equity share", "dividend"),
        ("Record Date", "Record date for the purpose of Dividend is 03-Nov-2026", "dividend"),
        ("Updates", "Approval of bonus issue of equity shares", "bonus_split"),
        ("Updates", "Sub-division of equity shares of face value Rs 10 to Rs 2", "bonus_split"),
        ("Updates", "Board approves capex of Rs 400 crore for capacity expansion", "capex"),
        ("Postal Ballot", "Notice of postal ballot", "postal_ballot"),
        ("Shareholders meeting", "Notice of the 30th Annual General Meeting", "agm_egm"),
        (
            "Insider Trading / SAST",
            "Disclosure under Regulation 29(2) of SEBI SAST",
            "insider_trading",
        ),
        (
            "Updates",
            "Disclosure under Regulation 7(2) of the SEBI (Prohibition of Insider Trading) Regulations",
            "insider_trading",
        ),
        (
            "Press Release",
            "a press release dated October 28, 2026, titled Press Release",
            "press_release",
        ),
        ("Bagging/Receiving of orders/contracts", "Bagging of orders", "press_release"),
        (
            "Analysts/Institutional Investor Meet/Con. Call Updates",
            "Schedule of meet",
            "investor_meetings",
        ),
        ("Investor Presentation", "Investor Presentation", "investor_meetings"),
    ],
)
def test_category_labels(category, subject, expected):
    assert got(category, subject)[0] == expected


def test_press_release_catch_all_is_flagged_for_the_llm():
    r = label("Press Release (Revised)", "revised press release")
    assert (r.category, r.rule, r.llm_review) == ("press_release", "press_release_catch_all", True)


def test_earnings_call_transcript_and_notice_tiers():
    assert got("Updates", "regarding 'Earnings Call Transcript'") == ("earnings_call", "transcript")
    assert got(
        "Analysts/Institutional Investor Meet/Con. Call Updates",
        "Eternal has filed transcript of earnings conference call",
    ) == ("earnings_call", "transcript")
    assert got("Updates", "Intimation of schedule of earnings conference call on 28 Oct") == (
        "earnings_call",
        "notice",
    )
    # a plain analyst meet is an investor meeting, not an earnings call
    assert got("Updates", "Schedule of meet with analysts") == ("investor_meetings", None)


@pytest.mark.parametrize(
    "category, subject, rule",
    [
        (
            "Monitoring Agency Report",
            "Funds raised through issue of Qualified Institutional Placement ( QIP )",
            "monitoring_agency_report",
        ),
        (
            "Statement of deviation(s) or variation(s) under Reg. 32",
            "statement of deviation",
            "deviation_statement",
        ),
        (
            "Copy of Newspaper Publication",
            "Copy of Newspaper Publication of Unaudited Financial Results",
            "newspaper_publication",
        ),
        (
            "Allotment of Securities",
            "allotment of 21098 securities pursuant to ESOP/ESPS",
            "esop_grant_or_allotment",
        ),
        ("Acquisition", "Incorporation of Wholly Owned Subsidiary", "intra_group_housekeeping"),
        (
            "Sale or disposal",
            "Board approved the Strike-off of Somit Solutions Limited, wholly owned step-down subsidiary",
            "intra_group_housekeeping",
        ),
        (
            "Updates",
            "Disclosure under Regulation 8(2) of the SEBI (Prohibition of Insider Trading) Regulations",
            "insider_fair_disclosure_code",
        ),
        (
            "Analysts/Institutional Investor Meet/Con. Call Updates",
            "The meeting of the Board of Directors of the Company is scheduled on October 30, 2026, inter alia, to consider and approve the unaudited financial results",
            "board_meeting_intimation",
        ),
    ],
)
def test_routine_items_are_unclassified_and_never_mistaken_for_events(category, subject, rule):
    r = label(category, subject)
    assert (r.category, r.rule) == ("unclassified", rule)


def test_text_no_rule_can_place_goes_to_the_llm():
    assert (
        label("General Updates", "Winners of the Qorus banking innovation awards announced") is None
    )
    assert label("Outcome of Board Meeting", "Outcome of Board Meeting held today") is None
    assert label(None, None) is None


def test_every_label_names_its_rule():
    assert label("Dividend", "Interim dividend").rule == "dividend_in_text"


# -- the loader checks its own YAML -----------------------------------------------------------


def base():
    return yaml.safe_load((CONFIG_DIR / "categories.yaml").read_text())


def test_loader_rejects_a_tier_the_category_does_not_have():
    data = base()
    data["categories"]["dividend"]["rules"][0]["tier"] = "special"
    with pytest.raises(RulesError, match="not a tier"):
        parse_categories(data)


def test_loader_rejects_a_bad_regex_and_an_unknown_macro_and_a_rule_with_no_pattern():
    data = base()
    data["categories"]["dividend"]["rules"][0]["subject"] = "(unclosed"
    with pytest.raises(RulesError, match="bad pattern"):
        parse_categories(data)
    data = base()
    data["categories"]["dividend"]["rules"][0]["subject"] = "<<nope>>"
    with pytest.raises(RulesError, match="bad pattern"):
        parse_categories(data)
    data = base()
    data["categories"]["dividend"]["rules"] = [{"name": "empty"}]
    with pytest.raises(RulesError, match="needs a name and a pattern"):
        parse_categories(data)


def test_loader_insists_on_17_categories():
    data = base()
    del data["categories"]["capex"]
    with pytest.raises(RulesError, match="17"):
        parse_categories(data)


def test_is_valid_wants_a_tier_exactly_when_the_category_has_tiers():
    cats = load_categories()
    assert cats.is_valid("management_change", "cxo") and not cats.is_valid(
        "management_change", None
    )
    assert cats.is_valid("dividend", None) and not cats.is_valid("dividend", "cxo")
    assert not cats.is_valid("made_up", None)
    assert (
        cats.tier_index("management_change", "other_kmp") == 3
        and cats.tier_index("dividend", None) is None
    )
