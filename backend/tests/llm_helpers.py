"""Fakes for the LLM layer. No test ever reaches a real model."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from finn.llm.router import LLMPending, MemoryStore, Router, load_models


class ScriptedRouter:
    """Stands in for finn.llm.router.Router: replies come from a script, calls are recorded.

    `replies` is a list of dicts (or exceptions to raise) used in order, or a function
    (step, prompt) -> dict.
    """

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[str, str, str]] = []
        self.store = MemoryStore()

    def call(self, step, role, prompt, schema=None, **kw):
        self.calls.append((step, role, prompt))
        reply = self.replies(step, prompt) if callable(self.replies) else self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        data = schema.model_validate(reply) if schema is not None else reply
        self.store.log(
            {
                "step": step,
                "model": "fake",
                "tokens_in": 10,
                "tokens_out": 5,
                "latency_ms": 1,
                "cost_inr": 0.001,
                "cache_hit": False,
            }
        )
        return SimpleNamespace(data=data, text=str(reply), cache_hit=False)


def pending():
    return LLMPending("rate limited on both free providers")


def groq_reply(content: str, tin=20, tout=10) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout},
        },
    )


def gemini_reply(content: str, tin=20, tout=10, thoughts=0) -> httpx.Response:
    usage = {"promptTokenCount": tin, "candidatesTokenCount": tout}
    if thoughts:
        usage["thoughtsTokenCount"] = thoughts
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": content}]}}], "usageMetadata": usage},
    )


def limited(retry_after=None, body=None) -> httpx.Response:
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return httpx.Response(429, headers=headers, json=body or {"error": {"message": "slow down"}})


def make_router(handler, sleeps=None, store=None) -> Router:
    """A real Router over a mock transport. handler(request) -> Response."""
    sleeps = sleeps if sleeps is not None else []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Router(
        config=load_models(),
        store=store or MemoryStore(),
        client=client,
        sleep=sleeps.append,
        env={"GROQ_API_KEY": "gsk_test_secret", "GEMINI_API_KEY": "gem_test_secret"},
    )
