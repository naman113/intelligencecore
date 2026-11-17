"""Phase 18 (SPEC-03, SPEC-06): prepare_spec, save_spec_version, validate_stored_queries and
the reader extras, through recording fake connections. No DB, LLM or network."""

import json

import pytest

from app import dashboardrender as dr
from app import dashboards as d
from app import semanticmodel
from app.dashboards import DashboardError, SpecRejected
from tests.test_model_compiler import build

SQL_ONLY_SQL = ("SELECT c.name AS customer, sum(i.amount) AS open_amount FROM invoices i "
                "JOIN customers c ON c.id = i.customer_id GROUP BY c.name")


def sql_only_spec():
    return {
        "spec_version": 1, "title": "Open invoices",
        "queries": [{"id": "open_invoices", "sql": SQL_ONLY_SQL, "refresh_minutes": 30}],
        "layout": {"rows": [["open"]]},
        "tiles": [{"id": "open", "type": "table", "title": "Open", "query": "open_invoices",
                   "columns": [{"column": "customer"}, {"column": "open_amount"}]}],
    }


def measure_spec():
    return {
        "spec_version": 1, "title": "Sales",
        "layout": {"rows": [["net", "regions"]]},
        "tiles": [
            {"id": "net", "type": "stat", "title": "Net sales",
             "measure": {"name": "net_sales", "grain": "month", "window": {"last_periods": 3}},
             "pick": "last_complete"},
            {"id": "regions", "type": "bar", "title": "By region",
             "measure": {"name": "net_sales", "by": ["regions.name"],
                         "window": {"last_days": 90}, "top": 8}},
        ],
    }


@pytest.fixture
def no_model(monkeypatch):
    async def boom():
        raise AssertionError("load_model must not be called for a SQL-only spec")

    monkeypatch.setattr(semanticmodel, "load_model", boom)


@pytest.fixture
def with_model(monkeypatch):
    model = build()

    async def load():
        return model

    monkeypatch.setattr(semanticmodel, "load_model", load)


def measure_entry(**kw):
    e = {"id": "m_net", "source": "measure", "measure": "net_sales", "request": {"measure": "net_sales"},
         "columns": ["metric_time__month", "net_sales"], "sql": "SELECT 1 AS n", "refresh_minutes": 15}
    e.update(kw)
    return e


# ------------------------------------------------------------ validate_stored_queries

def test_accepts_every_kind():
    qs = [{"id": "a", "sql": "SELECT 1 AS n", "refresh_minutes": 15},
          {"id": "k", "source": "kpi_history", "watch_id": 3},
          {"id": "f", "source": "forecast", "forecast_id": 2},
          measure_entry()]
    out = d.validate_stored_queries(qs)
    assert [q["id"] for q in out] == ["a", "k", "f", "m_net"]
    assert out[3]["source"] == "measure"


@pytest.mark.parametrize("mut", [
    {"id": "net"},                       # no m_ prefix
    {"extra": 1},                        # extra key
    {"columns": "x"},                    # non-list columns
    {"measure": "Bad Name"},             # fails NAME_RE
    {"sql": "DROP TABLE x"},             # guardrails
    {"refresh_minutes": 1},              # out of range
    {"request": []},
])
def test_measure_entry_refused(mut):
    with pytest.raises(DashboardError) as ei:
        d.validate_stored_queries([measure_entry(**mut)])
    assert ei.value.status == 422


def test_measure_entry_missing_key_refused():
    e = measure_entry()
    del e["request"]
    with pytest.raises(DashboardError):
        d.validate_stored_queries([e])


def test_duplicates_and_cap_refused():
    with pytest.raises(DashboardError):
        d.validate_stored_queries([measure_entry(), measure_entry()])
    with pytest.raises(DashboardError):
        d.validate_stored_queries([{"id": "m_x", "sql": "SELECT 1 AS n"}])
    many = [{"id": f"q{i}", "sql": "SELECT 1 AS n"} for i in range(21)]
    with pytest.raises(DashboardError):
        d.validate_stored_queries(many)


# ------------------------------------------------------------ prepare_spec

