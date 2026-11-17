"""Real-Postgres tests for Phase 18 (SPEC-03, SPEC-06): the spec and renderer columns, the
shape CHECK, the immutability trigger, the spec save and publish path and the HTML gate.

Skipped unless DASH_TEST_DATABASE_URL is set (loopback only, scratch database per test).
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9283/agent_memory
Gate (never skips): from app/backend run bash scripts/dashboards_db_gate.sh.
"""
import json
import os

import pytest

from app import dashboardrender as dr
from app import dashboards as d
from app.dashboards import DashboardError
from tests.test_dashboard_spec_store import SQL_ONLY_SQL, measure_entry, sql_only_spec
from tests.test_dashboards_db import dash_db  # noqa: F401 - scratch-database fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres dashboard tests)",
)

HTML = "<!doctype html><html><body>old</body></html>"


async def _spec_draft(conn, slug="open", with_measure=False):
    prep = await d.prepare_spec(sql_only_spec())
    queries = prep["queries"] + ([measure_entry()] if with_measure else [])
    return prep, await d.save_spec_version(
        conn, slug=slug, stored=prep["stored"], queries=queries, title=prep["title"],
        renderer=dr.CURRENT_RENDERER, change_note="spec", task_run_id=None)


def _payload(saved):
    return {"slug": saved["slug"], "dashboard_id": saved["dashboard_id"],
            "version_id": saved["version_id"], "version": saved["version"]}


async def test_schema_columns_constraint_and_trigger_once(dash_db):  # noqa: F811
    c = dash_db.conn
    await c.execute(dash_db.sql)  # third application: still no error
    cols = await c.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'dashboard_versions' AND column_name IN ('spec', 'renderer') "
        "ORDER BY 1")
    assert [(r[0], r[1]) for r in cols] == [("renderer", "text"), ("spec", "jsonb")]
    assert await c.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname = 'dashboard_versions_spec_shape'") == 1
    assert await c.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_dashboard_versions_spec_immutable'") == 1
    assert await c.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_dashboard_versions_immutable'") == 1


async def test_check_refuses_bad_spec_rows(dash_db):  # noqa: F811
    import asyncpg
    c = dash_db.conn
    did = await c.fetchval(
        "INSERT INTO dashboards (slug, title, view_token) VALUES ('x', 'x', 't') RETURNING id")
    ins = ("INSERT INTO dashboard_versions (dashboard_id, version, title, code, spec, renderer) "
           "VALUES ($1, $2, 't', $3, $4::jsonb, $5)")
    for n, (code, spec, renderer) in enumerate([
            ("<p>x</p>", "{}", "ic-render-1.0.0"),     # code must be ''
            ("", "{}", None),                          # renderer required
            ("", "[]", "ic-render-1.0.0"),             # spec must be an object
            ("", json.dumps({"x": "y" * 270000}), "ic-render-1.0.0")], start=1):  # 256 KB backstop
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute(ins, did, n, code, spec, renderer)
    await c.execute(ins, did, 9, "", "{}", "ic-render-1.0.0")  # a valid row still goes in


async def test_trigger_makes_spec_and_renderer_immutable(dash_db):  # noqa: F811
    import asyncpg
    c = dash_db.conn
    _, saved = await _spec_draft(c)
    vid = saved["version_id"]
    with pytest.raises(asyncpg.RaiseError):
        await c.execute("UPDATE dashboard_versions SET spec = '{}'::jsonb WHERE id = $1", vid)
    with pytest.raises(asyncpg.RaiseError):
        await c.execute("UPDATE dashboard_versions SET renderer = 'other' WHERE id = $1", vid)
    # status moves stay allowed, and the Phase 3 trigger still guards code on HTML rows
    await c.execute("UPDATE dashboard_versions SET status = 'retired' WHERE id = $1", vid)
    html = await d.save_version(c, slug="old", title="Old", code=HTML,
                                queries=[{"id": "q", "sql": SQL_ONLY_SQL}])
    with pytest.raises(asyncpg.RaiseError):
        await c.execute("UPDATE dashboard_versions SET code = '' WHERE id = $1", html["version_id"])


async def test_save_spec_version_round_trip(dash_db):  # noqa: F811
    c = dash_db.conn
    prep, saved = await _spec_draft(c, with_measure=True)
    row = await c.fetchrow(
        "SELECT code, spec, renderer, script_hashes, status FROM dashboard_versions WHERE id = $1",
        saved["version_id"])
    assert row["code"] == "" and row["status"] == "draft"
    assert json.loads(row["spec"]) == prep["stored"]
    assert row["renderer"] == "ic-render-1.0.0"
    assert list(row["script_hashes"]) == [dr.RENDERERS[dr.CURRENT_RENDERER]["csp_hash"],
                                          dr.LIB_CSP_HASH]
    snaps = await c.fetch("SELECT query_id FROM dashboard_snapshots WHERE version_id = $1 "
                          "ORDER BY 1", saved["version_id"])
    assert [s[0] for s in snaps] == ["m_net", "open_invoices"]
    assert await c.fetchval(
        "SELECT current_version_id FROM dashboards WHERE id = $1", saved["dashboard_id"]) is None
    second = await d.save_spec_version(
        c, slug="open", stored=prep["stored"], queries=prep["queries"], title=prep["title"],
        renderer=dr.CURRENT_RENDERER)
    assert second["version"] == 2


