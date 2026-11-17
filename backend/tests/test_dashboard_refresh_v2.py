"""Phase 22 (DCAP-08): v2 snapshot caps, too_large, timezone stamp, v1 unchanged."""
import json

import pytest

from app import dashboard_refresh as dr
from app import dashsettings, guardrails


class FakePool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def pair(renderer=None, sql="select 1"):
    return {"version_id": 1, "query_id": "q", "version": 1, "dashboard_id": "d",
            "sql": sql, "renderer": renderer}


@pytest.fixture(autouse=True)
def caps(monkeypatch):
    monkeypatch.setattr(dashsettings, "snapshot_caps", lambda: (10000, 2097152))
    monkeypatch.setattr(dashsettings, "day_timezone", lambda: None)


def fake_execute(monkeypatch, rows):
    calls = []

    async def ex(*args):
        calls.append(args)
        return rows
    monkeypatch.setattr(dr, "_execute", ex)
    return calls


async def test_v1_calls_execute_with_sql_only(monkeypatch):
    calls = fake_execute(monkeypatch, [{"a": 1}])
    assert await dr._refresh_pair(FakePool(), pair()) is None
    assert calls == [("select 1",)]


async def test_v2_passes_cap_plus_one(monkeypatch):
    calls = fake_execute(monkeypatch, [{"a": 1}])
    assert await dr._refresh_pair(FakePool(), pair(dr.V2_RENDERER)) is None
    assert calls == [("select 1", 10001)]


async def test_v2_too_many_rows(monkeypatch):
    fake_execute(monkeypatch, [{"a": i} for i in range(10001)])
    pool = FakePool()
    assert await dr._refresh_pair(pool, pair(dr.V2_RENDERER)) == "too_large"
    sqls = [s for s, _ in pool.executed]
    assert dr.UPSERT_OK_SQL not in sqls
    fail = [a for s, a in pool.executed if s == dr.FAIL_SQL][0]
    assert fail[2] == "too much data for one tile (over 10,000 rows or 2 MB)"


async def test_v2_too_many_bytes(monkeypatch):
    monkeypatch.setattr(dashsettings, "snapshot_caps", lambda: (10000, 262144))
    fake_execute(monkeypatch, [{"a": "x" * 1000} for _ in range(300)])
    pool = FakePool()
    assert await dr._refresh_pair(pool, pair(dr.V2_RENDERER)) == "too_large"
    fail = [a for s, a in pool.executed if s == dr.FAIL_SQL][0]
    assert "256 KB" in fail[2]
    assert dr.UPSERT_OK_SQL not in [s for s, _ in pool.executed]


async def test_v1_is_not_capped_by_v2_rules(monkeypatch):
    fake_execute(monkeypatch, [{"a": i} for i in range(10001)])
    assert await dr._refresh_pair(FakePool(), pair()) is None


@pytest.mark.parametrize("tz", [None, "Asia/Kolkata"])
async def test_tz_stamp(monkeypatch, tz):
    monkeypatch.setattr(dashsettings, "day_timezone", lambda: tz)
    fake_execute(monkeypatch, [{"a": 1}])
    pool = FakePool()
    await dr._refresh_pair(pool, pair())
    stamp = [a for s, a in pool.executed if s == dr.TZ_SQL]
    assert stamp == [(1, "q", tz)]


async def test_execute_passes_timezone_only_when_set(monkeypatch):
    seen = []

    async def fake(sql, **kw):
        seen.append(kw)
        return []
    monkeypatch.setattr(guardrails, "run_readonly_sql", fake)
    await dr._execute("select 2")
    monkeypatch.setattr(dashsettings, "day_timezone", lambda: "Asia/Kolkata")
    await dr._execute("select 2", 5)
    assert seen == [{}, {"limit": 5, "timezone": "Asia/Kolkata"}]


def test_claim_returns_renderer_and_data_sql_has_timezone():
    from app import dashboards_serve
    assert "v.renderer" in dr.CLAIM_SQL
    assert "'timezone', s.day_timezone" in dashboards_serve.DATA_SQL


async def test_zone_is_read_once_per_pair(monkeypatch):
    """A live setting change mid-refresh cannot make the stamp differ from the zone the SQL ran in."""
    zones = iter(["Asia/Kolkata", "Europe/London", "Europe/London"])
    monkeypatch.setattr(dashsettings, "day_timezone", lambda: next(zones))
    seen = []

    async def fake(sql, **kw):
        seen.append(kw)
        return []
    monkeypatch.setattr(guardrails, "run_readonly_sql", fake)
    pool = FakePool()
    await dr._refresh_pair(pool, pair())
    stamp = [a for s, a in pool.executed if s == dr.TZ_SQL]
    assert seen == [{"timezone": "Asia/Kolkata"}]
    assert stamp == [(1, "q", "Asia/Kolkata")]
