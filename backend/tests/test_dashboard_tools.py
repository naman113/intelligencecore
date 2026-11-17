"""Unit tests for app.dashboardtools (AGENT-01..03): fake pool, monkeypatched collaborators."""

import asyncio
import datetime
import json
import re
from pathlib import Path

import pytest

from app import dashboards, dashboardtools as dt, guardrails
from app.agents import graph as g

pytestmark = pytest.mark.asyncio

VIEW_TOKEN = "VIEWTOKEN-secret-111"
PREVIEW_TOKEN = "TOKEN-abc123-xyz"
SQLQ = {"id": "a", "sql": "SELECT 1 AS n", "refresh_minutes": 15}


def spec_of(queries=None, title="Sales"):
    """A small valid chart spec: one table tile on the first SQL query (or a text tile)."""
    queries = [SQLQ] if queries is None else queries
    first = queries[0]["id"] if queries else None
    tile = ({"id": "t", "type": "table", "title": "T", "query": first,
             "columns": [{"column": "n"}]} if first else
            {"id": "t", "type": "text", "title": "T", "text": "x"})
    return {"spec_version": 1, "title": title, "queries": queries,
            "layout": {"rows": [["t"]]}, "tiles": [tile]}


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
        self.current = None  # {"version":, "queries":}
        self.fail_insert = None

    def transaction(self):
        return _Tx(self)

    async def fetchrow(self, sql, *a):
        self.events.append("current")
        return self.current

    async def fetchval(self, sql, *a):
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
    st = {"conn": FakeConn(), "logs": [], "calls": [], "samples": []}
    pool = FakePool(st["conn"])

    async def get_pool():
        return pool

    async def log_tool(name, args, result, start, refused=False):
        st["logs"].append({"name": name, "args": args, "result": result, "refused": refused})

    monkeypatch.setattr(dt, "get_pool", get_pool)
    monkeypatch.setattr(dt, "_log_tool", log_tool)
    monkeypatch.setattr(dt.guardrails, "validate_readonly_sql", lambda s: s.strip())

    async def run_sql(sql):
        st["samples"].append(sql)
        return [{"n": i, "t": "x" * 300} for i in range(8)]
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run_sql)

    async def scope(wid, thread):
        return {"id": wid} if wid == 7 else None
    monkeypatch.setattr(dt.kpihistory, "watch_in_scope", scope)

    async def daily(wid, days):
        st["calls"].append(("daily", wid, days))
        return [{"day": f"2026-01-{i:02d}", "value": i} for i in range(1, 9)]
    monkeypatch.setattr(dt.kpihistory, "daily_rows", daily)

    async def save_version(conn, **kw):
        conn.events.append("save_spec_version")
        st["save_kw"] = kw
        return {"dashboard_id": 3, "slug": kw["slug"], "version_id": 9, "version": 2 if conn.current else 1,
                "status": "draft", "created": conn.current is None, "script_hashes": [],
                "path": f"/d/{kw['slug']}/v/1/"}

    async def mint(conn, *, slug, version, ttl_minutes):
        conn.events.append("mint")
        st["ttl"] = ttl_minutes
        return {"slug": slug, "version": version, "preview_token": PREVIEW_TOKEN,
                "expires_at": datetime.datetime(2026, 1, 2), "path": f"/d/{slug}/v/{version}/"}
    monkeypatch.setattr(dt.dashboards, "save_spec_version", save_version)
    monkeypatch.setattr(dt.dashboards, "mint_preview_token", mint)

    tok = g.ctx_phone.set("test-local")
    tok2 = g.ctx_run_id.set(55)
    yield st
    g.ctx_phone.reset(tok)
    g.ctx_run_id.reset(tok2)


