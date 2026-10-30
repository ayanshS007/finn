import httpx
import pytest

from finn.enrich import people_card as pc
from finn.enrich.pdf import PdfText
from finn.llm.router import LLMPending, MemoryStore
from tests.llm_helpers import ScriptedRouter

# an HDFC Bank style letter: the new MD & CEO joins, the old one leaves, a profile is annexed
LETTER = (
    "HDFC Bank Limited. Disclosure under Regulation 30. Change in Managing Director and Chief "
    "Executive Officer of the Bank. This is to inform that Mr. Sashidhar Jagdishan has vacated "
    "the office as the MD & CEO of the Bank from the close of business hours on October 26, 2026 "
    "on account of completion of his tenure and Mr. Anup Bagchi has assumed charge as MD & CEO "
    "of the Bank effective today i.e. October 27, 2026.\n"
    "Annexure I. Brief profile of Mr. Anup Bagchi. Mr. Anup Bagchi is a graduate in commerce and "
    "a chartered accountant. He has more than three decades of experience in banking and "
    "financial services, and has led the retail and wealth businesses of a large financial group."
)
PROFILE = (
    "Mr. Anup Bagchi is a graduate in commerce and a chartered accountant. He has more than three "
    "decades of experience in banking and financial services, and has led the retail and wealth "
    "businesses of a large financial group."
)
ROW = {
    "symbol": "HDFCBANK",
    "source_id": "106829496",
    "subject": "HDFC Bank Limited has informed the Exchange regarding disclosure under Regulation 30 "
    "- Change in Managing Director and Chief Executive Officer of the Bank",
    "attachment_url": "https://nsearchives.nseindia.com/corporate/HDFCBANK_x.pdf",
}


def field(value, quote=None):
    return {"value": value, "quote": quote if quote is not None else value}


def joiner(**over):
    person = {
        "name": field("Mr. Anup Bagchi"),
        "role": field("MD & CEO", "assumed charge as MD & CEO of the Bank effective today"),
        "change": field("joins", "Mr. Anup Bagchi has assumed charge as MD & CEO of the Bank"),
        "effective_date": field("October 27, 2026", "effective today i.e. October 27, 2026"),
        "brief_profile": field(PROFILE),
    }
    return {**person, **over}


def leaver():
    return {
        "name": field("Mr. Sashidhar Jagdishan"),
        "role": field("MD & CEO", "vacated the office as the MD & CEO of the Bank"),
        "change": field("leaves", "has vacated the office as the MD & CEO of the Bank"),
        "effective_date": field("October 26, 2026", "close of business hours on October 26, 2026"),
        "brief_profile": {"value": None, "quote": None},
    }


def pdf(url, pages=3):
    return PdfText(url, LETTER, 2, "pdfplumber")


class FakeTavily(httpx.MockTransport):
    """Counts searches and refuses any other host: a profile page is never fetched."""

    def __init__(self, results=None, statuses=None):
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])
        self.results = results if results is not None else [
            {"url": "https://www.hdfc.bank.in/about-us/management", "title": "Management"},
            {"url": "https://in.linkedin.com/in/anup-bagchi-1a2b3c?trk=x", "title": "Anup Bagchi"},
            {"url": "https://www.linkedin.com/in/someone-else", "title": "Someone else"},
        ]  # fmt: skip

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            assert request.url.host == "api.tavily.com", f"unexpected fetch of {request.url}"
            status = self.statuses.pop(0) if self.statuses else 200
            if status != 200:
                return httpx.Response(status, headers={"retry-after": "1"}, json={})
            return httpx.Response(200, json={"results": self.results})

        super().__init__(handler)


def tavily(transport, store=None, key="tvly-test", sleeps=None):
    return pc.Tavily(
        store=store or MemoryStore(),
        client=httpx.Client(transport=transport),
        env={"TAVILY_API_KEY": key} if key else {},
        sleep=(sleeps if sleeps is not None else []).append,
    )


def build(replies, transport=None, **kw):
    router = ScriptedRouter(replies)
    t = transport or FakeTavily()
    deps = pc.Deps(router=router, tavily=kw.pop("tavily", None) or tavily(t), pdf=pdf)
    return pc.build_people_card(ROW, "HDFC Bank Limited", deps), router, t


# -- extraction ------------------------------------------------------------------------------------------


