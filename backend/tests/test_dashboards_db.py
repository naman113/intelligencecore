"""Real-Postgres invariant tests for Phase 3 (DASH-01, DASH-03).

Skipped unless DASH_TEST_DATABASE_URL is set. Each run creates and drops a
throwaway database. Enable against the p3 stack with:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9033/agent_memory
Gate (never skips): from app/backend run bash scripts/dashboards_db_gate.sh, adding --if-changed <ref> after review fixes.
"""
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from _lintclean import QUERIES as LINT_QUERIES, clean_page

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres dashboard tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture(autouse=True)
def _legacy_html_on(monkeypatch):
    """Phase 18: these tests exercise publish mechanics with HTML drafts, which the
    Phase 18 gate refuses while dashboard_legacy_html is off (the default)."""
    from app import dashboards as _d
    monkeypatch.setattr(_d, "legacy_html_on", lambda: True)


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


async def make_dashboard(conn, slug, n_versions):
    did = await conn.fetchval(
        "INSERT INTO dashboards (slug, title, view_token) VALUES ($1, $1, 'x') RETURNING id", slug
    )
    vids = []
    for n in range(1, n_versions + 1):
        vids.append(await conn.fetchval(
            "INSERT INTO dashboard_versions (dashboard_id, version, title, code) "
            "VALUES ($1, $2, 't', $3) RETURNING id", did, n, clean_page()))
    return did, vids


