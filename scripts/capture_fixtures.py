"""Save raw source responses to backend/tests/fixtures/ for the parser tests.

Run from the repo root on a machine where NSE answers (it blocks most cloud IPs):

    backend/.venv/bin/python scripts/capture_fixtures.py [--only nse|bse|screener|prices|rss]

Everything is fetched fresh (ttl=0) and written unmodified, except the RSS feed, which is cut to
its first RSS_ITEMS items to keep the repo small. Writes a README.md manifest beside the files.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

from finn.sources import bse, nse, nse_rss, prices, screener
from finn.sources.http import SourceError, shared_client

FIXTURES = Path(__file__).resolve().parents[1] / "backend" / "tests" / "fixtures"
RSS_ITEMS = 60
# One INFY results-filing day and one INFY management-change day (both seen live on 2026-10-29).
RESULTS_DAY = date(2026, 10, 23)
MGMT_CHANGE_DAY = date(2026, 8, 24)
RESULT_SYMBOLS = ("INFY", "HDFCBANK")  # HDFCBANK exercises the bank format
PRICE_SYMBOLS = ("INFY", "HDFCBANK", "ETERNAL")
SCREENER_SYMBOLS = ("INFY", "HDFCBANK")

manifest: list[tuple[str, str]] = []


def save(name: str, content: bytes | str, source: str) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    (FIXTURES / name).write_bytes(data)
    manifest.append((name, source))
    print(f"saved {name} ({len(data):,} bytes)")


def save_json(name: str, obj: object, source: str) -> None:
    save(name, json.dumps(obj, indent=1, ensure_ascii=False), source)


def capture_nse() -> None:
    client = nse.NSEClient()
    for name, day in (
        ("nse_announcements_results_day.json", RESULTS_DAY),
        ("nse_announcements_mgmt_change.json", MGMT_CHANGE_DAY),
    ):
        raw = client.announcements_raw("INFY", day, day, ttl=0)
        save_json(name, raw, f"/api/corporate-announcements?index=equities&symbol=INFY&{day:%d-%m-%Y}")
    for symbol in RESULT_SYMBOLS:
        save_json(
            f"nse_results_comparision_{symbol}.json",
            client.get_json("/api/results-comparision", {"symbol": symbol}),
            f"/api/results-comparision?symbol={symbol}",
        )
        save_json(
            f"nse_financial_results_legacy_{symbol}.json",
            client.financial_results_legacy_raw(symbol, ttl=0),
            f"/api/corporates-financial-results?index=equities&period=Quarterly&symbol={symbol}",
        )
        save_json(
            f"nse_integrated_results_{symbol}.json",
            client.integrated_results_raw(symbol, ttl=0),
            f"/api/integrated-filing-results?index=equities&symbol={symbol}"
            "&period_ended=all&type=Integrated Filing- Financials",
        )


def capture_rss() -> None:
    body = shared_client().get(nse_rss.FEED_URL, ttl=0).content.decode("utf-8")
    parts = body.split("<item>")
    cut = "<item>".join(parts[: RSS_ITEMS + 1]) + "</channel></rss>"
    save("nse_rss_online_announcements.xml", cut, f"{nse_rss.FEED_URL} (first {RSS_ITEMS} items)")


def capture_bse() -> None:
    code = bse.scrip_code("INFY", ttl=0)
    raw = bse.announcements_raw(code, datetime(2026, 10, 20), datetime(2026, 10, 29), ttl=0)
    save_json("bse_announcements_INFY.json", raw, f"bse package, scrip {code}, 2026-10-20..29")


def capture_prices() -> None:
    raw = prices._download([f"{s}.NS" for s in PRICE_SYMBOLS], prices.DAYS)
    path = FIXTURES / "prices_raw.csv"
    FIXTURES.mkdir(parents=True, exist_ok=True)
    raw.to_csv(path)  # two header rows (ticker, field); read with header=[0, 1]
    manifest.append((path.name, f"yfinance download {PRICE_SYMBOLS} .NS, {prices.DAYS}d, raw"))
    print(f"saved {path.name}")


def capture_screener() -> None:
    http = shared_client()
    for symbol in SCREENER_SYMBOLS:
        url = f"{screener.BASE}/{symbol}/consolidated/"
        save(f"screener_{symbol}.html", http.get(url, ttl=0).content, url)
    company = screener.parse_company_page(
        (FIXTURES / "screener_INFY.html").read_text(), "INFY", "fixture"
    )
    transcript = next((c["transcript_url"] for c in company.concalls if c["transcript_url"]), None)
    if not transcript:
        raise SourceError("no transcript link on the INFY screener page")
    pdf = http.get(transcript, ttl=0).content
    if not pdf.startswith(b"%PDF"):
        raise SourceError(f"transcript is not a PDF: {pdf[:40]!r}")
    save("transcript_INFY.pdf", pdf, transcript)


def write_manifest() -> None:
    path = FIXTURES / "README.md"
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("| `"):
                name, src = [c.strip() for c in line.strip("| ").split("|", 1)]
                existing[name.strip("`")] = src
    existing.update({n: s for n, s in manifest})
    rows = "\n".join(f"| `{n}` | {s} |" for n, s in sorted(existing.items()))
    path.write_text(
        "# Raw fixtures\n\nCaptured by `scripts/capture_fixtures.py` "
        f"(last run {datetime.now():%Y-%m-%d}). Parser tests run on these; no network in CI.\n\n"
        f"| file | source |\n|---|---|\n{rows}\n"
    )


STEPS = {
    "nse": capture_nse,
    "rss": capture_rss,
    "bse": capture_bse,
    "prices": capture_prices,
    "screener": capture_screener,
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=STEPS, action="append")
    args = ap.parse_args()
    failed = []
    for name in args.only or STEPS:
        try:
            STEPS[name]()
        except SourceError as exc:  # report, never fake: the fixture is simply not written
            failed.append(f"{name}: {exc}")
            print(f"FAILED {name}: {exc}", file=sys.stderr)
    write_manifest()
    sys.exit(1 if failed else 0)
