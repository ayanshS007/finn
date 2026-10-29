"""NSE corporate announcements RSS.

URL found on https://www.nseindia.com/static/rss-feed (served from nsearchives.nseindia.com).
Items carry only company name, attachment link, text and pubDate - no symbol and no id - so the
symbol is filled only from an exact match: a caller-supplied company-name map, or the attachment
file name prefix when it is one of the caller's known symbols. Otherwise it stays None.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Mapping

from finn.sources.http import HttpClient, SourceError, shared_client
from finn.sources.shapes import announcement, clean, ist_iso

FEED_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
TTL_FEED = 120


def fetch(
    known_symbols: Collection[str] = (),
    name_to_symbol: Mapping[str, str] | None = None,
    *,
    ttl: float = TTL_FEED,
    http: HttpClient | None = None,
) -> list[dict]:
    resp = (http or shared_client()).get(FEED_URL, ttl=ttl)
    return parse_feed(resp.content, known_symbols, name_to_symbol)


def parse_feed(
    xml_bytes: bytes,
    known_symbols: Collection[str] = (),
    name_to_symbol: Mapping[str, str] | None = None,
) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SourceError(f"RSS is not XML: {xml_bytes[:60]!r}") from exc
    channel = root.find("channel")
    if channel is None:
        raise SourceError("RSS has no <channel>")
    names = {k.strip().lower(): v for k, v in (name_to_symbol or {}).items()}
    known = set(known_symbols)
    out = []
    for item in channel.findall("item"):
        company = clean(item.findtext("title"))
        link = clean(item.findtext("link"))
        text, _, subject = (item.findtext("description") or "").partition("|SUBJECT:")
        pub = clean(item.findtext("pubDate"))
        symbol = names.get((company or "").lower()) or _symbol_from_link(link, known)
        out.append(
            announcement(
                exchange="NSE",
                symbol=symbol,
                company=company,
                category=clean(subject),
                subcategory=None,
                text=clean(text),
                attachment_url=link,
                published_at=ist_iso(pub, "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"),
                source_id=_item_id(company, link, pub),
                has_xbrl=bool(link and "/xbrl/" in link),
            )
        )
    return out


def _symbol_from_link(link: str | None, known: set[str]) -> str | None:
    match = re.search(r"/corporate/([^/_]+)_", link or "")
    return match.group(1) if match and match.group(1) in known else None


def _item_id(company: str | None, link: str | None, pub: str | None) -> str:
    """RSS has no id; a stable hash of the item keeps dedupe working."""
    return "rss-" + hashlib.sha1(f"{company}|{link}|{pub}".encode()).hexdigest()[:16]
