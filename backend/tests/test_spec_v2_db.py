"""Real-Postgres test for Phase 22 plan 06: a large v2 version saves on ic-render-2.0.0.

Skipped unless DASH_TEST_DATABASE_URL is set (see test_dashboards_db.py)."""
import json
import os

import pytest

from app import dashboards as d
from app import dashboardrender as dr
from tests.test_dashboards_db import dash_db  # noqa: F401 - scratch-database fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres dashboard tests)",
)


def _spec():
    qs = [{"id": f"q{i}", "sql": "SELECT 1 AS total", "refresh_minutes": 30} for i in range(25)]
    return {"spec_version": 2, "title": "Ops", "queries": qs, "sections": [{"tiles": ["s"]}],
            "tiles": [{"id": "s", "type": "stat", "title": "Total", "query": "q0",
                       "column": "total"}]}


async def test_100kb_v2_spec_with_25_queries_saves_and_stays_immutable(dash_db):  # noqa: F811
    import asyncpg
    c = dash_db.conn
    await c.execute(dash_db.sql)  # idempotent, applies the Phase 22 spec-size block
    prep = await d.prepare_spec(_spec())
    stored = {**prep["stored"], "padding": "x" * 100_000}
    assert 100_000 < len(json.dumps(stored).encode()) < 131_072
    saved = await d.save_spec_version(
        c, slug="ops", stored=stored, queries=prep["queries"], title=prep["title"],
        renderer=prep["renderer"], change_note="big")
    assert saved["version"] == 1 and prep["renderer"] == "ic-render-2.0.0"
    row = await c.fetchrow("SELECT renderer, script_hashes FROM dashboard_versions WHERE id = $1",
                           saved["version_id"])
    pin = dr.RENDERERS["ic-render-2.0.0"]
    assert row["renderer"] == "ic-render-2.0.0"
    assert list(row["script_hashes"]) == [pin["csp_hash"], pin["lib_csp_hash"]]
    n = await c.fetchval("SELECT count(*) FROM dashboard_snapshots WHERE version_id = $1",
                         saved["version_id"])
    assert n == 25
    with pytest.raises(asyncpg.RaiseError):
        await c.execute("UPDATE dashboard_versions SET spec = '{}'::jsonb WHERE id = $1",
                        saved["version_id"])
    did = saved["dashboard_id"]
    with pytest.raises(asyncpg.CheckViolationError):  # the 262,144-byte backstop
        await c.execute(
            "INSERT INTO dashboard_versions (dashboard_id, version, title, code, spec, renderer)"
            " VALUES ($1, 9, 't', '', $2::jsonb, 'ic-render-2.0.0')", did,
            json.dumps({"x": "y" * 270_000}))