def _dash_fakes(monkeypatch, published=True, code="<html></html>", versions=None):
    async def gd(conn, *, slug):
        if slug == "nope":
            raise dashboards.DashboardError(404, "dashboard not found")
        vs = versions or [{"version": n, "status": "published" if n == 2 and published else "draft",
                           "change_note": "n", "created_at": "t", "last_error": None}
                          for n in range(25, 0, -1)]
        if published:
            vs = [dict(v, status="published" if v["version"] == 2 else "draft") for v in vs]
        return {"dashboard": {"slug": slug, "title": "T", "view_token": VIEW_TOKEN,
                              "current_version": 2 if published else None, "token_rotated_at": None,
                              "path": "/d/x/"},
                "versions": vs}

    async def gv(conn, *, slug, version):
        return {"version": version, "status": "published" if published else "draft", "code": code,
                "queries": [SQLQ], "is_current": published,
                "snapshots": [{"query_id": "a", "error": "boom"}, {"query_id": "b", "error": None}]}

    async def ld(conn, *, limit):
        return [{"slug": f"s{i}", "title": f"t{i}"} for i in range(60)]
    monkeypatch.setattr(dt.dashboards, "get_dashboard", gd)
    monkeypatch.setattr(dt.dashboards, "get_version", gv)
    monkeypatch.setattr(dt.dashboards, "list_dashboards", ld)


# ------------------------------------------------------------- get_dashboard

async def test_get_published(env, monkeypatch):
    _dash_fakes(monkeypatch)
    out = await dt.get_dashboard.ainvoke({"slug": "sales"})
    d = json.loads(out)
    assert d["version_shown"] == d["current_version"] == 2 and d["is_published"] is True
    assert len(d["versions"]) == 20
    assert d["snapshot_errors"] == [{"query_id": "a", "error": "boom"}]
    assert VIEW_TOKEN not in out
    for log in env["logs"]:
        assert VIEW_TOKEN not in json.dumps(log)


async def test_get_unpublished_shows_latest_draft(env, monkeypatch):
    _dash_fakes(monkeypatch, published=False)
    d = json.loads(await dt.get_dashboard.ainvoke({"slug": "sales"}))
    assert d["is_published"] is False and d["status"] == "draft" and d["version_shown"] == 25


async def test_get_code_truncated(env, monkeypatch):
    _dash_fakes(monkeypatch, code="x" * 70000)
    d = json.loads(await dt.get_dashboard.ainvoke({"slug": "sales"}))
    assert d["code_truncated"] is True and len(d["code"]) == 60000


async def test_get_unknown_lists_slugs(env, monkeypatch):
    _dash_fakes(monkeypatch)
    out = await dt.get_dashboard.ainvoke({"slug": "nope"})
    assert out.startswith("REFUSED:") and "s0 (t0)" in out and "s49" in out and "s50" not in out


async def test_get_invalid_slug(env, monkeypatch):
    _dash_fakes(monkeypatch)
    out = await dt.get_dashboard.ainvoke({"slug": "Bad Slug!"})
    assert out.startswith("REFUSED:")


# ----------------------------------------------------------- preview_dashboard

async def test_preview_clean(env):
    out = await dt.preview_dashboard.ainvoke({"spec": spec_of(title="SECRETTITLE")})
    d = json.loads(out)
    assert d["ok"] is True and d["errors"] == [] and d["size_bytes"] > 0
    q = d["queries"][0]
    assert q["id"] == "a" and q["source"] == "sql" and q["columns"] == ["n", "t"]
    assert len(q["sample"]) == 5 and q["error"] is None and len(q["sample"][0]["t"]) == 200
    assert "save_dashboard_version" in d["next"]
    assert env["samples"] == ["SELECT * FROM (SELECT 1 AS n\n) AS _preview LIMIT 5"]
    assert "SECRETTITLE" not in json.dumps(env["logs"])


async def test_preview_spec_errors_do_not_sample(env):
    bad = spec_of()
    bad["tiles"][0]["query"] = "missing"
    d = json.loads(await dt.preview_dashboard.ainvoke({"spec": bad}))
    assert d["ok"] is False and d["errors"] and d["queries"] == [] and env["samples"] == []


