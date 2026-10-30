import json

import httpx
import pytest
from pydantic import BaseModel

from finn.llm.router import (
    ACTUAL_COST_INR,
    LLMConfigError,
    LLMOutputError,
    LLMPending,
    Router,
    Throttle,
    cache_key,
    load_models,
    paid_equivalent_inr,
)
from tests.llm_helpers import gemini_reply, groq_reply, limited, make_router


class Pick(BaseModel):
    answer: str


def is_groq(request: httpx.Request) -> bool:
    return "groq.com" in str(request.url)


def test_models_yaml_names_the_roles_and_only_real_ids():
    cfg = load_models()
    assert set(cfg["roles"]) == {"small", "reader", "judge", "lite", "failover"}
    assert cfg["roles"]["failover"] == {"provider": "groq", "model": "openai/gpt-oss-120b"}
    assert all(r["provider"] in ("groq", "gemini") for r in cfg["roles"].values())
    # a *-latest alias can move to a paid model, so none is allowed
    assert not any("latest" in r["model"] for r in cfg["roles"].values())
    for role, other in cfg["fallback"].items():  # failover always crosses providers
        assert cfg["roles"][role]["provider"] != cfg["roles"][other]["provider"]


def test_groq_call_returns_json_logs_a_row_and_never_sends_the_key_to_the_log():
    seen = []

    def handler(request):
        seen.append(request)
        return groq_reply('{"answer": "yes"}', tin=120, tout=30)

    router = make_router(handler)
    res = router.call("label", "small", "Is it?", Pick)
    assert res.data.answer == "yes" and res.cache_hit is False and res.model == "openai/gpt-oss-20b"
    [req] = seen
    assert req.headers["authorization"] == "Bearer gsk_test_secret"
    body = json.loads(req.content)
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    [row] = router.store.calls
    assert row == {
        "step": "label",
        "model": "openai/gpt-oss-20b",
        "tokens_in": 120,
        "tokens_out": 30,
        "latency_ms": row["latency_ms"],
        "cost_inr": pytest.approx(paid_equivalent_inr(load_models(), res.model, 120, 30)),
        "cache_hit": False,
    }
    assert row["cost_inr"] > 0 and ACTUAL_COST_INR == 0.0
    assert "gsk_test_secret" not in json.dumps(router.store.calls)


def test_gemini_call_uses_a_header_key_and_counts_thought_tokens():
    seen = []

    def handler(request):
        seen.append(request)
        return gemini_reply('{"answer": "ok"}', tin=50, tout=8, thoughts=40)

    router = make_router(handler)
    res = router.call("claims", "reader", "Read this", Pick)
    assert res.model == "gemini-3.8-flash" and (res.tokens_in, res.tokens_out) == (50, 48)
    [req] = seen
    assert "gem_test_secret" not in str(req.url)  # the key travels in a header, not the URL
    assert req.headers["x-goog-api-key"] == "gem_test_secret"
    assert json.loads(req.content)["generationConfig"]["responseMimeType"] == "application/json"


def test_second_identical_call_is_a_cache_hit_with_no_request_and_zero_cost():
    calls = []

    def handler(request):
        calls.append(1)
        return groq_reply('{"answer": "a"}')

    router = make_router(handler)
    first = router.call("label", "small", "same prompt", Pick)
    second = router.call("label", "small", "same prompt", Pick)
    assert len(calls) == 1 and second.cache_hit and second.data == first.data
    assert [c["cache_hit"] for c in router.store.calls] == [False, True]
    assert router.store.calls[1]["cost_inr"] == 0.0
    # keyed by sha256(model + prompt): another model does not share the entry
    assert cache_key("m1", "p") != cache_key("m2", "p")
    assert len(cache_key("m", "p")) == 64


def test_a_429_waits_for_retry_after_then_retries_once():
    answers = [limited(retry_after=3), groq_reply('{"answer": "late"}')]
    sleeps: list[float] = []
    router = make_router(lambda r: answers.pop(0), sleeps)
    assert router.call("label", "small", "p", Pick).data.answer == "late"
    assert any(3 <= s <= 4 for s in sleeps)  # backed off by the server's Retry-After


