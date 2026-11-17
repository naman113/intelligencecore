"""Phase 6 real-PG coverage of save_dashboard_version's single transaction.

Skipped without DASH_TEST_DATABASE_URL; added to the gate by plan 06-06. Enable against the p6 stack:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9063/agent_memory
"""
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import pytest

from app import dashboards, dashboardtools as dt
from app.agents import graph as g

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("DASH_TEST_DATABASE_URL"),
                       reason="DASH_TEST_DATABASE_URL not set (real-Postgres dashboard tests)"),
]

APP_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = APP_ROOT / "backend" / "app" / "schema.sql"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def dash_db():
    import asyncpg

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "dash_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    conn = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        conn = await asyncpg.connect(url)
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.execute(sql)
        yield types.SimpleNamespace(conn=conn, url=url)
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def env(dash_db, monkeypatch):
    import asyncpg

    pool = await asyncpg.create_pool(dash_db.url, min_size=1, max_size=4)

    async def get_pool():
        return pool

    async def run_sql(sql):
        return []

    async def log_tool(*a, **k):
        return None

    monkeypatch.setattr(dt, "get_pool", get_pool)
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run_sql)
    monkeypatch.setattr(dt, "_log_tool", log_tool)
    t1 = g.ctx_phone.set("test-local")
    t2 = g.ctx_run_id.set(None)
    try:
        yield types.SimpleNamespace(conn=dash_db.conn, pool=pool)
    finally:
        g.ctx_phone.reset(t1)
        g.ctx_run_id.reset(t2)
        await pool.close()


def _spec(extra=False, refresh=15, title="Sales"):
    spec = {
        "spec_version": 1, "title": title,
        "queries": [{"id": "orders", "sql": "SELECT 1 AS n, 'x' AS customer",
                     "refresh_minutes": refresh}],
        "layout": {"rows": [["tbl"]]},
        "tiles": [{"id": "tbl", "type": "table", "title": "Orders", "query": "orders",
                   "columns": [{"column": "customer"}, {"column": "n"}]}],
    }
    if extra:
        spec["queries"].append({"id": "extra", "sql": "SELECT 2 AS n", "refresh_minutes": 15})
        spec["tiles"].append({"id": "big", "type": "stat", "title": "Big", "query": "extra",
                              "column": "n"})
        spec["layout"] = {"rows": [["tbl", "big"]]}
    return spec


async def _save(spec, slug="sales", note="first"):
    return await dt.save_dashboard_version.ainvoke(
        {"slug": slug, "spec": spec, "change_note": note})


async def _counts(c):
    out = {}
    for t in ("dashboards", "dashboard_versions", "dashboard_snapshots", "dashboard_preview_tokens"):
        out[t] = await c.fetchval(f"SELECT count(*) FROM {t}")
    out["actions"] = await c.fetchval("SELECT count(*) FROM actions WHERE kind = 'publish_dashboard'")
    return out


async def test_save_creates_spec_draft_token_and_one_action(env):
    spec = _spec()
    out = await _save(spec)
    assert out.startswith("Saved draft v1"), out
    c = env.conn
    assert await c.fetchval("SELECT count(*) FROM dashboards") == 1
    row = await c.fetchrow("SELECT version, status, code, spec, renderer FROM dashboard_versions")
    assert (row["version"], row["status"], row["code"]) == (1, "draft", "")
    stored = json.loads(row["spec"]) if isinstance(row["spec"], str) else row["spec"]
    assert isinstance(stored, dict) and stored["tiles"] and row["renderer"]
    assert await c.fetchval("SELECT count(*) FROM dashboard_snapshots") == 1
    assert await c.fetchval("SELECT count(*) FROM dashboard_preview_tokens") == 1
    acts = await c.fetch("SELECT kind, status, tier, task_run_id, payload FROM actions")
    assert len(acts) == 1
    a = acts[0]
    assert (a["kind"], a["status"], a["tier"], a["task_run_id"]) == ("publish_dashboard", "proposed", "draft", None)
    p = json.loads(a["payload"]) if isinstance(a["payload"], str) else a["payload"]
    assert {"_summary", "slug", "dashboard_id", "version_id", "version", "title", "change_note", "path",
            "draft_url", "preview_expires_at", "from_version", "is_new_dashboard", "queries_added",
            "queries_removed", "queries_changed", "origin", "thread_id", "format", "tiles_added",
            "tiles_removed", "tiles_changed", "tiles", "measures", "queries"} <= set(p)
    assert p["format"] == "spec" and p["tiles_added"] == ["tbl"]
    assert p["origin"] == "agent" and p["thread_id"] == "test-local" and p["is_new_dashboard"] is True
    token = parse_qs(urlsplit(p["draft_url"]).query)["p"][0]
    assert await dashboards.preview_token_ok(c, version_id=p["version_id"], token=token) is True
    assert await c.fetchval("SELECT current_version_id FROM dashboards") is None
    assert token not in out


async def test_second_save_diffs_against_published(env):
    await _save(_spec())
    c = env.conn
    d = await c.fetchrow("SELECT id FROM dashboards")
    vid = await c.fetchval("SELECT id FROM dashboard_versions WHERE version = 1")
    async with c.transaction():
        await dashboards.publish(c, dashboard_id=d["id"], version_id=vid)
    out = await _save(_spec(extra=True, refresh=16), note="tweak")
    assert out.startswith("Saved draft v2"), out
    assert await c.fetchval("SELECT current_version_id FROM dashboards") == vid
    pl = await c.fetchval("SELECT payload FROM actions ORDER BY id DESC LIMIT 1")
    p = json.loads(pl) if isinstance(pl, str) else pl
    assert p["from_version"] == 1 and p["is_new_dashboard"] is False
    assert p["queries_added"] == ["extra"] and p["queries_changed"] == ["orders"]
    assert p["tiles_added"] == ["big"] and p["tiles_removed"] == []
    assert await c.fetchval("SELECT status FROM dashboard_versions WHERE version = 2") == "draft"


async def test_action_insert_failure_rolls_back_everything(env):
    tok = g.ctx_run_id.set(987654321)
    try:
        out = await _save(_spec())
    finally:
        g.ctx_run_id.reset(tok)
    assert out.startswith("TOOL ERROR:"), out
    assert all(v == 0 for v in (await _counts(env.conn)).values())


async def test_invalid_spec_refusal_writes_nothing(env):
    bad = _spec()
    bad["tiles"][0]["query"] = "missing"
    out = await _save(bad)
    assert out.startswith("REFUSED:"), out
    assert all(v == 0 for v in (await _counts(env.conn)).values())
