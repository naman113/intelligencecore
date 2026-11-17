"""Real-Postgres tests for Phase 4 (SERVE-01, SERVE-02, SERVE-05).

Covers claim eligibility, SKIP LOCKED, the lease, snapshot write paths through
the immutability trigger, the seed backfill, DATA_SQL and preview tokens.
Skipped unless DASH_TEST_DATABASE_URL is set. Enable against the p4 stack with:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9043/agent_memory
Gate (never skips): from app/backend run bash scripts/dashboards_db_gate.sh
"""
import datetime
import decimal
import json
import os
import re
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
async def refresh_pool(dash_db, monkeypatch):
    import asyncpg
    from app import dashboard_refresh

    pool = await asyncpg.create_pool(dash_db.url, min_size=1, max_size=4)

    async def get_pool():
        return pool

    monkeypatch.setattr(dashboard_refresh, "db", types.SimpleNamespace(get_pool=get_pool))
    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    try:
        yield pool
    finally:
        await pool.close()


async def _dashboard(conn, slug):
    return await conn.fetchval(
        "INSERT INTO dashboards (slug, title, view_token) VALUES ($1, 'raw', 'x') RETURNING id", slug)


async def _version(conn, dashboard_id, n, status, queries, created_at_sql="now()"):
    # created_at_sql is always a fixed literal from the tests below, never user data.
    vid = await conn.fetchval(
        "INSERT INTO dashboard_versions (dashboard_id, version, title, code, queries, status, "
        "published_at, retired_at, created_at) VALUES ($1, $2, 'raw', '<!doctype html><p>x</p>', "
        "$3::jsonb, $4::text, CASE WHEN $4::text = 'published' THEN now() END, "
        f"CASE WHEN $4::text = 'retired' THEN now() END, {created_at_sql}) RETURNING id",
        dashboard_id, n, json.dumps(queries), status)
    if status == "published":
        await conn.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2", vid, dashboard_id)
    return vid


async def _snapshot(conn, version_id, qid, refresh_minutes=15, next_refresh_sql="now()"):
    await conn.execute(
        "INSERT INTO dashboard_snapshots (version_id, query_id, refresh_minutes, next_refresh_at) "
        f"VALUES ($1, $2, $3, {next_refresh_sql})", version_id, qid, refresh_minutes)


def _q(qid, sql="SELECT 1 AS n", rm=15):
    return {"id": qid, "sql": sql, "refresh_minutes": rm}


async def _snap_row(conn, vid, qid):
    return await conn.fetchrow(
        "SELECT * FROM dashboard_snapshots WHERE version_id=$1 AND query_id=$2", vid, qid)


async def _due_now(conn, vid):
    await conn.execute("UPDATE dashboard_snapshots SET next_refresh_at = now() WHERE version_id=$1", vid)


async def test_schema_has_phase4_table(dash_db):
    assert await dash_db.conn.fetchval("SELECT to_regclass('dashboard_preview_tokens')") is not None


async def test_claim_eligibility(dash_db):
    from app import dashboard_refresh
    c = dash_db.conn
    d = [await _dashboard(c, f"d{i}") for i in range(4)]
    pub = await _version(c, d[0], 1, "published", [_q("q")])
    young = await _version(c, d[1], 1, "draft", [_q("q")])
    old = await _version(c, d[2], 1, "draft", [_q("q")], "now() - interval '25 hours'")
    ret = await _version(c, d[3], 1, "retired", [_q("q")])
    for v in (pub, young, old, ret):
        await _snapshot(c, v, "q")
    rows = await c.fetch(dashboard_refresh.CLAIM_SQL, 10)
    assert {(r["version_id"], r["query_id"]) for r in rows} == {(pub, "q"), (young, "q")}


async def test_not_due_rows_are_not_claimed(dash_db):
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q", next_refresh_sql="now() + interval '1 hour'")
    assert await c.fetch(dashboard_refresh.CLAIM_SQL, 10) == []


