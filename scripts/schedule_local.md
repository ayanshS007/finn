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

See the result at the bottom of this file ("GitHub Actions probe"). S5 decides where the job runs.