async def test_html_gate_with_real_rows(dash_db, monkeypatch):  # noqa: F811
    c = dash_db.conn
    html = await d.save_version(c, slug="old", title="Old", code=HTML,
                                queries=[{"id": "q", "sql": SQL_ONLY_SQL}])
    _, spec = await _spec_draft(c)
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    with pytest.raises(DashboardError) as ei:
        await d.apply_publish_proposal(c, _payload(html))
    assert ei.value.status == 409
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(c, slug="old", version=html["version"], op="publish")
    assert ei.value.status == 409
    assert await d.apply_publish_proposal(c, _payload(spec)) is True
    assert await c.fetchval("SELECT status FROM dashboard_versions WHERE id = $1",
                            spec["version_id"]) == "published"
    monkeypatch.setattr(d, "legacy_html_on", lambda: True)
    assert await d.apply_publish_proposal(c, _payload(html)) is True


async def test_rollback_to_retired_html_works_while_off(dash_db, monkeypatch):  # noqa: F811
    c = dash_db.conn
    await c.execute("INSERT INTO people (name, is_primary, active) "
                    "VALUES ('Owner', true, true)")
    html = await d.save_version(c, slug="mix", title="Mix", code=HTML,
                                queries=[{"id": "q", "sql": SQL_ONLY_SQL}])
    monkeypatch.setattr(d, "legacy_html_on", lambda: True)
    await d.apply_publish_proposal(c, _payload(html))
    prep = await d.prepare_spec(sql_only_spec())
    spec = await d.save_spec_version(c, slug="mix", stored=prep["stored"],
                                     queries=prep["queries"], title=prep["title"],
                                     renderer=dr.CURRENT_RENDERER)
    await d.apply_publish_proposal(c, _payload(spec))  # retires the HTML version
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    out = await d.operator_set_version(c, slug="mix", version=html["version"], op="rollback")
    assert out["from_version"] == spec["version"]


async def test_readers_report_format_and_authoring_spec(dash_db, monkeypatch):  # noqa: F811
    c = dash_db.conn
    monkeypatch.setattr(d, "legacy_html_on", lambda: False)
    html = await d.save_version(c, slug="old", title="Old", code=HTML,
                                queries=[{"id": "q", "sql": SQL_ONLY_SQL}])
    await d.publish(c, dashboard_id=html["dashboard_id"], version_id=html["version_id"])
    prep, spec = await _spec_draft(c)
    await d.publish(c, dashboard_id=spec["dashboard_id"], version_id=spec["version_id"])
    listing = {r["slug"]: r for r in await d.list_dashboards(c)}
    assert listing["old"]["current_format"] == "html" and listing["old"]["needs_conversion"] is True
    assert listing["open"]["current_format"] == "spec" and listing["open"]["needs_conversion"] is False
    got = await d.get_version(c, slug="open", version=1)
    assert got["format"] == "spec" and got["renderer"] == "ic-render-1.0.0" and got["code"] == ""
    assert all("qid" not in t for t in got["spec"]["tiles"])
    assert got["summary"]["queries"] == [{"id": "open_invoices", "source": "sql"}]
    old = await d.get_version(c, slug="old", version=1)
    assert old["format"] == "html" and old["spec"] is None
    versions = (await d.get_dashboard(c, slug="open"))["versions"]
    assert versions[0]["format"] == "spec" and versions[0]["tiles_added"] == ["open"]


# ── Phase 18 (SPEC-04): the dashboard-authoring skill refresh ────────────────

def _skill_norm(t: str) -> str:
    return t.replace(chr(13), "").strip(" " + chr(10) + chr(9))


async def test_dashboard_authoring_refresh_reaches_unedited_p131_only(dash_db):  # noqa: F811
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    new = (root / "skills_library" / "intelligence-corner" / "dashboard-authoring"
           / "SKILL.md").read_bytes().decode("utf-8")
    old = (Path(__file__).parent / "fixtures"
           / "dashboard_authoring_skill_p131.md").read_bytes().decode("utf-8")
    c = dash_db.conn

    async def row():
        return await c.fetchrow(
            "SELECT body, enabled, updated_at FROM skills WHERE name = 'dashboard-authoring'")

    r = await row()
    assert _skill_norm(r["body"]) == _skill_norm(new) and r["enabled"] is False

    # an install that still carries the Phase 13.1 body is refreshed; enabled is untouched
    await c.execute("UPDATE skills SET body = $1 WHERE name = 'dashboard-authoring'", old)
    await c.execute(dash_db.sql)
    r = await row()
    assert _skill_norm(r["body"]) == _skill_norm(new) and r["enabled"] is False
    stamp = r["updated_at"]
    await c.execute(dash_db.sql)  # idempotent: nothing matches any more
    assert (await row())["updated_at"] == stamp

    # the same refresh keeps an owner-enabled flag
    await c.execute(
        "UPDATE skills SET body = $1, enabled = TRUE WHERE name = 'dashboard-authoring'", old)
    await c.execute(dash_db.sql)
    r = await row()
    assert _skill_norm(r["body"]) == _skill_norm(new) and r["enabled"] is True

    # an owner-edited copy is never touched
    edited = old + chr(10) + "Owner note." + chr(10)
    await c.execute("UPDATE skills SET body = $1 WHERE name = 'dashboard-authoring'", edited)
    await c.execute(dash_db.sql)
    r = await row()
    assert r["body"] == edited and r["enabled"] is True
