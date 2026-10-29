# Raw fixtures

Captured by `scripts/capture_fixtures.py` (last run 2026-10-29). Parser tests run on these; no network in CI.

| file | source |
|---|---|
| `bse_announcements_INFY.json` | bse package, scrip 500209, 2026-10-20..29 |
| `nse_announcements_mgmt_change.json` | /api/corporate-announcements?index=equities&symbol=INFY&24-08-2026 |
| `nse_announcements_results_day.json` | /api/corporate-announcements?index=equities&symbol=INFY&23-10-2026 |
| `nse_financial_results_legacy_HDFCBANK.json` | /api/corporates-financial-results?index=equities&period=Quarterly&symbol=HDFCBANK |
| `nse_financial_results_legacy_INFY.json` | /api/corporates-financial-results?index=equities&period=Quarterly&symbol=INFY |
| `nse_integrated_results_HDFCBANK.json` | /api/integrated-filing-results?index=equities&symbol=HDFCBANK&period_ended=all&type=Integrated Filing- Financials |
| `nse_integrated_results_INFY.json` | /api/integrated-filing-results?index=equities&symbol=INFY&period_ended=all&type=Integrated Filing- Financials |
| `nse_results_comparision_HDFCBANK.json` | /api/results-comparision?symbol=HDFCBANK |
| `nse_results_comparision_INFY.json` | /api/results-comparision?symbol=INFY |
| `nse_rss_online_announcements.xml` | https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml (first 60 items) |
| `prices_raw.csv` | yfinance download ('INFY', 'HDFCBANK', 'ETERNAL') .NS, 120d, raw |
| `screener_HDFCBANK.html` | https://www.screener.in/company/HDFCBANK/consolidated/ |
| `screener_INFY.html` | https://www.screener.in/company/INFY/consolidated/ |
| `transcript_INFY.pdf` | https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=7982fc61-7765-4981-ab3d-6aec36ba2137.pdf |
