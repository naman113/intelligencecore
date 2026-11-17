"""Phase 13.1 (plan 13.1-04): the forecast dashboard query source.

Validator, forecasts.dashboard_rows / def_usable, the refresh-loop seed and branch,
preview sampling and the save-time check. The real-Postgres proof lives in
test_forecasts_db.py.
"""
import asyncio
import datetime
import json

import pytest

from app import dashboards, dashboardtools, forecasts
from app import dashboard_refresh as dr
from app.dashboards import DashboardError, validate_queries


def _fq(**kw):
    q = {"id": "fc", "source": "forecast", "forecast_id": 3}
    q.update(kw)
    return q


# -- validator ---------------------------------------------------------------

def test_defaults_and_explicit_refresh():
    assert validate_queries([_fq()]) == [
        {"id": "fc", "source": "forecast", "forecast_id": 3, "refresh_minutes": 15}]
    assert validate_queries([_fq(refresh_minutes=60)])[0]["refresh_minutes"] == 60


@pytest.mark.parametrize("kw", [
    {"sql": "SELECT 1"}, {"days": 30}, {"watch_id": 1},
    {"forecast_id": 0}, {"forecast_id": -1}, {"forecast_id": "3"}, {"forecast_id": 3.0},
    {"forecast_id": True}, {"forecast_id": 2 ** 63},
    {"refresh_minutes": 4}, {"refresh_minutes": 1441}, {"refresh_minutes": True},
    {"id": "Bad Id"}, {"id": 5},
])
def test_rejects(kw):
    with pytest.raises(DashboardError) as ei:
        validate_queries([_fq(**kw)])
    assert ei.value.status == 422


def test_missing_forecast_id():
    q = _fq()
    del q["forecast_id"]
    with pytest.raises(DashboardError):
        validate_queries([q])


def test_duplicate_id_across_sources():
    with pytest.raises(DashboardError):
        validate_queries([{"id": "fc", "sql": "SELECT 1"}, _fq()])


def test_messages_name_query_and_field():
    with pytest.raises(DashboardError) as ei:
        validate_queries([_fq(forecast_id=0)])
    assert "fc" in str(ei.value) and "forecast_id" in str(ei.value)
    with pytest.raises(DashboardError) as ei:
        validate_queries([_fq(refresh_minutes=4)])
    assert "fc" in str(ei.value) and "refresh_minutes" in str(ei.value)


def test_mixed_order_kept():
    out = validate_queries([
        {"id": "a", "sql": "SELECT 1"},
        {"id": "k", "source": "kpi_history", "watch_id": 2},
        _fq()])
    assert [q["id"] for q in out] == ["a", "k", "fc"]


# -- reader ------------------------------------------------------------------

class _ReadPool:
    def __init__(self, usable=True, recs=()):
        self.usable, self.recs = usable, list(recs)

    async def fetchrow(self, sql, *a):
        return {"id": a[0], "name": "n", "status": "active"} if self.usable else None

    async def fetch(self, sql, *a):
        return self.recs


def _rec(day, actual=None, mean=None, lo=None, hi=None):
    return {"period": datetime.date(2026, 10, day), "actual": actual, "mean": mean,
            "lo80": lo, "hi80": hi, "method": "ets", "wape": 0.12,
            "run_at": datetime.datetime(2026, 10, 25, 4, tzinfo=datetime.timezone.utc)}


