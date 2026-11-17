"""Phase 6 real-Postgres coverage of the kpi_history snapshot source (plan 06-02).

Timezone bucketing, latest-metric filter, seed backfill, refresh and the
deleted-watch failure. Skipped unless DASH_TEST_DATABASE_URL is set; plan 06-06
adds this file to scripts/dashboards_db_gate.sh. Enable against the p6 stack:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9063/agent_memory
Each test uses a throwaway database; the shared tenant DB is never addressed.
"""
import datetime
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres dashboard tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
CODE = "<!doctype html><html><body><p>x</p></body></html>"


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
async def pool(dash_db, monkeypatch):
    import asyncpg
    from app import dashboard_refresh, kpihistory

    p = await asyncpg.create_pool(dash_db.url, min_size=1, max_size=4)

    async def get_pool():
        return p

    async def no_tenant(sql):
        raise AssertionError("tenant path used")

    monkeypatch.setattr(dashboard_refresh, "db", types.SimpleNamespace(get_pool=get_pool))
    monkeypatch.setattr(kpihistory, "get_pool", get_pool)
    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    monkeypatch.setattr(dashboard_refresh, "_execute", no_tenant)
    monkeypatch.setattr(kpihistory, "_tz", lambda: "UTC")
    try:
        yield p
    finally:
        await p.close()


async def _watch(conn, kind="kpi", thread="t"):
    return await conn.fetchval(
        "INSERT INTO watches (thread_id, kind, title, condition, status) "
        "VALUES ($1, $2, 'rev', 'c', 'paused') RETURNING id", thread, kind)


async def _obs(conn, wid, ts_sql, metric, value):
    # ts_sql is a fixed SQL literal from the tests below, never user data.
    await conn.execute(
        f"INSERT INTO metric_history (watch_id, observed_at, metric, value) "
        f"VALUES ($1, {ts_sql}, $2, $3)", wid, metric, value)


async def test_daily_rows_buckets_in_operator_timezone(dash_db, pool, monkeypatch):
    from app import kpihistory
    wid = await _watch(dash_db.conn)
    await _obs(dash_db.conn, wid,
               "((now() AT TIME ZONE 'UTC')::date - 1 + time '20:30') AT TIME ZONE 'UTC'",
               "n", 5.0)
    base = await dash_db.conn.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date - 1")
    monkeypatch.setattr(kpihistory, "_tz", lambda: "UTC")
    assert [r["day"] for r in await kpihistory.daily_rows(wid, 30)] == [base.isoformat()]
    monkeypatch.setattr(kpihistory, "_tz", lambda: "Asia/Kolkata")
    nxt = base + datetime.timedelta(days=1)
    assert [r["day"] for r in await kpihistory.daily_rows(wid, 30)] == [nxt.isoformat()]


async def test_daily_rows_latest_metric_only_and_closing_value(dash_db, pool):
    from app import kpihistory
    c = dash_db.conn
    wid = await _watch(c)
    await _obs(c, wid, "now() - interval '3 days'", "a", 100.0)
    await _obs(c, wid, "now() - interval '2 days 2 hours'", "b", 1.0)
    await _obs(c, wid, "now() - interval '2 days 1 hour'", "b", 9.0)
    await _obs(c, wid, "now() - interval '1 hour'", "b", 4.0)
    rows = await kpihistory.daily_rows(wid, 30)
    assert rows and all(r["metric"] == "b" for r in rows)
    two = [r for r in rows if r["n"] == 2]
    assert len(two) <= 1
    if two:
        assert (two[0]["value"], two[0]["min"], two[0]["max"]) == (9.0, 1.0, 9.0)
    assert rows[-1]["value"] == 4.0
    assert [r["day"] for r in rows] == sorted(r["day"] for r in rows)
    assert sum(r["n"] for r in rows) == 3


async def test_daily_rows_unknown_watch_raises_lookup(dash_db, pool):
    from app import kpihistory
    with pytest.raises(LookupError, match="no longer exists"):
        await kpihistory.daily_rows(987654, 30)


async def test_watch_in_scope_real_rows(dash_db, pool):
    from app import kpihistory
    k = await _watch(dash_db.conn, "kpi", "owner")
    u = await _watch(dash_db.conn, "user", "mine")
    assert (await kpihistory.watch_in_scope(k, "anyone"))["kind"] == "kpi"
    assert (await kpihistory.watch_in_scope(k, None))["id"] == k
    assert (await kpihistory.watch_in_scope(u, "mine"))["id"] == u
    assert await kpihistory.watch_in_scope(u, "other") is None
    assert await kpihistory.watch_in_scope(u, None) is None
    assert await kpihistory.watch_in_scope(999999, "mine") is None


