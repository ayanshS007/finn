"""NSE unofficial JSON endpoints (verified live 2026-10-29, see docs/BUILD_LOG.md).

* announcements: /api/corporate-announcements
* results_comparison: /api/results-comparision - frozen at the Dec 2024 quarter
* financial_results: /api/corporates-financial-results (legacy, frozen at Dec 2024) merged with
  /api/integrated-filing-results (SEBI integrated filing; the only source for Mar 2025 onwards)
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from finn.sources.http import HttpClient, SourceError, shared_client
from finn.sources.shapes import announcement, clean, ist_iso

log = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"
# The home page is sometimes 403 to cloud IPs while content pages still hand out cookies.
WARMUP_URLS = (BASE, f"{BASE}/companies-listing/corporate-filings-announcements")
JSON_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": f"{BASE}/"}
INTEGRATED_FINANCIALS = "Integrated Filing- Financials"

TTL_ANNOUNCEMENTS = 300
TTL_RESULTS = 6 * 3600

_DT = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M")


def ddmmyyyy(value: date | datetime | str) -> str:
    if isinstance(value, str):
        datetime.strptime(value, "%d-%m-%Y")  # validate
        return value
    return value.strftime("%d-%m-%Y")


def _iso_date(value: str | None) -> str | None:
    """'31-Dec-2024' / '30-SEP-2026' -> '2024-12-31'."""
    value = clean(value)
    if not value:
        return None
    try:
        return datetime.strptime(value.title(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


class NSEClient:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or shared_client()
        self._warm = False

    def warm_up(self) -> None:
        """Collect session cookies. Best effort: the API call itself decides if we are blocked."""
        for url in WARMUP_URLS:
            try:
                resp = self.http.get(url, ttl=0, raise_for_status=False)
            except SourceError as exc:
                log.warning("NSE warm-up %s failed: %s", url, exc)
                continue
            if resp.status < 400:
                break
            log.warning("NSE warm-up %s -> HTTP %s", url, resp.status)
        self._warm = True

    def _ensure_warm(self) -> None:
        if not self._warm:
            self.warm_up()

    def get_json(self, path: str, params: dict | None = None, ttl: float = 0):
        resp = self.http.get(
            f"{BASE}{path}",
            params,
            ttl=ttl,
            headers=JSON_HEADERS,
            before_network=self._ensure_warm,
            on_retry=self.warm_up,
        )
        return resp.json()

    # -- announcements ------------------------------------------------------------------

    def announcements_raw(self, symbol, from_date, to_date, ttl=TTL_ANNOUNCEMENTS) -> list[dict]:
        params = {
            "index": "equities",
            "from_date": ddmmyyyy(from_date),
            "to_date": ddmmyyyy(to_date),
        }
        if symbol:
            params["symbol"] = symbol
        data = self.get_json("/api/corporate-announcements", params, ttl)
        if not isinstance(data, list):
            raise SourceError(f"corporate-announcements: expected a list, got {type(data)}")
        return data

    def announcements(self, symbol, from_date, to_date, ttl=TTL_ANNOUNCEMENTS) -> list[dict]:
        return parse_announcements(self.announcements_raw(symbol, from_date, to_date, ttl))

    # -- results ------------------------------------------------------------------------

    def results_comparison(self, symbol: str, ttl=TTL_RESULTS) -> dict:
        data = self.get_json("/api/results-comparision", {"symbol": symbol}, ttl)
        if not isinstance(data, dict) or "resCmpData" not in data:
            raise SourceError("results-comparision: unexpected shape")
        return data

    def financial_results_legacy_raw(self, symbol: str, ttl=TTL_RESULTS) -> list[dict]:
        params = {"index": "equities", "period": "Quarterly", "symbol": symbol}
        data = self.get_json("/api/corporates-financial-results", params, ttl)
        if not isinstance(data, list):
            raise SourceError("corporates-financial-results: expected a list")
        return data

    def integrated_results_raw(self, symbol: str, ttl=TTL_RESULTS, page_size: int = 50) -> dict:
        """All Integrated Filing- Financials rows. Pages only if totalCount exceeds one reply."""
        params = {
            "index": "equities",
            "symbol": symbol,
            "period_ended": "all",
            "type": INTEGRATED_FINANCIALS,
        }
        first = self.get_json("/api/integrated-filing-results", params, ttl)
        if not isinstance(first, dict) or "data" not in first:
            raise SourceError("integrated-filing-results: unexpected shape")
        rows = list(first["data"])
        total = first.get("totalCount", 0)
        if len(rows) < total:
            rows = []
            for page in range(20):
                more = self.get_json(
                    "/api/integrated-filing-results",
                    {**params, "page": page, "size": page_size},
                    ttl,
                )
                rows += more["data"]
                if not more["data"] or len(rows) >= total:
                    break
        return {**first, "data": rows}

    def financial_results(self, symbol: str, ttl=TTL_RESULTS) -> list[dict]:
        legacy = parse_financial_results_legacy(self.financial_results_legacy_raw(symbol, ttl))
        integrated = parse_integrated_results(self.integrated_results_raw(symbol, ttl))
        rows = integrated + legacy
        rows.sort(key=lambda r: (r["period_end"] or "", r["filed_at"] or ""), reverse=True)
        return rows


# -- parsers (pure; tested on fixtures) ---------------------------------------------------


def parse_announcements(raw: list[dict]) -> list[dict]:
    out = []
    for r in raw:
        out.append(
            announcement(
                exchange="NSE",
                symbol=clean(r.get("symbol")),
                company=clean(r.get("sm_name")),
                category=clean(r.get("desc")),
                subcategory=None,
                text=clean(r.get("attchmntText")),
                attachment_url=clean(r.get("attchmntFile")),
                published_at=ist_iso(r.get("an_dt") or r.get("exchdisstime"), *_DT),
                source_id=clean(r.get("seq_id")),
                has_xbrl=bool(r.get("hasXbrl")),
            )
        )
    return out


def _result_row(raw: dict, **fields) -> dict:
    return {"symbol": raw.get("symbol"), "raw": raw, **fields}


def parse_financial_results_legacy(raw: list[dict]) -> list[dict]:
    """Rows of /api/corporates-financial-results. Last period available: Dec 2024."""
    return [
        _result_row(
            r,
            source="legacy",
            period_end=_iso_date(r.get("toDate")),
            basis="consolidated" if r.get("consolidated") == "Consolidated" else "standalone",
            audited=r.get("audited") == "Audited",
            is_bank=r.get("bank") == "B",
            cumulative=r.get("cumulative") == "Cumulative",
            revision=False,
            filed_at=ist_iso(r.get("broadCastDate") or r.get("filingDate"), *_DT),
            xbrl_url=clean(r.get("xbrl")),
            ixbrl_url=None,
            attachment_url=clean(r.get("resultDetailedDataLink")),
            seq_id=clean(r.get("seqNumber")),
        )
        for r in raw
    ]


def parse_integrated_results(raw: dict) -> list[dict]:
    """Rows of /api/integrated-filing-results (type 'Integrated Filing- Financials')."""
    rows = []
    for r in raw.get("data", []):
        if r.get("type") != INTEGRATED_FINANCIALS:
            continue
        link = clean(r.get("xbrl")) or clean(r.get("ixbrl")) or ""
        fmt = re.search(r"INTEGRATED_FILING_(.+?)_\d+_", link)
        pdf = clean(r.get("pdf_attach"))
        basis = (clean(r.get("consolidated")) or "").lower()
        rows.append(
            _result_row(
                r,
                source="integrated",
                period_end=_iso_date(r.get("qe_Date")),
                basis=basis if basis in ("consolidated", "standalone") else None,
                audited=(r.get("audited") == "Audited") if r.get("audited") else None,
                is_bank=bool(fmt) and fmt.group(1) == "BANKING",
                cumulative=False,
                revision=r.get("type_Sub") == "Revision",
                filed_at=ist_iso(r.get("broadcast_Date") or r.get("creation_Date"), *_DT),
                xbrl_url=clean(r.get("xbrl")),
                ixbrl_url=clean(r.get("ixbrl")),
                attachment_url=None if not pdf or pdf.endswith("/null") else pdf,
                seq_id=clean(r.get("seq_Id")),
            )
        )
    return rows


def results_comparison_period_end(raw: dict) -> str | None:
    """Latest period end (ISO) in a results_comparison payload; used to flag stale data."""
    ends = [_iso_date(r.get("re_to_dt")) for r in raw.get("resCmpData", [])]
    return max((e for e in ends if e), default=None)


# -- module-level convenience (shared client) ---------------------------------------------

_default: NSEClient | None = None


def _client() -> NSEClient:
    global _default
    if _default is None:
        _default = NSEClient()
    return _default


def announcements(symbol, from_date, to_date, ttl=TTL_ANNOUNCEMENTS) -> list[dict]:
    """symbol=None fetches every company. Dates: date/datetime or dd-mm-yyyy strings."""
    return _client().announcements(symbol, from_date, to_date, ttl)


def results_comparison(symbol: str, ttl=TTL_RESULTS) -> dict:
    return _client().results_comparison(symbol, ttl)


def financial_results(symbol: str, ttl=TTL_RESULTS) -> list[dict]:
    return _client().financial_results(symbol, ttl)
