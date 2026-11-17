"""Phase 18 (SPEC-03, SPEC-06, SPEC-05): the spec preview and save tools, through a fake pool and
monkeypatched collaborators. No DB, LLM or network."""

import datetime
import json

import pytest

from app import dashboards, dashboardspec, guardrails, semanticmodel
from app import dashboardtools as dt
from app.agents import graph as g
from tests.test_model_compiler import build

pytestmark = pytest.mark.asyncio

PREVIEW_TOKEN = "TOKEN-abc123-xyz"
VIEW_TOKEN = "VIEWTOKEN-secret-111"
OLD_HTML = "<html><script>alert(1)</script></html>"

CONTRACT_KEYS = {"_summary", "slug", "dashboard_id", "version_id", "version", "title",
                 "change_note", "path", "draft_url", "preview_expires_at", "from_version",
                 "is_new_dashboard", "queries_added", "queries_removed", "queries_changed",
                 "origin", "thread_id"}
SPEC_KEYS = {"format", "tiles_added", "tiles_removed", "tiles_changed", "tiles", "measures",
             "queries"}


def sql_spec(title="Open invoices"):
    return {
        "spec_version": 1, "title": title,
        "queries": [{"id": "open_invoices", "sql": "SELECT 1 AS n, 'x' AS customer",
                     "refresh_minutes": 30}],
        "layout": {"rows": [["open"]]},
        "tiles": [{"id": "open", "type": "table", "title": "Open", "query": "open_invoices",
                   "columns": [{"column": "customer"}, {"column": "n"}]}],
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


def mixed_spec():
    return {
        "spec_version": 1, "title": "Mixed",
        "queries": [
            {"id": "kpi", "source": "kpi_history", "watch_id": 7, "days": 30,
             "refresh_minutes": 60},
            {"id": "fc", "source": "forecast", "forecast_id": 4, "refresh_minutes": 60},
            {"id": "sq", "sql": "SELECT 1 AS n, 'x' AS customer", "refresh_minutes": 30},
        ],
        "layout": {"rows": [["trend", "fc_line"], ["tbl", "note"]]},
        "tiles": [
            {"id": "trend", "type": "line", "title": "Trend", "query": "kpi",
             "x": "day", "y": [{"column": "value"}]},
            {"id": "fc_line", "type": "line", "title": "Forecast", "query": "fc",
             "x": "period",
             "y": [{"column": "actual"}, {"column": "forecast", "dashed": True}],
             "band": {"low": "low", "high": "high"}},
            {"id": "tbl", "type": "table", "title": "T", "query": "sq",
             "columns": [{"column": "customer"}]},
            {"id": "note", "type": "text", "title": "Note", "text": "Hello"},
        ],
    }


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.events.append("BEGIN")

    async def __aexit__(self, *exc):
        self.conn.events.append("END")
        return False


class FakeConn:
    def __init__(self):
        self.events = []
        self.inserts = []
        self.sqls = []
        self.current = None  # {"version":, "queries":, "spec":}
        self.fail_insert = None

    def transaction(self):
        return _Tx(self)

    async def fetchrow(self, sql, *a):
        self.sqls.append(sql)
        self.events.append("current")
        return self.current

    async def fetchval(self, sql, *a):
        self.sqls.append(sql)
        if sql.lstrip().startswith("INSERT INTO actions"):
            if self.fail_insert:
                raise self.fail_insert
            self.events.append("insert")
            self.inserts.append((sql, a))
            return 41
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *e):
                return False
        return _A()


