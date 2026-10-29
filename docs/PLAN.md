# FINN build plan

Each segment is one branch and one PR (see the git workflow in CLAUDE.md).

| # | Segment | Scope |
|---|---------|-------|
| S0 | Bootstrap | Repo layout, tooling, CI, config stubs, CLAUDE.md |
| S1 | Sources | Source adapters (NSE/BSE announcements, prices, RSS, transcripts); fills the SMALLCAP_TBD slot |
| S2 | Fact ledger | Normalisation, parsing, facts table, quote matching |
| S3 | Push pipeline | Classify, grade, score and rank filings end to end |
| S4 | Cards | One-liners, number check, card payloads |
| S5 | EOD alerts | Price, volume and category alerts, end-of-day jobs |
| S6 | Brief UI | Mobile-first Brief, "why ranked", routine drawer |
| S7 | Pull API | Promise Ledger claims, verdicts and strips over FastAPI |
| S8 | Dossier UI | Promise Ledger and five strips for any NSE ticker |
| S9 | Eval | Eval harness and labelled sets |
| S10 | Rules and triage | User rules, triage and tuning of materiality weights |
| S11 | Hardening | Retries, failover, observability, edge cases |
| S12 | Ship | Deploy (Render, Vercel), PWA, notifications, docs |
| S13 | Extras | Stretch items if time allows |
