"""Phase 7 insights plumbing: evidence checks, exploration notes, KPI proposals.

Fakes only; no DB, no LLM, no network. Tenant SQL is monkeypatched at
guardrails.run_readonly_sql so the guardrail module itself stays the seam."""

import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import cases_util, guardrails, insights


class FakeConn:
    """Records (sql, args); answers by SQL substring (first matching key wins)."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.log = []

    def _answer(self, sql):
        for key, val in self.answers.items():
            if key in sql:
                return val(sql) if callable(val) else val
        return None

    async def fetchval(self, sql, *args):
        self.log.append((sql, args))
        return self._answer(sql)

    async def fetchrow(self, sql, *args):
        self.log.append((sql, args))
        return self._answer(sql)

    async def fetch(self, sql, *args):
        self.log.append((sql, args))
        return self._answer(sql) or []

    async def execute(self, sql, *args):
        self.log.append((sql, args))
        return "OK"

    @asynccontextmanager
    async def transaction(self):
        yield

    def sqls(self):
        return [s for s, _ in self.log]


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetchval(self, sql, *a):
        return await self.conn.fetchval(sql, *a)

    async def fetchrow(self, sql, *a):
        return await self.conn.fetchrow(sql, *a)

    async def fetch(self, sql, *a):
        return await self.conn.fetch(sql, *a)

    async def execute(self, sql, *a):
        return await self.conn.execute(sql, *a)


def use_conn(monkeypatch, conn):
    async def get_pool():
        return FakePool(conn)
    monkeypatch.setattr(insights, "get_pool", get_pool)
    return conn


def fake_settings(monkeypatch, **kw):
    s = SimpleNamespace(sql_row_limit=500, owner_number="")
    for k, v in kw.items():
        setattr(s, k, v)
    monkeypatch.setattr(insights, "get_settings", lambda: s)
    return s


def approved(monkeypatch, *names):
    rows = [{"schema_name": "public", "table_name": n, "ref": f"public.{n}",
             "description": f"{n} table"} for n in names]

    async def fake():
        return rows
    monkeypatch.setattr(insights, "approved_tables", fake)


# -- pure helpers ----------------------------------------------------------

def test_norm_title():
    assert insights.norm_title("  South Orders -- fell 35%! ") == "south orders fell 35"
    assert insights.norm_title("") == ""


def test_evidence_tables_join():
    sql = "SELECT 1 FROM sales_orders o JOIN customers c ON c.id = o.customer_id"
    assert insights.evidence_tables(sql) == {("", "sales_orders"), ("", "customers")}


def test_evidence_tables_skips_cte_and_table_functions():
    sql = "WITH m AS (SELECT * FROM regions) SELECT * FROM m, generate_series(1,3)"
    assert insights.evidence_tables(sql) == {("", "regions")}
    assert insights.evidence_tables("SELECT * FROM public.regions") == {("public", "regions")}


def test_evidence_tables_rejects_writes():
    with pytest.raises(guardrails.SQLGuardrailError):
        insights.evidence_tables("DELETE FROM regions")


def test_unapproved_refs():
    appr = [{"schema_name": "public", "table_name": "regions"},
            {"schema_name": "sales", "table_name": "orders"}]
    assert insights.unapproved_refs({("", "regions"), ("", "orders")}, appr) == []
    assert insights.unapproved_refs({("public", "regions")}, appr) == []
    assert insights.unapproved_refs({("other", "regions"), ("", "reps")}, appr) == [
        "other.regions", "reps"]


def test_has_hidden_chars():
    assert insights.has_hidden_chars("a‮b")
    assert not insights.has_hidden_chars("a\nb\tc")


# -- check_evidence --------------------------------------------------------

async def test_check_evidence_no_approved_tables(monkeypatch):
    approved(monkeypatch)

    async def boom(sql):
        raise AssertionError("must not run")
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    ev, reason = await insights.check_evidence("SELECT * FROM regions")
    assert ev is None and "no approved tables" in reason


async def test_check_evidence_unapproved_table(monkeypatch):
    approved(monkeypatch, "regions")
    ev, reason = await insights.check_evidence("SELECT * FROM sales_reps")
    assert ev is None and "sales_reps" in reason and "not approved" in reason


async def test_check_evidence_guardrail_and_execution_errors(monkeypatch):
    approved(monkeypatch, "regions")
    ev, reason = await insights.check_evidence("DELETE FROM regions")
    assert ev is None and reason.startswith("evidence_sql rejected by guardrails")

    async def boom(sql):
        raise RuntimeError("boom")
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    ev, reason = await insights.check_evidence("SELECT * FROM regions")
    assert ev is None and reason.startswith("evidence_sql failed: RuntimeError")


async def test_check_evidence_zero_rows(monkeypatch):
    approved(monkeypatch, "regions")

    async def none(sql):
        return []
    monkeypatch.setattr(guardrails, "run_readonly_sql", none)
    ev, reason = await insights.check_evidence("SELECT * FROM regions")
    assert ev is None and "0 rows" in reason


async def test_check_evidence_success_shape(monkeypatch):
    approved(monkeypatch, "regions")
    fake_settings(monkeypatch)
    rows = [{"a": Decimal("1.5"), "d": date(2026, 8, 1), "s": "x" * 500}
            for _ in range(7)]

    async def run(sql):
        return rows
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    ev, reason = await insights.check_evidence("SELECT * FROM regions")
    assert reason == ""
    assert ev["row_count"] == 7 and ev["columns"] == ["a", "d", "s"]
    assert len(ev["sample"]) == 5 and ev["truncated"] is False
    assert ev["sample"][0]["d"] == "2026-08-01"
    assert len(ev["sample"][0]["s"]) == 200
    json.dumps(ev)
    fake_settings(monkeypatch, sql_row_limit=7)
    ev, _ = await insights.check_evidence("SELECT * FROM regions")
    assert ev["truncated"] is True


async def test_check_evidence_flags_relative_dates(monkeypatch):
    approved(monkeypatch, "regions")
    fake_settings(monkeypatch)

    async def run(sql):
        return [{"a": 1}]
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    ev, _ = await insights.check_evidence(
        "SELECT * FROM regions WHERE d > current_date - 7")
    assert ev["relative_dates"] is True
    ev, _ = await insights.check_evidence(
        "SELECT * FROM regions WHERE d > DATE '2026-08-01'")
    assert ev["relative_dates"] is False


# -- record_note -----------------------------------------------------------

EV = {"row_count": 3, "columns": ["a"], "sample": [{"a": 1}], "truncated": False}


async def test_record_note_locks_then_dedupes(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn({"regexp_replace": 1}))

    async def never(*a, **k):
        raise AssertionError("must not write")
    monkeypatch.setattr(cases_util, "add_case_note", never)
    eid, cid, reason = await insights.record_note("T", "f", "SELECT 1", "info", EV)
    assert eid is None and cid is None and "already recorded" in reason
    assert ("pg_advisory_xact_lock(hashtextextended('ic:exploration-insights', 0))"
            in conn.sqls()[0])


async def test_record_note_run_cap(monkeypatch):
    use_conn(monkeypatch, FakeConn({"count(*)": 5}))

    async def never(*a, **k):
        raise AssertionError("must not write")
    monkeypatch.setattr(cases_util, "add_case_note", never)
    eid, _, reason = await insights.record_note(
        "T", "f", "SELECT 1", "info", EV, run_id=42)
    assert eid is None and "5" in reason

    conn2 = use_conn(monkeypatch, FakeConn({"SELECT id FROM cases": 3}))

    async def ok(*a, **k):
        return 1
    monkeypatch.setattr(cases_util, "add_case_note", ok)
    await insights.record_note("T", "f", "SELECT 1", "info", EV, run_id=None)
    assert not any("count(*)" in s for s in conn2.sqls())


async def test_record_note_uses_given_case(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn())
    seen = {}

    async def spy(conn_, case_id, text, **k):
        seen["case"] = case_id
        return 11
    monkeypatch.setattr(cases_util, "add_case_note", spy)
    res = await insights.record_note("T", "f", "SELECT 1", "info", EV, case_id=9)
    assert res == (11, 9, "") and seen["case"] == 9
    assert not any("INSERT INTO cases" in s for s in conn.sqls())


async def test_record_note_find_or_create_carrier(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn({"INSERT INTO cases": 77}))

    async def spy(conn_, case_id, text, **k):
        return 5
    monkeypatch.setattr(cases_util, "add_case_note", spy)
    eid, cid, _ = await insights.record_note("T", "f", "SELECT 1", "info", EV)
    assert cid == 77
    ins = [a for s, a in conn.log if "INSERT INTO cases" in s]
    assert ins == [("Insights from data exploration", "exploration")]

    conn2 = use_conn(monkeypatch, FakeConn({"SELECT id FROM cases": 12}))
    _, cid2, _ = await insights.record_note("T", "f", "SELECT 1", "info", EV)
    assert cid2 == 12
    assert not any("INSERT INTO cases" in s for s in conn2.sqls())


async def test_record_note_writes_exploration_note(monkeypatch):
    use_conn(monkeypatch, FakeConn({"SELECT id FROM cases": 4}))
    seen = {}

    async def spy(conn_, case_id, text, **k):
        seen.update(case=case_id, text=text, kw=k)
        return 5
    monkeypatch.setattr(cases_util, "add_case_note", spy)
    await insights.record_note("South fell", "orders dropped", "SELECT 1 FROM regions",
                               "warning", EV, run_id=42)
    assert seen["kw"]["kind"] == "exploration"
    meta = seen["kw"]["meta"]
    assert set(meta) >= {"origin", "title", "severity", "evidence_sql", "evidence",
                         "task_run_id"}
    assert meta["origin"] == "exploration" and meta["task_run_id"] == 42
    assert seen["text"].startswith("South fell")
    assert "SELECT 1 FROM regions" in seen["text"]


# -- KPI proposals and playbook context (07-01 Task 3) ---------------------

def test_parse_cadence():
    p = insights.parse_cadence
    assert p("hourly") == 3600 and p("daily") == 86400 and p("weekly") == 604800
    assert p(" Daily ") == 86400
    assert p("30") == 1800 and p("5") == 900
    assert p("100000") == 604800
    assert p("bogus") is None and p("") is None


def test_cadence_label():
    assert insights.cadence_label(3600) == "hourly"
    assert insights.cadence_label(86400) == "daily"
    assert insights.cadence_label(604800) == "weekly"
    assert insights.cadence_label(1800) == "every 30 min"


def test_validate_kpi():
    v = insights.validate_kpi
    assert v("", "c") and v("x" * 121, "c") and v("t", "") and v("t", "c" * 2001)
    assert v("t‮x", "c") and v("t", "c‮x")
    assert v("Weekly orders", "orders per week drops 20%") is None


async def test_kpi_conflict_existing_watch(monkeypatch):
    use_conn(monkeypatch, FakeConn({"FROM watches": {"id": 3, "title": "Weekly orders"}}))
    r = await insights.kpi_conflict("weekly orders")
    assert r and "already watched" in r


async def test_kpi_conflict_prior_proposal(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn(
        {"FROM actions": {"id": 8, "status": "rejected"}}))
    r = await insights.kpi_conflict("weekly orders")
    assert r and "already proposed or declined" in r
    sql = next(s for s in conn.sqls() if "FROM actions" in s)
    assert "'failed'" in sql


async def test_kpi_conflict_run_cap(monkeypatch):
    use_conn(monkeypatch, FakeConn({"count(*)": 2}))
    r = await insights.kpi_conflict("k", run_id=42)
    assert r and "2" in r
    conn = use_conn(monkeypatch, FakeConn())
    assert await insights.kpi_conflict("k", run_id=None) is None
    assert not any("count(*)" in s for s in conn.sqls())
    use_conn(monkeypatch, FakeConn({"count(*)": 0}))
    assert await insights.kpi_conflict("k", run_id=42) is None


GOOD = {"proposal_key": "pk1", "title": "Weekly orders", "condition": "drops 20%",
        "cadence": "daily"}


async def test_create_watch_rejects_invalid_payload(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn())
    fake_settings(monkeypatch)
    for bad in ({**GOOD, "proposal_key": ""}, {**GOOD, "title": ""},
                {**GOOD, "title": "a‮b"}, {**GOOD, "cadence": "bogus"},
                {k: v for k, v in GOOD.items() if k != "proposal_key"}):
        assert await insights.create_watch_from_proposal(bad) is False
    assert not any("INSERT" in s for s in conn.sqls())


async def test_create_watch_inserts_hardcoded_provenance(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn({"INSERT INTO watches": 5}))
    fake_settings(monkeypatch, owner_number="919999")
    payload = {**GOOD, "kind": "user", "origin": "owner", "thread_id": "x",
               "one_shot": True, "cadence_seconds": 5}
    assert await insights.create_watch_from_proposal(payload) is True
    sql, args = next((s, a) for s, a in conn.log if "INSERT INTO watches" in s)
    assert "ON CONFLICT (origin_ref) WHERE origin_ref IS NOT NULL DO NOTHING" in sql
    assert args == ("919999", "kpi", "Weekly orders", "drops 20%", 900, False,
                    "exploration", "pk1", None, None)
    conn2 = use_conn(monkeypatch, FakeConn({"INSERT INTO watches": 6}))
    fake_settings(monkeypatch)
    await insights.create_watch_from_proposal({**GOOD, "cadence_seconds": 10 ** 9})
    _, a2 = next((s, a) for s, a in conn2.log if "INSERT INTO watches" in s)
    assert a2[0] == "dashboard" and a2[4] == 604800


async def test_create_watch_conflict_counts_as_done(monkeypatch):
    use_conn(monkeypatch, FakeConn({"SELECT 1 FROM watches": 1}))
    fake_settings(monkeypatch)
    assert await insights.create_watch_from_proposal(GOOD) is True
    use_conn(monkeypatch, FakeConn())
    assert await insights.create_watch_from_proposal(GOOD) is False


async def test_exploration_context_no_tables(monkeypatch):
    approved(monkeypatch)
    use_conn(monkeypatch, FakeConn())
    t = await insights.exploration_context()
    assert ("APPROVED TABLES: none. Nothing has been approved for study yet. "
            "Stop now without calling record_insight or propose_kpi.") in t


async def test_exploration_context_lists_tables_titles_kpis(monkeypatch):
    approved(monkeypatch, "sales_orders", "customers")
    use_conn(monkeypatch, FakeConn({
        "case_events": [{"title": "South fell", "day": date(2026, 8, 3)}],
        "FROM watches": [{"title": "Weekly orders"}],
        "FROM actions": [{"title": "Late payments"}]}))
    t = await insights.exploration_context(include_kpis=True)
    assert "APPROVED TABLES (study only these):" in t
    assert "- public.sales_orders: sales_orders table" in t
    assert "ALREADY RECORDED (last 30 days; do not repeat):" in t
    assert "South fell" in t and "2026-08-03" in t
    assert "ALREADY WATCHED, PROPOSED OR DECLINED KPIs (do not propose again):" in t
    assert "Weekly orders" in t and "Late payments" in t
    t2 = await insights.exploration_context(include_kpis=False)
    assert "ALREADY WATCHED" not in t2


async def test_exploration_context_caps(monkeypatch):
    approved(monkeypatch, *[f"t{i}" for i in range(45)])
    use_conn(monkeypatch, FakeConn({
        "case_events": [{"title": f"n{i}", "day": date(2026, 8, 3)} for i in range(35)],
        "FROM watches": [{"title": f"w{i}"} for i in range(45)]}))
    t = await insights.exploration_context()
    assert t.count("- public.t") == 40
    assert sum(1 for ln in t.splitlines() if " n" in ln and "2026-08-03" in ln) == 30
    assert sum(1 for ln in t.splitlines() if ln.startswith("- w")) == 40


async def test_record_note_run_cap_param_is_text(monkeypatch):
    """Regression (07-06 live run): asyncpg rejects an int for a $1::text parameter."""
    conn = use_conn(monkeypatch, FakeConn({"count(*)": 5}))
    await insights.record_note("T", "f", "SELECT 1", "info", EV, run_id=42)
    cap = [a for s, a in conn.log if "task_run_id' = $1::text" in s]
    assert cap and cap[0] == ("42",)


# Phase 7 review CR-01 / WR-02 regressions
import pytest as _pytest_cr01


@_pytest_cr01.mark.parametrize("sql", [
    "SELECT table_to_xml('pg_authid', true, false, '')",
    "SELECT * FROM ts_stat('SELECT 1')",
    "SELECT ts_rewrite('a'::tsquery, 'SELECT 1')",
    "SELECT schema_to_xml('public', true, false, '')",
    "SELECT xmlagg(table_to_xml('secret', true, false, '')) FROM customers",
])
def test_string_sql_functions_rejected(sql):
    from app import guardrails, insights
    with _pytest_cr01.raises(guardrails.SQLGuardrailError):
        insights.evidence_tables(sql)


def test_norm_title_unicode():
    from app import insights
    assert insights.norm_title("销售下降") == "销售下降"
    assert insights.norm_title("बिक्री गिरी") != ""
    assert insights.norm_title("  A--b!! ") == "a b"


# ── Phase 17 (CONS-02): stale/fresh events are never alerts ──
def test_watch_sql_reads_only_fire_events():
    assert "we.kind = 'fire'" in insights._WATCH_SQL


async def test_get_insight_watch_event_filters_kind(monkeypatch):
    conn = use_conn(monkeypatch, FakeConn())
    out = await insights.get_insight("watch_event:5", thread_id="t")
    assert out is None
    q = [s for s in conn.sqls() if "FROM watch_events" in s]
    assert q and all("we.kind = 'fire'" in s for s in q)