@pytest.fixture
def env(monkeypatch):
    st = {"conn": FakeConn(), "logs": [], "samples": [], "count": 3}
    pool = FakePool(st["conn"])

    async def get_pool():
        return pool

    async def log_tool(name, args, result, start, refused=False):
        st["logs"].append({"name": name, "args": args, "result": result, "refused": refused})

    monkeypatch.setattr(dt, "get_pool", get_pool)
    monkeypatch.setattr(dt, "_log_tool", log_tool)
    model = build()

    async def load():
        return model
    monkeypatch.setattr(semanticmodel, "load_model", load)

    async def run_sql(sql):
        st["samples"].append(sql)
        if "count(*) AS n" in sql:
            return [{"n": st["count"]}]
        if st.get("fail_probe") and "LIMIT 0" in sql:
            raise RuntimeError("no such column")
        return [{"n": i, "customer": "c" * 300} for i in range(8)]
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run_sql)

    async def scope(wid, thread):
        return {"id": wid} if wid == 7 else None
    monkeypatch.setattr(dt.kpihistory, "watch_in_scope", scope)

    async def daily(wid, days):
        return [{"day": f"2026-01-{i:02d}", "value": i} for i in range(1, 9)]
    monkeypatch.setattr(dt.kpihistory, "daily_rows", daily)

    async def usable(fid):
        return {"id": fid} if fid == 4 else None
    monkeypatch.setattr(dt.forecasts, "def_usable", usable)

    async def frows(fid):
        if fid != 4:
            raise LookupError(fid)
        return [{"period": f"2026-0{i}", "actual": i if i < 4 else None,
                 "forecast": None if i < 3 else i, "low": 1, "high": 9} for i in range(1, 8)]
    monkeypatch.setattr(dt.forecasts, "dashboard_rows", frows)

    async def save_spec_version(conn, **kw):
        conn.events.append("save_spec_version")
        st["save_kw"] = kw
        return {"dashboard_id": 3, "slug": kw["slug"], "version_id": 9,
                "version": 2 if conn.current else 1, "status": "draft",
                "created": conn.current is None, "script_hashes": [],
                "path": f"/d/{kw['slug']}/v/1/", "format": "spec"}

    async def mint(conn, *, slug, version, ttl_minutes):
        conn.events.append("mint")
        st["ttl"] = ttl_minutes
        return {"slug": slug, "version": version, "preview_token": PREVIEW_TOKEN,
                "expires_at": datetime.datetime(2026, 1, 2), "path": f"/d/{slug}/v/{version}/"}
    monkeypatch.setattr(dt.dashboards, "save_spec_version", save_spec_version)
    monkeypatch.setattr(dt.dashboards, "mint_preview_token", mint)

    tok = g.ctx_phone.set("test-local")
    tok2 = g.ctx_run_id.set(55)
    yield st
    g.ctx_phone.reset(tok)
    g.ctx_run_id.reset(tok2)


async def preview(spec):
    return await dt.preview_dashboard.ainvoke({"spec": spec})


async def save(spec=None, **kw):
    a = {"slug": "sales", "spec": spec if spec is not None else sql_spec(),
         "change_note": "first cut"}
    a.update(kw)
    return await dt.save_dashboard_version.ainvoke(a)


def nothing_written(env):
    assert env["conn"].events == [] and env["conn"].inserts == []


# ----------------------------------------------------------------- preview

async def test_preview_sql_spec(env):
    d = json.loads(await preview(sql_spec()))
    assert d["ok"] is True and d["errors"] == [] and d["warnings"] == []
    q = d["queries"][0]
    assert q["id"] == "open_invoices" and q["source"] == "sql" and q["error"] is None
    assert len(q["sample"]) == 5 and len(q["sample"][0]["customer"]) == 200
    assert d["tiles"][0]["binding"] == "query" and "save_dashboard_version" in d["next"]
    assert env["conn"].events == [] and env["conn"].inserts == []


async def test_preview_json_string(env):
    d = json.loads(await preview(json.dumps(sql_spec())))
    assert d["ok"] is True


@pytest.mark.parametrize("bad", [OLD_HTML, "{not json", "[1, 2]"])
async def test_preview_refuses_html_and_non_objects(env, bad):
    d = json.loads(await preview(bad))
    assert d["ok"] is False and d["queries"] == [] and d["errors"]
    assert "HTML dashboards are no longer accepted" in d["errors"][0]["message"]
    assert env["samples"] == []


async def test_preview_findings_have_paths_and_no_sampling(env):
    spec = sql_spec()
    spec["tiles"][0]["query"] = "missing_query"
    d = json.loads(await preview(spec))
    assert d["ok"] is False and d["queries"] == []
    assert all("path" in f and "message" in f for f in d["errors"])
    assert d["next"] == "Fix the errors and preview again." and env["samples"] == []


