"""Phase 13.1 real-Postgres coverage of forecast definitions (plan 13.1-01).

Idempotent schema, approval inserts exactly one definition, pending-name unique
index. Skipped unless DASH_TEST_DATABASE_URL is set. Enable against the p19 stack:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9193/agent_memory
Each test uses a throwaway database; the shared tenant DB is never addressed.
"""
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
SQL = "SELECT d AS period, v AS value FROM t"


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
    from app import forecasts

    p = await asyncpg.create_pool(dash_db.url, min_size=1, max_size=4)

    async def get_pool():
        return p

    monkeypatch.setattr(forecasts, "get_pool", get_pool)
    try:
        yield p
    finally:
        await p.close()


def _payload(**kw):
    p = {"name": "Weekly sales", "name_key": "weekly sales", "sql": SQL, "grain": "week",
         "horizon": 8, "season": 52, "kind": "flow", "proposal_key": "key-1", "thread_id": None}
    p.update(kw)
    return p


async def test_schema_applies_twice(dash_db):
    n = await dash_db.conn.fetchval(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name IN ('forecast_defs','forecast_points')")
    assert n == 2
    await dash_db.conn.execute(dash_db.sql)


async def test_approval_inserts_exactly_one(pool):
    from app import forecasts

    assert await forecasts.create_from_proposal(_payload()) is True
    assert await forecasts.create_from_proposal(_payload()) is True
    rows = await pool.fetch("SELECT * FROM forecast_defs")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "active" and r["origin_ref"] == "key-1" and r["season"] == 52
    assert r["next_run_at"] is not None


async def test_name_taken_returns_false(pool):
    from app import forecasts

    assert await forecasts.create_from_proposal(_payload()) is True
    assert await forecasts.create_from_proposal(_payload(proposal_key="key-2")) is False
    assert await pool.fetchval("SELECT count(*) FROM forecast_defs") == 1


async def test_pending_name_unique_index(pool):
    import asyncpg

    q = ("INSERT INTO actions (kind, payload, tier, status) "
         "VALUES ('create_forecast', $1::jsonb, 'draft', 'proposed')")
    await pool.execute(q, json.dumps(_payload()))
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(q, json.dumps(_payload(proposal_key="other")))


# -- nightly storage (plan 13.1-03) -----------------------------------------

import asyncio  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402


def _res(h=2):
    return {"ok": True,
            "meta": {"method": "ets", "baseline": "naive", "mae": 1.0, "mape": 0.1, "wape": 0.1,
                     "baseline_wape": 0.2, "coverage": 0.8, "n_periods": 20, "origins": 3,
                     "notes": []},
            "actuals": [{"period": date(2026, 1, 5), "value": 1.0}],
            "points": [{"period": date(2026, 1, 12) + timedelta(days=7 * i), "mean": 2.0,
                        "low": 1.0, "high": 3.0} for i in range(h)]}


async def _new_def(pool, name="Nightly"):
    return await pool.fetchrow(
        """INSERT INTO forecast_defs (name, name_key, sql, grain, horizon, season, kind)
           VALUES ($1, $1, 'select 1', 'week', 2, 0, 'flow')
           RETURNING id, name, sql, grain, horizon, season, kind""", name)


async def test_retention_keeps_35_runs(pool):
    from app import forecasts

    d = await _new_def(pool)
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    for i in range(40):
        await forecasts._store_ok(pool, d, _res(), base + timedelta(days=i), date(2026, 1, 1))
    assert await pool.fetchval(
        "SELECT count(DISTINCT run_at) FROM forecast_points WHERE def_id = $1", d["id"]) == 35
    assert await pool.fetchval(
        "SELECT min(run_at) FROM forecast_points WHERE def_id = $1", d["id"]) == base + timedelta(days=5)


async def test_failure_after_success_then_clear(pool):
    from app import forecasts

    d = await _new_def(pool)
    run_at = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    await forecasts._store_ok(pool, d, _res(), run_at, date(2026, 1, 1))
    n = await pool.fetchval("SELECT count(*) FROM forecast_points WHERE def_id = $1", d["id"])
    nxt = datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc)
    await forecasts._store_fail(pool, d["id"], "boom", nxt)
    r = await pool.fetchrow("SELECT * FROM forecast_defs WHERE id = $1", d["id"])
    assert r["last_error"] == "boom" and r["failing_since"] is not None and r["next_run_at"] == nxt
    first = r["failing_since"]
    await asyncio.sleep(0.01)
    await forecasts._store_fail(pool, d["id"], "boom2", nxt)
    r = await pool.fetchrow("SELECT * FROM forecast_defs WHERE id = $1", d["id"])
    assert r["failing_since"] == first and r["last_error"] == "boom2"
    assert await pool.fetchval("SELECT count(*) FROM forecast_points WHERE def_id = $1", d["id"]) == n
    await forecasts._store_ok(pool, d, _res(), run_at + timedelta(days=2), date(2026, 1, 3))
    r = await pool.fetchrow("SELECT * FROM forecast_defs WHERE id = $1", d["id"])
    assert r["last_error"] is None and r["failing_since"] is None and r["last_ok_at"] is not None
    assert json.loads(r["last_meta"])["method"] == "ets"


