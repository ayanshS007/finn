"""Shared httpx client: browser headers, on-disk cache, polite per-host spacing, one retry."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from finn.config import cache_dir

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Minimum seconds between two requests to the same host.
HOST_SPACING = {
    "www.nseindia.com": 0.35,
    "nsearchives.nseindia.com": 0.35,
    "www.screener.in": 2.0,
}

RETRY_STATUS = {401, 403, 429}
MAX_RETRY_AFTER = 10.0


class SourceError(Exception):
    """A source failed or changed shape. Callers report it; they never substitute data."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SourceBlocked(SourceError):
    """The source refused us (401/403/429 after the retry) or served a block page."""


@dataclass
class Fetched:
    url: str
    status: int
    content: bytes
    content_type: str = ""
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except ValueError as exc:
            head = self.text[:80].replace("\n", " ")
            raise SourceBlocked(f"{self.url}: expected JSON, got {head!r}", self.status) from exc


class DiskCache:
    """Bytes + small metadata on disk, keyed by sha256 of the request. TTL is checked on read."""

    def __init__(self, root: Path | None = None, clock: Callable[[], float] = time.time):
        self.root = Path(root) if root else cache_dir()
        self.clock = clock

    @staticmethod
    def key(url: str, params: Mapping[str, Any] | None = None) -> str:
        blob = json.dumps([url, sorted((params or {}).items())], default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def read(self, key: str, ttl: float) -> Fetched | None:
        meta_path = self.root / f"{key}.json"
        body_path = self.root / f"{key}.bin"
        if ttl <= 0 or not meta_path.exists() or not body_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
            if self.clock() - meta["fetched_at"] > ttl:
                return None
            return Fetched(
                meta["url"], meta["status"], body_path.read_bytes(), meta["content_type"], True
            )
        except (ValueError, KeyError, OSError):
            return None

    def write(self, key: str, fetched: Fetched) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        meta = {
            "url": fetched.url,
            "status": fetched.status,
            "content_type": fetched.content_type,
            "fetched_at": self.clock(),
        }
        (self.root / f"{key}.bin").write_bytes(fetched.content)
        (self.root / f"{key}.json").write_text(json.dumps(meta))


class HttpClient:
    def __init__(
        self,
        cache: DiskCache | None = None,
        spacing: Mapping[str, float] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cache = cache or DiskCache()
        self.spacing = dict(HOST_SPACING if spacing is None else spacing)
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        # HTTP/1.1 on purpose: NSE's edge resets HTTP/2 streams from some networks.
        self._client = httpx.Client(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            transport=transport,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    def close(self) -> None:
        self._client.close()

    def _space(self, host: str) -> None:
        gap = self.spacing.get(host, 0.0)
        if gap <= 0:
            return
        with self._lock:
            now = self._clock()
            start = max(now, self._last.get(host, float("-inf")) + gap)
            self._last[host] = start
        if start > now:
            self._sleep(start - now)

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        ttl: float = 0,
        headers: Mapping[str, str] | None = None,
        before_network: Callable[[], None] | None = None,
        on_retry: Callable[[], None] | None = None,
        raise_for_status: bool = True,
    ) -> Fetched:
        """GET with cache. ttl is in seconds; ttl <= 0 neither reads nor writes the cache.

        before_network runs once before the first real request (not on a cache hit);
        on_retry runs before the single retry on 401/403/429 or a transport error.
        Only 200 responses are cached.
        """
        key = self.cache.key(url, params)
        if ttl > 0 and (hit := self.cache.read(key, ttl)):
            return hit

        host = urlparse(url).netloc
        if before_network:
            before_network()
        fetched: Fetched | None = None
        for attempt in (0, 1):
            self._space(host)
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt == 0:
                    log.warning("GET %s failed (%s); retrying once", url, exc)
                    if on_retry:
                        on_retry()
                    continue
                raise SourceError(f"{url}: {exc!r}") from exc
            if resp.status_code in RETRY_STATUS and attempt == 0:
                log.warning("GET %s -> %s; retrying once", url, resp.status_code)
                if resp.status_code == 429:
                    self._sleep(_retry_after(resp))
                if on_retry:
                    on_retry()
                continue
            fetched = Fetched(
                str(resp.url), resp.status_code, resp.content, resp.headers.get("content-type", "")
            )
            break
        assert fetched is not None
        if fetched.status in RETRY_STATUS:
            raise SourceBlocked(f"{url}: HTTP {fetched.status} after retry", fetched.status)
        if raise_for_status and fetched.status >= 400:
            raise SourceError(f"{url}: HTTP {fetched.status}", fetched.status)
        if ttl > 0 and fetched.status == 200:
            self.cache.write(key, fetched)
        return fetched


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "2")), MAX_RETRY_AFTER)
    except ValueError:
        return 2.0


_shared: HttpClient | None = None


def shared_client() -> HttpClient:
    """Process-wide client so host spacing and NSE cookies are shared between modules."""
    global _shared
    if _shared is None:
        _shared = HttpClient()
    return _shared
