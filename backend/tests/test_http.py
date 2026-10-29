import httpx
import pytest

from finn.sources.http import DiskCache, HttpClient, SourceBlocked, SourceError


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make(tmp_path, handler, clock=None, **kw):
    clock = clock or Clock()
    client = HttpClient(
        cache=DiskCache(tmp_path, clock=clock.time),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock.time,
        **kw,
    )
    return client, clock


def test_cache_hit_within_ttl_and_expiry(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, text="body")

    client, clock = make(tmp_path, handler, spacing={})
    assert not client.get("https://x.test/a", {"q": 1}, ttl=60).from_cache
    assert client.get("https://x.test/a", {"q": 1}, ttl=60).from_cache
    assert len(calls) == 1
    clock.now += 61
    assert not client.get("https://x.test/a", {"q": 1}, ttl=60).from_cache
    assert len(calls) == 2


def test_cache_key_covers_params_and_ttl_zero_bypasses(tmp_path):
    calls = []
    client, _ = make(
        tmp_path, lambda r: calls.append(1) or httpx.Response(200, text="b"), spacing={}
    )
    client.get("https://x.test/a", {"q": 1}, ttl=60)
    client.get("https://x.test/a", {"q": 2}, ttl=60)
    assert len(calls) == 2
    client.get("https://x.test/a", {"q": 1}, ttl=0)  # no read
    assert len(calls) == 3
    assert list(tmp_path.glob("*.bin"))  # earlier ttl>0 calls were written
    assert DiskCache.key("u", {"b": 1, "a": 2}) == DiskCache.key("u", {"a": 2, "b": 1})


def test_errors_are_not_cached(tmp_path):
    client, _ = make(tmp_path, lambda r: httpx.Response(500, text="x"), spacing={})
    with pytest.raises(SourceError) as exc:
        client.get("https://x.test/a", ttl=60)
    assert exc.value.status == 500
    assert not list(tmp_path.glob("*.bin"))


def test_spacing_nse_and_screener(tmp_path):
    client, clock = make(tmp_path, lambda r: httpx.Response(200, text="ok"))
    for _ in range(3):
        client.get("https://www.nseindia.com/api/x")
    assert clock.slept == pytest.approx([0.35, 0.35])
    clock.slept.clear()
    client.get("https://www.screener.in/a")
    client.get("https://www.screener.in/b")
    assert clock.slept == pytest.approx([2.0])
    clock.slept.clear()
    client.get("https://other.test/a")
    client.get("https://other.test/b")
    assert clock.slept == []


def test_one_retry_on_403_runs_hook_then_succeeds(tmp_path):
    seen = []

    def handler(req):
        seen.append(1)
        return httpx.Response(403 if len(seen) == 1 else 200, text="ok")

    client, _ = make(tmp_path, handler, spacing={})
    hook = []
    resp = client.get("https://x.test/a", on_retry=lambda: hook.append(1))
    assert resp.status == 200 and len(seen) == 2 and hook == [1]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_second_refusal_raises_blocked(tmp_path, status):
    n = []
    client, _ = make(tmp_path, lambda r: n.append(1) or httpx.Response(status), spacing={})
    with pytest.raises(SourceBlocked) as exc:
        client.get("https://x.test/a", raise_for_status=False)
    assert len(n) == 2 and exc.value.status == status


def test_429_honours_capped_retry_after(tmp_path):
    seen = []

    def handler(req):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "999"})
        return httpx.Response(200, text="ok")

    client, clock = make(tmp_path, handler, spacing={})
    client.get("https://x.test/a")
    assert clock.slept == [10.0]


def test_transport_error_retried_once(tmp_path):
    seen = []

    def handler(req):
        seen.append(1)
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, text="ok")

    client, _ = make(tmp_path, handler, spacing={})
    assert client.get("https://x.test/a").status == 200


def test_404_is_a_plain_source_error_with_status(tmp_path):
    client, _ = make(tmp_path, lambda r: httpx.Response(404), spacing={})
    with pytest.raises(SourceError) as exc:
        client.get("https://x.test/a")
    assert not isinstance(exc.value, SourceBlocked) and exc.value.status == 404


def test_html_block_page_is_not_json(tmp_path):
    client, _ = make(
        tmp_path, lambda r: httpx.Response(200, text="<html>Access Denied"), spacing={}
    )
    with pytest.raises(SourceBlocked):
        client.get("https://x.test/a").json()


def test_browser_headers_sent(tmp_path):
    got = {}
    client, _ = make(tmp_path, lambda r: got.update(r.headers) or httpx.Response(200), spacing={})
    client.get("https://x.test/a")
    assert "Chrome" in got["user-agent"] and got["accept-language"].startswith("en")
