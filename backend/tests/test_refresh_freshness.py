"""Phase 17 (CONS-03): freshness-aware dashboard refresh."""
import datetime as dt
import json

import pytest

from app import dashboard_refresh as dr
from app import freshness, kpihistory, warehouse

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
SYNC = NOW - dt.timedelta(hours=1)


class Pool:
    def __init__(self, claims=()):
        self.claims = list(claims)
        self.executed = []

    async def fetch(self, sql, *a):
        return self.claims

    async def execute(self, sql, *a):
        self.executed.append((sql, a))


def pair(**kw):
    p = {"version_id": 1, "query_id": "q", "version": 1, "dashboard_id": "d",
         "sql": "select * from t", "query": None, "sync_aligned": False,
         "inputs_synced_at": None, "refreshed_at": None, "error": None, "reads": []}
    p.update(kw)
    return p


def fr(**kw):
    d = dict(as_of=SYNC, stale=False, reads=("public.t",), import_reads=("public.t",),
             live_reads=(), inputs_synced_at=SYNC, failed=False)
    d.update(kw)
    return freshness.Freshness(**d)


@pytest.fixture
def env(monkeypatch):
    calls = []

    async def ex(sql):
        calls.append(sql)
        return [{"n": 1}]
    monkeypatch.setattr(dr, "_execute", ex)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    state = {"f": fr()}

    async def for_sql(sql, cache=None, now=None):
        return state["f"]
    monkeypatch.setattr(freshness, "for_sql", for_sql)
    return calls, state


def fresh_row(pool):
    return [a for s, a in pool.executed if s == dr.FRESHNESS_SQL][0]


def facts_with(last, **row):
    c = freshness.FactCache()
    d = {"warehouse_schema": "public", "warehouse_table": "Order",
         "mode": "import", "last_success_at": last}
    d.update(row)
    c.add_table(d)
    return c


def test_claim_sql_text():
    assert ("GREATEST(COALESCE(s.inputs_synced_at, '-infinity'::timestamptz), "
            "COALESCE(s.attempted_at, '-infinity'::timestamptz))") in dr.CLAIM_SQL
    assert "ORDER BY s.next_refresh_at" in dr.CLAIM_SQL
    assert "s.next_refresh_at <= now() OR" in dr.CLAIM_SQL


@pytest.mark.asyncio
async def test_import_only_is_sync_aligned(env):
    pool = Pool()
    assert await dr._refresh_pair(pool, pair()) is None
    a = fresh_row(pool)
    assert a[2] == SYNC and a[3] == ["public.t"] and a[4] is True and a[5] == SYNC


@pytest.mark.asyncio
async def test_mixed_case_reads_canonical(env):
    env[1]["f"] = fr(reads=("public.Order",), import_reads=("public.Order",))
    pool = Pool()
    await dr._refresh_pair(pool, pair())
    assert fresh_row(pool)[3] == ["public.Order"]


@pytest.mark.asyncio
async def test_live_read_not_aligned(env):
    env[1]["f"] = fr(live_reads=("public.l",), import_reads=())
    pool = Pool()
    await dr._refresh_pair(pool, pair())
    a = fresh_row(pool)
    assert a[4] is False and a[2] > SYNC


@pytest.mark.asyncio
async def test_clock_function_not_aligned(env):
    pool = Pool()
    await dr._refresh_pair(pool, pair(sql="select now() from t"))
    assert fresh_row(pool)[4] is False


@pytest.mark.asyncio
async def test_outside_warehouse(env, monkeypatch):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    pool = Pool()
    await dr._refresh_pair(pool, pair())
    a = fresh_row(pool)
    assert a[3] == [] and a[4] is False and a[5] is None and a[2] >= NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failed", "raises"])
async def test_freshness_failure_never_fails(env, monkeypatch, mode):
    if mode == "failed":
        env[1]["f"] = fr(failed=True)
    else:
        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(freshness, "for_sql", boom)
    pool = Pool()
    assert await dr._refresh_pair(pool, pair()) is None
    a = fresh_row(pool)
    assert a[3] == [] and a[4] is False and a[2] >= NOW


