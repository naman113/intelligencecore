"""Phase 6 real-PG coverage of the publish_dashboard executor: single-winner publish,
stale/mismatch failure, reject. Skipped without DASH_TEST_DATABASE_URL (added to the
gate by plan 06-06). Enable against the p6 stack with:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9063/agent_memory
"""
import asyncio
import json
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
CODE = clean_page()   # Quick 029 F4: publish() lints, so the fixture page must be lint-clean
QUERIES = LINT_QUERIES


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def pub_db(monkeypatch):
    import asyncpg
    from app import actionsvc

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "dash_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        setup = await asyncpg.connect(url)
        await setup.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        await setup.close()
        pool = await asyncpg.create_pool(url, min_size=2, max_size=4)

        async def get_pool():
            return pool

        monkeypatch.setattr(actionsvc, "get_pool", get_pool)
        yield types.SimpleNamespace(pool=pool, url=url)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _draft(pool, slug="sales"):
    from app import dashboards
    async with pool.acquire() as conn:
        return await dashboards.save_version(
            conn, slug=slug, title="Sales", code=CODE, queries=QUERIES, author="agent")


async def _propose(pool, saved, **over):
    """Insert a proposed publish_dashboard action built to the payload contract."""
    from app import dashboards
    async with pool.acquire() as conn:
        tok = await dashboards.mint_preview_token(
            conn, slug=saved["slug"], version=saved["version"], ttl_minutes=60)
        payload = {
            "_summary": f"Publish dashboard 'Sales' v{saved['version']}",
            "slug": saved["slug"], "dashboard_id": saved["dashboard_id"],
            "version_id": saved["version_id"], "version": saved["version"],
            "title": "Sales", "change_note": None, "path": tok["path"],
            "draft_url": tok["path"] + "?p=" + tok["preview_token"],
            "preview_expires_at": str(tok["expires_at"]), "from_version": None,
            "is_new_dashboard": saved["version"] == 1, "queries_added": [],
            "queries_removed": [], "queries_changed": [], "origin": "agent",
            "thread_id": None}
        payload.update(over)
        aid = await conn.fetchval(
            "INSERT INTO actions (kind, payload, tier, status) "
            "VALUES ('publish_dashboard', $1::jsonb, 'draft', 'proposed') RETURNING id",
            json.dumps(payload))
    return aid, tok["preview_token"]


async def _status(pool, aid):
    return await pool.fetchval("SELECT status FROM actions WHERE id = $1", aid)


async def _published_count(pool, did):
    return await pool.fetchval(
        "SELECT count(*) FROM dashboard_versions WHERE dashboard_id = $1 "
        "AND status = 'published'", did)


async def test_concurrent_approvals_publish_once(pub_db):
    from app import actionsvc
    saved = await _draft(pub_db.pool)
    aid, _ = await _propose(pub_db.pool, saved)
    res = await asyncio.gather(actionsvc.approve(aid), actionsvc.approve(aid))
    errs = [r for r in res if r.get("error")]
    oks = [r for r in res if not r.get("error")]
    assert len(oks) == 1 and oks[0]["status"] == "executed"
    assert errs == [{"error": "action is not in a proposed state"}]
    cur = await pub_db.pool.fetchval(
        "SELECT current_version_id FROM dashboards WHERE id = $1", saved["dashboard_id"])
    assert cur == saved["version_id"]
    assert await _published_count(pub_db.pool, saved["dashboard_id"]) == 1
    assert await _status(pub_db.pool, aid) == "executed"


async def test_two_proposals_same_draft_idempotent(pub_db):
    from app import actionsvc
    saved = await _draft(pub_db.pool)
    a1, _ = await _propose(pub_db.pool, saved)
    a2, _ = await _propose(pub_db.pool, saved)
    await asyncio.gather(actionsvc.approve(a1), actionsvc.approve(a2))
    assert await _status(pub_db.pool, a1) == "executed"
    assert await _status(pub_db.pool, a2) == "executed"
    assert await _published_count(pub_db.pool, saved["dashboard_id"]) == 1
    n = await pub_db.pool.fetchval(
        "SELECT count(*) FROM dashboard_versions WHERE dashboard_id = $1 "
        "AND published_at IS NOT NULL", saved["dashboard_id"])
    assert n == 1