def test_one_entry_per_person_with_every_field_quoted_and_checked():
    card, router, _ = build([{"people": [joiner(), leaver()]}])
    f = card.fragment
    assert f["state"] == "ok" and f["card_type"] == "people" and f["company"] == "HDFC Bank Limited"
    assert [p["name"] for p in f["people"]] == ["Mr. Anup Bagchi", "Mr. Sashidhar Jagdishan"]
    new, old = f["people"]
    assert (new["change"], old["change"]) == ("joins", "leaves")
    assert new["role"] == "MD & CEO" and new["effective_date"] == "2026-10-27"
    assert old["effective_date"] == "2026-10-26" and old["brief_profile"] is None
    for person in f["people"]:
        assert person["evidence"]["name"]["quote_match"] >= 0.4
        assert all(e["quote"] and e["quote_match"] >= 0.4 for e in person["evidence"].values())
    assert len(router.calls) == 1 and router.calls[0][:2] == ("people", "lite")
    assert "Use only the provided text. Do not use outside knowledge." in router.calls[0][2]
    assert card.facts == [] and card.figures == {}  # a people card carries no figures


def test_the_brief_profile_shown_is_the_filings_own_text_that_passes_the_match():
    card, _, _ = build([{"people": [joiner()]}])
    new = card.fragment["people"][0]
    assert new["brief_profile"] == PROFILE
    assert new["evidence"]["brief_profile"]["quote_match"] == 1.0


def test_a_profile_the_model_wrote_itself_is_never_shown():
    invented = "Mr. Bagchi is widely regarded as a visionary leader who transformed ICICI Prudential."
    replies = [{"people": [joiner(brief_profile=field(invented))]}] * 2
    card, router, _ = build(replies)
    person = card.fragment["people"][0]
    assert person["brief_profile"] is None and "brief_profile" not in person["evidence"]
    assert person["name"] == "Mr. Anup Bagchi"  # the checked fields survive
    assert len(router.calls) == 2  # one retry, then dropped
    assert any("brief_profile" in d for d in card.fragment["dropped"])


def test_a_paraphrased_profile_under_the_display_bar_is_dropped_even_if_it_passes_0_4():
    half = (
        "Mr. Anup Bagchi is a graduate in commerce and a chartered accountant. He built the "
        "digital payments business and earlier ran a family office in Singapore for years."
    )
    from finn.normalise.quote import quote_match

    assert 0.4 <= quote_match(half, LETTER) < pc.PROFILE_MATCH
    card, _, _ = build([{"people": [joiner(brief_profile=field(half))]}] * 2)
    assert card.fragment["people"][0]["brief_profile"] is None


def test_a_field_whose_quote_is_not_in_the_text_is_retried_once_then_dropped():
    made_up = field("Chairman", "appointed as the non-executive Chairman of the board of the Bank")
    card, router, _ = build([{"people": [joiner(role=made_up)]}] * 2)
    person = card.fragment["people"][0]
    assert person["role"] is None and person["name"] == "Mr. Anup Bagchi"
    assert len(router.calls) == 2 and "rejected" in router.calls[1][2]


def test_the_retry_can_fix_a_field():
    bad = joiner(role=field("Chairman", "appointed as the non-executive Chairman of the board"))
    card, router, _ = build([{"people": [bad]}, {"people": [joiner()]}])
    assert card.fragment["people"][0]["role"] == "MD & CEO" and len(router.calls) == 2
    assert card.fragment["dropped"] == []


def test_a_value_that_is_not_inside_its_own_quote_is_dropped():
    card, _, _ = build([{"people": [joiner(role=field("Chief Risk Officer", "assumed charge as MD & CEO of the Bank"))]}] * 2)  # fmt: skip
    assert card.fragment["people"][0]["role"] is None


def test_a_person_whose_name_fails_the_quote_check_is_dropped_and_none_left_is_check_source():
    ghost = joiner(name=field("Ms. Priya Nair", "Ms. Priya Nair has been appointed as Chief Risk Officer"))
    card, _, _ = build([{"people": [ghost]}] * 2)
    assert card.fragment["state"] == "check_source" and card.fragment["people"] == []
    # with a second, real person the ghost is dropped and the real one stays
    card, _, _ = build([{"people": [ghost, leaver()]}] * 2)
    assert [p["name"] for p in card.fragment["people"]] == ["Mr. Sashidhar Jagdishan"]


def test_dates_are_parsed_in_code_and_an_unreadable_one_is_null():
    odd = joiner(effective_date=field("the first working day after approval", "effective today i.e. October 27, 2026"))  # fmt: skip
    card, _, _ = build([{"people": [odd]}, {"people": [odd]}])
    assert card.fragment["people"][0]["effective_date"] is None


