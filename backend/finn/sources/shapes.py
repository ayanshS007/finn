"""The announcement dict shared by NSE, NSE RSS and BSE."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

ANNOUNCEMENT_KEYS = (
    "exchange",
    "symbol",
    "company",
    "category",
    "subcategory",
    "text",
    "attachment_url",
    "published_at",
    "source_id",
    "has_xbrl",
)


def ist_iso(value: str | None, *formats: str) -> str | None:
    """Parse an IST wall-clock string with the first matching format; return ISO-8601 +05:30."""
    if not value:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=IST).isoformat()
        except ValueError:
            continue
    return None


def clean(value: object) -> str | None:
    """Empty strings and NSE's '-' placeholders become None."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "-", "null") else text


def announcement(**fields: object) -> dict:
    missing = set(ANNOUNCEMENT_KEYS) - fields.keys()
    extra = fields.keys() - set(ANNOUNCEMENT_KEYS)
    if missing or extra:
        raise ValueError(f"announcement fields: missing={missing} extra={extra}")
    return {k: fields[k] for k in ANNOUNCEMENT_KEYS}