async def test_stale_proposal_for_retired_version_fails(pub_db):
    from app import actionsvc
    v1 = await _draft(pub_db.pool)
    a1, _ = await _propose(pub_db.pool, v1)
    a1_stale, _ = await _propose(pub_db.pool, v1)
    assert (await actionsvc.approve(a1))["status"] == "executed"
    v2 = await _draft(pub_db.pool)
    a2, _ = await _propose(pub_db.pool, v2)
    assert (await actionsvc.approve(a2))["status"] == "executed"
    # Quick 052: publishing v2 already closed the older still-proposed v1 proposal.
    assert await _status(pub_db.pool, a1_stale) == "rejected"
    res = await actionsvc.approve(a1_stale)
    assert res == {"error": "action is not in a proposed state"}
    cur = await pub_db.pool.fetchval(
        "SELECT current_version_id FROM dashboards WHERE id = $1", v1["dashboard_id"])
    assert cur == v2["version_id"]


@pytest.mark.parametrize("over", [{"slug": "other"}, {"version": 99}])
async def test_payload_mismatch_fails(pub_db, over):
    from app import actionsvc
    saved = await _draft(pub_db.pool)
    aid, _ = await _propose(pub_db.pool, saved, **over)
    res = await actionsvc.approve(aid)
    assert "executor failed" in json.dumps(res)
    assert await _status(pub_db.pool, aid) == "failed"
    assert await _published_count(pub_db.pool, saved["dashboard_id"]) == 0


async def test_reject_leaves_draft_in_place(pub_db):
    from app import actionsvc, dashboards
    saved = await _draft(pub_db.pool)
    aid, token = await _propose(pub_db.pool, saved)
    await actionsvc.reject(aid)
    assert await _status(pub_db.pool, aid) == "rejected"
    st = await pub_db.pool.fetchval(
        "SELECT status FROM dashboard_versions WHERE id = $1", saved["version_id"])
    assert st == "draft"
    async with pub_db.pool.acquire() as conn:
        assert await dashboards.preview_token_ok(
            conn, version_id=saved["version_id"], token=token) is True
    cur = await pub_db.pool.fetchval(
        "SELECT current_version_id FROM dashboards WHERE id = $1", saved["dashboard_id"])
    assert cur is None


async def test_operator_rows_untouched(pub_db):
    from app import actionsvc, dashboards
    saved = await _draft(pub_db.pool)
    await pub_db.pool.execute("INSERT INTO people (name, is_primary) VALUES ('Op', true)")
    async with pub_db.pool.acquire() as conn:
        out = await dashboards.operator_set_version(
            conn, slug=saved["slug"], version=saved["version"], op="publish")
    res = await actionsvc.approve(out["action_id"])
    assert res == {"error": "action is not in a proposed state"}


async def test_publish_closes_older_proposals_only_for_same_dashboard(pub_db):
    from app import actionsvc
    pool = pub_db.pool
    v1 = await _draft(pool)
    v2 = await _draft(pool)
    v3 = await _draft(pool)
    v4 = await _draft(pool)
    o1 = await _draft(pool, slug="other")
    a1, _ = await _propose(pool, v1)
    a2, _ = await _propose(pool, v2)
    a3, _ = await _propose(pool, v3)
    a4, _ = await _propose(pool, v4)
    ao, _ = await _propose(pool, o1)
    assert (await actionsvc.approve(a3))["status"] == "executed"
    assert await _status(pool, a1) == "rejected" and await _status(pool, a2) == "rejected"
    assert await _status(pool, a4) == "proposed"       # newer: untouched
    assert await _status(pool, ao) == "proposed"       # other dashboard: untouched
    row = await pool.fetchrow("SELECT decided_by, payload FROM actions WHERE id = $1", a1)
    assert row["decided_by"] is None
    assert json.loads(row["payload"])["superseded_by_version"] == 3


async def test_approving_older_proposal_while_newer_live_refuses(pub_db):
    from app import actionsvc
    pool = pub_db.pool
    v1 = await _draft(pool)
    v2 = await _draft(pool)
    a1, _ = await _propose(pool, v1)
    a2, _ = await _propose(pool, v2)
    # reopen v1's proposal as if the close had not run (belt and braces)
    assert (await actionsvc.approve(a2))["status"] == "executed"
    await pool.execute("UPDATE actions SET status = 'proposed' WHERE id = $1", a1)
    res = await actionsvc.approve(a1)
    assert "v1 is older than the live v2" in res["error"]
    assert await _status(pool, a1) == "failed"
    cur = await pool.fetchval(
        "SELECT current_version_id FROM dashboards WHERE id = $1", v1["dashboard_id"])
    assert cur == v2["version_id"]