async def _fingerprint(conn):
    tables = ["dashboards", "dashboard_versions", "dashboard_snapshots"]
    cons = await conn.fetch(
        "SELECT conname FROM pg_constraint WHERE conrelid = ANY($1::text[]::regclass[]) ORDER BY 1", tables)
    idx = await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE tablename = ANY($1::text[]) ORDER BY 1", tables)
    trg = await conn.fetch(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'dashboard_versions'::regclass AND NOT tgisinternal ORDER BY 1")
    return ([r[0] for r in cons], [r[0] for r in idx], [r[0] for r in trg])


async def test_schema_runs_twice_without_change(dash_db):
    before = await _fingerprint(dash_db.conn)
    await dash_db.conn.execute(dash_db.sql)
    after = await _fingerprint(dash_db.conn)
    assert before == after
    assert after[2] == ["trg_dashboard_versions_immutable", "trg_dashboard_versions_spec_immutable"]  # Phase 18 adds the second


async def test_second_published_version_rejected(dash_db):
    import asyncpg
    c = dash_db.conn
    _, (v1, v2) = await make_dashboard(c, "a", 2)
    await c.execute("UPDATE dashboard_versions SET status='published' WHERE id=$1", v1)
    with pytest.raises(asyncpg.UniqueViolationError):
        await c.execute("UPDATE dashboard_versions SET status='published' WHERE id=$1", v2)


async def test_pointer_must_belong_to_same_dashboard(dash_db):
    import asyncpg
    c = dash_db.conn
    a, _ = await make_dashboard(c, "a", 1)
    _, (vb,) = await make_dashboard(c, "b", 1)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await c.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2", vb, a)


async def test_current_version_cannot_be_deleted(dash_db):
    import asyncpg
    c = dash_db.conn
    a, (v1,) = await make_dashboard(c, "a", 1)
    await c.execute("UPDATE dashboards SET current_version_id=$1 WHERE id=$2", v1, a)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await c.execute("DELETE FROM dashboard_versions WHERE id=$1", v1)


async def test_version_content_is_immutable(dash_db):
    import asyncpg
    c = dash_db.conn
    _, (v1,) = await make_dashboard(c, "a", 1)
    for setter, arg in (
        ("code = $2", "<b>x</b>"),
        ("queries = $2::jsonb", '[{"id": "q"}]'),
        ("script_hashes = $2::text[]", ["sha256-abc"]),
        ("title = $2", "other"),
        ("change_note = $2", "n"),
    ):
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await c.execute(f"UPDATE dashboard_versions SET {setter} WHERE id=$1", v1, arg)
    await c.execute(
        "UPDATE dashboard_versions SET status='published', last_error='e', "
        "published_at=now() WHERE id=$1", v1)
    await c.execute(
        "UPDATE dashboard_versions SET status='retired', retired_at=now(), last_error=NULL WHERE id=$1", v1)
    assert await c.fetchval("SELECT status FROM dashboard_versions WHERE id=$1", v1) == "retired"


async def test_code_byte_cap_counts_bytes(dash_db):
    import asyncpg
    c = dash_db.conn
    did, _ = await make_dashboard(c, "a", 0)
    ins = "INSERT INTO dashboard_versions (dashboard_id, version, title, code) VALUES ($1, $2, 't', $3)"
    with pytest.raises(asyncpg.CheckViolationError):
        await c.execute(ins, did, 1, "é" * 131073)
    await c.execute(ins, did, 2, "a" * 262144)


async def test_snapshot_refresh_bounds(dash_db):
    import asyncpg
    c = dash_db.conn
    _, (v1,) = await make_dashboard(c, "a", 1)
    ins = "INSERT INTO dashboard_snapshots (version_id, query_id, refresh_minutes) VALUES ($1, $2, $3)"
    for i, bad in enumerate((4, 1441)):
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute(ins, v1, f"bad{i}", bad)
    await c.execute(ins, v1, "lo", 5)
    await c.execute(ins, v1, "hi", 1440)


async def test_status_vocabulary(dash_db):
    import asyncpg
    c = dash_db.conn
    _, (v1,) = await make_dashboard(c, "a", 1)
    with pytest.raises(asyncpg.CheckViolationError):
        await c.execute("UPDATE dashboard_versions SET status='live' WHERE id=$1", v1)


# ---------------------------------------------------------------------------
# Plan 03-05: service-level tests against real Postgres (save/publish/rollback,
# concurrency, observability, hash round trip).
# ---------------------------------------------------------------------------
import asyncio
import hashlib
import base64

LIB_HASH = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
LIB_LINE = ('<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" '
            f'integrity="{LIB_HASH}"></script>')


async def _save(conn, slug="svc", n=1, code=None, queries=None):
    from app import dashboards
    out = []
    for i in range(n):
        out.append(await dashboards.save_version(
            conn, slug=slug, title="T", code=(clean_page(f"<p>{i}</p>") if code is None else code + f"<!-- {i} -->"),
            queries=queries if queries is not None else LINT_QUERIES, change_note=f"n{i}"))
    return out


async def published_state(conn, dashboard_id):
    """One statement: pointer, published count and published id share one snapshot."""
    r = await conn.fetchrow(
        "SELECT d.current_version_id AS pointer, "
        "(SELECT count(*) FROM dashboard_versions v WHERE v.dashboard_id = d.id AND v.status = 'published') AS n, "
        "(SELECT v.id FROM dashboard_versions v WHERE v.dashboard_id = d.id AND v.status = 'published' LIMIT 1) AS pub "
        "FROM dashboards d WHERE d.id = $1", dashboard_id)
    return r["pointer"], r["n"], r["pub"]


async def test_save_twice_leaves_pointer_null(dash_db):
    c = dash_db.conn
    q = [{"id": "a", "sql": "SELECT 1", "refresh_minutes": 15},
         {"id": "b", "sql": "SELECT 2", "refresh_minutes": 30}]
    v1, v2 = await _save(c, "twice", 2, queries=q)
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v1["status"] == v2["status"] == "draft"
    assert await c.fetchval("SELECT current_version_id FROM dashboards WHERE slug='twice'") is None
    for v in (v1, v2):
        rows = await c.fetch(
            "SELECT query_id, next_refresh_at FROM dashboard_snapshots WHERE version_id=$1", v["version_id"])
        assert sorted(r["query_id"] for r in rows) == ["a", "b"]
        assert all(r["next_refresh_at"] is not None for r in rows)


async def test_publish_then_publish_then_rollback(dash_db):
    from app import dashboards
    c = dash_db.conn
    pid = await c.fetchval("INSERT INTO people (name) VALUES ('Test Operator') RETURNING id")
    v1, v2 = await _save(c, "pub", 2)
    did = v1["dashboard_id"]
    await dashboards.operator_set_version(c, slug="pub", version=1, op="publish", decided_by=pid)
    assert await published_state(c, did) == (v1["version_id"], 1, v1["version_id"])
    await dashboards.operator_set_version(c, slug="pub", version=2, op="publish", decided_by=pid)
    assert await published_state(c, did) == (v2["version_id"], 1, v2["version_id"])
    assert await c.fetchval("SELECT status FROM dashboard_versions WHERE id=$1", v1["version_id"]) == "retired"
    before = await c.fetchval("SELECT count(*) FROM actions WHERE kind='publish_dashboard'")
    await dashboards.operator_set_version(c, slug="pub", version=1, op="rollback", decided_by=pid)
    assert await published_state(c, did) == (v1["version_id"], 1, v1["version_id"])
    assert await c.fetchval("SELECT status FROM dashboard_versions WHERE id=$1", v2["version_id"]) == "retired"
    rows = await c.fetch(
        "SELECT status, decided_by, payload->>'op' AS op FROM actions "
        "WHERE kind='publish_dashboard' AND payload->>'op'='rollback'")
    assert len(rows) == 1 and rows[0]["status"] == "executed" and rows[0]["decided_by"] == pid
    assert await c.fetchval("SELECT count(*) FROM actions WHERE kind='publish_dashboard'") == before + 1


async def test_rollback_without_person_is_refused(dash_db):
    from app import dashboards
    c = dash_db.conn
    v1, v2 = await _save(c, "nop", 2)
    pid = await c.fetchval("INSERT INTO people (name) VALUES ('Tmp') RETURNING id")
    await dashboards.operator_set_version(c, slug="nop", version=1, op="publish", decided_by=pid)
    await dashboards.operator_set_version(c, slug="nop", version=2, op="publish", decided_by=pid)
    await c.execute("DELETE FROM actions")
    await c.execute("DELETE FROM people")
    with pytest.raises(dashboards.DashboardError) as ei:
        await dashboards.operator_set_version(c, slug="nop", version=1, op="rollback")
    assert ei.value.status == 422
    assert await c.fetchval("SELECT count(*) FROM actions") == 0
    assert await published_state(c, v1["dashboard_id"]) == (v2["version_id"], 1, v2["version_id"])


# Raw flip primitive: these tests deliberately flip back to a retired version, so they pass
# expect_status=None. The WR-02 default precondition is covered by
# test_raw_publish_default_precondition and the operator rollback tests.
async def test_concurrent_publishes_serialize(dash_db):
    import asyncpg
    from app import dashboards
    v1, v2 = await _save(dash_db.conn, "conc", 2)
    did = v1["dashboard_id"]
    c1 = await asyncpg.connect(dash_db.url)
    c2 = await asyncpg.connect(dash_db.url)
    try:
        await dashboards.publish(c1, dashboard_id=did, version_id=v1["version_id"], expect_status=None)
        for i in range(10):
            a, b = (v1, v2) if i % 2 else (v2, v1)
            await asyncio.gather(
                dashboards.publish(c1, dashboard_id=did, version_id=a["version_id"], expect_status=None),
                dashboards.publish(c2, dashboard_id=did, version_id=b["version_id"], expect_status=None))
            pointer, n, pub = await published_state(dash_db.conn, did)
            assert n == 1 and pointer == pub
    finally:
        await c1.close()
        await c2.close()


# Raw flip primitive: these tests deliberately flip back to a retired version, so they pass
# expect_status=None. The WR-02 default precondition is covered by
# test_raw_publish_default_precondition and the operator rollback tests.
async def test_no_observable_zero_or_two_published(dash_db):
    import asyncpg
    from app import dashboards
    v1, v2 = await _save(dash_db.conn, "obs", 2)
    did = v1["dashboard_id"]
    await dashboards.publish(dash_db.conn, dashboard_id=did, version_id=v1["version_id"], expect_status=None)
    writer = await asyncpg.connect(dash_db.url)
    reader = await asyncpg.connect(dash_db.url)
    stop = asyncio.Event()
    polls = 0
    bad = []

    async def poll():
        nonlocal polls
        while not stop.is_set():
            pointer, n, pub = await published_state(reader, did)
            polls += 1
            if n != 1 or pointer != pub:
                bad.append((pointer, n, pub))
            await asyncio.sleep(0)

    task = asyncio.create_task(poll())
    try:
        for i in range(30):
            tgt = v2 if i % 2 == 0 else v1
            await dashboards.publish(writer, dashboard_id=did, version_id=tgt["version_id"], expect_status=None)
    finally:
        stop.set()
        await task
        await writer.close()
        await reader.close()
    assert polls >= 1
    assert bad == []


# Plan 03-06 (gap closure): WR-02 default precondition on real Postgres
async def test_raw_publish_default_precondition(dash_db):
    from app import dashboards
    c = dash_db.conn
    v1, v2 = await _save(c, "pre", 2)
    did = v1["dashboard_id"]
    r = await dashboards.publish(c, dashboard_id=did, version_id=v1["version_id"])
    assert r["changed"] is True
    r = await dashboards.publish(c, dashboard_id=did, version_id=v2["version_id"])
    assert r["changed"] is True
    assert await c.fetchval("SELECT status FROM dashboard_versions WHERE id=$1", v1["version_id"]) == "retired"
    with pytest.raises(dashboards.DashboardError) as ei:
        await dashboards.publish(c, dashboard_id=did, version_id=v1["version_id"])
    assert ei.value.status == 409
    assert "retired" in str(ei.value) and "expected draft" in str(ei.value)
    assert await published_state(c, did) == (v2["version_id"], 1, v2["version_id"])
    r = await dashboards.publish(c, dashboard_id=did, version_id=v2["version_id"])
    assert r["changed"] is False
    r = await dashboards.publish(c, dashboard_id=did, version_id=v1["version_id"], expect_status=("retired",))
    assert r["changed"] is True
    assert await published_state(c, did) == (v1["version_id"], 1, v1["version_id"])
    assert await c.fetchval("SELECT count(*) FROM actions WHERE kind = 'publish_dashboard'") == 0


async def test_saved_version_content_is_immutable_via_service(dash_db):
    import asyncpg
    c = dash_db.conn
    (v,) = await _save(c, "imm", 1)
    with pytest.raises(asyncpg.RaiseError):
        await c.execute("UPDATE dashboard_versions SET code = 'x' WHERE id=$1", v["version_id"])


async def test_saved_script_hashes_include_library_hash(dash_db):
    c = dash_db.conn
    inline = "console.log('hi');"
    code = f"<html><head>{LIB_LINE}</head><body><script>{inline}</script></body></html>"
    v = await _save(c, "hash", 1, code=code)
    # _save appends an HTML comment, which does not change script hashes
    got = await c.fetchval("SELECT script_hashes FROM dashboard_versions WHERE id=$1", v[0]["version_id"])
    want = "sha256-" + base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    assert list(got) == [LIB_HASH, want]