async def test_claim_single_winner_and_paused(pool):
    from app import forecasts

    d = await _new_def(pool)
    paused = await _new_def(pool, "Paused one")
    await pool.execute("UPDATE forecast_defs SET status = 'paused' WHERE id = $1", paused["id"])
    got = await asyncio.gather(*[pool.fetchrow(forecasts.CLAIM_SQL, forecasts.LEASE_MINUTES)
                                 for _ in range(3)])
    ids = [g["id"] for g in got if g]
    assert ids == [d["id"]]
    assert await pool.fetchrow(forecasts.CLAIM_SQL, forecasts.LEASE_MINUTES) is None
    r = await pool.fetchrow("SELECT next_run_at, last_run_at FROM forecast_defs WHERE id = $1", d["id"])
    assert r["last_run_at"] is not None and r["next_run_at"] > datetime.now(timezone.utc)


async def test_run_one_due_end_to_end_fake_sandbox(pool, monkeypatch, tmp_path):
    from app import forecasts, sandbox

    d = await _new_def(pool, "E2E")
    series = [(date(2026, 1, 5), 1.0), (date(2026, 1, 12), 2.0)]

    async def dr(sql, grain=None):
        return series

    async def fake_run(run_id, code):
        res = {"ok": True, "method": "naive", "baseline": "naive", "mae": 1.0, "mape": 0.1,
               "wape": 0.1, "baseline_wape": 0.1, "coverage": 0.8, "n_periods": 2, "h": 2,
               "origins": 1, "notes": [],
               "points": [{"period": "2099-01-05", "mean": 2.0, "low": 1.0, "high": 3.0},
                          {"period": "2099-01-12", "mean": 2.0, "low": 1.0, "high": 3.0}],
               "actuals": [{"period": "2026-01-12", "value": 2.0}]}
        (sandbox.scratch_dir(run_id) / "result.json").write_text(json.dumps(res))
        return "[OK]"

    monkeypatch.setattr(forecasts, "dry_run", dr)
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    monkeypatch.setattr(forecasts.sandbox, "run_python", fake_run)
    assert await forecasts.run_one_due() == "ok"
    assert await pool.fetchval("SELECT count(*) FROM forecast_points WHERE def_id = $1", d["id"]) == 3
    assert await forecasts.run_one_due() is None  # leased and rescheduled to the next night


# -- plan 13.1-04: the forecast dashboard source, end to end ------------------