async def test_claim_sets_lease(dash_db):
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q", "SELECT 42 AS n", 10)])
    await _snapshot(c, v, "q", refresh_minutes=10)
    rows = await c.fetch(dashboard_refresh.CLAIM_SQL, 10)
    assert len(rows) == 1
    r = rows[0]
    assert r["version"] == 1 and r["dashboard_id"] == d and r["sql"] == "SELECT 42 AS n"
    s = await _snap_row(c, v, "q")
    assert s["attempted_at"] is not None
    ok = await c.fetchval(
        "SELECT next_refresh_at BETWEEN now() + interval '9 minutes' AND now() + interval '11 minutes' "
        "FROM dashboard_snapshots WHERE version_id=$1", v)
    assert ok


async def test_skip_locked_disjoint(dash_db):
    import asyncpg
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("x"), _q("y")])
    await _snapshot(c, v, "x", next_refresh_sql="now() - interval '2 minutes'")
    await _snapshot(c, v, "y", next_refresh_sql="now() - interval '1 minute'")
    a = await asyncpg.connect(dash_db.url)
    b = await asyncpg.connect(dash_db.url)
    try:
        async with a.transaction():
            rows_a = await a.fetch(dashboard_refresh.CLAIM_SQL, 1)
            rows_b = await b.fetch(dashboard_refresh.CLAIM_SQL, 10)
        set_a = {r["query_id"] for r in rows_a}
        set_b = {r["query_id"] for r in rows_b}
    finally:
        await a.close()
        await b.close()
    assert set_a == {"x"} and set_b == {"y"}
    assert not (set_a & set_b) and (set_a | set_b) == {"x", "y"}


async def test_refresh_due_success_writes_snapshot(dash_db, refresh_pool, monkeypatch):
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q")

    async def fake(sql):
        return [{"customer": "A\x00", "invoiced": decimal.Decimal("10.50"),
                 "day": datetime.date(2026, 7, 1), "note": None}]

    monkeypatch.setattr(dashboard_refresh, "_execute", fake)
    await dashboard_refresh.refresh_due()
    s = await _snap_row(c, v, "q")
    assert json.loads(s["data"]) == [{"customer": "A", "invoiced": 10.5, "day": "2026-07-01", "note": None}]
    assert json.loads(s["columns"]) == ["customer", "invoiced", "day", "note"]
    assert s["row_count"] == 1 and s["refreshed_at"] is not None and s["error"] is None


async def test_refresh_due_failure_keeps_rows_and_sets_last_error(dash_db, refresh_pool, monkeypatch):
    import asyncpg
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q")

    async def ok(sql):
        return [{"n": 7}]

    async def boom(sql):
        raise asyncpg.UndefinedTableError('relation "no_such_table" does not exist')

    monkeypatch.setattr(dashboard_refresh, "_execute", ok)
    await dashboard_refresh.refresh_due()
    await _due_now(c, v)
    monkeypatch.setattr(dashboard_refresh, "_execute", boom)
    await dashboard_refresh.refresh_due()
    s = await _snap_row(c, v, "q")
    assert json.loads(s["data"]) == [{"n": 7}]
    assert s["error"] == "query failed"
    soon = await c.fetchval(
        "SELECT next_refresh_at <= now() + make_interval(mins => $2) FROM dashboard_snapshots "
        "WHERE version_id=$1", v, dashboard_refresh.FAIL_RETRY_MAX_MINUTES)
    assert soon
    last = await c.fetchval("SELECT last_error FROM dashboard_versions WHERE id=$1", v)
    assert last.startswith("query q: query failed: relation")


async def test_guardrail_rejection_is_recorded_and_batch_continues(dash_db, refresh_pool, monkeypatch):
    from app import dashboard_refresh, guardrails
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [
        _q("a", "SELECT pg_sleep(1) AS s"), _q("b", "SELECT 1 AS n")])
    await _snapshot(c, v, "a", next_refresh_sql="now() - interval '2 minutes'")
    await _snapshot(c, v, "b", next_refresh_sql="now() - interval '1 minute'")

    async def guarded(sql):
        guardrails.validate_readonly_sql(sql)
        return [{"n": 1}]

    monkeypatch.setattr(dashboard_refresh, "_execute", guarded)
    await dashboard_refresh.refresh_due()
    a = await _snap_row(c, v, "a")
    b = await _snap_row(c, v, "b")
    assert a["error"] == "query rejected by the read-only guardrail"
    last = await c.fetchval("SELECT last_error FROM dashboard_versions WHERE id=$1", v)
    assert last.startswith("query ") and "rejected" in last
    assert b["refreshed_at"] is not None