async def test_preview_bad_sql_and_exception(env, monkeypatch):
    def val(s):
        if "bad" in s:
            raise guardrails.SQLGuardrailError("only SELECT")
        return s
    monkeypatch.setattr(dt.guardrails, "validate_readonly_sql", val)
    calls = []

    async def run(sql):
        calls.append(sql)
        if "boom" in sql:
            raise RuntimeError("kaput")
        return [{"n": 1}]
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run)
    qs = [{"id": "x", "sql": "bad"}, {"id": "y", "sql": "boom"}, {"id": "z", "sql": "ok"}]
    import time
    entries = [await dt._sample(q, time.monotonic() + 60) for q in qs]
    assert "only SELECT" in entries[0]["error"]
    assert entries[1]["error"] == "RuntimeError: kaput"
    assert entries[2]["sample"] == [{"n": 1}]
    assert len(calls) == 2


async def test_preview_budget(env, monkeypatch):
    monkeypatch.setattr(dt, "SAMPLE_TOTAL_BUDGET", 0.05)

    async def slow(sql):
        await asyncio.sleep(0.1)
        return []
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", slow)
    qs = [{"id": "a", "sql": "SELECT 1 AS n"}, {"id": "b", "sql": "SELECT 2 AS n"}]
    d = json.loads(await dt.preview_dashboard.ainvoke({"spec": spec_of(qs)}))
    assert d["queries"][1]["error"] == "not sampled (time budget)"


async def test_preview_kpi(env):
    qs = [{"id": "k", "source": "kpi_history", "watch_id": 7, "days": 30},
          {"id": "o", "source": "kpi_history", "watch_id": 99, "days": 30}]
    import time
    entries = [await dt._sample(q, time.monotonic() + 60) for q in qs]
    k, o = entries
    assert k["source"] == "kpi_history" and len(k["sample"]) == 5 and k["sample"][-1]["value"] == 8
    assert o["error"] == "watch #99 is unknown or not chartable from this conversation"
    assert env["calls"] == [("daily", 7, 30)]


async def test_preview_output_capped(env, monkeypatch):
    monkeypatch.setattr(dt, "OUTPUT_CAP", 1500)
    out = await dt.preview_dashboard.ainvoke({"spec": spec_of()})
    assert len(out) <= 1500 or json.loads(out)["queries"][0]["sample"] == []


async def test_preview_spec_as_string(env):
    d = json.loads(await dt.preview_dashboard.ainvoke({"spec": json.dumps(spec_of())}))
    assert d["queries"][0]["id"] == "a"
    d = json.loads(await dt.preview_dashboard.ainvoke({"spec": "{not json"}))
    assert d["ok"] is False and "no longer accepted" in d["errors"][0]["message"]


# ------------------------------------------------------- save_dashboard_version

async def _save(**kw):
    a = {"slug": "sales", "spec": spec_of(), "change_note": "first cut"}
    a.update(kw)
    return await dt.save_dashboard_version.ainvoke(a)


PAYLOAD_KEYS = {"_summary", "slug", "dashboard_id", "version_id", "version", "title", "change_note",
                "path", "draft_url", "preview_expires_at", "from_version", "is_new_dashboard",
                "queries_added", "queries_removed", "queries_changed", "origin", "thread_id",
                "format", "tiles_added", "tiles_removed", "tiles_changed", "tiles", "measures",
                "queries"}


async def test_save_clean(env):
    out = await _save()
    conn = env["conn"]
    assert conn.events == ["BEGIN", "current", "save_spec_version", "mint", "insert", "END"]
    sql, args = conn.inserts[0]
    assert "'publish_dashboard', $1::jsonb, 'draft', 'proposed'" in sql and args[1] == 55
    p = json.loads(args[0])
    assert set(p) == PAYLOAD_KEYS
    assert p["draft_url"] == p["path"] + "?p=" + PREVIEW_TOKEN
    assert p["origin"] == "agent" and p["thread_id"] == "test-local"
    assert p["from_version"] is None and p["is_new_dashboard"] is True and p["queries_added"] == ["a"]
    assert p["format"] == "spec" and p["tiles_added"] == ["t"]
    assert env["ttl"] == 1440 and env["save_kw"]["author"] == "agent" and env["save_kw"]["task_run_id"] == 55
    assert "sales" in out and "#41" in out and "awaiting" in out and "NOT published" in out
    assert PREVIEW_TOKEN not in out and "?p=" not in out and "/d/" not in out
    for log in env["logs"]:
        assert PREVIEW_TOKEN not in json.dumps(log)


