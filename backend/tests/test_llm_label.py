import pytest

from finn.classify import grader, llm_label
from finn.classify.rules import label as rule_label
from finn.llm.router import LLMOutputError
from finn.normalise.quote import quote_match
from tests.llm_helpers import ScriptedRouter, groq_reply, make_router

PDF = (
    "Trent Limited. Sub: Outcome of Board Meeting. The Board of Directors at its meeting held "
    "today approved the appointment of Mr Rajiv Dube as Non-Executive Director of the Company "
    "with effect from October 30, 2026."
)
GOOD_QUOTE = "approved the appointment of Mr Rajiv Dube as Non-Executive Director of the Company"
MADE_UP = "the company announced a record quarterly profit and a surprise acquisition of a rival"


def run(replies, hint=None, excerpt=PDF, subject="Outcome of Board Meeting held today"):
    router = ScriptedRouter(replies)
    out = llm_label.label_with_llm(
        "Outcome of Board Meeting", subject, excerpt, hint, router=router
    )
    return out, router


def test_quote_found_in_the_pdf_text_passes():
    out, router = run([{"category": "management_change", "tier": "director", "quote": GOOD_QUOTE}])
    assert out.ok and (out.category, out.tier) == ("management_change", "director")
    assert out.quote_match >= 0.99 and out.attempts == 1 and len(router.calls) == 1
    step, role, prompt = router.calls[0]
    assert (step, role) == ("label", "small")
    assert "Use only the provided text. Do not use outside knowledge." in prompt
    assert "Rajiv Dube" in prompt  # the PDF excerpt is what the model reads


def test_quote_not_in_the_text_is_retried_once_then_fails_to_the_drawer():
    out, router = run([{"category": "M&A", "tier": None, "quote": MADE_UP}] * 2)
    assert not out.ok and out.quote_match < 0.4 and len(router.calls) == 2
    assert "rejected" in router.calls[1][2] and out.attempts == 2
    assert "quote_match" in out.reason


def test_retry_can_succeed():
    out, router = run(
        [
            {"category": "M&A", "tier": None, "quote": MADE_UP},
            {"category": "management_change", "tier": "director", "quote": GOOD_QUOTE},
        ]
    )
    assert out.ok and out.attempts == 2 and len(router.calls) == 2


def test_the_threshold_is_0_4_of_the_4_word_runs():
    quote = (
        "approved the appointment of Mr Rajiv Dube and also something entirely unrelated here today"
    )
    m = quote_match(quote, PDF)
    assert 0.3 < m < 0.6
    out, _ = run([{"category": "management_change", "tier": "director", "quote": quote}] * 2)
    assert out.ok is (m >= 0.4)


def test_results_label_whose_quote_is_a_notice_of_a_future_meeting_is_rejected():
    """Real case: a Reliance filing about Jio Platforms' listing mentioned that JPL's board
    meeting 'is scheduled to consider' results; the small model called the filing results."""
    excerpt = "Further, a meeting of board of directors of JPL is scheduled to consider the financial results of JPL for the quarter."
    future = {
        "category": "results",
        "tier": None,
        "quote": "is scheduled to consider the financial results of JPL for the quarter",
    }
    other = {
        "category": "M&A",
        "tier": None,
        "quote": "a meeting of board of directors of JPL is scheduled to consider the financial results",
    }
    out, router = run([future, other], excerpt=excerpt)
    assert out.ok and out.category == "M&A" and len(router.calls) == 2
    out, _ = run([future, future], excerpt=excerpt)
    assert not out.ok and "future event" in out.reason
    # the same words under another category are not vetoed
    approved = {"category": "results", "tier": None, "quote": GOOD_QUOTE}
    assert run([approved])[0].ok


def test_unknown_category_and_missing_tier_are_failures_not_guesses():
    out, _ = run([{"category": "weather", "tier": None, "quote": GOOD_QUOTE}] * 2)
    assert not out.ok and "unknown category" in out.reason
    out, _ = run([{"category": "management_change", "tier": None, "quote": GOOD_QUOTE}] * 2)
    assert not out.ok and "bad tier" in out.reason


def test_a_tier_on_a_category_without_tiers_is_dropped():
    out, _ = run([{"category": "dividend", "tier": "special", "quote": GOOD_QUOTE}])
    assert out.ok and out.tier is None


def test_abstaining_with_unclassified_is_a_valid_label():
    out, _ = run([{"category": "unclassified", "tier": None, "quote": GOOD_QUOTE}])
    assert out.ok and out.category == "unclassified"


def test_without_a_pdf_the_quote_is_checked_against_the_subject():
    subject = (
        "Clarification on outcome of the meeting of the Board of Directors held on October 28, 2026"
    )
    out, _ = run(
        [{"category": "unclassified", "tier": None, "quote": subject}], excerpt="", subject=subject
    )
    assert out.ok


def test_press_release_catch_all_never_offers_results_and_tier_hint_is_in_the_prompt():
    hint = rule_label("Press Release", "a press release")
    _, router = run([{"category": "capex", "tier": None, "quote": GOOD_QUOTE}], hint=hint)
    assert "- results:" not in router.calls[0][2] and "- capex:" in router.calls[0][2]
    hint = rule_label("Change in Management", "change in Management")
    _, router = run(
        [{"category": "management_change", "tier": "cxo", "quote": GOOD_QUOTE}], hint=hint
    )
    assert "pick its tier" in router.calls[0][2]


def test_unusable_json_from_the_router_propagates_for_the_job_to_drawer():
    router = make_router(lambda r: groq_reply("nope"))
    with pytest.raises(LLMOutputError):
        llm_label.label_with_llm("c", "s", PDF, router=router)


# -- grader ------------------------------------------------------------------------------------


@pytest.mark.parametrize("body, score, passed", [('{"score": 5}', 5, True), ('{"score": 4}', 4, True),
                                                 ('{"score": 3}', 3, False), ('{"score": 1}', 1, False),
                                                 ('```json\n{"score": 4}\n```', 4, True)])  # fmt: skip
def test_grader_parses_json_scores_and_only_4_5_pass(body, score, passed):
    router = make_router(lambda r: groq_reply(body))
    g = grader.grade_label("management_change", "cxo", GOOD_QUOTE, router=router)
    assert (g.score, g.passed) == (score, passed)


def test_grader_with_an_out_of_range_or_unparsable_score_is_unverified_not_a_pass():
    for body in ('{"score": 9}', '{"score": "high"}', "garbage"):
        router = make_router(lambda r, b=body: groq_reply(b))
        g = grader.grade_label("dividend", None, GOOD_QUOTE, router=router)
        assert (g.score, g.passed) == (None, False)


def test_grader_sees_only_the_quote_and_the_definition():
    router = ScriptedRouter([{"score": 5}])
    grader.grade_label("management_change", "cxo", GOOD_QUOTE, router=router)
    step, role, prompt = router.calls[0]
    assert (step, role) == ("grade", "small")
    assert GOOD_QUOTE in prompt and "chair of the board" in prompt  # the tier's description
    assert "Subject" not in prompt and "Exchange category" not in prompt