async def test_success_after_failure_clears_errors(dash_db, refresh_pool, monkeypatch):
    import asyncpg
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q")

    async def boom(sql):
        raise asyncpg.UndefinedTableError('relation "t" does not exist')

    async def ok(sql):
        return [{"n": 1}]

    monkeypatch.setattr(dashboard_refresh, "_execute", boom)
    await dashboard_refresh.refresh_due()
    assert (await _snap_row(c, v, "q"))["error"] is not None
    assert await c.fetchval("SELECT last_error FROM dashboard_versions WHERE id=$1", v) is not None
    await _due_now(c, v)
    monkeypatch.setattr(dashboard_refresh, "_execute", ok)
    await dashboard_refresh.refresh_due()
    assert (await _snap_row(c, v, "q"))["error"] is None
    assert await c.fetchval("SELECT last_error FROM dashboard_versions WHERE id=$1", v) is None


async def test_empty_result_keeps_previous_columns(dash_db, refresh_pool, monkeypatch):
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q")
    result = [{"a": 1, "b": 2}]

    async def fake(sql):
        return result

    monkeypatch.setattr(dashboard_refresh, "_execute", fake)
    await dashboard_refresh.refresh_due()
    await _due_now(c, v)
    result = []
    await dashboard_refresh.refresh_due()
    s = await _snap_row(c, v, "q")
    assert json.loads(s["columns"]) == ["a", "b"]
    assert json.loads(s["data"]) == [] and s["row_count"] == 0


async def test_seed_backfills_hand_inserted_published_version(dash_db, refresh_pool, monkeypatch):
    from app import dashboard_refresh
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [
        {"id": "orders", "sql": "SELECT 1 AS n", "refresh_minutes": 5},
        {"id": "Bad-Id", "sql": "SELECT 1"},
        {"id": "x", "sql": "SELECT 2", "refresh_minutes": "abc"},
        7])

    async def fake(sql):
        return [{"n": 1}]

    monkeypatch.setattr(dashboard_refresh, "_execute", fake)
    await dashboard_refresh.refresh_due()
    rows = {r["query_id"]: r for r in await c.fetch(
        "SELECT * FROM dashboard_snapshots WHERE version_id=$1", v)}
    assert set(rows) == {"orders", "x"}
    assert rows["orders"]["refresh_minutes"] == 5 and rows["orders"]["refreshed_at"] is not None
    assert rows["x"]["refresh_minutes"] == 15


async def test_seed_ignores_retired_and_old_drafts(dash_db):
    from app import dashboard_refresh
    c = dash_db.conn
    d1 = await _dashboard(c, "a")
    d2 = await _dashboard(c, "b")
    await _version(c, d1, 1, "retired", [_q("q")])
    await _version(c, d2, 1, "draft", [_q("q")], "now() - interval '25 hours'")
    await c.execute(dashboard_refresh.SEED_SQL)
    assert await c.fetchval("SELECT count(*) FROM dashboard_snapshots") == 0


async def test_data_sql_shape(dash_db, refresh_pool, monkeypatch):
    from app import dashboard_refresh, dashboards_serve
    c = dash_db.conn
    d = await _dashboard(c, "a")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q")

    async def fake(sql):
        return [{"n": 1}]

    monkeypatch.setattr(dashboard_refresh, "_execute", fake)
    await dashboard_refresh.refresh_due()
    text = await c.fetchval(dashboards_serve.DATA_SQL, v, "q")
    obj = json.loads(text)
    assert set(obj) == {"query_id", "columns", "rows", "refreshed_at", "data_as_of", "timezone", "error"}
    assert obj["query_id"] == "q" and obj["rows"] == [{"n": 1}] and obj["error"] is None
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", obj["refreshed_at"])
    assert await c.fetchval(dashboards_serve.DATA_SQL, v, "nope") is None


