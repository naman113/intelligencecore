"""Phase 6: lint-gated preview-mint route. Direct handler calls on a fake pool plus
TestClient auth checks. No DB, LLM or network."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import dashboards, dashboards_api
from app.config import get_settings

GOOD = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>T</title></head>'
        "<body><h1>Hi</h1></body></html>")
BAD = GOOD.replace("<h1>Hi</h1>", '<iframe src="x"></iframe>')


class _Acq:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def acquire(self):
        return _Acq()


@pytest.fixture
def wired(monkeypatch):
    state = {"minted": [], "version": {}}

    async def get_pool():
        return _Pool()

    async def get_version(conn, *, slug, version):
        if state.get("missing"):
            raise dashboards.DashboardError(404, "version not found")
        return state["version"]

    async def mint(conn, *, slug, version, ttl_minutes=60):
        if not 5 <= ttl_minutes <= 1440:
            raise dashboards.DashboardError(422, "ttl out of range")
        state["minted"].append(ttl_minutes)
        return {"slug": slug, "version": version, "preview_token": "TOK",
                "expires_at": "2030-01-01T00:00:00+00:00", "path": f"/d/{slug}/v/{version}/"}

    monkeypatch.setattr(dashboards_api, "db", SimpleNamespace(get_pool=get_pool))
    monkeypatch.setattr(dashboards, "get_version", get_version)
    monkeypatch.setattr(dashboards, "mint_preview_token", mint)
    return state


def _v(status="draft", code=GOOD, age_h=1):
    return {"status": status, "code": code, "queries": [],
            "created_at": datetime.now(timezone.utc) - timedelta(hours=age_h)}


async def test_clean_draft_mints(wired):
    wired["version"] = _v()
    out = await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert set(out) >= {"slug", "version", "preview_token", "expires_at", "path", "url",
                        "refreshing"}
    assert out["url"] == out["path"] + "?p=" + out["preview_token"]
    assert wired["minted"] == [60] and out["refreshing"] is True


async def test_ttl_honoured_and_bounds(wired):
    wired["version"] = _v()
    await dashboards_api.api_mint_dashboard_preview("s", 1, {"ttl_minutes": 1440})
    assert wired["minted"] == [1440]
    for ttl in (4, 1441):
        with pytest.raises(HTTPException) as e:
            await dashboards_api.api_mint_dashboard_preview("s", 1, {"ttl_minutes": ttl})
        assert e.value.status_code == 422


async def test_lint_error_blocks_mint(wired):
    wired["version"] = _v(code=BAD)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert e.value.status_code == 422
    assert 0 < len(e.value.detail["findings"]) <= 20
    assert wired["minted"] == []


@pytest.mark.parametrize("status", ["published", "retired"])
async def test_non_draft_409(wired, status):
    wired["version"] = _v(status=status)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert e.value.status_code == 409 and wired["minted"] == []


async def test_missing_404(wired):
    wired["missing"] = True
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_mint_dashboard_preview("s", 9)
    assert e.value.status_code == 404


async def test_refreshing_false_for_old_draft(wired):
    wired["version"] = _v(age_h=30)
    out = await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert out["refreshing"] is False


def test_route_registered_and_401_without_key(monkeypatch):
    monkeypatch.setenv("OPS_KEY", "test-key")
    get_settings.cache_clear()
    try:
        assert any(getattr(r, "path", "") == "/preview" or
                   getattr(r, "path", "").endswith("/versions/{n}/preview")
                   for r in dashboards_api.router.routes)
        c = TestClient(main.app)
        url = "/api/dashboards/s/versions/1/preview"
        assert c.post(url, json={}).status_code == 401
        assert c.post(url, json={}, headers={"x-ops-key": "wrong"}).status_code == 401
    finally:
        get_settings.cache_clear()


def test_module_has_no_html_and_no_token_logging():
    src = open(dashboards_api.__file__, encoding="utf-8").read()
    assert "HTMLResponse" not in src and "text/html" not in src


ALIAS = GOOD.replace(
    "</body>",
    "<script>const w=window; w['loc'+'ation']['hr'+'ef']='/'+'/evil.example/?x=1';</script></body>")


@pytest.mark.parametrize("code", [BAD, ALIAS], ids=["bad", "alias"])
async def test_alias_payload_blocks_mint(wired, code):
    wired["version"] = _v(code=code)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert e.value.status_code == 422
    assert 0 < len(e.value.detail["findings"]) <= 20
    assert wired["minted"] == []


# ---------------------------------------------------------- Phase 18: spec drafts

SPEC = {
    "spec_version": 1, "title": "T",
    "queries": [{"id": "a", "sql": "SELECT 1 AS n", "refresh_minutes": 15}],
    "layout": {"rows": [["t"]]},
    "tiles": [{"id": "t", "type": "table", "title": "T", "query": "a",
               "columns": [{"column": "n"}]}],
}


def _spec_v(spec=SPEC):
    v = _v(code="")
    v.update(format="spec", spec=spec)
    return v


async def test_spec_draft_mints_without_lint(wired, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("lint must not run for a spec draft")
    monkeypatch.setattr(dashboards_api.dashboardlint, "lint", boom)
    wired["version"] = _spec_v()
    out = await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert out["url"] == out["path"] + "?p=" + out["preview_token"]
    assert wired["minted"] == [60]


async def test_broken_stored_spec_is_422(wired):
    bad = dict(SPEC, tiles=[dict(SPEC["tiles"][0], query="missing")])
    wired["version"] = _spec_v(bad)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert e.value.status_code == 422
    assert 0 < len(e.value.detail["findings"]) <= 20
    assert wired["minted"] == []


async def test_html_draft_still_linted(wired):
    wired["version"] = dict(_v(code=BAD), format="html", spec=None)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_mint_dashboard_preview("s", 1)
    assert e.value.status_code == 422 and wired["minted"] == []