def test_an_unknown_change_value_is_dropped():
    card, _, _ = build([{"people": [joiner(change=field("promoted", "assumed charge as MD & CEO"))]}] * 2)  # fmt: skip
    assert card.fragment["people"][0]["change"] is None


def test_no_attachment_text_is_check_source_without_a_model_call():
    router = ScriptedRouter([])
    deps = pc.Deps(router=router, tavily=tavily(FakeTavily()), pdf=lambda u, p=3: None)
    card = pc.build_people_card(ROW, "HDFC Bank Limited", deps)
    assert card.fragment["state"] == "check_source" and router.calls == []


def test_a_rate_limited_model_is_pending_not_a_guess():
    with pytest.raises(LLMPending):
        build([LLMPending("rate limited")])


# -- Tavily / LinkedIn ----------------------------------------------------------------------------------------


def test_one_search_per_joiner_with_the_exact_query_and_only_the_first_linkedin_in_url_is_kept():
    transport = FakeTavily()
    card, _, _ = build([{"people": [joiner(), leaver()]}], transport)
    assert len(transport.requests) == 1  # the leaver is not searched
    body = __import__("json").loads(transport.requests[0].content)
    assert body["query"] == "Anup Bagchi HDFC Bank LinkedIn"
    assert transport.requests[0].headers["authorization"] == "Bearer tvly-test"
    new, old = card.fragment["people"]
    assert new["linkedin_url"] == "https://in.linkedin.com/in/anup-bagchi-1a2b3c"  # no ?trk=x
    assert new["linkedin_state"] == "found" and old["linkedin_url"] is None
    assert old["linkedin_state"] == "not_searched"


def test_a_company_page_or_other_site_is_not_a_profile():
    transport = FakeTavily(results=[
        {"url": "https://www.linkedin.com/company/hdfc-bank"},
        {"url": "https://www.linkedin.com/posts/anup-bagchi_x"},
        {"url": "https://example.com/linkedin.com/in/anup"},
    ])  # fmt: skip
    card, _, _ = build([{"people": [joiner()]}], transport)
    p = card.fragment["people"][0]
    assert p["linkedin_url"] is None and p["linkedin_state"] == "none"


def test_the_same_person_is_never_searched_twice_even_across_runs():
    store = MemoryStore()
    first = FakeTavily()
    build([{"people": [joiner()]}], first, tavily=tavily(first, store))
    second = FakeTavily()
    card, _, _ = build([{"people": [joiner()]}], second, tavily=tavily(second, store))
    assert len(first.requests) == 1 and second.requests == []
    assert card.fragment["people"][0]["linkedin_url"].endswith("/in/anup-bagchi-1a2b3c")


def test_a_search_with_no_result_is_cached_too():
    store = MemoryStore()
    empty = FakeTavily(results=[])
    t = tavily(empty, store)
    assert t.linkedin("Anup Bagchi", "HDFC Bank Limited") == pc.LinkedIn(None, "none")
    assert t.linkedin("Anup Bagchi", "HDFC Bank Limited") == pc.LinkedIn(None, "none", True)
    assert len(empty.requests) == 1


def test_honorifics_and_company_suffix_are_left_out_of_the_query():
    assert pc.Tavily.query("Dr. Ravi Kumar", "Trent Limited") == "Ravi Kumar Trent LinkedIn"
    assert pc.Tavily.query("Mrs Kalpana Merchant", "Trent Ltd.") == "Kalpana Merchant Trent LinkedIn"


def test_a_429_waits_and_retries_once_then_gives_up_pending_and_does_not_cache():
    store, sleeps = MemoryStore(), []
    limited = FakeTavily(statuses=[429, 429])
    t = tavily(limited, store, sleeps=sleeps)
    assert t.linkedin("Anup Bagchi", "HDFC Bank").state == "pending"
    assert len(limited.requests) == 2 and sleeps == [1.0] and store.cache == {}
    # a 429 and then success is fine
    ok = FakeTavily(statuses=[429])
    assert tavily(ok, MemoryStore()).linkedin("Anup Bagchi", "HDFC Bank").state == "found"


def test_a_server_error_is_pending_not_a_loop():
    err = FakeTavily(statuses=[500])
    t = tavily(err)
    assert t.linkedin("Anup Bagchi", "HDFC Bank").state == "pending" and len(err.requests) == 1


def test_no_api_key_skips_the_search_and_says_so():
    transport = FakeTavily()
    card, _, _ = build([{"people": [joiner()]}], tavily=tavily(transport, key=None))
    assert card.fragment["people"][0]["linkedin_state"] == "no_api_key"
    assert transport.requests == [] and card.fragment["state"] == "ok"
