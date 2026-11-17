"""Phase 22 plan 11 task 2: POST /api/dashboards/{slug}/layout."""
import asyncpg
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import dashboards, dashboardspec
from app.config import get_settings
from tests.test_dashboards_api import FakeConn, _wire

H = {"x-ops-key": "test-key"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_KEY", "test-key")
    get_settings.cache_clear()
    _wire(monkeypatch, FakeConn())
    yield TestClient(main.app)
    get_settings.cache_clear()


def test_requires_ops_key(client):
    assert client.post("/api/dashboards/s/layout", json={}).status_code == 401
    assert client.post("/api/dashboards/s/layout", json={}, headers={"x-ops-key": "x"}).status_code == 401


def test_ok_passes_body(client, monkeypatch):
    seen = {}

    async def fake(conn, *, slug, base_version, changes, decided_by=None):
        seen.update(slug=slug, base=base_version, changes=changes, who=decided_by)
        return {"slug": slug, "version": 4, "version_id": 9, "action_id": 3, "warnings": []}
    monkeypatch.setattr(dashboards, "save_layout_tweak", fake)
    body = {"base_version": 3, "sections": [{"title": None, "tiles": ["a"]}], "widths": {"a": "full"},
            "titles": {}, "hidden": [], "default_range": None, "note": None}
    r = client.post("/api/dashboards/ops/layout", json=body, headers=H)
    assert r.status_code == 200
    assert r.json() == {"slug": "ops", "version": 4, "version_id": 9, "action_id": 3, "warnings": []}
    assert seen["slug"] == "ops" and seen["base"] == 3 and seen["changes"]["widths"] == {"a": "full"}


def test_spec_error_is_422_capped(client, monkeypatch):
    async def fake(conn, **k):
        raise dashboardspec.SpecError([{"rule": "layout", "path": f"p{i}", "message": "m"}
                                       for i in range(30)])
    monkeypatch.setattr(dashboards, "save_layout_tweak", fake)
    r = client.post("/api/dashboards/ops/layout", json={"base_version": 1}, headers=H)
    assert r.status_code == 422
    f = r.json()["detail"]["findings"]
    assert len(f) == 20 and f[0]["rule"] == "layout"


def test_dashboard_error_maps(client, monkeypatch):
    async def fake(conn, **k):
        raise dashboards.DashboardError(409, "the dashboard changed since you opened the editor; reload")
    monkeypatch.setattr(dashboards, "save_layout_tweak", fake)
    r = client.post("/api/dashboards/ops/layout", json={"base_version": 1}, headers=H)
    assert r.status_code == 409 and "reload" in r.json()["detail"]


def test_integrity_conflict_is_409(client, monkeypatch):
    async def fake(conn, **k):
        raise asyncpg.exceptions.UniqueViolationError("dup")
    monkeypatch.setattr(dashboards, "save_layout_tweak", fake)
    r = client.post("/api/dashboards/ops/layout", json={"base_version": 1}, headers=H)
    assert r.status_code == 409
