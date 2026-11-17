"""Phase 13.1 plan 05: /api/forecasts routes and the shared readers (fake pool)."""

import datetime
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import forecasts
from app.config import get_settings

KEY = {"x-ops-key": "test-key"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_KEY", "test-key")
    get_settings.cache_clear()
    yield TestClient(main.app)
    get_settings.cache_clear()


ROUTES = [("get", "/api/forecasts"), ("get", "/api/forecasts/1"),
          ("patch", "/api/forecasts/1"), ("post", "/api/forecasts/1/run")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_401_without_or_wrong_key(client, method, path):
    kw = {"json": {"status": "paused"}} if method == "patch" else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers={"x-ops-key": "bad"}, **kw).status_code == 401


def test_list_clamps_limit_and_all(client, monkeypatch):
    seen = {}

    async def lb(limit=50, include_retired=False):
        seen.update(limit=limit, inc=include_retired)
        return []
    monkeypatch.setattr(forecasts, "list_brief", lb)
    assert client.get("/api/forecasts?limit=9999&status=all", headers=KEY).json() == []
    assert seen == {"limit": 200, "inc": True}
    client.get("/api/forecasts?limit=0", headers=KEY)
    assert seen == {"limit": 1, "inc": False}
    assert client.get("/api/forecasts?status=x", headers=KEY).status_code == 422


def test_detail_404_and_ok(client, monkeypatch):
    async def d(i):
        return None if i == 9 else {"forecast_id": i}
    monkeypatch.setattr(forecasts, "detail", d)
    assert client.get("/api/forecasts/9", headers=KEY).status_code == 404
    assert client.get("/api/forecasts/3", headers=KEY).json() == {"forecast_id": 3}


def test_patch_mapping(client, monkeypatch):
    async def ss(i, status, decided_by=None):
        if i == 404:
            raise LookupError("x")
        if i == 409:
            raise ValueError("retired")
        return {"forecast_id": i, "status": status}
    monkeypatch.setattr(forecasts, "set_status", ss)
    p = lambda i, body: client.patch(f"/api/forecasts/{i}", json=body, headers=KEY)  # noqa: E731
    assert p(1, {"status": "paused"}).json() == {"forecast_id": 1, "status": "paused"}
    assert p(404, {"status": "paused"}).status_code == 404
    assert p(409, {"status": "paused"}).status_code == 409
    assert p(1, {"status": "bogus"}).status_code == 422
    assert p(1, {"status": "paused", "sql": "x"}).status_code == 422
    assert p(1, {}).status_code == 422


def test_run_mapping(client, monkeypatch):
    async def rr(i):
        if i == 404:
            raise LookupError("x")
        if i == 409:
            raise ValueError("paused")
        if i == 429:
            raise forecasts.RunTooSoon("wait 5 minutes between runs")
        return {"forecast_id": i, "queued": True}
    monkeypatch.setattr(forecasts, "request_run", rr)
    post = lambda i: client.post(f"/api/forecasts/{i}/run", headers=KEY)  # noqa: E731
    assert post(1).json() == {"forecast_id": 1, "queued": True}
    assert [post(i).status_code for i in (404, 409, 429)] == [404, 409, 429]


# -- readers with a fake pool ---------------------------------------------

def D(y, m, d):
    return datetime.date(y, m, d)


def P(period, actual=None, mean=None, lo=None, hi=None, as_of=D(2026, 10, 25), method="ets"):
    return {"period": period, "actual": actual, "mean": mean, "lo80": lo, "hi80": hi,
            "as_of": as_of, "method": method}


class Pool:
    def __init__(self, d, runs, pts):
        self.d, self.runs, self.pts = d, runs, pts

    async def fetchrow(self, sql, *a):
        return self.d

    async def fetch(self, sql, *a):
        if "SELECT DISTINCT run_at" in sql:
            return [{"run_at": r} for r in self.runs]
        return self.pts[a[1]]

    async def fetchval(self, sql, *a):
        return None


DEF = {"id": 3, "name": "Sales", "grain": "week", "horizon": 2, "season": 0, "kind": "flow",
       "status": "active", "sql": "select 1", "last_error": None, "failing_since": None,
       "next_run_at": datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc),
       "last_meta": json.dumps({"method": "ets", "baseline": "naive", "wape": 0.1,
                                "mae": 2.0, "mape": 0.1, "coverage": 0.8, "notes": ["n"]})}


