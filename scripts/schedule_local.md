# Running the announcement job from the laptop

NSE blocks cloud IPs, so the 15-minute announcement job runs from the laptop (prices and RSS run on
GitHub Actions). The job is `python -m finn.jobs.announcements`; `scripts/run_announcements.sh` wraps
it for cron.

Everything here is free tier: Groq free tier, Gemini via an AI Studio key with no billing, Supabase
Free. The job logs a *paid-equivalent* cost for its model calls; the actual cost is Rs 0.

## One-time setup (macOS)

```bash
make setup                      # creates backend/.venv and installs the package
cp .env.example .env            # then fill GROQ_API_KEY, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
backend/.venv/bin/python -m finn.jobs.announcements --since 24h --dry-run   # classify, write nothing
backend/.venv/bin/python -m finn.jobs.announcements --since 24h             # first real run
```

`.env` is git-ignored. The job reads it itself; real environment variables win over it.

## Cron: every 15 minutes, 09:00-18:00 IST

`crontab -e`, then add (replace the path):

```cron
*/15 * * * * /Users/you/finn/scripts/run_announcements.sh
```

The wrapper checks the time in `Asia/Kolkata` and exits quietly outside 09:00-18:00 IST, so the line
above is correct whatever time zone the laptop is in. If the laptop is set to IST you can instead
put the window in cron itself (the second line adds the 18:00 run):

```cron
*/15 9-17 * * * /Users/you/finn/scripts/run_announcements.sh
0 18 * * *      /Users/you/finn/scripts/run_announcements.sh
```

Each run looks back 3 hours, so one missed run (lid closed) costs nothing: filings already stored
are skipped by `source_id`, and filings left *pending* by a rate limit are retried. Logs are in
`data/logs/announcements-YYYY-MM-DD.log`.

macOS notes:

- Give `cron` Full Disk Access (System Settings > Privacy & Security > Full Disk Access > add
  `/usr/sbin/cron`), or cron cannot read the repo when it lives under Documents or Desktop.
- A sleeping laptop runs no cron jobs. For the working day: `caffeinate -s` in a terminal, or
  Settings > Battery > "Prevent automatic sleeping on power adapter".
- Foreground alternative with no cron: `python -m finn.jobs.announcements --since 3h --every 15m`.
- `launchd` (a LaunchAgent with `StartInterval` 900) is the macOS-native equivalent if cron is
  disabled.

## Does NSE answer from GitHub Actions?

Probed once on 2026-10-30 (S3, a throwaway `nse-probe` workflow on a GitHub-hosted `ubuntu-latest`
runner, since removed; run 41017177884 on the S3 pull request):

- `www.nseindia.com/` itself answers 403, the same as from the dev sandbox.
- The project's own `NSEClient` (HTTP/1.1, browser user agent, the 403 warm-up tolerated) still got
  `/api/corporate-announcements` for INFY: **OK, 6 announcements for the last 3 days**.

So NSE does answer from GitHub Actions today, contrary to the assumption that it blocks cloud IPs.
That is one probe at one moment: Akamai's treatment of runner IP ranges can change, and runner IPs
differ between runs. It is not enough to move the job off the laptop on its own; S5 should decide,
ideally after a few scheduled probes at different times of day (also during 09:00-18:00 IST). The
fallback if Actions gets blocked is the cron setup above.
