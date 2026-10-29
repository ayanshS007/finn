"""BSE announcements via the `bse` package (symbol -> scrip code, announcements by scrip code)."""

from __future__ import annotations

import json
import time
from datetime import date, datetime

from bse import BSE

from finn.config import cache_dir
from finn.sources.http import DiskCache, Fetched, SourceError
from finn.sources.shapes import announcement, clean, ist_iso

ATTACHMENT_URL = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname={name}"
TTL_SCRIP = 30 * 24 * 3600
TTL_ANNOUNCEMENTS = 300
MAX_PAGES = 20
PAGE_GAP = 0.5  # seconds between pages


def _cached(cache: DiskCache, name: str, params: dict, ttl: float, fetch):
    key = cache.key(f"bse://{name}", params)
    if hit := cache.read(key, ttl):
        return json.loads(hit.content)
    value = fetch()
    cache.write(key, Fetched(f"bse://{name}", 200, json.dumps(value).encode(), "application/json"))
    return value


def scrip_code(symbol: str, *, ttl: float = TTL_SCRIP, cache: DiskCache | None = None) -> str:
    def fetch() -> str:
        with BSE(download_folder=cache_dir() / "bse") as bse:
            try:
                return str(bse.getScripCode(symbol))
            except ValueError as exc:
                raise SourceError(f"BSE has no scrip code for {symbol}") from exc

    return _cached(cache or DiskCache(), "scrip", {"symbol": symbol}, ttl, fetch)


def announcements_raw(
    scripcode: str,
    from_date: date | datetime,
    to_date: date | datetime,
    *,
    ttl: float = TTL_ANNOUNCEMENTS,
    cache: DiskCache | None = None,
) -> list[dict]:
    start = (
        datetime.combine(from_date, datetime.min.time()) if type(from_date) is date else from_date
    )
    end = datetime.combine(to_date, datetime.min.time()) if type(to_date) is date else to_date

    def fetch() -> list[dict]:
        rows: list[dict] = []
        with BSE(download_folder=cache_dir() / "bse") as bse:
            for page in range(1, MAX_PAGES + 1):
                reply = bse.announcements(
                    page_no=page, from_date=start, to_date=end, scripcode=scripcode
                )
                table = reply.get("Table") or []
                rows += table
                total = (reply.get("Table1") or [{}])[0].get("ROWCNT")
                if not table or (total is not None and len(rows) >= total):
                    break
                time.sleep(PAGE_GAP)
        return rows

    params = {"scrip": scripcode, "from": start.isoformat(), "to": end.isoformat()}
    return _cached(cache or DiskCache(), "announcements", params, ttl, fetch)


def announcements(
    scripcode: str,
    from_date: date | datetime,
    to_date: date | datetime,
    *,
    symbol: str | None = None,
    ttl: float = TTL_ANNOUNCEMENTS,
    cache: DiskCache | None = None,
) -> list[dict]:
    return parse_announcements(
        announcements_raw(scripcode, from_date, to_date, ttl=ttl, cache=cache), symbol
    )


def parse_announcements(raw: list[dict], symbol: str | None = None) -> list[dict]:
    """Same dict shape as nse.announcements(), exchange='BSE'. `symbol` comes from the caller."""
    out = []
    for r in raw:
        subject, detail = clean(r.get("NEWSSUB")), clean(r.get("HEADLINE"))
        text = subject if not detail or detail == subject else f"{subject}: {detail}"
        name = clean(r.get("ATTACHMENTNAME"))
        out.append(
            announcement(
                exchange="BSE",
                symbol=symbol,
                company=clean(r.get("SLONGNAME")),
                category=clean(r.get("CATEGORYNAME")),
                subcategory=clean(r.get("SUBCATNAME")),
                text=text,
                attachment_url=ATTACHMENT_URL.format(name=name) if name else None,
                published_at=ist_iso(
                    (r.get("DissemDT") or r.get("DT_TM") or "").split(".")[0], "%Y-%m-%dT%H:%M:%S"
                ),
                source_id=clean(r.get("NEWSID")),
                has_xbrl=False,
            )
        )
    return out
