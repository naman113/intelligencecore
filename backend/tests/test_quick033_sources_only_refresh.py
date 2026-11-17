"""Quick 033: a sources-only install (no business database, warehouse on) refreshes dashboards.

The dashboard refresh loop and the forecast step skipped all work unless a business database URL
was set, although every tenant read goes through guardrails.run_readonly_sql, which reads from the
warehouse whenever warehouse mode is on. These tests run the real gate, the real refresh_due and the
real run_readonly_sql routing; only the settings, the agent-memory pool and the warehouse reader
are faked. No warehouse and no business URL keeps today's quiet skip.
"""
import types

import pytest

from app import dashboard_refresh as dr
from app import forecasts, freshness, guardrails, warehouse

URL = "postgresql://ro:x@business.invalid:5432/biz"
WH = "postgresql://wh.invalid:5432/warehouse"
SQL = "SELECT order_status, count(*) AS n FROM src_distributor.orders GROUP BY 1"
ROWS = [{"order_status": "open", "n": 7}, {"order_status": "closed", "n": 3}]

CASES = {
    "sources_only": dict(url="", wh=True),
    "nothing": dict(url="", wh=False),
    "business_url": dict(url=URL, wh=False),
    "business_url_and_warehouse": dict(url=URL, wh=True),
}


def _settings(url: str, wh: bool):
    return types.SimpleNamespace(
        tenant_database_url=url, business_database_url="",
        warehouse_url=WH if wh else "", wh_reader_password="pw" if wh else "",
        sql_row_limit=500)


@pytest.fixture
def env(monkeypatch):
    """Install one settings object for every module on the path; return the call log."""
    st = {"wh_sql": [], "business_pool": 0}

    def use(case: str):
        s = _settings(**CASES[case])
        for mod in (guardrails, warehouse, dr):
            monkeypatch.setattr(mod, "get_settings", lambda s=s: s, raising=False)
        return st

    async def run_on_warehouse(validated, limit):
        st["wh_sql"].append(validated)
        return [dict(r) for r in ROWS]

    async def business_pool():
        st["business_pool"] += 1
        raise AssertionError("the business pool must not be used in warehouse mode")

    async def no_facts(*a, **k):
        raise RuntimeError("no freshness facts in this test")

    async def record(*a, **k):
        return None

    monkeypatch.setattr(guardrails, "_run_on_warehouse", run_on_warehouse)
    monkeypatch.setattr(guardrails, "get_business_pool", business_pool)
    monkeypatch.setattr(freshness, "load_facts", no_facts)
    monkeypatch.setattr(freshness, "for_sql", no_facts)
    monkeypatch.setattr(dr.jobruns, "record", record)
    return use


class FakePool:
    """agent_memory: one due draft pair, every write recorded."""

    def __init__(self):
        self.claimed = 0
        self.executed = []

    async def fetch(self, sql, *args):
        assert sql == dr.CLAIM_SQL, sql[:80]
        self.claimed += 1
        return [{"version_id": 41, "query_id": "orders_by_status", "version": 1,
                 "dashboard_id": "orders-health", "refresh_minutes": 15, "sql": SQL,
                 "query": {"id": "orders_by_status", "sql": SQL}}]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def writes(self, sql):
        return [a for s, a in self.executed if s == sql]


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()

    async def get_pool():
        return p
    monkeypatch.setattr(dr.db, "get_pool", get_pool)
    return p


# -- the gate ---------------------------------------------------------------------

@pytest.mark.parametrize("case,expected", [
    ("sources_only", True), ("nothing", False),
    ("business_url", True), ("business_url_and_warehouse", True)])
def test_reads_configured_matrix(env, case, expected):
    env(case)
    assert guardrails.reads_configured() is expected
    assert dr._tenant_configured() is expected


def test_sources_only_is_reads_without_a_business_database(env):
    """One definition: sources-only = reads configured and no business database."""
    from app import schemadiscovery
    env("sources_only")
    assert guardrails.reads_configured() and not schemadiscovery.business_db_configured()
    env("business_url_and_warehouse")
    assert guardrails.reads_configured() and schemadiscovery.business_db_configured()


# -- refresh_due --------------------------------------------------------------------

async def test_sources_only_claims_runs_on_warehouse_and_writes_rows(env, pool):
    st = env("sources_only")
    assert await dr.refresh_due() is False  # one pair, not a full batch
    assert pool.claimed == 1
    assert st["wh_sql"] == [SQL]  # through run_readonly_sql to the warehouse reader
    assert st["business_pool"] == 0
    ok = pool.writes(dr.UPSERT_OK_SQL)
    assert ok == [(41, "orders_by_status", '["order_status", "n"]',
                   '[{"order_status": "open", "n": 7}, {"order_status": "closed", "n": 3}]', 2)]
    assert pool.writes(dr.CLEAR_VERSION_ERROR_SQL) == [(41,)]
    assert pool.writes(dr.FAIL_SQL) == []


async def test_no_warehouse_and_no_business_url_stays_quiet(env, monkeypatch):
    st = env("nothing")

    async def get_pool():
        raise AssertionError("get_pool called: nothing may be claimed")
    monkeypatch.setattr(dr.db, "get_pool", get_pool)
    assert await dr.refresh_due() is False
    assert st["wh_sql"] == [] and st["business_pool"] == 0


async def test_business_url_unchanged(env, pool, monkeypatch):
    env("business_url")
    calls = []

    async def run_readonly_sql(sql, limit=None):
        calls.append(sql)
        return [dict(r) for r in ROWS]
    monkeypatch.setattr(guardrails, "run_readonly_sql", run_readonly_sql)
    await dr.refresh_due()
    assert pool.claimed == 1 and calls == [SQL]
    assert len(pool.writes(dr.UPSERT_OK_SQL)) == 1


async def test_business_url_and_warehouse_unchanged(env, pool, monkeypatch):
    st = env("business_url_and_warehouse")

    async def linked(timeout):
        return True
    monkeypatch.setattr(warehouse, "wait_linked", linked)
    await dr.refresh_due()
    assert pool.claimed == 1 and st["wh_sql"] == [SQL]
    assert len(pool.writes(dr.UPSERT_OK_SQL)) == 1


# -- forecast_due -------------------------------------------------------------------

@pytest.mark.parametrize("case,runs", [
    ("sources_only", True), ("nothing", False),
    ("business_url", True), ("business_url_and_warehouse", True)])
async def test_forecast_due_matrix(env, monkeypatch, case, runs):
    env(case)
    called = []

    async def run_one_due(info=None):
        called.append(1)
        return None
    monkeypatch.setattr(forecasts, "run_one_due", run_one_due)
    await dr.forecast_due()
    assert bool(called) is runs
