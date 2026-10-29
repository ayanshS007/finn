import json
from datetime import date

import httpx
import pytest

from finn.sources import nse
from finn.sources.http import DiskCache, HttpClient, SourceBlocked
from finn.sources.shapes import ANNOUNCEMENT_KEYS


def load(fixtures, name):
    return json.loads((fixtures / name).read_text())


def test_parse_announcements_results_day(fixtures):
    rows = nse.parse_announcements(load(fixtures, "nse_announcements_results_day.json"))
    assert [tuple(r) for r in rows][0] == ANNOUNCEMENT_KEYS
    outcome = next(r for r in rows if r["category"] == "Outcome of Board Meeting")
    assert outcome["exchange"] == "NSE" and outcome["symbol"] == "INFY"
    assert outcome["company"] == "Infosys Limited"
    assert outcome["published_at"] == "2026-10-23T16:08:26+05:30"
    assert outcome["attachment_url"].startswith("https://nsearchives.nseindia.com/")
    assert outcome["source_id"] and "financial results" in outcome["text"].lower()


def test_parse_announcements_management_change(fixtures):
    rows = nse.parse_announcements(load(fixtures, "nse_announcements_mgmt_change.json"))
    change = [r for r in rows if r["category"] == "Change in Management"]
    assert len(change) == 1 and change[0]["published_at"] == "2026-08-24T21:54:11+05:30"


def test_placeholder_values_become_none():
    row = nse.parse_announcements(
        [{"symbol": "X", "desc": "Updates", "attchmntFile": "-", "attchmntText": "", "seq_id": "1"}]
    )[0]
    assert row["attachment_url"] is None and row["text"] is None and row["published_at"] is None


def test_legacy_results_infy_and_bank(fixtures):
    infy = nse.parse_financial_results_legacy(
        load(fixtures, "nse_financial_results_legacy_INFY.json")
    )
    bank = nse.parse_financial_results_legacy(
        load(fixtures, "nse_financial_results_legacy_HDFCBANK.json")
    )
    assert max(r["period_end"] for r in infy) == "2024-12-31"  # endpoint is frozen here
    assert {r["basis"] for r in infy} == {"consolidated", "standalone"}
    assert all(r["xbrl_url"] or r["attachment_url"] for r in infy[:5])
    assert all(r["is_bank"] for r in bank) and not any(r["is_bank"] for r in infy)
    assert infy[0]["raw"]["symbol"] == "INFY" and infy[0]["source"] == "legacy"


def test_integrated_results_infy_and_bank(fixtures):
    infy = nse.parse_integrated_results(load(fixtures, "nse_integrated_results_INFY.json"))
    bank = nse.parse_integrated_results(load(fixtures, "nse_integrated_results_HDFCBANK.json"))
    assert max(r["period_end"] for r in infy) == "2026-09-30"
    assert min(r["period_end"] for r in infy) == "2025-03-31"
    latest = [r for r in infy if r["period_end"] == "2026-09-30"]
    assert {r["basis"] for r in latest} == {"consolidated", "standalone"}
    assert all(r["xbrl_url"].endswith(".xml") and r["ixbrl_url"].endswith(".html") for r in latest)
    assert all(r["attachment_url"] is None for r in infy)  # NSE sends ".../corporate/null"
    assert all(r["is_bank"] for r in bank) and not any(r["is_bank"] for r in infy)


def test_results_comparison_is_stale_at_dec_2024(fixtures):
    for symbol in ("INFY", "HDFCBANK"):
        raw = load(fixtures, f"nse_results_comparision_{symbol}.json")
        assert nse.results_comparison_period_end(raw) == "2024-12-31"
    assert load(fixtures, "nse_results_comparision_HDFCBANK.json")["bankNonBnking"] == "B"


# -- client behaviour against a mock transport --------------------------------------------


def mock_client(tmp_path, handler):
    http = HttpClient(
        cache=DiskCache(tmp_path),
        transport=httpx.MockTransport(handler),
        spacing={},
        sleep=lambda s: None,
    )
    return nse.NSEClient(http)