def _pool(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(forecasts, "get_pool", gp)


@pytest.mark.asyncio
async def test_dashboard_rows_shape(monkeypatch):
    _pool(monkeypatch, _ReadPool(recs=[_rec(1, actual=5), _rec(2, mean=6, lo=4, hi=8)]))
    rows = await forecasts.dashboard_rows(3)
    assert rows[0] == {"period": "2026-10-01", "actual": 5.0, "forecast": None, "low": None,
                       "high": None, "method": "ets", "wape": 0.12,
                       "as_of": "2026-10-25T04:00:00+00:00"}
    assert rows[1]["actual"] is None and (rows[1]["forecast"], rows[1]["low"],
                                          rows[1]["high"]) == (6.0, 4.0, 8.0)


@pytest.mark.asyncio
async def test_dashboard_rows_unknown_and_empty(monkeypatch):
    _pool(monkeypatch, _ReadPool(usable=False))
    with pytest.raises(LookupError):
        await forecasts.dashboard_rows(3)
    _pool(monkeypatch, _ReadPool())
    assert await forecasts.dashboard_rows(3) == []


@pytest.mark.asyncio
async def test_def_usable_rejects_non_int(monkeypatch):
    _pool(monkeypatch, _ReadPool())
    assert await forecasts.def_usable(True) is None
    assert await forecasts.def_usable("3") is None
    assert (await forecasts.def_usable(3))["id"] == 3


# -- refresh loop ------------------------------------------------------------

class _LoopPool:
    def __init__(self, claims=()):
        self.claims = list(claims)
        self.executed = []

    async def fetch(self, sql, *args):
        assert sql == dr.CLAIM_SQL
        return self.claims

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _pair(qid="fc", sql=None, query=None, vid=1):
    return {"version_id": vid, "query_id": qid, "version": 1, "dashboard_id": "d",
            "sql": sql, "query": query}


def _wire(monkeypatch, pool):
    monkeypatch.setattr(dr, "_tenant_configured", lambda: True)

    async def gp():
        return pool
    monkeypatch.setattr(dr.db, "get_pool", gp)

    async def no_tenant(sql):
        raise AssertionError("tenant path used")
    monkeypatch.setattr(dr, "_execute", no_tenant)


FQ = {"id": "fc", "source": "forecast", "forecast_id": 3, "refresh_minutes": 15}
ROW = {"period": "2026-10-01", "actual": 1.0, "forecast": None, "low": None, "high": None,
       "method": "ets", "wape": 0.1, "as_of": "x"}


def test_seed_sql_has_both_sources():
    assert "q->>'source' = 'kpi_history'" in dr.SEED_SQL
    assert "q->>'source' = 'forecast'" in dr.SEED_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("as_str", [False, True])
async def test_pair_uses_dashboard_rows(monkeypatch, as_str):
    pool = _LoopPool([_pair(query=json.dumps(FQ) if as_str else FQ)])
    _wire(monkeypatch, pool)
    seen = []

    async def fake(fid):
        seen.append(fid)
        return [ROW]
    monkeypatch.setattr(forecasts, "dashboard_rows", fake)
    await dr.refresh_due()
    assert seen == [3]
    ex = pool.executed
    assert ex[1][0] == dr.UPSERT_OK_SQL and ex[1][1][:2] == (1, "fc") and ex[1][1][4] == 1
    assert json.loads(ex[1][1][2])[:3] == ["period", "actual", "forecast"]


@pytest.mark.asyncio
async def test_retired_isolated_and_sql_still_runs(monkeypatch):
    pool = _LoopPool([_pair(query=FQ), _pair("s", sql="SELECT 1", query={"id": "s", "sql": "SELECT 1"})])
    _wire(monkeypatch, pool)

    async def fake(fid):
        raise LookupError("gone")
    monkeypatch.setattr(forecasts, "dashboard_rows", fake)

    async def ok(sql):
        return [{"a": 1}]
    monkeypatch.setattr(dr, "_execute", ok)
    await dr.refresh_due()
    ex = pool.executed
    assert ex[1] == (dr.FAIL_SQL, (1, "fc", "query failed", dr.FAIL_RETRY_MAX_MINUTES))
    assert ex[3][0] == dr.UPSERT_OK_SQL and ex[3][1][1] == "s"


@pytest.mark.asyncio
async def test_timeout_categorized(monkeypatch):
    pool = _LoopPool([_pair(query=FQ)])
    _wire(monkeypatch, pool)

    async def fake(fid):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(forecasts, "dashboard_rows", fake)
    await dr.refresh_due()
    assert pool.executed[1] == (dr.FAIL_SQL, (1, "fc", "query timed out",
                                              dr.FAIL_RETRY_MAX_MINUTES))


# -- preview and save --------------------------------------------------------

def _rows(n_hist=30, n_fut=8):
    h = [dict(ROW, period=f"2026-09-{i + 1:02d}", actual=float(i)) for i in range(n_hist)]
    f = [dict(ROW, period=f"2026-11-{i + 1:02d}", actual=None, forecast=1.0, low=0.5, high=2.0)
         for i in range(n_fut)]
    return h + f


@pytest.mark.asyncio
async def test_sample_boundary(monkeypatch):
    import time

    async def fake(fid):
        return _rows()
    monkeypatch.setattr(forecasts, "dashboard_rows", fake)
    e = await dashboardtools._sample(_fq(), time.monotonic() + 60)
    assert e["source"] == "forecast" and e["error"] is None
    assert e["columns"] == ["period", "actual", "forecast", "low", "high", "method", "wape",
                            "as_of"]
    assert [r["actual"] is not None for r in e["sample"]] == [True, True, False, False, False]
    assert e["sample"][0]["period"] == "2026-09-29"


@pytest.mark.asyncio
async def test_sample_unknown_and_empty(monkeypatch):
    import time

    async def gone(fid):
        raise LookupError("x")
    monkeypatch.setattr(forecasts, "dashboard_rows", gone)
    e = await dashboardtools._sample(_fq(), time.monotonic() + 60)
    assert e["error"] == "forecast #3 is unknown or retired; get_forecast lists the forecasts"

    async def empty(fid):
        return []
    monkeypatch.setattr(forecasts, "dashboard_rows", empty)
    e = await dashboardtools._sample(_fq(), time.monotonic() + 60)
    assert e["error"] is None and e["sample"] == [] and "first run" in e["note"]


def test_preview_description_mentions_forecast():
    t = dashboardtools.preview_dashboard.description
    assert '"source": "forecast"' in t and "forecast_id" in t
    assert '"source": "kpi_history"' in t


SPEC = {
    "spec_version": 1, "title": "T",
    "queries": [{"id": "fc", "source": "forecast", "forecast_id": 3, "refresh_minutes": 15}],
    "layout": {"rows": [["line"]]},
    "tiles": [{"id": "line", "type": "line", "title": "Forecast", "query": "fc", "x": "period",
               "y": [{"column": "actual"}, {"column": "forecast", "dashed": True}],
               "band": {"low": "low", "high": "high"}}],
}


@pytest.mark.asyncio
async def test_save_refuses_unknown_forecast(monkeypatch):
    async def none(fid):
        return None
    monkeypatch.setattr(forecasts, "def_usable", none)

    async def boom(sql):
        raise AssertionError("dry run used for a forecast query")
    monkeypatch.setattr(dashboardtools.guardrails, "run_readonly_sql", boom)

    async def no_pool():
        raise AssertionError("nothing may be saved")
    monkeypatch.setattr(dashboardtools, "get_pool", no_pool)
    msg, refused = await dashboardtools._save("s", SPEC, "why")
    assert refused
    assert msg == ("REFUSED: query fc: forecast #3 is unknown or retired; "
                   "get_forecast lists the forecasts")


@pytest.mark.asyncio
async def test_save_usable_skips_dry_run(monkeypatch):
    async def ok(fid):
        return {"id": fid, "name": "n", "status": "active"}
    monkeypatch.setattr(forecasts, "def_usable", ok)

    async def boom(sql):
        raise AssertionError("dry run used for a forecast query")
    monkeypatch.setattr(dashboardtools.guardrails, "run_readonly_sql", boom)

    class Stop(Exception):
        pass

    async def reached():
        raise Stop()  # past the per-query loop: the save proceeds to the DB
    monkeypatch.setattr(dashboardtools, "get_pool", reached)
    with pytest.raises(Stop):
        await dashboardtools._save("s", SPEC, "why")