async def test_sql_only_spec_never_loads_the_model(no_model):
    out = await d.prepare_spec(sql_only_spec())
    assert set(out) == {"spec", "title", "stored", "queries", "compiled", "renderer"}
    assert out["renderer"] == "ic-render-1.0.0"
    assert out["title"] == "Open invoices" and out["compiled"] == []
    assert [q["id"] for q in out["queries"]] == ["open_invoices"]
    assert out["stored"]["tiles"][0]["qid"] == "open_invoices"


async def test_measure_spec_compiles(with_model):
    out = await d.prepare_spec(measure_spec())
    ids = [q["id"] for q in out["queries"]]
    assert ids and all(i.startswith("m_") for i in ids)
    assert all(q["source"] == "measure" for q in out["queries"])
    assert out["compiled"] == out["queries"]


async def test_invalid_spec_carries_every_finding():
    bad = sql_only_spec()
    bad["spec_version"] = 9
    bad["tiles"][0]["query"] = "nope"
    with pytest.raises(SpecRejected) as ei:
        await d.prepare_spec(bad)
    assert ei.value.status == 422 and len(ei.value.findings) >= 2
    assert all({"rule", "path", "message"} <= set(f) for f in ei.value.findings)


async def test_html_is_refused_as_spec():
    with pytest.raises(SpecRejected) as ei:
        await d.prepare_spec("<html></html>")
    assert ei.value.findings[0]["path"] == "spec"


async def test_declared_query_refused_by_guardrails_becomes_finding(no_model):
    bad = sql_only_spec()
    bad["queries"][0]["sql"] = "DELETE FROM invoices"
    with pytest.raises(SpecRejected) as ei:
        await d.prepare_spec(bad)
    f = ei.value.findings[0]
    assert f["rule"] == "query" and f["path"] == "queries[0]"


async def test_unmodeled_measure_is_a_finding(with_model):
    bad = measure_spec()
    bad["tiles"][0]["measure"]["name"] = "not_a_measure"
    with pytest.raises(SpecRejected) as ei:
        await d.prepare_spec(bad)
    assert any(f["rule"] == "not_modeled" for f in ei.value.findings)


# ------------------------------------------------------------ save_spec_version

class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class Conn:
    def __init__(self, rules=None):
        self.calls = []
        self.rules = rules or []

    def transaction(self):
        return _Tx()

    def _r(self, method, sql, args, default):
        self.calls.append((method, sql, args))
        for sub, resp in self.rules:
            if sub in sql:
                return resp
        return default

    async def fetchrow(self, sql, *a):
        return self._r("fetchrow", sql, a, None)

    async def fetchval(self, sql, *a):
        return self._r("fetchval", sql, a, None)

    async def fetch(self, sql, *a):
        return self._r("fetch", sql, a, [])

    async def execute(self, sql, *a):
        return self._r("execute", sql, a, "UPDATE 1")

    async def executemany(self, sql, rows):
        return self._r("executemany", sql, (rows,), None)


def save_conn():
    return Conn([("INSERT INTO dashboards", {"id": 7, "created": True}),
                 ("COALESCE(MAX(version)", 3), ("INSERT INTO dashboard_versions", 42)])


async def test_save_spec_version_writes_draft_and_snapshots(no_model):
    prep = await d.prepare_spec(sql_only_spec())
    conn = save_conn()
    out = await d.save_spec_version(conn, slug="open", stored=prep["stored"],
                                    queries=prep["queries"] + [measure_entry()],
                                    title=prep["title"], renderer=dr.CURRENT_RENDERER,
                                    change_note="first", task_run_id=9)
    assert out["format"] == "spec" and out["version"] == 3 and out["version_id"] == 42
    assert out["status"] == "draft" and out["path"] == "/d/open/v/3/"
    assert out["script_hashes"] == [dr.RENDERERS[dr.CURRENT_RENDERER]["csp_hash"], dr.LIB_CSP_HASH]
    ins = next(c for c in conn.calls if "INSERT INTO dashboard_versions" in c[1])
    assert "''" in ins[1] and "spec" in ins[1] and "renderer" in ins[1]
    assert json.loads(ins[2][4])[1]["id"] == "m_net"
    assert ins[2][5] == json.dumps(prep["stored"], ensure_ascii=False)
    assert ins[2][6] == dr.CURRENT_RENDERER
    snap = next(c for c in conn.calls if c[0] == "executemany")
    assert [r[1] for r in snap[2][0]] == ["open_invoices", "m_net"]
    assert not any("current_version_id" in c[1] and "UPDATE" in c[1] for c in conn.calls)