async def test_preview_findings_capped(env):
    spec = measure_spec()
    spec["tiles"] = [dict(spec["tiles"][1], id=f"t{i}", measure={"name": f"nope{i}"})
                     for i in range(40)]
    spec["layout"] = {"rows": [[t["id"]] for t in spec["tiles"]]}
    d = json.loads(await preview(spec))
    assert d["ok"] is False and len(d["errors"]) <= dt.FINDINGS_SHOWN


async def test_preview_measure_tiles_sampled_with_compiled_sql(env):
    d = json.loads(await preview(measure_spec()))
    assert d["ok"] is True and d["measures"] == ["net_sales"]
    srcs = {q["source"] for q in d["queries"]}
    assert srcs == {"measure"}
    assert all(q["measure"] == "net_sales" and q["sample"] for q in d["queries"])
    assert any("_preview LIMIT 5" in s for s in env["samples"])
    assert d["warnings"] == []


async def test_preview_row_cap_warning(env):
    env["count"] = 500
    d = json.loads(await preview(measure_spec()))
    assert d["ok"] is True and d["warnings"]
    w = d["warnings"][0]
    assert "500" in w["message"] and "shorter window" in w["message"]
    assert w["path"].startswith("tiles[")


async def test_preview_mixed_sources(env):
    d = json.loads(await preview(mixed_spec()))
    assert d["ok"] is True, d
    by = {q["id"]: q for q in d["queries"]}
    assert by["kpi"]["source"] == "kpi_history" and by["kpi"]["sample"][-1]["value"] == 8
    assert by["fc"]["source"] == "forecast" and by["fc"]["sample"]
    assert by["sq"]["source"] == "sql"


async def test_preview_query_errors_reported(env, monkeypatch):
    async def boom(sql):
        raise RuntimeError("kaput")
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", boom)
    d = json.loads(await preview(sql_spec()))
    assert d["queries"][0]["error"] == "RuntimeError: kaput"


async def test_preview_output_capped(env, monkeypatch):
    monkeypatch.setattr(dt, "OUTPUT_CAP", 1500)
    out = await preview(sql_spec())
    assert len(out) <= 1500 or json.loads(out)["queries"][0]["sample"] == []


async def test_preview_logs_no_spec_body(env):
    await preview(sql_spec("SECRETTITLE"))
    args = env["logs"][0]["args"]
    assert set(args) == {"spec_bytes", "tile_ids", "query_ids"}
    assert "SECRETTITLE" not in json.dumps(env["logs"][0]["args"])


# -------------------------------------------------------------------- save

async def test_save_clean(env):
    out = await save()
    conn = env["conn"]
    assert conn.events == ["BEGIN", "current", "save_spec_version", "mint", "insert", "END"]
    sql, args = conn.inserts[0]
    assert "'publish_dashboard', $1::jsonb, 'draft', 'proposed'" in sql and args[1] == 55
    p = json.loads(args[0])
    assert set(p) == CONTRACT_KEYS | SPEC_KEYS
    assert p["format"] == "spec" and p["tiles_added"] == ["open"]
    assert p["tiles_removed"] == [] and p["tiles_changed"] == []
    assert p["tiles"] == [{"id": "open", "type": "table", "title": "Open",
                           "binding": "query", "ref": "open_invoices"}]
    assert p["queries"] == [{"id": "open_invoices", "source": "sql"}]
    assert p["draft_url"] == p["path"] + "?p=" + PREVIEW_TOKEN
    assert p["origin"] == "agent" and p["thread_id"] == "test-local"
    kw = env["save_kw"]
    assert kw["author"] == "agent" and kw["task_run_id"] == 55 and kw["title"] == "Open invoices"
    assert kw["renderer"] == "ic-render-1.0.0" and env["ttl"] == 1440
    assert "#41" in out and "awaiting" in out and "NOT published" in out
    assert PREVIEW_TOKEN not in out and "?p=" not in out
    for log in env["logs"]:
        assert PREVIEW_TOKEN not in json.dumps(log)


async def test_save_never_moves_publish_pointer(env):
    await save()
    for s in env["conn"].sqls:
        assert "current_version_id =" not in s and "UPDATE dashboards" not in s


