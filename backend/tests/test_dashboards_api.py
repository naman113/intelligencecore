"""Phase 3 dashboards ops routes: direct handler calls with a fake pool, plus TestClient auth checks (no lifespan, no DB)."""

import json
from types import SimpleNamespace

import asyncpg
import pytest

from _lintclean import QUERIES as LINT_QUERIES, clean_page
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import dashboards, dashboards_api
from app.config import get_settings
from app.dashboards import DashboardError

CODE = "<html><script>const a = 1;</script></html>"
QUERIES = [{"id": "sales", "sql": "SELECT 1 AS n", "refresh_minutes": 15}]


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.calls.append(("BEGIN", ""))

    async def __aexit__(self, *exc):
        self.conn.calls.append(("END", ""))
        return False


class FakeConn:
    def __init__(self, rules=None):
        self.calls = []
        self.rules = list(rules or [])

    def transaction(self):
        return _Tx(self)

    def _r(self, kind, sql, default):
        self.calls.append((kind, sql))
        for sub, resp in self.rules:
            if sub in sql:
                return resp
        return default

    async def fetchrow(self, sql, *a):
        return self._r("fetchrow", sql, None)

    async def fetchval(self, sql, *a):
        return self._r("fetchval", sql, None)

    async def fetch(self, sql, *a):
        return self._r("fetch", sql, [])

    async def execute(self, sql, *a):
        return self._r("execute", sql, "UPDATE 1")


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()


def _wire(monkeypatch, conn):
    async def fake_get_pool():
        return FakePool(conn)
    monkeypatch.setattr(dashboards_api, "db", SimpleNamespace(get_pool=fake_get_pool))
    return conn


@pytest.fixture
def settings_reset():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ handlers

async def test_list_returns_service_rows(monkeypatch):
    _wire(monkeypatch, FakeConn())
    seen = {}

    async def fake_list(conn, *, limit):
        seen["limit"] = limit
        return [{"slug": "s"}]
    monkeypatch.setattr(dashboards, "list_dashboards", fake_list)
    assert await dashboards_api.api_list_dashboards(limit=7) == [{"slug": "s"}]
    assert seen["limit"] == 7


SPEC = {
    "spec_version": 1, "title": "T",
    "queries": [{"id": "a", "sql": "SELECT 1 AS n", "refresh_minutes": 15}],
    "layout": {"rows": [["t"]]},
    "tiles": [{"id": "t", "type": "table", "title": "T", "query": "a",
               "columns": [{"column": "n"}]}],
}


async def test_save_version_passes_operator_author(monkeypatch):
    _wire(monkeypatch, FakeConn())
    seen = {}

    async def fake_prepare(raw):
        seen["raw"] = raw
        return {"stored": {"tiles": []}, "queries": [{"id": "a"}], "title": "T"}

    async def fake_save(conn, **kw):
        seen.update(kw)
        return {"dashboard_id": 1, "slug": "s", "version_id": 2, "version": 1,
                "status": "draft", "created": True, "script_hashes": [], "path": "/d/s/v/1/",
                "format": "spec"}
    monkeypatch.setattr(dashboards, "prepare_spec", fake_prepare)
    monkeypatch.setattr(dashboards, "save_spec_version", fake_save)
    out = await dashboards_api.api_save_dashboard_version(
        "s", {"spec": SPEC, "change_note": "n"})
    assert seen["author"] == "operator" and seen["task_run_id"] is None
    assert seen["title"] == "T" and seen["change_note"] == "n" and seen["slug"] == "s"
    assert seen["raw"] == SPEC and seen["renderer"]
    assert out["created"] is True and out["path"] == "/d/s/v/1/" and out["format"] == "spec"


async def test_save_version_missing_spec_is_422(monkeypatch):
    _wire(monkeypatch, FakeConn())
    for body in ({}, None):
        with pytest.raises(HTTPException) as e:
            await dashboards_api.api_save_dashboard_version("s", body)
        assert e.value.status_code == 422


async def test_save_version_with_code_is_refused(monkeypatch):
    _wire(monkeypatch, FakeConn())
    for body in ({"code": CODE, "queries": QUERIES, "title": "T"},
                 {"spec": SPEC, "code": CODE}):
        with pytest.raises(HTTPException) as e:
            await dashboards_api.api_save_dashboard_version("s", body)
        assert e.value.status_code == 422
        assert e.value.detail == "new dashboard versions are chart specs; send {spec, change_note}"


async def test_save_version_invalid_spec_returns_findings(monkeypatch):
    _wire(monkeypatch, FakeConn())
    bad = dict(SPEC, tiles=[dict(SPEC["tiles"][0], query="missing")])
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_save_dashboard_version("s", {"spec": bad, "change_note": "n"})
    assert e.value.status_code == 422
    findings = e.value.detail["findings"]
    assert 0 < len(findings) <= 20 and all("path" in f for f in findings)


@pytest.mark.parametrize("handler,op", [("api_publish_dashboard", "publish"),
                                        ("api_rollback_dashboard", "rollback")])