async def test_save_spec_version_refusals():
    conn = save_conn()
    base = dict(slug="open", stored={"tiles": []}, queries=[], title="T")
    with pytest.raises(DashboardError):
        await d.save_spec_version(conn, renderer="ic-render-9.9.9", **base)
    with pytest.raises(DashboardError):
        await d.save_spec_version(conn, renderer=dr.CURRENT_RENDERER,
                                  **{**base, "stored": {"x": "y" * 70000}})
    with pytest.raises(DashboardError):
        await d.save_spec_version(conn, renderer=dr.CURRENT_RENDERER,
                                  **{**base, "queries": [measure_entry(id="net")]})
    assert conn.calls == []


# ------------------------------------------------------------ readers

STORED = {"spec_version": 1, "title": "T", "layout": {"rows": [["a"]]},
          "tiles": [{"id": "a", "type": "text", "text": "hi"}]}
STORED2 = {**STORED, "tiles": [{"id": "a", "type": "text", "text": "changed"},
                               {"id": "b", "type": "text", "text": "new"}]}
STORED3 = {**STORED, "tiles": [{"id": "b", "type": "text", "text": "new"}]}


async def test_list_extras(monkeypatch):
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    conn = Conn([("ANY($1", [{"id": 1, "is_html": False}, {"id": 2, "is_html": True}])])
    await d._p18_list_extras(conn, rows)
    assert [(r["current_format"], r["needs_conversion"]) for r in rows] == [
        ("spec", False), ("html", True), (None, False)]
    monkeypatch.setattr(d, "legacy_html_on", lambda: True)
    await d._p18_list_extras(conn, rows)
    assert rows[1]["needs_conversion"] is False


async def test_dashboard_extras_diff_against_previous():
    conn = Conn([("SELECT version, spec, queries", [
        {"version": 1, "spec": None, "queries": "[]"},
        {"version": 2, "spec": json.dumps(STORED), "queries": "[]"},
        {"version": 3, "spec": json.dumps(STORED2), "queries": "[]"},
        {"version": 4, "spec": json.dumps(STORED3), "queries": "[]"}])])
    versions = [{"version": n} for n in (4, 3, 2, 1)]
    await d._p18_dashboard_extras(conn, 7, versions)
    v4, v3, v2, v1 = versions
    assert v1["format"] == "html" and v1["tiles"] == [] and v1["tiles_added"] == []
    assert v2["format"] == "spec" and v2["tiles_added"] == ["a"]  # HTML before: all added
    assert v3["tiles_added"] == ["b"] and v3["tiles_changed"] == ["a"]
    assert v4["tiles_removed"] == ["a"] and v4["tiles_changed"] == ["b"] or v4["tiles_removed"] == ["a"]
    assert v3["queries_used"] == [] and v3["measures"] == []
    assert [t["id"] for t in v3["tiles"]] == ["a", "b"]


async def test_extras_tolerate_empty_results():
    versions = [{"version": 1}]
    await d._p18_dashboard_extras(Conn(), 7, versions)
    assert versions[0]["format"] == "html"
    rows = [{"id": 1}]
    await d._p18_list_extras(Conn(), rows)
    assert rows[0]["current_format"] is None
    out = {"version": 1}
    await d._p18_version_extras(Conn(), "x", 1, out)
    assert out["format"] == "html" and out["spec"] is None and out["renderer"] is None


async def test_version_extras_authoring_form_has_no_qid(no_model):
    prep = await d.prepare_spec(sql_only_spec())
    row = {"spec": json.dumps(prep["stored"]), "renderer": dr.CURRENT_RENDERER,
           "queries": json.dumps(prep["queries"])}
    out = {"version": 1}
    await d._p18_version_extras(Conn([("SELECT spec, renderer", row)]), "x", 1, out)
    assert out["format"] == "spec" and out["renderer"] == dr.CURRENT_RENDERER
    assert all("qid" not in t for t in out["spec"]["tiles"])
    assert out["summary"]["queries"] == [{"id": "open_invoices", "source": "sql"}]