async def _kpi_dashboard(conn, wid, slug="kpi-board", days=30):
    from app import dashboards
    saved = await dashboards.save_version(
        conn, slug=slug, title="KPI", code=CODE,
        queries=[{"id": "trend", "source": "kpi_history", "watch_id": wid, "days": days}])
    await dashboards.publish(conn, dashboard_id=saved["dashboard_id"],
                             version_id=saved["version_id"])
    return saved


async def test_refresh_due_fills_kpi_snapshot(dash_db, pool):
    from app import dashboard_refresh, dashboards_serve
    c = dash_db.conn
    wid = await _watch(c)
    for k in range(30):
        await _obs(c, wid, f"now() - make_interval(days => {k}) - interval '1 hour'",
                   "n", float(k))
    saved = await _kpi_dashboard(c, wid)
    await dashboard_refresh.refresh_due()
    snap = await c.fetchrow(
        "SELECT * FROM dashboard_snapshots WHERE version_id=$1 AND query_id='trend'",
        saved["version_id"])
    assert snap["row_count"] == 30 and snap["refreshed_at"] is not None
    assert snap["error"] is None
    assert json.loads(snap["columns"]) == ["day", "value", "min", "max", "n", "metric"]
    text = await c.fetchval(dashboards_serve.DATA_SQL, saved["version_id"], "trend")
    days = [r["day"] for r in json.loads(text)["rows"]]
    assert len(days) == 30 and days == sorted(days)


async def test_seed_backfills_kpi_entry(dash_db, pool):
    from app import dashboard_refresh
    c = dash_db.conn
    wid = await _watch(c)
    await _obs(c, wid, "now() - interval '1 hour'", "n", 7.0)
    did = await c.fetchval(
        "INSERT INTO dashboards (slug, title, view_token) VALUES ('raw', 'raw', 'x') RETURNING id")
    q = [{"id": "trend", "source": "kpi_history", "watch_id": wid, "days": 7,
          "refresh_minutes": 15}]
    vid = await c.fetchval(
        "INSERT INTO dashboard_versions (dashboard_id, version, title, code, queries, status, "
        "published_at) VALUES ($1, 1, 'raw', $2, $3::jsonb, 'published', now()) RETURNING id",
        did, CODE, json.dumps(q))
    await c.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2", vid, did)
    assert await c.fetchval("SELECT count(*) FROM dashboard_snapshots WHERE version_id=$1",
                            vid) == 0
    await dashboard_refresh.refresh_due()
    snap = await c.fetchrow(
        "SELECT row_count, error FROM dashboard_snapshots WHERE version_id=$1", vid)
    assert snap["row_count"] == 1 and snap["error"] is None


async def test_deleted_watch_records_failure(dash_db, pool):
    from app import dashboard_refresh
    c = dash_db.conn
    wid = await _watch(c)
    await _obs(c, wid, "now() - interval '1 hour'", "n", 1.0)
    saved = await _kpi_dashboard(c, wid, "gone")
    await dashboard_refresh.refresh_due()
    await c.execute("DELETE FROM watches WHERE id=$1", wid)
    await c.execute("UPDATE dashboard_snapshots SET next_refresh_at = now() "
                    "WHERE version_id=$1", saved["version_id"])
    await dashboard_refresh.refresh_due()
    snap = await c.fetchrow(
        "SELECT error FROM dashboard_snapshots WHERE version_id=$1", saved["version_id"])
    assert snap["error"] == "query failed"
    err = await c.fetchval("SELECT last_error FROM dashboard_versions WHERE id=$1",
                           saved["version_id"])
    assert "no longer exists" in err and "trend" in err


async def test_claim_returns_query_object(dash_db, pool):
    from app import dashboard_refresh
    c = dash_db.conn
    wid = await _watch(c)
    saved = await _kpi_dashboard(c, wid, "claim")
    rows = await c.fetch(dashboard_refresh.CLAIM_SQL, 10)
    mine = [r for r in rows if r["version_id"] == saved["version_id"]]
    assert len(mine) == 1
    assert mine[0]["sql"] is None
    q = mine[0]["query"]
    q = json.loads(q) if isinstance(q, str) else q
    assert q["source"] == "kpi_history" and q["watch_id"] == wid