def test_two_429s_fail_over_to_the_other_free_provider_once():
    def handler(request):
        return limited(retry_after=1) if is_groq(request) else gemini_reply('{"answer": "g"}')

    router = make_router(handler)
    res = router.call("label", "small", "p", Pick)
    assert res.failed_over and res.role == "lite" and res.model == "gemini-3.5-flash-lite"
    assert [c["model"] for c in router.store.calls] == ["gemini-3.5-flash-lite"]


def test_a_daily_quota_429_does_not_sit_and_wait():
    hits = []

    def handler(request):
        hits.append(is_groq(request))
        return limited(retry_after=3600) if is_groq(request) else gemini_reply('{"answer": "g"}')

    sleeps: list[float] = []
    router = make_router(handler, sleeps)
    assert router.call("label", "small", "p", Pick).failed_over
    assert hits.count(True) == 1  # no retry on the exhausted provider
    assert not any(s > 30 for s in sleeps)


def test_both_providers_limited_marks_the_call_pending_and_stops():
    hits = []

    def handler(request):
        hits.append(1)
        return limited(retry_after=1)

    router = make_router(handler)
    with pytest.raises(LLMPending):
        router.call("label", "small", "p", Pick)
    assert len(hits) == 4  # each provider: one try and one retry. No loop.
    assert router.store.calls == []


def test_bad_json_is_retried_once_with_a_nudge_then_raises():
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return groq_reply("not json at all")

    router = make_router(handler)
    with pytest.raises(LLMOutputError):
        router.call("label", "small", "p", Pick)
    assert len(prompts) == 2 and "not valid JSON" in prompts[1]


def test_reply_that_breaks_the_schema_counts_as_bad_and_a_fenced_reply_is_fine():
    answers = [groq_reply('{"wrong": 1}'), groq_reply('```json\n{"answer": "fixed"}\n```')]
    router = make_router(lambda r: answers.pop(0))
    assert router.call("label", "small", "p", Pick).data.answer == "fixed"


def test_missing_key_or_role_is_a_config_error_not_a_pending_item():
    router = make_router(lambda r: groq_reply("{}"))
    router.env = {}
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        router.call("label", "small", "p")
    with pytest.raises(LLMConfigError, match="nope"):
        router.call("label", "nope", "p")


def test_http_error_message_carries_no_key():
    router = make_router(lambda r: httpx.Response(400, json={"error": {"message": "bad request"}}))
    with pytest.raises(Exception) as err:
        router.call("label", "small", "p")
    assert "gsk_test_secret" not in str(err.value) and "400" in str(err.value)


def test_throttle_spaces_calls_and_keeps_tokens_per_minute_under_the_limit():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    t = Throttle(
        {"p": {"min_gap_s": 2.0, "tokens_per_minute": 1000}}, clock=lambda: now[0], sleep=sleep
    )
    t.wait("p", 100)
    t.spent("p", 900)
    t.wait("p", 100)  # 900 + 100 fits but the 2 s gap does not
    assert sleeps == [2.0]
    t.spent("p", 100)
    t.wait("p", 500)  # 1000 already used in the window: wait for the first entry to age out
    assert now[0] >= 60


def test_paid_equivalent_is_an_estimate_in_inr_for_unknown_models_too():
    cfg = load_models()
    known = paid_equivalent_inr(cfg, "openai/gpt-oss-20b", 1_000_000, 0)
    assert known == pytest.approx(0.075 * cfg["usd_to_inr"])
    assert paid_equivalent_inr(cfg, "something-new", 1_000_000, 0) == pytest.approx(
        0.30 * cfg["usd_to_inr"]
    )


def test_router_default_construction_needs_no_network():
    assert isinstance(Router(config=load_models()), Router)


def test_a_503_overload_is_treated_like_a_rate_limit_and_fails_over():
    def handler(request):
        if is_groq(request):
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return gemini_reply('{"answer": "g"}')

    router = make_router(handler)
    assert router.call("label", "small", "p", Pick).failed_over
