"""Phase 11 (GOLIVE-05): health alerts API."""
import datetime

import pytest
from fastapi import HTTPException

from app import healthapi, healthwatch, heartbeat, main


class P:
    def __init__(self):
        self.sql, self.args = None, None

    async def fetch(self, sql, *a):
        self.sql, self.args = sql, a
        return [{"id": 1, "component": "worker", "detail": "never started",
                 "created_at": datetime.datetime(2026, 1, 1), "resolved_at": None},
                {"id": 2, "component": "weird", "detail": "",
                 "created_at": datetime.datetime(2026, 1, 1), "resolved_at": None}]


@pytest.fixture
def pool(monkeypatch):
    p = P()

    async def gp():
        return p

    monkeypatch.setattr(healthapi, "get_pool", gp)
    return p


async def test_alerts_shape_and_clamp(pool, monkeypatch):
    monkeypatch.setattr(healthwatch, "_last_checks", {"worker": {"state": "failing", "detail": "x"}})
    out = await healthapi.health_alerts()
    assert "resolved_at IS NULL" in pool.sql and pool.args == (20,)
    a = out["alerts"][0]
    assert set(a) == {"id", "component", "title", "detail", "created_at", "resolved_at"}
    assert a["title"] == healthwatch.TITLES["worker"]
    assert out["alerts"][1]["title"] == "System health alert"
    assert out["checks"]["worker"]["state"] == "failing"
    await healthapi.health_alerts(include_resolved=True, limit=999)
    assert "IS NULL" not in pool.sql and pool.args == (100,)
    await healthapi.health_alerts(limit=-5)
    assert pool.args == (1,)


async def test_stop_loop(monkeypatch):
    heartbeat.reset()
    monkeypatch.setattr(heartbeat, "stop", lambda n: 2)
    assert await healthapi.test_stop_loop("worker") == {"stopped": 2}
    with pytest.raises(HTTPException) as e:
        await healthapi.test_stop_loop("nope")
    assert e.value.status_code == 404


def _routes_with_deps():
    found = {}
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inh = list(r.include_context.dependencies)
            for s in r.original_router.routes:
                found[s.path] = inh + list(s.dependencies)
    return found


def test_routes_wired_with_key():
    found = _routes_with_deps()
    assert any(getattr(d, "dependency", None) is main._require_ops_key
               for d in found["/api/health/alerts"])


def test_hooks_enabled():
    assert healthapi.test_hooks_enabled({"IC_TEST_HOOKS": "1"})
    for v in ("true", "0", "", "yes"):
        assert not healthapi.test_hooks_enabled({"IC_TEST_HOOKS": v})
    assert not healthapi.test_hooks_enabled({})


def test_mount_test_hooks():
    from fastapi import FastAPI
    on, off = FastAPI(), FastAPI()
    main._mount_test_hooks(on, True)
    main._mount_test_hooks(off, False)
    assert "/api/test-stop-loop/{name}" not in {r.path for r in off.router.routes}
    paths = {}
    for r in on.router.routes:
        if hasattr(r, "original_router"):
            for s in r.original_router.routes:
                paths[s.path] = list(r.include_context.dependencies)
    assert any(getattr(d, "dependency", None) is main._require_ops_key
               for d in paths["/api/test-stop-loop/{name}"])


def test_app_has_hook_only_when_enabled():
    has = "/api/test-stop-loop/{name}" in _routes_with_deps()
    assert has == healthapi.test_hooks_enabled()


async def test_alert_title_uses_mode_aware_title(monkeypatch):  # Phase 14 (WH-08)
    from app import warehouse

    class PB(P):
        async def fetch(self, sql, *a):
            return [{"id": 3, "component": "business_db", "detail": "x",
                     "created_at": datetime.datetime(2026, 1, 1), "resolved_at": None}]
    p = PB()

    async def gp():
        return p
    monkeypatch.setattr(healthapi, "get_pool", gp)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    out = await healthapi.health_alerts()
    assert out["alerts"][0]["title"] == "The business database is unreachable through the warehouse"
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    out = await healthapi.health_alerts()
    assert out["alerts"][0]["title"] == healthwatch.TITLES["business_db"]