def aligned(**kw):
    d = dict(sync_aligned=True, error=None, refreshed_at=NOW - dt.timedelta(minutes=5),
             inputs_synced_at=SYNC, reads=["public.Order"])
    d.update(kw)
    return pair(**d)


@pytest.mark.asyncio
async def test_confirm_only(env):
    calls, _ = env
    pool = Pool()
    assert await dr._refresh_pair(pool, aligned(), facts=facts_with(SYNC)) is None
    assert [s for s, _ in pool.executed] == [dr.CONFIRM_SQL] and calls == []


@pytest.mark.asyncio
async def test_rerun_after_sync_mixed_case(env):
    calls, _ = env
    pool = Pool()
    f = facts_with(SYNC + dt.timedelta(minutes=10))
    await dr._refresh_pair(pool, aligned(), facts=f)
    assert len(calls) == 1 and dr.CONFIRM_SQL not in [s for s, _ in pool.executed]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"refreshed_at": None}, {"refreshed_at": NOW - dt.timedelta(hours=30)},
    {"refreshed_at": NOW - dt.timedelta(days=2)}, {"error": "query failed"},
])
async def test_rerun_conditions(env, kw):
    calls, _ = env
    await dr._refresh_pair(Pool(), aligned(**kw), facts=facts_with(SYNC))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_facts_reruns(env):
    calls, _ = env
    await dr._refresh_pair(Pool(), aligned(), facts=None)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_kpi_history_and_forecast(env, monkeypatch):
    marker = NOW - dt.timedelta(days=1)

    async def latest(wid):
        assert wid == 7
        return marker

    async def kpi(q):
        return [{"d": 1}]

    async def fc(q):
        return [{"d": 1}]
    monkeypatch.setattr(kpihistory, "latest_as_of", latest)
    monkeypatch.setattr(dr, "_kpi_rows", kpi)
    monkeypatch.setattr(dr, "_forecast_rows", fc)
    p = Pool()
    await dr._refresh_pair(p, pair(sql=None, query=json.dumps({"source": "kpi_history", "watch_id": 7})))
    a = fresh_row(p)
    assert a[2] == marker and a[3] == [] and a[4] is False
    p = Pool()
    await dr._refresh_pair(p, pair(sql=None, query={"source": "forecast", "forecast_id": 1}))
    a = fresh_row(p)
    assert a[2] is None and a[4] is False


# ---- Phase 17 gap closure (17-15): confirm only while every read is still imported ----
_NOT_IMPORTED = [
    lambda: facts_with(SYNC, mode="live"),
    lambda: facts_with(SYNC, mode="live", pending_mode="import"),
    lambda: facts_with(SYNC, mode="import", pending_mode="import"),
    lambda: freshness.FactCache(),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mk", _NOT_IMPORTED)
async def test_confirm_reruns_when_read_not_imported(env, mk):
    calls, _ = env
    pool = Pool()
    await dr._refresh_pair(pool, aligned(), facts=mk())
    assert len(calls) == 1
    assert dr.CONFIRM_SQL not in [s for s, _ in pool.executed]


@pytest.mark.asyncio
async def test_rerun_after_switch_back_to_live_writes_live_freshness(env):
    calls, state = env
    state["f"] = fr(reads=("public.Order",), import_reads=(), live_reads=("public.Order",),
                    inputs_synced_at=None, as_of=NOW)
    pool = Pool()
    await dr._refresh_pair(pool, aligned(), facts=facts_with(SYNC, mode="live"))
    a = fresh_row(pool)
    assert a[2] > SYNC and a[2] >= NOW
    assert a[3] == ["public.Order"] and a[4] is False and a[5] is None


def test_should_confirm_requires_every_read_imported():
    cache = facts_with(SYNC)
    assert dr._should_confirm(aligned(reads=["public.Order", "public.Gone"]), cache, NOW) is False
    assert dr._should_confirm(aligned(reads=[]), cache, NOW) is False
    assert dr._should_confirm(aligned(), cache, NOW) is True