async def test_save_diff_against_published(env):
    env["conn"].current = {"version": 1, "queries": json.dumps([
        {"id": "a", "sql": "SELECT 1 AS n", "refresh_minutes": 5},
        {"id": "b", "sql": "SELECT 2", "refresh_minutes": 15}])}
    await _save(spec=spec_of([SQLQ, {"id": "c", "sql": "SELECT 3 AS n", "refresh_minutes": 15}]))
    p = json.loads(env["conn"].inserts[0][1][0])
    assert (p["queries_added"], p["queries_removed"], p["queries_changed"]) == (["c"], ["b"], ["a"])
    assert p["from_version"] == 1 and p["is_new_dashboard"] is False


def _nothing_written(env):
    assert env["conn"].events == [] and env["conn"].inserts == []


async def test_save_spec_error_refused(env):
    bad = spec_of()
    bad["tiles"][0]["query"] = "missing"
    out = await _save(spec=bad)
    assert out.startswith("REFUSED:") and '"path"' in out
    _nothing_written(env)


async def test_save_probe_failure(env, monkeypatch):
    seen = []

    async def run(sql):
        seen.append(sql)
        raise RuntimeError("no such column")
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run)
    out = await _save()
    assert out.startswith("REFUSED:") and "a" in out and "no such column" in out
    assert seen == ["SELECT * FROM (SELECT 1 AS n\n) AS _probe LIMIT 0"]
    _nothing_written(env)


async def test_save_kpi_out_of_scope(env):
    out = await _save(spec=spec_of([{"id": "k", "source": "kpi_history", "watch_id": 99}]))
    assert out.startswith("REFUSED:") and "unknown or not chartable from this conversation" in out
    _nothing_written(env)


@pytest.mark.parametrize("note", ["", "   "])
async def test_save_blank_note(env, note):
    out = await _save(change_note=note)
    assert out.startswith("REFUSED:") and "change" in out
    _nothing_written(env)


async def test_save_dashboard_error(env, monkeypatch):
    async def bad(conn, **kw):
        raise dashboards.DashboardError(422, "bad slug")
    monkeypatch.setattr(dt.dashboards, "save_spec_version", bad)
    assert await _save() == "REFUSED: bad slug"


async def test_save_unique_violation(env):
    import asyncpg
    env["conn"].fail_insert = asyncpg.exceptions.UniqueViolationError("dup")
    assert (await _save()) == "REFUSED: conflicting concurrent change; retry"


async def test_save_other_error(env):
    env["conn"].fail_insert = RuntimeError("db down")
    assert (await _save()).startswith("TOOL ERROR: RuntimeError")


async def test_source_never_publishes():
    src = (Path(dt.__file__)).read_text(encoding="utf-8")
    for bad in ("dashboards.publish", "operator_set_version", "apply_publish_proposal", "rotate_token"):
        assert bad not in src
    assert not re.search(r"UPDATE\s+dashboard", src, re.I)


# ------------------------------------------------------- Phase 22: SET LOCAL TimeZone on every tenant read

@pytest.mark.parametrize("tz,expected", [("Asia/Kolkata", {"timezone": "Asia/Kolkata"}), (None, {})])
async def test_tenant_reads_carry_the_day_timezone(env, monkeypatch, tz, expected):
    import time
    monkeypatch.setattr(dt.dashsettings, "day_timezone", lambda: tz)
    kws = []

    async def run(sql, **kw):
        kws.append((sql, kw))
        return [{"n": 3}]
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run)

    await dt._sample({"id": "a", "sql": "SELECT 1 AS n"}, time.monotonic() + 60)
    await dt._row_cap_warning({"id": "a"}, "SELECT 1 AS n", time.monotonic() + 60, 10, ["t"])
    assert await _save()  # the dry run probe
    assert len(kws) == 3
    assert [k for _s, k in kws] == [expected] * 3
    assert "_preview" in kws[0][0] and "_c" in kws[1][0] and "_probe" in kws[2][0]