async def test_preview_tokens_real_pg(dash_db):
    from app import dashboards
    c = dash_db.conn
    saved = await dashboards.save_version(
        c, slug="prev", title="Prev", code="<!doctype html><p>x</p>", queries=[])
    v1 = saved["version_id"]
    minted = await dashboards.mint_preview_token(c, slug="prev", version=1)
    token = minted["preview_token"]
    assert await dashboards.preview_token_ok(c, version_id=v1, token=token) is True
    assert await dashboards.preview_token_ok(c, version_id=v1 + 999, token=token) is False
    digest = await c.fetchval("SELECT token_sha256 FROM dashboard_preview_tokens WHERE version_id=$1", v1)
    assert digest == dashboards.preview_token_digest(token)
    assert re.fullmatch(r"[0-9a-f]{64}", digest) and digest != token
    await c.execute("UPDATE dashboard_preview_tokens SET expires_at = now() - interval '1 second'")
    assert await dashboards.preview_token_ok(c, version_id=v1, token=token) is False
    await dashboards.publish(c, dashboard_id=saved["dashboard_id"], version_id=v1)
    with pytest.raises(dashboards.DashboardError) as ei:
        await dashboards.mint_preview_token(c, slug="prev", version=1)
    assert ei.value.status == 409


# ---- Phase 17 (CONS-03) -------------------------------------------------------

async def _p17_setup(c, reads, slug="p17", **snap):
    sid = await c.fetchval(
        "INSERT INTO sources (name, kind) VALUES ($1, 'postgres') RETURNING id", "s_" + slug)
    schema, table = reads[0].split(".", 1)
    await c.execute(
        "INSERT INTO source_tables (source_id, remote_table, warehouse_schema, warehouse_table, "
        "mode, last_success_at) VALUES ($1, $2, $3, $4, 'import', now() - interval '2 hours')",
        sid, table, schema, table)
    d = await _dashboard(c, slug)
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q", next_refresh_sql="now() + interval '1 day'")
    now = datetime.datetime.now(datetime.timezone.utc)
    await c.execute(
        "UPDATE dashboard_snapshots SET sync_aligned = true, reads = $3::text[], "
        "inputs_synced_at = now() - interval '1 hour', attempted_at = $4, error = $5, "
        "next_refresh_at = COALESCE($6, now() + interval '1 day') "
        "WHERE version_id = $1 AND query_id = $2", v, "q", reads,
        snap.get("attempted_at", now - datetime.timedelta(hours=3)),
        snap.get("error"), snap.get("next"))
    return v, table, schema


async def _sync(c, schema, table, ago=datetime.timedelta(0)):
    await c.execute(
        "UPDATE source_tables SET last_success_at = now() - $3::interval "
        "WHERE warehouse_schema = $1 AND warehouse_table = $2", schema, table, ago)


async def _claimed(c, v):
    from app import dashboard_refresh
    rows = await c.fetch(dashboard_refresh.CLAIM_SQL, 10)
    return v in {r["version_id"] for r in rows}


async def test_p17_claim_due_after_sync(dash_db):
    c = dash_db.conn
    v, table, schema = await _p17_setup(c, ["src_x.t"])
    assert not await _claimed(c, v)
    await _sync(c, schema, table)
    assert await _claimed(c, v)


async def test_p17_failed_pair_waits_for_backoff(dash_db):
    c = dash_db.conn
    now = datetime.datetime.now(datetime.timezone.utc)
    v, table, schema = await _p17_setup(
        c, ["src_x.t"], attempted_at=now - datetime.timedelta(minutes=1),
        error="query failed", next=now + datetime.timedelta(minutes=5))
    await _sync(c, schema, table, datetime.timedelta(minutes=5))
    assert not await _claimed(c, v)
    await c.execute("UPDATE dashboard_snapshots SET next_refresh_at = now() - interval '1 second'")
    assert await _claimed(c, v)


