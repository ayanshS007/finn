"""A small in-memory stand-in for the supabase-py calls the card and rescore jobs make."""

from __future__ import annotations

from types import SimpleNamespace


class Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self._range = None

    def select(self, *_):
        return self

    def _get(self, row, column):
        if "->>" in column:
            col, key = column.split("->>")
            return str((row.get(col) or {}).get(key)) if (row.get(col) or {}).get(key) is not None else None
        return row.get(column)

    def eq(self, column, value):
        self.rows = [r for r in self.rows if self._get(r, column) == value]
        return self

    def in_(self, column, values):
        self.rows = [r for r in self.rows if self._get(r, column) in values]
        return self

    def gte(self, column, value):
        self.rows = [r for r in self.rows if (r.get(column) or "") >= value]
        return self

    def order(self, column, **_):
        self.rows.sort(key=lambda r: r.get(column) or "")
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = self.rows
        if self._range:
            rows = rows[self._range[0] : self._range[1] + 1]
        return SimpleNamespace(data=[dict(r) for r in rows])


class FakeSupabase:
    """tables: name -> list of row dicts. Upserts of filings replace by source_id."""

    def __init__(self, filings=(), tables=None):
        self.tables = {"filings": [dict(r) for r in filings], "rules": [], "alerts": [],
                       **(tables or {})}  # fmt: skip
        self.facts: list = []
        self.client = self
        self.upserts = 0
        self.fail_facts = False

    def table(self, name):
        return Query(self.tables.get(name, []))

    def upsert_filings(self, rows):
        by_id = {r["source_id"]: r for r in self.tables["filings"]}
        for row in rows:
            by_id[row["source_id"]] = {**by_id.get(row["source_id"], {}), **row}
            self.upserts += 1
        self.tables["filings"] = list(by_id.values())
        return len(rows)

    def upsert_facts(self, facts):
        if self.fail_facts:
            raise RuntimeError("facts table unavailable")
        self.facts += list(facts)
        return len(facts)

    def upsert(self, table, rows, **_):  # the router's cache and llm_calls go through here
        return len(list(rows))

    def insert(self, table, rows, **_):
        return len(list(rows))
