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

## XBRL fixtures (segment S2)

Captured by `scripts/capture_xbrl_fixtures.py` (2026-10-29) from `/api/integrated-filing-results`; the XBRL files are saved unmodified.

| file | source |
|---|---|
| `xbrl_INFY_FY27Q2_consolidated.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1745058_23102026082117_WEB.xml |
| `xbrl_INFY_FY27Q1_consolidated.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1700136_23072026054446_WEB.xml |
| `xbrl_INFY_FY26Q2_consolidated.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1551001_16102025094433_WEB.xml |
| `xbrl_HDFCBANK_FY27Q2_consolidated.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml (banking taxonomy) |
| `xbrl_GRAVITA_FY27Q2_consolidated.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1747256_27102026084426_WEB.xml |
| `xbrl_GRAVITA_FY27Q2_standalone.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1747260_27102026084836_WEB.xml (standalone-only test) |
| `xbrl_BAJFINANCE_FY27Q2_consolidated.xml` | https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_NBFC_INDAS_1742040_19102026075918_WEB.xml (NBFC taxonomy) |
| `nse_integrated_results_GRAVITA.json` | /api/integrated-filing-results?index=equities&symbol=GRAVITA&period_ended=all&type=Integrated Filing- Financials |