def _wire(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(forecasts, "get_pool", gp)


@pytest.mark.asyncio
async def test_detail_unknown_and_single_run(monkeypatch):
    _wire(monkeypatch, Pool(None, [], {}))
    assert await forecasts.detail(1) is None
    _wire(monkeypatch, Pool(DEF, [1], {1: [P(D(2026, 10, 12), actual=5),
                                          P(D(2026, 10, 19), mean=6, lo=4, hi=8)]}))
    out = await forecasts.detail(3)
    json.dumps(out)
    assert out["change"] is None and out["method"] == "ets" and out["wape"] == 0.1
    assert out["actuals"] == [{"period": "2026-10-12", "value": 5.0}]
    assert out["forecast"] == [{"period": "2026-10-19", "mean": 6.0, "low": 4.0, "high": 8.0}]
    assert "not instructions" in out["_note"] and out["as_of"] == "2026-10-25"


@pytest.mark.asyncio
async def test_detail_change_block(monkeypatch):
    now = [P(D(2026, 10, 12), actual=10), P(D(2026, 10, 19), mean=12, lo=1, hi=2),
           P(D(2026, 10, 26), mean=14, lo=1, hi=2)]
    before = [P(D(2026, 10, 12), mean=8, as_of=D(2026, 10, 24), method="naive"),
              P(D(2026, 10, 19), mean=10, as_of=D(2026, 10, 24), method="naive")]
    _wire(monkeypatch, Pool(DEF, [2, 1], {2: now, 1: before}))
    ch = (await forecasts.detail(3))["change"]
    assert ch["method_changed"] is True and ch["previous_method"] == "naive"
    assert ch["previous_as_of"] == "2026-10-24"
    assert ch["total_forecast_now"] == 12.0 and ch["total_forecast_before"] == 10.0
    assert ch["last_actual_vs_previous_forecast"] == {
        "period": "2026-10-12", "actual": 10.0, "forecast": 8.0, "miss_pct": 25.0}


@pytest.mark.asyncio
async def test_detail_budget(monkeypatch):
    acts = [P(D(2024, 1, 1) + datetime.timedelta(days=i), actual=1234567.891 + i)
            for i in range(310)]
    fc = [P(D(2026, 10, 1) + datetime.timedelta(days=i), mean=1.5, lo=1.0, hi=2.0)
          for i in range(90)]
    _wire(monkeypatch, Pool(DEF, [1], {1: acts + fc}))
    out = await forecasts.detail(3)
    assert len(json.dumps(out)) <= forecasts.OUTPUT_BUDGET
    assert len(out["forecast"]) == 90 and 0 < len(out["actuals"]) < 310
    assert out["actuals"][-1]["period"] == acts[-1]["period"].isoformat()


class AuditConn:
    def __init__(self, status):
        self.status, self.rows = status, []

    async def fetchrow(self, sql, *a):
        return None if self.status is None else {"id": 1, "name": "N", "status": self.status}

    async def execute(self, sql, *a):
        self.rows.append(("x", sql, a))

    async def fetchval(self, sql, *a):
        if "INSERT INTO actions" in sql:
            self.rows.append(("audit", a))
            return 77
        return None

    def transaction(self):
        class T:
            async def __aenter__(s): return None
            async def __aexit__(s, *e): return False
        return T()


class AcqPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        c = self.conn

        class A:
            async def __aenter__(s): return c
            async def __aexit__(s, *e): return False
        return A()


@pytest.mark.asyncio
@pytest.mark.parametrize("cur,to", [("active", "paused"), ("paused", "active"),
                                    ("active", "retired"), ("paused", "retired")])
async def test_set_status_ok_writes_audit(monkeypatch, cur, to):
    conn = AuditConn(cur)
    _wire(monkeypatch, AcqPool(conn))
    out = await forecasts.set_status(1, to)
    assert out["status"] == to and out["action_id"] == 77
    audit = [r for r in conn.rows if r[0] == "audit"][0][1]
    payload = json.loads(audit[0])
    assert payload["from"] == cur and payload["to"] == to and payload["origin"] == "owner"


@pytest.mark.asyncio
async def test_set_status_errors(monkeypatch):
    _wire(monkeypatch, AcqPool(AuditConn("retired")))
    with pytest.raises(ValueError):
        await forecasts.set_status(1, "active")
    _wire(monkeypatch, AcqPool(AuditConn(None)))
    with pytest.raises(LookupError):
        await forecasts.set_status(1, "paused")
    with pytest.raises(ValueError):
        await forecasts.set_status(1, "bogus")


class RunPool:
    def __init__(self, row):
        self.row, self.executed = row, []

    async def fetchrow(self, sql, *a):
        return self.row

    async def execute(self, sql, *a):
        self.executed.append(sql)


@pytest.mark.asyncio
async def test_request_run_rules(monkeypatch):
    p = RunPool({"status": "active", "last_run_at": None, "age": None})
    _wire(monkeypatch, p)
    assert await forecasts.request_run(1) == {"forecast_id": 1, "queued": True}
    assert "next_run_at = now()" in p.executed[0]
    _wire(monkeypatch, RunPool({"status": "active", "last_run_at": 1, "age": 60.0}))
    with pytest.raises(forecasts.RunTooSoon):
        await forecasts.request_run(1)
    _wire(monkeypatch, RunPool({"status": "active", "last_run_at": 1, "age": 600.0}))
    assert (await forecasts.request_run(1))["queued"] is True
    _wire(monkeypatch, RunPool({"status": "paused", "last_run_at": None, "age": None}))
    with pytest.raises(ValueError):
        await forecasts.request_run(1)
    _wire(monkeypatch, RunPool(None))
    with pytest.raises(LookupError):
        await forecasts.request_run(1)