async def test_forecast_source_refreshes_from_memory_db(dash_db, pool, monkeypatch):
    from app import dashboard_refresh, dashboards, forecasts

    async def get_pool():
        return pool

    async def no_tenant(sql):
        raise AssertionError("tenant path used")

    monkeypatch.setattr(dashboard_refresh, "db", types.SimpleNamespace(get_pool=get_pool))
    monkeypatch.setattr(dashboard_refresh, "_tenant_configured", lambda: True)
    monkeypatch.setattr(dashboard_refresh, "_execute", no_tenant)

    d = await _new_def(pool, "Dash")
    await forecasts._store_ok(pool, d, _res(h=4), datetime.now(timezone.utc), date(2026, 1, 12))
    c = dash_db.conn
    saved = await dashboards.save_version(
        c, slug="fc-board", title="FC",
        code="<!doctype html><html><body><p>x</p></body></html>",
        queries=[{"id": "fc", "source": "forecast", "forecast_id": d["id"],
                  "refresh_minutes": 60}])
    await dashboards.publish(c, dashboard_id=saved["dashboard_id"],
                             version_id=saved["version_id"])
    await dashboard_refresh.refresh_due()
    snap = await c.fetchrow(
        "SELECT * FROM dashboard_snapshots WHERE version_id=$1 AND query_id='fc'",
        saved["version_id"])
    assert snap["error"] is None and snap["row_count"] == 1 + 4
    rows = json.loads(snap["data"])
    rows = rows["rows"] if isinstance(rows, dict) else rows
    assert rows, "snapshot has rows"
    first_fc = next(r for r in forecasts_rows(rows) if r[1] is not None)
    assert first_fc[2] <= first_fc[1] <= first_fc[3]

    await pool.execute("UPDATE forecast_defs SET status='retired' WHERE id=$1", d["id"])
    await c.execute("UPDATE dashboard_snapshots SET next_refresh_at = now() "
                    "WHERE version_id=$1", saved["version_id"])
    await dashboard_refresh.refresh_due()
    err = await c.fetchval("SELECT error FROM dashboard_snapshots WHERE version_id=$1",
                           saved["version_id"])
    assert err == "query failed"


def forecasts_rows(rows):
    """(period, forecast, low, high) tuples from snapshot rows in either encoding."""
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append((r["period"], r["forecast"], r["low"], r["high"]))
        else:
            out.append((r[0], r[2], r[3], r[4]))
    return out


# -- plan 13.1-05: readers and owner actions --------------------------------

async def test_detail_change_and_budget_real_db(pool):
    from app import forecasts

    d = await _new_def(pool, "Detail")
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    await forecasts._store_ok(pool, d, _res(h=2), base, date(2026, 1, 1))
    assert (await forecasts.detail(d["id"]))["change"] is None
    await forecasts._store_ok(pool, d, _res(h=3), base + timedelta(days=1), date(2026, 1, 2))
    out = await forecasts.detail(d["id"])
    json.dumps(out)
    assert out["change"]["total_forecast_now"] == 4.0 == out["change"]["total_forecast_before"]
    assert out["change"]["method_changed"] is False and len(out["forecast"]) == 3
    assert await forecasts.detail(d["id"] + 1000) is None
    big = _res(h=90)
    big["actuals"] = [{"period": date(2025, 1, 1) + timedelta(days=i), "value": 123456.789}
                      for i in range(310)]
    await forecasts._store_ok(pool, d, big, base + timedelta(days=2), date(2026, 1, 3))
    out = await forecasts.detail(d["id"])
    assert len(json.dumps(out)) <= forecasts.OUTPUT_BUDGET and len(out["forecast"]) == 90
    rows = await forecasts.list_brief()
    assert rows[0]["id"] == d["id"] and rows[0]["method"] == "ets"


async def test_set_status_and_run_limit_real_db(pool):
    from app import forecasts

    d = await _new_def(pool, "Owner")
    out = await forecasts.set_status(d["id"], "paused")
    row = await pool.fetchrow("SELECT kind, tier, status, payload FROM actions WHERE id = $1",
                              out["action_id"])
    assert (row["kind"], row["tier"], row["status"]) == ("forecast_status", "operator", "executed")
    assert json.loads(row["payload"])["to"] == "paused"
    with pytest.raises(ValueError):
        await forecasts.request_run(d["id"])
    await forecasts.set_status(d["id"], "active")
    await pool.execute("UPDATE forecast_defs SET next_run_at = now() + interval '1 day', "
                       "last_run_at = NULL WHERE id = $1", d["id"])
    assert (await forecasts.request_run(d["id"]))["queued"] is True
    assert await pool.fetchval("SELECT next_run_at <= now() FROM forecast_defs WHERE id = $1",
                               d["id"])
    await pool.execute("UPDATE forecast_defs SET last_run_at = now() WHERE id = $1", d["id"])
    with pytest.raises(forecasts.RunTooSoon):
        await forecasts.request_run(d["id"])
    await forecasts.set_status(d["id"], "retired")
    with pytest.raises(ValueError):
        await forecasts.set_status(d["id"], "active")
    with pytest.raises(LookupError):
        await forecasts.set_status(d["id"] + 1000, "paused")
