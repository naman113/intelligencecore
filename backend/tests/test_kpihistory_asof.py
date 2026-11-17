"""Phase 17 (CONS-02): as_of on KPI history rows and stale marker rows."""
import json
from datetime import datetime, timezone

from app import kpihistory

DT = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
TRIG = {"type": "threshold", "column": "n", "value_column": "net"}


class Pool:
    def __init__(self, row=None, fail=False):
        self.calls, self.sqls, self.row, self.fail = [], [], row, fail

    async def execute(self, sql, *a):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((sql, a))

    async def fetch(self, sql, *a):
        self.sqls.append(sql)
        return []

    async def fetchrow(self, sql, *a):
        self.sqls.append(sql)
        if "FROM watches" in sql:
            return {"id": 7, "title": "t", "kind": "kpi", "status": "active", "trigger_rule": "{}"}
        if "count(*)" in sql:
            return {"n": 0, "metrics": []}
        return self.row

    async def fetchval(self, sql, *a):
        return 7


async def test_record_with_as_of():
    p = Pool()
    assert await kpihistory.record(p, 7, {"column": "n"}, {"n": 3}, as_of=DT)
    assert json.loads(p.calls[0][1][3]) == {"n": 3, "as_of": DT.isoformat()}


async def test_record_as_of_column_collision():
    p = Pool()
    await kpihistory.record(p, 7, {"column": "n"}, {"n": 3, "as_of": "x"}, as_of=DT)
    obs = json.loads(p.calls[0][1][3])
    assert obs["as_of"] == DT.isoformat() and obs["as_of_column"] == "x"


async def test_record_without_as_of_unchanged():
    p = Pool()
    await kpihistory.record(p, 7, {"column": "n"}, {"n": 3, "as_of": "x"})
    assert p.calls[0][1][3] == json.dumps({"n": 3, "as_of": "x"})
    assert p.calls[0][0] == ("INSERT INTO metric_history (watch_id, metric, value, observation) "
                             "VALUES ($1, $2, $3, $4::jsonb)")


async def test_record_stale_marker():
    p = Pool()
    assert await kpihistory.record_stale(p, 7, TRIG, DT, stale_tables=("src_x.t",))
    sql, a = p.calls[0]
    assert "NULL" in sql and "true" in sql
    assert a[0] == 7 and a[1] == "net"
    assert json.loads(a[2]) == {"as_of": DT.isoformat(), "stale": True, "stale_tables": ["src_x.t"]}


async def test_record_stale_fail_soft():
    assert await kpihistory.record_stale(Pool(fail=True), 7, TRIG, DT) is False


async def test_readers_skip_marker_rows(monkeypatch):
    p = Pool()

    async def gp():
        return p
    monkeypatch.setattr(kpihistory, "get_pool", gp)
    await kpihistory.series(7, 30)
    await kpihistory._points(p, 7, 30, "raw", "UTC")
    await kpihistory._points(p, 7, 30, "day", "UTC")
    await kpihistory.daily_rows(7, 30)
    seen = [s for s in p.sqls if "metric_history" in s]
    assert len(seen) >= 5
    for s in seen:
        assert "NOT stale" in s or "NOT h.stale" in s, s
    daily = kpihistory._DAILY_SQL
    assert "latest AS (SELECT metric FROM metric_history WHERE watch_id = $1 AND NOT stale" in daily
    assert "NOT h.stale" in daily


async def test_latest_as_of(monkeypatch):
    rows = {"a": DT.isoformat(), "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    p = Pool(row=rows)

    async def gp():
        return p
    monkeypatch.setattr(kpihistory, "get_pool", gp)
    assert await kpihistory.latest_as_of(7) == DT
    assert "NOT stale" not in p.sqls[-1]
    p.row = {"a": None, "observed_at": rows["observed_at"]}
    assert await kpihistory.latest_as_of(7) == rows["observed_at"]
    p.row = None
    assert await kpihistory.latest_as_of(7) is None
