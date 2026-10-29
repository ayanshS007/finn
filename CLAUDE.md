# FINN - project guide

## Product
FINN is a mobile-first investment agent. Push: a finite, ranked briefing of NSE/BSE filings for an 18-stock portfolio, plus price, volume and category alerts. Pull: the Promise Ledger for any NSE ticker - what management promised on the last four earnings calls versus what the numbers delivered, every claim linked to its transcript page - plus five compact strips (business mix, financial snapshot, trajectory, balance sheet, cash quality). Graded on judgment, product sense and architecture. Thin-but-working beats broad-but-broken.

## Non-negotiable rules
1. Code owns every number. Figures in the UI come only from the facts table, computed in Python. Models never produce or compute figures.
2. Normalise before any model sees a figure: money in Rs crore (1 crore = 100 lakh = 10 million), original unit kept; periods labelled like FY27Q2 (Jul-Sep 2026); Q4 = FY - 9M and H2 = FY - H1, marked derived with the formula.
3. Every model label or claim carries a verbatim quote. quote_match = share of the quote's 4-word runs found in the source after normalising whitespace and punctuation. Below 0.4: retry once, then drop.
4. A separate grader call scores each model label 1-5 from the quote and the category definition only. Scores 1-3 go to the routine drawer.
5. Models may abstain (verdict not_enough_evidence). A failed check never loops: one retry, then drawer or log.
6. Pull uses primary sources only: transcripts, annual reports, results filings, screener.in tables. Every pull prompt says: "Use only the provided text. Do not use outside knowledge."
7. Small models (Groq llama-3.1-8b-instant, Gemini Flash-Lite) never handle numbers; they label and write one-liners only.
8. Every LLM call goes through finn.llm.router: cached by sha256(model + prompt), logged to llm_calls (step, model, tokens in and out, latency ms, estimated cost, cache hit).
9. Never invent data. If a source blocks or changes, stop and report. Fixtures are for tests and for the clearly labelled replay mode only.
10. Number check: every number in a one-liner or reason must appear in its inputs, or the text is dropped.

## Stack
backend: Python 3.11 package finn; FastAPI app finn.api on Render; jobs in finn.jobs. The announcement job runs every 15 minutes, 09:00-18:00 IST, from my laptop because NSE blocks cloud IPs; prices and RSS run on GitHub Actions. DB: Supabase Postgres. web: Next.js App Router, TypeScript, Tailwind, PWA, on Vercel.
Models: Groq llama-3.1-8b-instant (labels, grader, one-liners); Gemini Flash (claims, evasion labels, verdicts, PDF fallback); Gemini Flash-Lite (appointee profile text); Groq openai/gpt-oss-120b as failover for Gemini steps. Tavily only to find an appointee's LinkedIn URL; never scrape LinkedIn.

## Data contracts (pydantic models in finn/db/models.py)
Fact: ticker, metric (enum: revenue, ebitda, pbt, pat, tax, finance_costs, d_and_a, other_income, nii, ppop, capex, cfo, fcf, debt, equity, ebitda_margin, pat_margin, segment_revenue), segment (optional), value_cr (float or null), unit_original (lakh|crore|million|rupee), period (e.g. FY27Q2), period_kind (Q|H1|9M|FY), basis (consolidated|standalone), derived (bool), formula (optional), source_url, page (optional), quote, quote_match.
Claim: ticker, said_in (call period), speaker, metric (same enum plus text), target_low, target_high, target_unit (%|cr|units|text), by_period, quote, page, quote_match.
Verdict: claim_id, actual_fact_id (null only when abstaining), verdict (delivered|missed|moved_goalposts|went_quiet|not_enough_evidence), reason (max 20 words, no new numbers). Claims whose by_period has not ended are shown as Open and are not judged.

## Materiality
score = category weight x holding factor (0.8-1.2) x recency, plus 15 if a price or volume rule fired the same day. 60 or more: card. 30-59: one-liner. Below 30: routine drawer. Weights live in config/materiality.yaml.

## Web design rules
Numbers before words. At most one line of prose per card. Every card shows a "why ranked" line. The Brief ends with "You're caught up". Works at 360px. Light and dark themes. Tabular numerals for figures. Colour only for direction (up/down) and verdict chips.

## Git workflow for every segment
1. git checkout main && git pull, then create the branch named in the prompt.
2. Commit in small steps with clear messages.
3. Append a dated entry to docs/BUILD_LOG.md: what changed, decisions, open issues.
4. Run make test; it must pass before pushing.
5. Push and open a PR titled "Segment SX: <name>" listing: what was built, how it was tested (summary of command output), what is stubbed, what the next segment needs.
6. Open the PR and wait for CI. If it is red, fix and push. When it is green, end your turn with the PR link. Do not merge, and ignore merge notifications: I merge myself and will tell you if anything is needed.
7. Never commit .env or API keys.

## Repo layout
backend/finn/<sources|normalise|parse|classify|score|enrich|summarise|pull|llm|db|jobs|api>; backend/tests (pytest, fixtures in tests/fixtures); web/ (Next.js); supabase/migrations; config/ (portfolio, categories, materiality YAML); eval/; scripts/; docs/ (PLAN.md, BUILD_LOG.md). Make targets: setup, test, dev-web, dev-api.
