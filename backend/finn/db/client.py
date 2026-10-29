"""Thin Supabase wrapper. Writes use the service key (RLS bypass); reads for the web use anon.

Keys come from the environment only (SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY).
They are never logged, printed or put into an exception message.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel

from finn.db.models import Claim, Fact, Verdict

# table -> comma separated columns that form its natural unique key (for on_conflict)
CONFLICT_KEYS = {
    "filings": "source_id",
    "facts": "ticker,metric,segment,period,period_kind,basis",
    "prices": "symbol,date",
    "cache": "key",
    "push_subscriptions": "endpoint",
}


class DbConfigError(RuntimeError):
    pass


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DbConfigError(f"{name} is not set")
    return value


def _rows(items: Iterable[BaseModel | dict[str, Any]]) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") if isinstance(i, BaseModel) else dict(i) for i in items]


class Db:
    def __init__(self, client: Any):
        self.client = client

    @classmethod
    def service(cls) -> Db:
        """Server-side client with the sb_secret_... key. Never ship this key to the browser."""
        from supabase import create_client

        return cls(create_client(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_KEY")))

    @classmethod
    def anon(cls) -> Db:
        """Read-only client with the sb_publishable_... key."""
        from supabase import create_client

        return cls(create_client(_env("SUPABASE_URL"), _env("SUPABASE_ANON_KEY")))

    def upsert(self, table: str, items: Iterable[BaseModel | dict[str, Any]], batch: int = 500):
        """Insert or update on the table's natural key. Returns the number of rows sent."""
        rows = _rows(items)
        on_conflict = CONFLICT_KEYS.get(table)
        for start in range(0, len(rows), batch):
            query = self.client.table(table).upsert(
                rows[start : start + batch], on_conflict=on_conflict
            )
            query.execute()
        return len(rows)

    def insert(self, table: str, items: Iterable[BaseModel | dict[str, Any]], batch: int = 500):
        rows = _rows(items)
        for start in range(0, len(rows), batch):
            self.client.table(table).insert(rows[start : start + batch]).execute()
        return len(rows)

    # -- typed helpers ---------------------------------------------------------------------

    def upsert_facts(self, facts: Sequence[Fact]) -> int:
        return self.upsert("facts", facts)

    def insert_claims(self, claims: Sequence[Claim]) -> int:
        return self.insert("claims", claims)

    def insert_verdicts(self, verdicts: Sequence[Verdict]) -> int:
        return self.insert("verdicts", verdicts)

    def upsert_filings(self, rows: Sequence[dict[str, Any]]) -> int:
        return self.upsert("filings", rows)

    def upsert_prices(self, rows: Sequence[dict[str, Any]]) -> int:
        return self.upsert("prices", rows)

    def select(self, table: str, **eq: Any) -> list[dict[str, Any]]:
        query = self.client.table(table).select("*")
        for column, value in eq.items():
            query = query.eq(column, value)
        return query.execute().data