async def test_save_measure_sql_frozen(env):
    await save(measure_spec())
    qs = env["save_kw"]["queries"]
    assert qs and all(q["source"] == "measure" and q["sql"].lstrip().upper().startswith(
        ("SELECT", "WITH")) for q in qs)
    p = json.loads(env["conn"].inserts[0][1][0])
    assert p["measures"] == ["net_sales"]
    assert {t["binding"] for t in p["tiles"]} == {"measure"}
    # dry run covered every compiled query
    assert sum("LIMIT 0" in s for s in env["samples"]) == len(qs)


async def test_save_mixed_sources(env):
    await save(mixed_spec())
    p = json.loads(env["conn"].inserts[0][1][0])
    assert {q["source"] for q in p["queries"]} == {"kpi_history", "forecast", "sql"}
    assert len(p["tiles"]) == 4


async def test_save_diff_against_published_spec(env):
    old = sql_spec()
    prep = await dashboards.prepare_spec(old)
    env["conn"].current = {"version": 1, "spec": json.dumps(prep["stored"]),
                           "queries": json.dumps(prep["queries"])}
    new = sql_spec()
    new["tiles"][0]["title"] = "Open now"
    new["tiles"].append({"id": "note", "type": "text", "title": "N", "text": "hi"})
    new["layout"] = {"rows": [["open"], ["note"]]}
    await save(new)
    p = json.loads(env["conn"].inserts[0][1][0])
    assert p["tiles_added"] == ["note"] and p["tiles_changed"] == ["open"]
    assert p["tiles_removed"] == [] and p["from_version"] == 1
    assert p["is_new_dashboard"] is False


async def test_save_conversion_from_html_counts_all_tiles_added(env):
    env["conn"].current = {"version": 3, "spec": None,
                           "queries": json.dumps([{"id": "old", "sql": "SELECT 1",
                                                   "refresh_minutes": 15}])}
    await save()
    p = json.loads(env["conn"].inserts[0][1][0])
    assert p["from_version"] == 3 and p["is_new_dashboard"] is False
    assert p["tiles_added"] == ["open"] and p["tiles_removed"] == []
    assert p["queries_added"] == ["open_invoices"] and p["queries_removed"] == ["old"]


async def test_save_hostile_title_saved_verbatim(env):
    title = "<script>alert(1)</script> & \"q\""
    await save(sql_spec(title))
    assert env["save_kw"]["title"] == title
    p = json.loads(env["conn"].inserts[0][1][0])
    assert p["title"] == title


@pytest.mark.parametrize("note", ["", "   "])
async def test_save_blank_note(env, note):
    out = await save(change_note=note)
    assert out.startswith("REFUSED:") and "change" in out
    nothing_written(env)


@pytest.mark.parametrize("bad", [OLD_HTML, "{nope"])
async def test_save_refuses_html(env, bad):
    out = await save(bad)
    assert out.startswith("REFUSED:")
    assert "chart spec" in out
    nothing_written(env)


async def test_save_invalid_spec_lists_findings(env):
    spec = sql_spec()
    spec["tiles"][0]["query"] = "missing"
    out = await save(spec)
    assert out.startswith("REFUSED:") and '"path"' in out
    nothing_written(env)


async def test_save_dry_run_failure(env):
    env["fail_probe"] = True
    out = await save()
    assert out.startswith("REFUSED:") and "failed its dry run" in out and "no such column" in out
    nothing_written(env)


async def test_save_dry_run_failure_on_measure(env):
    env["fail_probe"] = True
    out = await save(measure_spec())
    assert out.startswith("REFUSED:") and "m_" in out
    nothing_written(env)


async def test_save_kpi_out_of_scope(env):
    spec = mixed_spec()
    spec["queries"][0]["watch_id"] = 99
    out = await save(spec)
    assert out.startswith("REFUSED:") and "unknown or not chartable" in out
    nothing_written(env)


async def test_save_unusable_forecast(env):
    spec = mixed_spec()
    spec["queries"][1]["forecast_id"] = 99
    out = await save(spec)
    assert out.startswith("REFUSED:") and "forecast #99" in out
    nothing_written(env)


async def test_save_dashboard_error(env, monkeypatch):
    async def bad(conn, **kw):
        raise dashboards.DashboardError(422, "bad slug")
    monkeypatch.setattr(dt.dashboards, "save_spec_version", bad)
    assert await save() == "REFUSED: bad slug"


