"""Real-Postgres tests for Phase 9 KPI-04 (consolidation.history_retention).
Skipped unless DASH_TEST_DATABASE_URL is set; run non-skippably by
scripts/dashboards_db_gate.sh.
"""
import asyncio
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres retention tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
QUERIES = [{"id": "q1", "sql": "SELECT 1 AS n"}, {"id": "q2", "sql": "SELECT 2 AS n"}]


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
        await conn.execute(sql)
        yield types.SimpleNamespace(conn=conn, url=url, sql=sql)
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def ret(dash_db, monkeypatch):
    import asyncpg
    from app import consolidation

    pool = await asyncpg.create_pool(dash_db.url, min_size=1, max_size=4)
    state = types.SimpleNamespace(dry_run=True)

    async def get_pool():
        return pool

    monkeypatch.setattr(consolidation, "get_pool", get_pool)
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: types.SimpleNamespace(retention_dry_run=state.dry_run))
    try:
        yield types.SimpleNamespace(db=dash_db, state=state, mod=consolidation, pool=pool)
    finally:
        await pool.close()


async def _version(conn, did, n, status, retired_days=None):
    return await conn.fetchval(
        "INSERT INTO dashboard_versions (dashboard_id, version, title, code, queries, status, "
        "published_at, retired_at) VALUES ($1, $2, 'raw', '<!doctype html><p>x</p>', $3::jsonb, "
        "$4::text, CASE WHEN $4::text = 'published' THEN now() END, "
        "CASE WHEN $5::int IS NOT NULL THEN now() - make_interval(days => $5::int) END) "
        "RETURNING id", did, n, json.dumps(QUERIES), status, retired_days)


async def _snap(conn, vid, qid, age_days):
    await conn.execute(
        "INSERT INTO dashboard_snapshots (version_id, query_id, refreshed_at, created_at) "
        "VALUES ($1, $2, now() - make_interval(days => $3::int), "
        "now() - make_interval(days => $3::int))", vid, qid, age_days)


async def _seed(conn):
    wid = await conn.fetchval(
        "INSERT INTO watches (thread_id, kind, title, condition, status) "
        "VALUES ('p9-retention-test', 'kpi', 't', 'c', 'paused') RETURNING id")
    for days in (401, 401, 399, 0):
        await conn.execute(
            "INSERT INTO metric_history (watch_id, observed_at, metric, value) "
            "VALUES ($1, now() - make_interval(days => $2::int), 'm', 1)", wid, days)
    a = await conn.fetchval(
        "INSERT INTO dashboards (slug, title, view_token) VALUES ('a', 'A', 'x') RETURNING id")
    v = {}
    v[1] = await _version(conn, a, 1, "retired", 31)
    v[2] = await _version(conn, a, 2, "retired", 29)
    v[3] = await _version(conn, a, 3, "published")
    v[4] = await conn.fetchval(
        "INSERT INTO dashboard_versions (dashboard_id, version, title, code, queries, status, "
        "created_at) VALUES ($1, 4, 'raw', '<p>x</p>', $2::jsonb, 'draft', "
        "now() - interval '40 days') RETURNING id", a, json.dumps(QUERIES))
    v[5] = await _version(conn, a, 5, "retired")
    v[6] = await _version(conn, a, 6, "retired")
    await conn.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2", v[3], a)
    for q in ("q1", "q2"):
        await _snap(conn, v[1], q, 31)
        await _snap(conn, v[3], q, 500)
    await _snap(conn, v[2], "q1", 29)
    await _snap(conn, v[4], "q1", 40)
    await _snap(conn, v[5], "q1", 31)
    await _snap(conn, v[6], "q1", 5)
    b = await conn.fetchval(
        "INSERT INTO dashboards (slug, title, view_token) VALUES ('b', 'B', 'x') RETURNING id")
    vb = await _version(conn, b, 1, "retired", 60)
    await conn.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2", vb, b)
    await _snap(conn, vb, "q1", 60)
    return types.SimpleNamespace(a=a, v=v, b=b, vb=vb, wid=wid)


async def _counts(conn):
    return (await conn.fetchval("SELECT count(*) FROM metric_history"),
            await conn.fetchval("SELECT count(*) FROM dashboard_snapshots"))


async def _snap_keys(conn):
    rows = await conn.fetch("SELECT version_id, query_id FROM dashboard_snapshots")
    return {(r["version_id"], r["query_id"]) for r in rows}


async def test_dry_run_counts_exactly_and_deletes_nothing(ret):
    conn = ret.db.conn
    await _seed(conn)
    before = await _counts(conn)
    out = await ret.mod.history_retention()
    assert out["dry_run"] is True
    assert out["metric_history"] == 2
    assert out["dashboard_snapshots"] == 3
    assert await _counts(conn) == before


async def test_live_run_deletes_exactly(ret):
    conn = ret.db.conn
    s = await _seed(conn)
    ret.state.dry_run = False
    out = await ret.mod.history_retention()
    assert out["metric_history"] == 2 and out["dashboard_snapshots"] == 3
    ages = await conn.fetch(
        "SELECT (now() - observed_at) > interval '400 days' AS old FROM metric_history")
    assert len(ages) == 2 and not any(r["old"] for r in ages)
    keys = await _snap_keys(conn)
    v = s.v
    assert (v[1], "q1") not in keys and (v[1], "q2") not in keys
    assert (v[5], "q1") not in keys
    for survivor in [(v[2], "q1"), (v[3], "q1"), (v[3], "q2"), (v[4], "q1"),
                     (v[6], "q1"), (s.vb, "q1")]:
        assert survivor in keys
    again = await ret.mod.history_retention()
    assert again["metric_history"] == 0 and again["dashboard_snapshots"] == 0


async def test_rollback_race_never_deletes_new_current(ret):
    conn = ret.db.conn
    s = await _seed(conn)
    ret.state.dry_run = False
    x = await ret.pool.acquire()
    try:
        tx = x.transaction()
        await tx.start()
        await x.fetchrow("SELECT id FROM dashboards WHERE id=$1 FOR UPDATE", s.a)
        task = asyncio.create_task(ret.mod.history_retention())
        await asyncio.sleep(0.5)
        assert not task.done()
        await x.execute("UPDATE dashboard_versions SET status='retired', retired_at=now() "
                        "WHERE dashboard_id=$1 AND status='published'", s.a)
        await x.execute("UPDATE dashboard_versions SET status='published', "
                        "published_at=now(), retired_at=NULL WHERE id=$1", s.v[1])
        await x.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2",
                        s.v[1], s.a)
        await tx.commit()
        out = await asyncio.wait_for(task, 10)
    finally:
        await ret.pool.release(x)
    assert out["dashboard_snapshots"] != -1  # no deadlock / abort
    keys = await _snap_keys(conn)
    assert (s.v[1], "q1") in keys and (s.v[1], "q2") in keys


async def test_republish_reseeds_snapshots(ret):
    from app import dashboards
    from app.dashboard_refresh import SEED_SQL

    conn = ret.db.conn
    s = await _seed(conn)
    ret.state.dry_run = False
    await ret.mod.history_retention()
    assert (s.v[1], "q1") not in await _snap_keys(conn)
    await dashboards.publish(conn, dashboard_id=s.a, version_id=s.v[1],
                             expect_status=("retired",))
    await conn.execute(SEED_SQL)
    keys = await _snap_keys(conn)
    assert (s.v[1], "q1") in keys and (s.v[1], "q2") in keys
