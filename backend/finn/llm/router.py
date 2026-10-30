"""Every LLM call goes through call(step, role, prompt, schema=None).

* Roles, not models: config/models.yaml maps role -> provider + model ID.
* FREE TIER ONLY. The actual cost of a call is always zero; llm_calls.cost_inr is a
  *paid-equivalent* estimate of what the same tokens would cost on a paid plan.
* Cached by sha256(model + prompt) in the `cache` table (and an in-process dict in front of it).
* A row in llm_calls for every call that returned: step, model, tokens in and out, latency,
  paid-equivalent cost in INR, cache hit.
* Rate limits are normal (a 5xx overload counts as one). On HTTP 429 the call waits (Retry-After,
  capped) and retries once; still 429, or a wait longer than the cap (a daily quota), it fails over
  to the other free provider's role once; still nothing and LLMPending is raised so the caller marks
  the item pending and moves on. Nothing loops.
* Output is JSON. A reply that is not valid JSON, or does not fit `schema`, is retried once with a
  nudge and then raises LLMOutputError.

Keys come from GROQ_API_KEY / GEMINI_API_KEY and are sent in headers only; they never reach a log,
an exception message or the cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import yaml
from pydantic import BaseModel

from finn.config import CONFIG_DIR

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ACTUAL_COST_INR = 0.0  # the free-tier rule: what FINN really pays
DEFAULT_MAX_OUTPUT = 1024
TIMEOUT_S = 60.0
BUSY_STATUS = {500, 502, 503, 504}  # a provider that is down or overloaded is treated as limited


class LLMError(RuntimeError):
    """Base class. Messages never contain a key."""


class LLMConfigError(LLMError):
    """A key or a role is missing."""


class LLMPending(LLMError):
    """Rate-limited on both free providers: mark the item pending and carry on."""


class LLMOutputError(LLMError):
    """The model answered but not with the JSON we asked for (after the one retry)."""


class RateLimited(LLMError):
    def __init__(self, wait_s: float | None):
        super().__init__("rate limited (HTTP 429)")
        self.wait_s = wait_s


@dataclass
class LLMResult:
    data: Any  # parsed JSON (a dict, or the pydantic model when `schema` is a model class)
    text: str
    model: str
    role: str
    cache_hit: bool
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_inr: float  # paid-equivalent estimate; the actual cost is ACTUAL_COST_INR (zero)
    failed_over: bool = False


# -- config ------------------------------------------------------------------------------------


def load_models(path=None) -> dict:
    return yaml.safe_load((path or CONFIG_DIR / "models.yaml").read_text())


def paid_equivalent_inr(config: dict, model: str, tokens_in: int, tokens_out: int) -> float:
    """What these tokens would cost on a paid plan, in INR. FINN does not pay this."""
    prices = config["paid_equivalent_usd_per_mtok"]
    p = prices.get(model) or prices["default"]
    usd = (tokens_in * p["in"] + tokens_out * p["out"]) / 1_000_000
    return round(usd * config["usd_to_inr"], 6)


def cache_key(model: str, prompt: str) -> str:
    return hashlib.sha256((model + prompt).encode("utf-8")).hexdigest()


# -- stores ------------------------------------------------------------------------------------


class Store(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...
    def log(self, row: dict) -> None: ...


class MemoryStore:
    """Cache and call log in process: tests, and runs without Supabase."""

    def __init__(self) -> None:
        self.cache: dict[str, dict] = {}
        self.calls: list[dict] = []

    def get(self, key):
        return self.cache.get(key)

    def put(self, key, value):
        self.cache[key] = value

    def log(self, row):
        self.calls.append(row)


class DbStore(MemoryStore):
    """`cache` and `llm_calls` tables over supabase-py. A database error never fails a call."""

    def __init__(self, db) -> None:
        super().__init__()
        self.db = db

    def get(self, key):
        if key in self.cache:
            return self.cache[key]
        try:
            rows = self.db.client.table("cache").select("value").eq("key", key).execute().data
        except Exception as exc:  # network or API trouble: behave as a miss
            log.warning("cache read failed: %s", type(exc).__name__)
            return None
        if rows:
            self.cache[key] = rows[0]["value"]
            return rows[0]["value"]
        return None

    def put(self, key, value):
        self.cache[key] = value
        try:
            self.db.upsert("cache", [{"key": key, "value": value}])
        except Exception as exc:
            log.warning("cache write failed: %s", type(exc).__name__)

    def log(self, row):
        self.calls.append(row)
        try:
            self.db.insert("llm_calls", [row])
        except Exception as exc:
            log.warning("llm_calls write failed: %s", type(exc).__name__)


# -- throttle ----------------------------------------------------------------------------------


class Throttle:
    """Spaces calls and keeps tokens per minute under the free-tier limit, per provider."""

    def __init__(self, limits: dict, clock=time.monotonic, sleep=time.sleep):
        self.limits, self.clock, self.sleep = limits, clock, sleep
        self._last: dict[str, float] = {}
        self._window: dict[str, deque] = {}
        self._lock = threading.Lock()

    def wait(self, provider: str, est_tokens: int) -> None:
        lim = self.limits[provider]
        with self._lock:
            now = self.clock()
            gap = self._last.get(provider, -1e9) + lim["min_gap_s"] - now
            window = self._window.setdefault(provider, deque())
            cap = lim["tokens_per_minute"]
            need = min(est_tokens, cap)
            while window and window[0][0] <= now - 60:
                window.popleft()
            used = sum(t for _, t in window)
            if used + need > cap:  # wait until enough of the window has aged out
                freed, release = used, 0.0
                for ts, tokens in window:
                    freed -= tokens
                    release = ts + 60 - now
                    if freed + need <= cap:
                        break
                gap = max(gap, release)
            if gap > 0:
                self.sleep(gap)
            self._last[provider] = self.clock()

    def spent(self, provider: str, tokens: int) -> None:
        with self._lock:
            self._window.setdefault(provider, deque()).append((self.clock(), tokens))


# -- the router --------------------------------------------------------------------------------


class Router:
    def __init__(
        self,
        config: dict | None = None,
        store: Store | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        env: dict | None = None,
    ):
        self.config = config or load_models()
        self.store = store or MemoryStore()
        self.client = client or httpx.Client(timeout=TIMEOUT_S)
        self.sleep = sleep
        self.env = os.environ if env is None else env
        self.throttle = Throttle(self.config["limits"], sleep=sleep)

    # -- public ----

    def call(
        self,
        step: str,
        role: str,
        prompt: str,
        schema: type[BaseModel] | dict | None = None,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT,
    ) -> LLMResult:
        """JSON-only call. `schema` is a pydantic model class (validated) or a JSON-schema dict
        (shown to the model; only the JSON shape is checked)."""
        full_prompt = prompt + _schema_note(schema)
        try:
            return self._on_role(step, role, full_prompt, schema, max_output_tokens, False)
        except RateLimited:
            pass
        fallback = self.config.get("fallback", {}).get(role)
        if fallback and fallback != role:
            log.warning("%s: %s is rate limited, failing over to %s", step, role, fallback)
            try:
                return self._on_role(step, fallback, full_prompt, schema, max_output_tokens, True)
            except RateLimited:
                pass
        raise LLMPending(f"{step}: both free providers are rate limited; marked pending")

    # -- one role: cache, throttle, send, parse ----

    def _on_role(self, step, role, prompt, schema, max_out, failed_over) -> LLMResult:
        spec = self._role(role)
        model = spec["model"]
        key = cache_key(model, prompt)
        hit = self.store.get(key)
        if hit is not None:
            parsed = _parse(hit["text"], schema)  # a cached reply was valid when stored
            return self._record(
                step,
                role,
                model,
                parsed,
                hit["text"],
                True,
                hit["tokens_in"],
                hit["tokens_out"],
                0,
                failed_over,
            )

        nudge = ""
        for attempt in (1, 2):
            started = time.monotonic()
            text, tin, tout = self._send_with_retry(spec, prompt + nudge, max_out)
            latency = int((time.monotonic() - started) * 1000)
            try:
                parsed = _parse(text, schema)
            except ValueError as exc:  # pydantic's ValidationError is a ValueError
                log.warning(
                    "%s: %s gave unusable output (attempt %d): %s",
                    step,
                    model,
                    attempt,
                    str(exc)[:120],
                )
                self._record(
                    step,
                    role,
                    model,
                    None,
                    text,
                    False,
                    tin,
                    tout,
                    latency,
                    failed_over,
                    keep=False,
                )
                if attempt == 2:
                    raise LLMOutputError(
                        f"{step}: {model} did not return the JSON asked for"
                    ) from exc
                nudge = (
                    "\n\nYour last reply was not valid JSON for this schema. "
                    "Reply with the JSON object only."
                )
                continue
            self.store.put(key, {"text": text, "tokens_in": tin, "tokens_out": tout})
            return self._record(
                step, role, model, parsed, text, False, tin, tout, latency, failed_over
            )
        raise AssertionError("unreachable")

    def _record(
        self, step, role, model, parsed, text, hit, tin, tout, latency, failed_over, keep=True
    ) -> LLMResult:
        cost = 0.0 if hit else paid_equivalent_inr(self.config, model, tin, tout)
        self.store.log(
            {
                "step": step,
                "model": model,
                "tokens_in": tin,
                "tokens_out": tout,
                "latency_ms": latency,
                "cost_inr": cost,  # PAID-EQUIVALENT estimate; actual cost is zero (free tier)
                "cache_hit": hit,
            }
        )
        return LLMResult(parsed, text, model, role, hit, tin, tout, latency, cost, failed_over)

    def _role(self, role: str) -> dict:
        try:
            return self.config["roles"][role]
        except KeyError as exc:
            raise LLMConfigError(f"no role {role!r} in config/models.yaml") from exc

    # -- sending ----

    def _send_with_retry(self, spec: dict, prompt: str, max_out: int):
        provider, model = spec["provider"], spec["model"]
        cap = self.config["limits"][provider]["max_wait_s"]
        try:
            return self._send(spec, prompt, max_out)
        except RateLimited as first:
            wait = first.wait_s if first.wait_s is not None else 10.0
            if wait > cap:  # a daily quota, not a blip: do not sit here
                raise
            log.warning("%s rate limited; waiting %.1fs then retrying once", model, wait)
            self.sleep(wait + 0.5)
        return self._send(spec, prompt, max_out)

    def _send(self, spec: dict, prompt: str, max_out: int):
        provider, model = spec["provider"], spec["model"]
        est = len(prompt) // 3 + max_out
        self.throttle.wait(provider, est)
        if provider == "groq":
            text, tin, tout = self._groq(model, prompt, max_out)
        elif provider == "gemini":
            text, tin, tout = self._gemini(model, prompt, max_out)
        else:
            raise LLMConfigError(f"unknown provider {provider!r}")
        self.throttle.spent(provider, tin + tout)
        return text, tin, tout

    def _key(self, name: str) -> str:
        value = self.env.get(name)
        if not value:
            raise LLMConfigError(f"{name} is not set")
        return value

    def _groq(self, model, prompt, max_out):
        body: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_completion_tokens": max_out,
        }
        if effort := self.config.get("options", {}).get(model, {}).get("reasoning_effort"):
            body["reasoning_effort"] = effort
        resp = self._post(GROQ_URL, {"Authorization": f"Bearer {self._key('GROQ_API_KEY')}"}, body)
        data = _json_body(resp)
        try:
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{model}: unexpected reply shape") from exc
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    def _gemini(self, model, prompt, max_out):
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0,
                "maxOutputTokens": max_out,
            },
        }
        resp = self._post(
            GEMINI_URL.format(model=model), {"x-goog-api-key": self._key("GEMINI_API_KEY")}, body
        )
        data = _json_body(resp)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
            raise LLMError(f"{model}: empty reply ({reason})") from exc
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        tout = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
        return text, tin, tout

    def _post(self, url: str, headers: dict, body: dict) -> httpx.Response:
        try:
            resp = self.client.post(url, headers=headers, json=body, timeout=TIMEOUT_S)
        except httpx.TimeoutException as exc:
            raise LLMError(f"timeout after {TIMEOUT_S:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"transport error: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            raise RateLimited(_retry_after(resp))
        if resp.status_code in BUSY_STATUS:  # overloaded: handled like a rate limit
            raise RateLimited(_retry_after(resp) or 5.0)
        if resp.status_code >= 400:
            message = ""
            try:
                err = resp.json().get("error")
                message = err.get("message", "") if isinstance(err, dict) else str(err or "")
            except ValueError:
                pass
            raise LLMError(f"HTTP {resp.status_code}: {message[:200]}")
        return resp


# -- helpers -----------------------------------------------------------------------------------


def _json_body(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError("reply was not JSON") from exc
    if not isinstance(data, dict):
        raise LLMError("reply was not a JSON object")
    return data


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    try:  # Groq: "Please try again in 7.66s" / "in 1m3.2s"; Gemini: "retryDelay": "12s"
        text = json.dumps(resp.json())
    except ValueError:
        return None
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", text) or re.search(
        r"retryDelay\W+()([\d.]+)s", text
    )
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return None


def _schema_note(schema) -> str:
    if schema is None:
        return "\n\nReply with a single JSON object and nothing else."
    if isinstance(schema, dict):
        shape = schema
    else:
        shape = schema.model_json_schema()
    return (
        "\n\nReply with a single JSON object and nothing else. It must match this JSON schema:\n"
        + json.dumps(shape, separators=(",", ":"), sort_keys=True)
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse(text: str, schema):
    cleaned = _FENCE.sub("", (text or "").strip())
    data = json.loads(cleaned)  # ValueError on bad JSON
    if not isinstance(data, dict):
        raise ValueError("JSON reply is not an object")
    if schema is not None and not isinstance(schema, dict):
        return schema.model_validate(data)
    return data


_default: Router | None = None


def default_router() -> Router:
    """The process-wide router. Wires the cache and llm_calls to Supabase when it is configured."""
    global _default
    if _default is None:
        store: Store = MemoryStore()
        if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
            from finn.db.client import Db

            store = DbStore(Db.service())
        _default = Router(store=store)
    return _default


def call(step, role, prompt, schema=None, **kw) -> LLMResult:
    return default_router().call(step, role, prompt, schema, **kw)