async def test_save_unique_violation(env):
    import asyncpg
    env["conn"].fail_insert = asyncpg.exceptions.UniqueViolationError("dup")
    assert (await save()) == "REFUSED: conflicting concurrent change; retry"


async def test_save_other_error(env):
    env["conn"].fail_insert = RuntimeError("db down")
    assert (await save()).startswith("TOOL ERROR: RuntimeError")


# ------------------------------------------------------------ get_dashboard

def _gd_fakes(monkeypatch, version):
    async def gd(conn, *, slug):
        return {"dashboard": {"slug": slug, "title": "T", "view_token": VIEW_TOKEN,
                              "current_version": 2},
                "versions": [{"version": 2, "status": "published", "change_note": "n",
                              "created_at": "t", "last_error": None}]}

    async def gv(conn, *, slug, version_=None, **kw):
        return version
    monkeypatch.setattr(dt.dashboards, "get_dashboard", gd)
    monkeypatch.setattr(dt.dashboards, "get_version", gv)


async def test_get_spec_version(env, monkeypatch):
    spec = dashboardspec.authoring_form((await dashboards.prepare_spec(measure_spec()))["stored"])
    comp = [{"id": "m_net", "source": "measure", "measure": "net_sales", "sql": "SELECT 1",
             "columns": ["metric_time__month", "net_sales"]},
            {"id": "x", "sql": "SELECT 1"}]
    _gd_fakes(monkeypatch, {"version": 2, "status": "published", "is_current": True,
                            "format": "spec", "spec": spec, "queries": comp, "snapshots": []})
    out = await dt.get_dashboard.ainvoke({"slug": "sales"})
    d = json.loads(out)
    assert d["format"] == "spec" and d["spec"] == spec
    assert d["compiled_measures"] == [{"id": "m_net", "measure": "net_sales",
                                       "columns": ["metric_time__month", "net_sales"]}]
    assert "code" not in d and VIEW_TOKEN not in out


async def test_get_html_version_needs_conversion(env, monkeypatch):
    _gd_fakes(monkeypatch, {"version": 2, "status": "published", "is_current": True,
                            "format": "html", "spec": None, "code": OLD_HTML,
                            "queries": [{"id": "a", "sql": "SELECT 1"}], "snapshots": []})
    monkeypatch.setattr(dt.dashboards, "legacy_html_on", lambda: False)
    d = json.loads(await dt.get_dashboard.ainvoke({"slug": "sales"}))
    assert d["format"] == "html" and d["needs_conversion"] is True
    assert d["code"] == OLD_HTML and d["queries"] == [{"id": "a", "sql": "SELECT 1"}]
    assert "chart spec" in d["convert_hint"] and "save_dashboard_version" in d["convert_hint"]


async def test_get_html_code_capped(env, monkeypatch):
    _gd_fakes(monkeypatch, {"version": 2, "status": "published", "is_current": True,
                            "format": "html", "code": "x" * 70000, "queries": [],
                            "snapshots": []})
    d = json.loads(await dt.get_dashboard.ainvoke({"slug": "sales"}))
    assert d["code_truncated"] is True and len(d["code"]) == dt.CODE_RETURN_CAP


# --------------------------------------------------------------- invariants

def test_docstrings_describe_specs_not_html():
    for fn in (dt.preview_dashboard, dt.save_dashboard_version):
        text = fn.description
        assert "chart spec" in text and "HTML" in text
        assert "contract" not in text.lower()
    assert "kpi_history" in dt.preview_dashboard.description
    assert "forecast_id" in dt.preview_dashboard.description


def test_tool_signatures_take_spec_not_code():
    for fn, expect in ((dt.preview_dashboard, {"spec"}),
                       (dt.save_dashboard_version, {"slug", "spec", "change_note",
                                                    "preview_id"})):  # Quick 036
        assert set(fn.args) == expect


def test_source_never_publishes_or_lints():
    from pathlib import Path
    import re
    src = Path(dt.__file__).read_text(encoding="utf-8")
    for bad in ("dashboards.publish", "operator_set_version", "apply_publish_proposal",
                "rotate_token", "dashboardlint"):
        assert bad not in src
    assert not re.search(r"UPDATE\s+dashboard", src, re.I)
    assert not re.search(r"current_version_id\s*=", src)
    assert guardrails  # imported for the dry-run seam
