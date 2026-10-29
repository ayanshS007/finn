import pytest

from finn.sources import nse_rss
from finn.sources.http import SourceError
from finn.sources.shapes import ANNOUNCEMENT_KEYS


@pytest.fixture
def feed(fixtures):
    return (fixtures / "nse_rss_online_announcements.xml").read_bytes()


def test_same_shape_as_announcements(feed):
    rows = nse_rss.parse_feed(feed)
    assert len(rows) == 60
    for r in rows:
        assert tuple(r) == ANNOUNCEMENT_KEYS and r["exchange"] == "NSE"
        assert r["company"] and r["published_at"].endswith("+05:30")
    # The feed itself repeats some items verbatim (Nashik Municipal Corporation, twice);
    # identical items share an id so dedupe downstream is a plain key match.
    distinct = {(r["company"], r["attachment_url"], r["published_at"]) for r in rows}
    assert len({r["source_id"] for r in rows}) == len(distinct) == 59


def test_subject_split_into_category_and_text(feed):
    r = next(r for r in nse_rss.parse_feed(feed) if r["category"] == "Outcome of Board Meeting")
    assert "|SUBJECT" not in r["text"] and r["attachment_url"].startswith("https://")


def test_symbol_only_from_exact_matches(feed):
    assert all(r["symbol"] is None for r in nse_rss.parse_feed(feed))
    rows = nse_rss.parse_feed(feed, known_symbols={"STRIDES", "CAPITALSFB"})
    assert {r["symbol"] for r in rows if r["symbol"]} == {"STRIDES", "CAPITALSFB"}
    by_name = nse_rss.parse_feed(
        feed, name_to_symbol={"Capital Small Finance Bank Limited": "CAPITALSFB"}
    )
    assert {r["symbol"] for r in by_name if r["symbol"]} == {"CAPITALSFB"}


def test_xbrl_flag(feed):
    rows = nse_rss.parse_feed(feed)
    assert any(r["has_xbrl"] for r in rows) and not all(r["has_xbrl"] for r in rows)


def test_non_xml_is_a_source_error():
    with pytest.raises(SourceError):
        nse_rss.parse_feed(b"<html>Access Denied")
