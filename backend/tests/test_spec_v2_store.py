"""Phase 22 plan 06 task 3: the v2 prepare and save path, renderer choice, per-version caps and
the serve-time timezone. Recording fakes only; no DB, LLM or network."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import dashboardrender as dr
from app import dashboards as d
from app import dashboards_serve as serve
from app import dashboardspec as ds
from app import dashguard, kpihistory, main, semanticmodel
from app.dashboards import DashboardError
from tests.test_compiler_tiles import build
from tests.test_dashboard_spec_store import Conn, measure_entry, save_conn, sql_only_spec
from tests.test_dashboard_spec_tools import env, save  # noqa: F401 - env is a fixture

V2_SQL = "SELECT 1 AS total, 2 AS other"


def v2_sql_spec(n_queries=1, **kw):
    qs = [{"id": f"q{i}", "sql": V2_SQL, "refresh_minutes": 30} for i in range(n_queries)]
    out = {"spec_version": 2, "title": "Ops", "queries": qs,
           "sections": [{"tiles": ["s"]}],
           "tiles": [{"id": "s", "type": "stat", "title": "Total", "query": "q0",
                      "column": "total"}]}
    out.update(kw)
    return out


def v2_measure_spec(**kw):
    out = {"spec_version": 2, "title": "Ops",
           "date_control": {"default": "last_7_days"},
           "sections": [{"tiles": ["s", "e"]}],
           "tiles": [{"id": "s", "type": "stat", "title": "Kg in", "date": "control",
                      "measure": {"name": "events_amount"}},
                     {"id": "e", "type": "text", "text": "hi"}]}
    out.update(kw)
    return out


@pytest.fixture
def model(monkeypatch):
    m = build()

    async def load():
        return m
    monkeypatch.setattr(semanticmodel, "load_model", load)
    return m


# ---------------------------------------------------------------- prepare_spec

async def test_v2_prepare_returns_renderer_and_locale(model):
    out = await d.prepare_spec(v2_measure_spec(number_locale="en-GB"))
    assert out["renderer"] == "ic-render-2.0.0"
    assert out["stored"]["number_locale"] == "en-GB"
    assert out["stored"]["spec_version"] == 2
    assert [q["id"] for q in out["queries"]] == ["m_s"]
    assert set(out) == {"spec", "title", "stored", "queries", "compiled", "renderer"}


async def test_v2_prepare_fills_locale_from_settings(model, monkeypatch):
    from app import dashsettings
    monkeypatch.setattr(dashsettings, "number_locale", lambda: "en-IN")
    out = await d.prepare_spec(v2_measure_spec())
    assert out["stored"]["number_locale"] == "en-IN"


async def test_v2_prepare_passes_naive_utc(model, monkeypatch):
    from app import dashsettings
    monkeypatch.setattr(dashsettings, "naive_utc", lambda: True)
    out = await d.prepare_spec(v2_measure_spec())
    assert "AT TIME ZONE 'UTC'" in out["queries"][0]["sql"]
    monkeypatch.setattr(dashsettings, "naive_utc", lambda: False)
    out = await d.prepare_spec(v2_measure_spec())
    assert "AT TIME ZONE 'UTC'" not in out["queries"][0]["sql"]


async def test_v2_sql_only_never_loads_the_model(monkeypatch):
    async def boom():
        raise AssertionError("no model for a SQL-only spec")
    monkeypatch.setattr(semanticmodel, "load_model", boom)
    out = await d.prepare_spec(v2_sql_spec())
    assert out["renderer"] == "ic-render-2.0.0" and out["compiled"] == []
    assert out["stored"]["number_locale"] in ds.LOCALES


async def test_v1_prepare_renderer_is_1_0_0(monkeypatch):
    out = await d.prepare_spec(sql_only_spec())
    assert out["renderer"] == "ic-render-1.0.0" and "number_locale" not in out["stored"]


async def test_v2_accepts_25_queries_and_v1_refuses_21():
    out = await d.prepare_spec(v2_sql_spec(25))
    assert len(out["queries"]) == 25
    many = sql_only_spec()
    many["queries"] = [{"id": f"q{i}", "sql": "SELECT 1 AS n"} for i in range(21)]
    with pytest.raises(d.SpecRejected):
        await d.prepare_spec(many)


async def test_v2_refuses_31_queries():
    with pytest.raises(d.SpecRejected):
        await d.prepare_spec(v2_sql_spec(31))


def test_validate_stored_queries_caps_by_keyword():
    qs = [{"id": f"q{i}", "sql": "SELECT 1 AS n"} for i in range(25)]
    assert len(d.validate_stored_queries(qs, ds.MAX_QUERIES_V2)) == 25
    with pytest.raises(DashboardError):
        d.validate_stored_queries(qs)
    with pytest.raises(DashboardError):
        d.validate_queries(qs)
    assert len(d.validate_queries(qs, 30)) == 25


async def test_compile_failure_is_a_finding(model):
    bad = v2_measure_spec()
    bad["tiles"][0]["measure"]["name"] = "kg_growth"
    with pytest.raises(d.SpecRejected) as e:
        await d.prepare_spec(bad)
    assert e.value.findings[0]["rule"] == "measure"


# ---------------------------------------------------------------- save_spec_version

def padded(n):
    return {"spec_version": 2, "pad": "x" * n}


async def test_save_v2_accepts_between_caps_and_refuses_above():
    pad = padded(100_000)
    await d.save_spec_version(save_conn(), slug="ops", stored=pad, queries=[], title="T",
                              renderer="ic-render-2.0.0")
    with pytest.raises(DashboardError) as e:
        await d.save_spec_version(save_conn(), slug="ops", stored=padded(131_072), queries=[],
                                  title="T", renderer="ic-render-2.0.0")
    assert "131072" in str(e.value)


async def test_save_v1_keeps_65536_cap_and_message():
    with pytest.raises(DashboardError) as e:
        await d.save_spec_version(save_conn(), slug="ops", stored={"x": "y" * 70000},
                                  queries=[], title="T", renderer="ic-render-1.0.0")
    assert "spec exceeds 65536 bytes" in str(e.value)


async def test_save_v2_accepts_30_queries_and_v1_refuses_25():
    qs = [{"id": f"q{i}", "sql": "SELECT 1 AS n"} for i in range(30)]
    await d.save_spec_version(save_conn(), slug="ops", stored={"spec_version": 2}, queries=qs,
                              title="T", renderer="ic-render-2.0.0")
    with pytest.raises(DashboardError):
        await d.save_spec_version(save_conn(), slug="ops", stored={"spec_version": 1},
                                  queries=qs[:25], title="T", renderer="ic-render-1.0.0")


async def test_script_hashes_come_from_the_pin():
    v2 = await d.save_spec_version(save_conn(), slug="ops", stored={"spec_version": 2},
                                   queries=[], title="T", renderer="ic-render-2.0.0")
    pin2 = dr.RENDERERS["ic-render-2.0.0"]
    assert v2["script_hashes"] == [pin2["csp_hash"], pin2["lib_csp_hash"]]
    assert pin2["lib_csp_hash"] == dr.CHARTLIB_CSP_HASH
    v1 = await d.save_spec_version(save_conn(), slug="ops", stored={"spec_version": 1},
                                   queries=[], title="T", renderer="ic-render-1.0.0")
    pin1 = dr.RENDERERS["ic-render-1.0.0"]
    assert v1["script_hashes"] == [pin1["csp_hash"], dr.LIB_CSP_HASH]
    assert pin1["lib_csp_hash"] == dr.LIB_CSP_HASH


async def test_save_v2_stores_renderer_and_spec():
    conn = save_conn()
    stored = {"spec_version": 2, "title": "T"}
    await d.save_spec_version(conn, slug="ops", stored=stored, queries=[measure_entry()],
                              title="T", renderer="ic-render-2.0.0")
    ins = next(c for c in conn.calls if "INSERT INTO dashboard_versions" in c[1])
    assert ins[2][6] == "ic-render-2.0.0" and ins[2][5] == json.dumps(stored)


# ---------------------------------------------------------------- tools and ops route

async def test_tool_saves_v2_on_2_0_0_and_v1_on_1_0_0(env):  # noqa: F811
    out = await save(v2_sql_spec())
    assert "saved" in out.lower() or "draft" in out.lower() or "v1" in out
    assert env["save_kw"]["renderer"] == "ic-render-2.0.0"
    assert env["save_kw"]["stored"]["number_locale"] in ds.LOCALES
    await save(sql_only_spec())
    assert env["save_kw"]["renderer"] == "ic-render-1.0.0"


def test_preview_handle_carries_renderer():
    tok = dashguard.begin()
    try:
        pid = dashguard.remember_preview({"stored": {}, "queries": [], "title": "T",
                                          "renderer": "ic-render-2.0.0"})
        assert dashguard.previewed(pid)["renderer"] == "ic-render-2.0.0"
        pid = dashguard.remember_preview({"stored": {}, "queries": [], "title": "T"})
        assert "renderer" not in dashguard.previewed(pid)
    finally:
        dashguard.end(tok)


async def test_ops_route_saves_with_prepared_renderer(monkeypatch):
    from app import dashboards_api

    async def fake_prepare(raw):
        return {"stored": {"spec_version": 2}, "queries": [], "title": "T",
                "renderer": "ic-render-2.0.0"}
    seen = {}

    async def fake_save(conn, **kw):
        seen.update(kw)
        return {"dashboard_id": 1, "slug": "s", "version_id": 2, "version": 1, "status": "draft",
                "created": True, "path": "/d/s/v/1/", "format": "spec"}

    class P:
        def acquire(self):
            class A:
                async def __aenter__(self_):
                    return None

                async def __aexit__(self_, *e):
                    return False
            return A()

    async def get_pool():
        return P()
    monkeypatch.setattr(d, "prepare_spec", fake_prepare)
    monkeypatch.setattr(d, "save_spec_version", fake_save)
    monkeypatch.setattr(dashboards_api.db, "get_pool", get_pool)
    await dashboards_api.api_save_dashboard_version("s", {"spec": {"x": 1}, "change_note": "n"})
    assert seen["renderer"] == "ic-render-2.0.0"


# ---------------------------------------------------------------- ./spec timezone

STORED_V2 = {
    "spec_version": 2, "title": "Ops", "number_locale": "en-IN",
    "sections": [{"tiles": ["t"]}],
    "tiles": [{"id": "t", "type": "text", "text": "hi"}]}


class SpecConn:
    def __init__(self, stored, renderer):
        self.stored, self.renderer = stored, renderer

    async def fetchrow(self, sql, *a):
        if sql in (serve.CURRENT_AUTH_SQL, serve.PINNED_AUTH_SQL):
            return {"view_token": "tok", "version_id": 11, "status": "published"}
        if sql == serve.SPEC_SQL:
            return {"spec": json.dumps(self.stored), "queries": "[]", "renderer": self.renderer}
        if sql == serve.FORMAT_SQL:
            return {"is_spec": True, "renderer": self.renderer}
        return None

    async def fetchval(self, sql, *a):
        return None


def _client(monkeypatch, conn):
    class Pool:
        def acquire(self, timeout=None):
            class A:
                async def __aenter__(self_):
                    return conn

                async def __aexit__(self_, *e):
                    return False
            return A()

    async def get_pool():
        return Pool()
    monkeypatch.setattr(serve, "db", SimpleNamespace(get_pool=get_pool))
    return TestClient(main.app, follow_redirects=False)


def test_serve_spec_v2_fills_operator_timezone(monkeypatch):
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    c = _client(monkeypatch, SpecConn(STORED_V2, "ic-render-2.0.0"))
    r = c.get("/d/sales/spec?t=tok", headers={"X-IC-Dashboards-Proxy": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["render_version"] == 2 and body["timezone"] == "Asia/Kolkata"
    assert "sql" not in r.text and "SELECT" not in r.text


def test_serve_spec_v1_has_no_timezone(monkeypatch):
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    stored = {"spec_version": 1, "title": "T", "layout": {"rows": [["t"]]},
              "tiles": [{"id": "t", "type": "text", "text": "hi"}]}
    c = _client(monkeypatch, SpecConn(stored, "ic-render-1.0.0"))
    r = c.get("/d/sales/spec?t=tok", headers={"X-IC-Dashboards-Proxy": "1"})
    assert r.status_code == 200 and r.json()["render_version"] == 1
    assert "timezone" not in r.json()
