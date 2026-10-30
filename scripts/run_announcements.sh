#!/usr/bin/env bash
# One announcement-job run for cron. Safe to call every 15 minutes, around the clock: it does
# nothing outside 09:00-18:00 IST (so the laptop's own time zone does not matter) and never runs
# two copies at once. Keys come from the repo's .env (never committed).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

hhmm="$(TZ=Asia/Kolkata date +%H%M)"
if [ "$hhmm" -lt 0900 ] || [ "$hhmm" -gt 1800 ]; then
  exit 0
fi

LOCK="$REPO/data/announcements.lock"
mkdir -p "$REPO/data/logs"
if ! mkdir "$LOCK" 2>/dev/null; then
  # a stale lock (a crashed run) is cleared after 30 minutes
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then rmdir "$LOCK"; mkdir "$LOCK"; else exit 0; fi
fi
trap 'rmdir "$LOCK"' EXIT

# --since 3h: three runs' worth of overlap, so one missed run (laptop asleep) loses nothing.
# Filings already stored are skipped by source_id; rate-limited ones are retried.
backend/.venv/bin/python -m finn.jobs.announcements --since 3h \
  >> "$REPO/data/logs/announcements-$(TZ=Asia/Kolkata date +%F).log" 2>&1