async def test_publish_and_rollback_pass_through(monkeypatch, handler, op):
    _wire(monkeypatch, FakeConn())
    seen = {}

    async def fake_set(conn, **kw):
        seen.update(kw)
        return {"slug": "s", "version": 2}
    monkeypatch.setattr(dashboards, "operator_set_version", fake_set)
    await getattr(dashboards_api, handler)("s", {"version": 2, "decided_by": 5, "note": "x"})
    assert seen == {"slug": "s", "version": 2, "op": op, "decided_by": 5, "note": "x"}


async def test_bool_version_is_422(monkeypatch):
    _wire(monkeypatch, FakeConn())
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_publish_dashboard("s", {"version": True})
    assert e.value.status_code == 422


async def test_rotate_returns_token_payload(monkeypatch):
    _wire(monkeypatch, FakeConn())

    async def fake_rotate(conn, *, slug):
        return {"slug": slug, "view_token": "t", "token_rotated_at": None, "path": "/d/s/"}
    monkeypatch.setattr(dashboards, "rotate_token", fake_rotate)
    assert (await dashboards_api.api_rotate_dashboard_token("s"))["view_token"] == "t"


@pytest.mark.parametrize("status", [404, 409, 422])
async def test_domain_errors_map_to_http(monkeypatch, status):
    _wire(monkeypatch, FakeConn())

    async def boom(conn, **kw):
        raise DashboardError(status, "nope")
    monkeypatch.setattr(dashboards, "get_dashboard", boom)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_get_dashboard("s")
    assert e.value.status_code == status and e.value.detail == "nope"


async def test_integrity_violation_is_409(monkeypatch):
    _wire(monkeypatch, FakeConn())

    async def boom(conn, **kw):
        raise asyncpg.exceptions.UniqueViolationError("dup")
    monkeypatch.setattr(dashboards, "operator_set_version", boom)
    with pytest.raises(HTTPException) as e:
        await dashboards_api.api_rollback_dashboard("s", {"version": 1})
    assert e.value.status_code == 409
    assert e.value.detail == "conflicting concurrent change; retry"


async def test_rollback_end_to_end_writes_action_in_transaction(monkeypatch):
    conn = FakeConn([
        ("FROM dashboards WHERE slug", {"id": 1, "slug": "s", "title": "T", "current_version_id": 9}),
        ("FROM dashboard_versions WHERE dashboard_id = $1 AND version", {
            "id": 4, "version": 1, "title": "T", "status": "retired", "change_note": None}),
        ("FROM dashboards WHERE id", {"id": 1, "current_version_id": 9}),
        ("WHERE id = $2 AND dashboard_id", {"id": 4, "version": 1, "title": "T", "status": "retired",
                                                     "code": clean_page(), "queries": LINT_QUERIES}),
        ("RETURNING id, version", {"id": 9, "version": 2}),
        ("FROM people", 3),
        ("INSERT INTO actions", 77),
    ])
    _wire(monkeypatch, conn)
    out = await dashboards_api.api_rollback_dashboard("s", {"version": 1})
    assert out["action_id"] == 77 and out["from_version"] == 2
    kinds = [c[0] if c[0] in ("BEGIN", "END") else c[1] for c in conn.calls]
    ins = next(i for i, k in enumerate(kinds) if "INSERT INTO actions" in k)
    ptr = next(i for i, k in enumerate(kinds) if "UPDATE dashboards SET current_version_id" in k)
    first_begin = kinds.index("BEGIN")
    last_end = len(kinds) - 1 - kinds[::-1].index("END")
    assert first_begin < ptr < ins < last_end


def test_api_module_does_not_import_main_or_serve_html():
    src = open(dashboards_api.__file__, encoding="utf-8").read()
    import re
    assert not re.search(r"^\s*(from \.main|from \. import main|import main|from app\.main|import app\.main)", src, re.M)
    assert "HTMLResponse" not in src and "text/html" not in src


# ----------------------------------------------------------------- auth wiring

ROUTES = [("get", "/api/dashboards"), ("get", "/api/dashboards/s"),
          ("get", "/api/dashboards/s/versions/1"), ("post", "/api/dashboards/s/versions"),
          ("post", "/api/dashboards/s/publish"), ("post", "/api/dashboards/s/rollback"),
          ("post", "/api/dashboards/s/rotate-token")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_all_routes_401_without_or_wrong_key(monkeypatch, settings_reset, method, path):
    monkeypatch.setenv("OPS_KEY", "test-key")
    get_settings.cache_clear()
    client = TestClient(main.app)
    kw = {"json": {}} if method == "post" else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers={"x-ops-key": "wrong"}, **kw).status_code == 401


def test_list_with_ops_key_is_200_empty(monkeypatch, settings_reset):
    monkeypatch.setenv("OPS_KEY", "test-key")
    get_settings.cache_clear()
    _wire(monkeypatch, FakeConn())
    client = TestClient(main.app)
    r = client.get("/api/dashboards", headers={"x-ops-key": "test-key"})
    assert r.status_code == 200 and r.json() == []
    assert json.dumps(r.json())
