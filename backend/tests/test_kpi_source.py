"""Phase 6 (plan 06-02): the kpi_history query source.

Validator branch, kpihistory.daily_rows / watch_in_scope, and the refresh-loop
branch. Real-Postgres proof lives in test_dashboard_kpi_db.py.
"""
import datetime

import pytest

from app import dashboards, kpihistory
from app.dashboards import DashboardError, validate_queries


def _kpi(**kw):
    q = {"id": "trend", "source": "kpi_history", "watch_id": 7}
    q.update(kw)
    return q


# ── validator ──

def test_kpi_normalizes_with_defaults():
    assert validate_queries([_kpi()]) == [
        {"id": "trend", "source": "kpi_history", "watch_id": 7, "days": 30,
         "refresh_minutes": 15}]


def test_kpi_explicit_values():
    out = validate_queries([_kpi(days=90, refresh_minutes=60)])
    assert out[0]["days"] == 90 and out[0]["refresh_minutes"] == 60


@pytest.mark.parametrize("kw", [
    {"days": 0}, {"days": 401}, {"days": "30"}, {"days": True},
    {"watch_id": 0}, {"watch_id": -1}, {"watch_id": "7"}, {"watch_id": True},
    {"watch_id": 2 ** 63},
    {"refresh_minutes": 4}, {"refresh_minutes": 1441}, {"refresh_minutes": True},
    {"sql": "SELECT 1"}, {"params": {}}, {"id": "Bad Id"}, {"id": 5},
])
def test_kpi_rejects(kw):
    with pytest.raises(DashboardError) as ei:
        validate_queries([_kpi(**kw)])
    assert ei.value.status == 422


def test_kpi_missing_watch_id():
    q = _kpi()
    del q["watch_id"]
    with pytest.raises(DashboardError):
        validate_queries([q])


def test_kpi_message_names_query_and_field():
    with pytest.raises(DashboardError) as ei:
        validate_queries([_kpi(days=0)])
    assert "trend" in str(ei.value) and "days" in str(ei.value)
    with pytest.raises(DashboardError) as ei:
        validate_queries([_kpi(watch_id=0)])
    assert "trend" in str(ei.value) and "watch_id" in str(ei.value)


def test_kpi_duplicate_ids_across_shapes():
    with pytest.raises(DashboardError):
        validate_queries([{"id": "trend", "sql": "SELECT 1"}, _kpi()])
    with pytest.raises(DashboardError):
        validate_queries([_kpi(), {"id": "trend", "sql": "SELECT 1"}])


def test_mixed_list_in_order():
    out = validate_queries([{"id": "a", "sql": "SELECT 1"}, _kpi(id="b")])
    assert [q["id"] for q in out] == ["a", "b"]
    assert "source" not in out[0] and out[1]["source"] == "kpi_history"


@pytest.mark.parametrize("q", [
    {"id": "a", "sql": "SELECT 1", "source": "x"},
    {"id": "a", "source": "x", "watch_id": 1},
    {"id": "a", "sql": "SELECT 1", "source": "kpi_history"},
])
def test_other_sources_rejected(q):
    with pytest.raises(DashboardError):
        validate_queries([q])


# ── kpihistory readers ──

class _Pool:
    def __init__(self, watch=None, rows=()):
        self.watch, self.rows = watch, list(rows)
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.watch["id"] if self.watch else None

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.watch

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows


def _install(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(kpihistory, "get_pool", gp)


@pytest.mark.asyncio
async def test_daily_rows_unknown_watch(monkeypatch):
    _install(monkeypatch, _Pool(watch=None))
    with pytest.raises(LookupError, match="no longer exists"):
        await kpihistory.daily_rows(9, 30)


@pytest.mark.asyncio
async def test_daily_rows_sql_shape_and_clamp(monkeypatch):
    rows = [{"d": datetime.date(2026, 10, 1), "value": 3.0, "min": 1.0, "max": 4.0,
             "n": 2, "metric": "n"}]
    pool = _Pool(watch={"id": 9}, rows=rows)
    _install(monkeypatch, pool)
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    out = await kpihistory.daily_rows(9, 500)
    kind, sql, args = [c for c in pool.calls if c[0] == "fetch"][0]
    assert "AT TIME ZONE" in sql and "IS NOT DISTINCT FROM" in sql
    assert "to_char(date_trunc" not in sql
    assert args == (9, 400, "Asia/Kolkata")
    assert out == [{"day": "2026-10-01", "value": 3.0, "min": 1.0, "max": 4.0,
                    "n": 2, "metric": "n"}]
    assert isinstance(out[0]["n"], int)
    assert set(out[0]) == {"day", "value", "min", "max", "n", "metric"}


@pytest.mark.asyncio
@pytest.mark.parametrize("watch,thread,ok", [
    ({"id": 1, "title": "t", "kind": "kpi", "status": "paused", "thread_id": "x"}, "any", True),
    ({"id": 1, "title": "t", "kind": "kpi", "status": "paused", "thread_id": "x"}, None, True),
    ({"id": 1, "title": "t", "kind": "user", "status": "active", "thread_id": "x"}, "x", True),
    ({"id": 1, "title": "t", "kind": "user", "status": "active", "thread_id": "x"}, "y", False),
    ({"id": 1, "title": "t", "kind": "user", "status": "active", "thread_id": "x"}, None, False),
    (None, "x", False),
])
async def test_watch_in_scope(monkeypatch, watch, thread, ok):
    _install(monkeypatch, _Pool(watch=watch))
    res = await kpihistory.watch_in_scope(1, thread)
    if ok:
        assert res == {"id": 1, "title": "t", "kind": watch["kind"], "status": watch["status"]}
    else:
        assert res is None


# ── refresh-loop branch ──

import asyncio
import json

from app import dashboard_refresh as dr


class _LoopPool:
    def __init__(self, claims=()):
        self.claims = list(claims)
        self.executed = []

    async def fetch(self, sql, *args):
        assert sql == dr.CLAIM_SQL
        return self.claims

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _pair(qid="trend", sql=None, query=None, vid=1):
    p = {"version_id": vid, "query_id": qid, "version": 1, "dashboard_id": "d", "sql": sql}
    if query is not None:
        p["query"] = query
    return p


def _wire(monkeypatch, pool):
    monkeypatch.setattr(dr, "_tenant_configured", lambda: True)

    async def gp():
        return pool
    monkeypatch.setattr(dr.db, "get_pool", gp)

    async def no_tenant(sql):
        raise AssertionError("tenant path used")
    monkeypatch.setattr(dr, "_execute", no_tenant)


KQ = {"id": "trend", "source": "kpi_history", "watch_id": 12, "days": 14, "refresh_minutes": 15}


def test_claim_and_seed_sql_shape():
    assert "AS query" in dr.CLAIM_SQL and "AS sql" in dr.CLAIM_SQL
    assert "q->>'source' = 'kpi_history'" in dr.SEED_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("as_str", [False, True])
async def test_kpi_pair_uses_daily_rows(monkeypatch, as_str):
    pool = _LoopPool([_pair(query=json.dumps(KQ) if as_str else KQ)])
    _wire(monkeypatch, pool)
    seen = []

    async def fake(watch_id, days):
        seen.append((watch_id, days))
        return [{"day": "2026-10-01", "value": 2.0, "min": 1.0, "max": 3.0, "n": 2,
                 "metric": "n"}]
    monkeypatch.setattr(dr.kpihistory, "daily_rows", fake)
    await dr.refresh_due()
    assert seen == [(12, 14)]
    ex = pool.executed
    assert ex[1][0] == dr.UPSERT_OK_SQL and ex[1][1][:2] == (1, "trend")
    assert json.loads(ex[1][1][2]) == ["day", "value", "min", "max", "n", "metric"]
    assert ex[1][1][4] == 1
    assert ex[2] == (dr.CLEAR_VERSION_ERROR_SQL, (1,))


@pytest.mark.asyncio
async def test_deleted_watch_isolated(monkeypatch):
    other = dict(KQ, id="other", watch_id=3)
    pool = _LoopPool([_pair(query=KQ), _pair("other", query=other)])
    _wire(monkeypatch, pool)

    async def fake(watch_id, days):
        if watch_id == 12:
            raise LookupError("watch 12 no longer exists")
        return [{"day": "2026-10-01", "value": 1.0, "min": 1.0, "max": 1.0, "n": 1,
                 "metric": "n"}]
    monkeypatch.setattr(dr.kpihistory, "daily_rows", fake)
    await dr.refresh_due()
    ex = pool.executed
    assert ex[1] == (dr.FAIL_SQL, (1, "trend", "query failed", dr.FAIL_RETRY_MAX_MINUTES))
    assert ex[2][0] == dr.VERSION_ERROR_SQL
    assert ex[2][1][1].startswith("query trend: query failed: watch 12 no longer exists")
    assert ex[3][0] == dr.UPSERT_OK_SQL and ex[3][1][1] == "other"


@pytest.mark.asyncio
async def test_kpi_timeout_categorized(monkeypatch):
    pool = _LoopPool([_pair(query=KQ)])
    _wire(monkeypatch, pool)

    async def fake(watch_id, days):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(dr.kpihistory, "daily_rows", fake)
    await dr.refresh_due()
    assert pool.executed[1] == (dr.FAIL_SQL, (1, "trend", "query timed out",
                                              dr.FAIL_RETRY_MAX_MINUTES))


@pytest.mark.asyncio
async def test_sql_none_without_kpi_still_failed(monkeypatch):
    pool = _LoopPool([_pair(query=None)])
    _wire(monkeypatch, pool)
    await dr.refresh_due()
    assert pool.executed[1][0] == dr.FAIL_SQL
    assert "not declared" in pool.executed[2][1][1]