async def test_p17_deferred_pair_not_reclaimed(dash_db):
    c = dash_db.conn
    now = datetime.datetime.now(datetime.timezone.utc)
    v, table, schema = await _p17_setup(
        c, ["src_x.t"], attempted_at=now, next=now + datetime.timedelta(minutes=1))
    await _sync(c, schema, table, datetime.timedelta(minutes=5))
    assert not await _claimed(c, v)
    await c.execute("UPDATE dashboard_snapshots SET attempted_at = now() - interval '3 hours', "
                    "next_refresh_at = now() + interval '1 day'")
    await _sync(c, schema, table)
    assert await _claimed(c, v)
    assert not await _claimed(c, v)


async def test_p17_mixed_case_reads_claimed(dash_db):
    c = dash_db.conn
    v, table, schema = await _p17_setup(c, ["public.Order"])
    assert not await _claimed(c, v)
    await _sync(c, schema, table)
    assert await _claimed(c, v)


async def test_p17_data_sql_has_data_as_of(dash_db):
    from app import dashboard_refresh, dashboards_serve
    c = dash_db.conn
    d = await _dashboard(c, "asof")
    v = await _version(c, d, 1, "published", [_q("q")])
    await _snapshot(c, v, "q")
    await c.execute(dashboard_refresh.UPSERT_OK_SQL, v, "q", "[]", "[]", 0)
    assert json.loads(await c.fetchval(dashboards_serve.DATA_SQL, v, "q"))["data_as_of"] is None
    ts = datetime.datetime(2026, 1, 2, 3, 4, tzinfo=datetime.timezone.utc)
    await c.execute(dashboard_refresh.FRESHNESS_SQL, v, "q", ts, ["public.t"], False, None)
    got = json.loads(await c.fetchval(dashboards_serve.DATA_SQL, v, "q"))["data_as_of"]
    assert re.match(r"^2026-01-02T03:04", got)


# Quick 033: a sources-only install (no business database URL, warehouse on) refreshes a draft.
@pytest.mark.parametrize("wh,refreshes", [(True, True), (False, False)])
async def test_q033_sources_only_refreshes_draft(dash_db, monkeypatch, wh, refreshes):
    import asyncpg
    from app import dashboard_refresh, freshness, guardrails, warehouse

    s = types.SimpleNamespace(tenant_database_url="", business_database_url="",
                              warehouse_url="postgresql://wh.invalid/w" if wh else "",
                              wh_reader_password="pw" if wh else "", sql_row_limit=500)
    for mod in (guardrails, warehouse, dashboard_refresh):
        monkeypatch.setattr(mod, "get_settings", lambda: s, raising=False)
    pool = await asyncpg.create_pool(dash_db.url, min_size=1, max_size=4)

    async def get_pool():
        return pool

    async def fake(sql):
        return [{"order_status": "open", "n": 7}]

    async def no_facts(*a, **k):
        raise RuntimeError("no freshness facts in this test")

    monkeypatch.setattr(dashboard_refresh, "db", types.SimpleNamespace(get_pool=get_pool))
    monkeypatch.setattr(dashboard_refresh, "_execute", fake)
    monkeypatch.setattr(freshness, "load_facts", no_facts)
    monkeypatch.setattr(freshness, "for_sql", no_facts)
    c = dash_db.conn
    try:
        d = await _dashboard(c, "orders-health")
        v = await _version(c, d, 1, "draft", [_q("q", "SELECT 1 FROM src_distributor.orders")])
        await _snapshot(c, v, "q")
        await dashboard_refresh.refresh_due()
        snap = await _snap_row(c, v, "q")
        if refreshes:
            assert snap["refreshed_at"] is not None and snap["attempted_at"] is not None
            assert snap["row_count"] == 1 and snap["error"] is None
            assert json.loads(snap["data"]) == [{"order_status": "open", "n": 7}]
        else:
            assert snap["attempted_at"] is None and snap["refreshed_at"] is None
    finally:
        await pool.close()
