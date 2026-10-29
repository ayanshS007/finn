import json

from finn.sources import bse
from finn.sources.http import DiskCache
from finn.sources.shapes import ANNOUNCEMENT_KEYS


def test_parse_announcements(fixtures):
    raw = json.loads((fixtures / "bse_announcements_INFY.json").read_text())
    rows = bse.parse_announcements(raw, symbol="INFY")
    assert len(rows) == len(raw) and tuple(rows[0]) == ANNOUNCEMENT_KEYS
    first = rows[0]
    assert first["exchange"] == "BSE" and first["symbol"] == "INFY"
    assert first["company"] == "Infosys Ltd" and first["source_id"] == raw[0]["NEWSID"]
    assert first["published_at"].startswith("2026-10-29T15:14:07") and first[
        "published_at"
    ].endswith("+05:30")
    assert first["attachment_url"] == bse.ATTACHMENT_URL.format(name=raw[0]["ATTACHMENTNAME"])
    assert first["category"] == "Company Update" and first["subcategory"] == "Newspaper Publication"
    assert "Newspaper Publication" in first["text"]


def test_symbol_unknown_stays_none(fixtures):
    raw = json.loads((fixtures / "bse_announcements_INFY.json").read_text())
    assert bse.parse_announcements(raw)[0]["symbol"] is None


class FakeBSE:
    calls = 0

    def __init__(self, download_folder):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getScripCode(self, symbol):  # noqa: N802
        FakeBSE.calls += 1
        return "500209"

    def announcements(self, page_no, from_date, to_date, scripcode):
        FakeBSE.calls += 1
        rows = [{"NEWSID": f"{page_no}-{i}"} for i in range(2)]
        return {"Table": rows if page_no < 3 else [], "Table1": [{"ROWCNT": 4}]}


def test_scrip_code_and_announcements_are_cached_and_paged(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(bse, "BSE", FakeBSE)
    monkeypatch.setattr(bse, "PAGE_GAP", 0)
    cache = DiskCache(tmp_path)
    assert bse.scrip_code("INFY", cache=cache) == "500209"
    assert bse.scrip_code("INFY", cache=cache) == "500209"
    assert FakeBSE.calls == 1
    FakeBSE.calls = 0
    args = ("500209", datetime(2026, 10, 1), datetime(2026, 10, 2))
    raw = bse.announcements_raw(*args, cache=cache)
    assert [r["NEWSID"] for r in raw] == ["1-0", "1-1", "2-0", "2-1"]  # stops at ROWCNT
    assert FakeBSE.calls == 2
    bse.announcements_raw(*args, cache=cache)
    assert FakeBSE.calls == 2