def test_warm_up_first_then_api_with_params(tmp_path, fixtures):
    seen = []
    payload = (fixtures / "nse_announcements_results_day.json").read_text()

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/api/corporate-announcements":
            return httpx.Response(200, text=payload)
        return httpx.Response(200, text="<html>", headers={"set-cookie": "nsit=abc; Path=/"})

    client = mock_client(tmp_path, handler)
    rows = client.announcements("INFY", date(2026, 10, 23), "23-10-2026", ttl=0)
    assert seen[0] == "https://www.nseindia.com"
    api = httpx.URL(seen[-1])
    assert dict(api.params) == {
        "index": "equities",
        "symbol": "INFY",
        "from_date": "23-10-2026",
        "to_date": "23-10-2026",
    }
    assert len(rows) == 3
    client.announcements("INFY", date(2026, 10, 23), date(2026, 10, 23), ttl=0)
    assert seen.count("https://www.nseindia.com") == 1  # warmed once


def test_symbol_none_omits_symbol_param(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, text="[]")

    mock_client(tmp_path, handler).announcements(
        None, date(2026, 10, 29), date(2026, 10, 29), ttl=0
    )
    assert "symbol" not in seen[-1].params


def test_403_refreshes_session_and_retries(tmp_path):
    api_calls = []

    def handler(req):
        if req.url.path.startswith("/api/"):
            api_calls.append(1)
            return httpx.Response(403 if len(api_calls) == 1 else 200, text="[]")
        return httpx.Response(200, text="<html>")

    client = mock_client(tmp_path, handler)
    assert client.announcements("INFY", date(2026, 10, 1), date(2026, 10, 2), ttl=0) == []
    assert len(api_calls) == 2


def test_blocked_twice_raises_not_fakes(tmp_path):
    client = mock_client(tmp_path, lambda r: httpx.Response(403, text="Access Denied"))
    with pytest.raises(SourceBlocked):
        client.announcements("INFY", date(2026, 10, 1), date(2026, 10, 2), ttl=0)


def test_warm_up_failure_is_tolerated(tmp_path):
    def handler(req):
        if req.url.path.startswith("/api/"):
            return httpx.Response(200, text="[]")
        return httpx.Response(403)

    client = mock_client(tmp_path, handler)
    assert client.announcements("INFY", date(2026, 10, 1), date(2026, 10, 2), ttl=0) == []


def test_integrated_results_pages_when_total_exceeds_reply(tmp_path):
    def row(i):
        return {"type": nse.INTEGRATED_FINANCIALS, "symbol": "X", "seq_Id": str(i)}

    def handler(req):
        if not req.url.path.startswith("/api/"):
            return httpx.Response(200, text="<html>")
        page = req.url.params.get("page")
        if page is None:
            return httpx.Response(200, json={"data": [row(0), row(1)], "totalCount": 5})
        start = int(page) * 2
        data = [row(i) for i in range(start, min(start + 2, 5))]
        return httpx.Response(200, json={"data": data, "totalCount": 5})

    raw = mock_client(tmp_path, handler).integrated_results_raw("X", ttl=0, page_size=2)
    assert [r["seq_Id"] for r in raw["data"]] == ["0", "1", "2", "3", "4"]


def test_financial_results_merges_newest_first(tmp_path, fixtures):
    legacy = (fixtures / "nse_financial_results_legacy_INFY.json").read_text()
    integrated = (fixtures / "nse_integrated_results_INFY.json").read_text()

    def handler(req):
        if req.url.path == "/api/corporates-financial-results":
            return httpx.Response(200, text=legacy)
        if req.url.path == "/api/integrated-filing-results":
            return httpx.Response(200, text=integrated)
        return httpx.Response(200, text="<html>")

    rows = mock_client(tmp_path, handler).financial_results("INFY", ttl=0)
    assert rows[0]["period_end"] == "2026-09-30" and rows[0]["source"] == "integrated"
    ends = [r["period_end"] for r in rows]
    assert ends == sorted(ends, reverse=True)
    assert {"legacy", "integrated"} == {r["source"] for r in rows}


def test_dates_validated():
    assert nse.ddmmyyyy(date(2026, 1, 5)) == "05-01-2026"
    with pytest.raises(ValueError):
        nse.ddmmyyyy("2026-01-05")


@pytest.mark.live
def test_live_nse_endpoints():
    rows = nse.announcements("INFY", date(2026, 10, 1), date.today(), ttl=0)
    assert rows and rows[0]["exchange"] == "NSE"
    assert nse.financial_results("INFY", ttl=0)
    assert nse.results_comparison("INFY", ttl=0)["resCmpData"]
