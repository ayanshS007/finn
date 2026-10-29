import re

import pytest

from finn.db import client as db_client
from finn.db.client import Db, DbConfigError
from finn.db.models import Claim, Fact, Verdict
from tests.conftest import FIXTURES

MIGRATION = FIXTURES.parents[2] / "supabase" / "migrations" / "001_init.sql"


class FakeTable:
    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def upsert(self, rows, on_conflict=None):
        self.calls.append(("upsert", self.name, rows, on_conflict))
        return self

    def insert(self, rows):
        self.calls.append(("insert", self.name, rows, None))
        return self

    def select(self, columns):
        self.calls.append(("select", self.name, columns, None))
        return self

    def eq(self, column, value):
        self.calls.append(("eq", self.name, column, value))
        return self

    def execute(self):
        return type("R", (), {"data": [{"ok": True}]})()


class FakeClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return FakeTable(self.calls, name)


def fact(period="FY27Q2", metric="revenue"):
    return Fact(
        ticker="INFY",
        metric=metric,
        value_cr=1.5,
        unit_original="rupee",
        period=period,
        period_kind="Q",
        basis="consolidated",
        source_url="u://x",
        quote="a b c d",
        quote_match=1.0,
    )


def test_upsert_facts_uses_the_natural_key_and_json_rows():
    fake = FakeClient()
    assert Db(fake).upsert_facts([fact(), fact(metric="pat")]) == 2
    op, table, rows, conflict = fake.calls[0]
    assert (op, table) == ("upsert", "facts")
    assert conflict == "ticker,metric,segment,period,period_kind,basis"
    assert rows[0]["metric"] == "revenue" and rows[0]["segment"] is None and len(rows) == 2


def test_upsert_batches():
    fake = FakeClient()
    Db(fake).upsert("facts", [fact()] * 5, batch=2)
    assert [len(c[2]) for c in fake.calls] == [2, 2, 1]


def test_insert_helpers_and_select():
    fake = FakeClient()
    db = Db(fake)
    db.insert_claims(
        [Claim(ticker="INFY", said_in="FY27Q1", metric="text", quote="q", quote_match=1.0)]
    )
    db.insert_verdicts(
        [
            Verdict(
                claim_id="c", actual_fact_id=None, verdict="not_enough_evidence", reason="no match"
            )
        ]
    )
    db.upsert_filings([{"source_id": "1", "symbol": "INFY", "exchange": "NSE"}])
    db.upsert_prices([{"symbol": "INFY", "date": "2026-10-29", "close": 1.0, "volume": 1}])
    assert db.select("facts", ticker="INFY") == [{"ok": True}]
    ops = [(c[0], c[1], c[3]) for c in fake.calls if c[0] in ("insert", "upsert")]
    assert ops == [
        ("insert", "claims", None),
        ("insert", "verdicts", None),
        ("upsert", "filings", "source_id"),
        ("upsert", "prices", "symbol,date"),
    ]


def test_conflict_keys_match_the_migration():
    sql = MIGRATION.read_text()
    assert "unique nulls not distinct" in sql
    for key in db_client.CONFLICT_KEYS["facts"].split(","):
        assert key in sql.split("facts_natural_key")[1].split(";")[0]
    assert "source_id      text not null unique" in sql


def test_missing_env_names_the_variable_but_never_a_value(monkeypatch):
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(DbConfigError, match="SUPABASE_URL is not set"):
        Db.service()


@pytest.mark.parametrize(
    "which, var", [(Db.service, "SUPABASE_SERVICE_KEY"), (Db.anon, "SUPABASE_ANON_KEY")]
)
def test_client_library_accepts_new_format_keys(monkeypatch, which, var):
    """supabase-py must build a client from sb_publishable_ / sb_secret_ keys (no network)."""
    prefix = "sb_secret_" if "SERVICE" in var else "sb_publishable_"
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv(var, prefix + "dummyvalue0123456789")
    assert which().client is not None


def test_migration_defines_every_table_with_rls_and_read_only_anon():
    sql = MIGRATION.read_text()
    tables = [
        "filings",
        "facts",
        "claims",
        "verdicts",
        "qa_labels",
        "prices",
        "rules",
        "alerts",
        "swipes",
        "llm_calls",
        "cache",
        "push_subscriptions",
    ]
    for t in tables:
        assert re.search(rf"create table if not exists {t} \(", sql), t
        assert f"alter table {t}" in re.sub(r"\s+", " ", sql), f"RLS not enabled on {t}"
    assert "revoke all on all tables in schema public from anon, authenticated" in sql
    grants = re.search(r"grant select on ([^;]+) to anon;", sql)
    assert grants and "insert" not in sql.split("to anon")[-1].lower()
    readable = {t.strip() for t in grants.group(1).split(",")}
    assert readable == {
        "filings",
        "facts",
        "claims",
        "verdicts",
        "qa_labels",
        "prices",
        "rules",
        "alerts",
    }
    assert not {"cache", "llm_calls", "push_subscriptions", "swipes"} & readable
