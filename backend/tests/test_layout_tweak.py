"""Phase 22 plan 11 task 1: pure layout_tweak and the audited save. Fakes only."""
import copy
import json
import os

import pytest

from app import dashboardrender as dr
from app import dashboards as d
from app import dashboardspec as ds
from app import guardrails, modelcompiler, semanticmodel
from app.dashboards import DashboardError
from tests.test_dashboard_spec_store import Conn

Q = [{"id": "q0", "sql": "SELECT 1 AS total, 2 AS other, 'a' AS k", "refresh_minutes": 30}]


def raw_spec(**kw):
    out = {"spec_version": 2, "title": "Ops", "queries": Q,
           "date_control": {"default": "last_7_days", "presets": ["last_7_days", "last_30_days"]},
           "sections": [{"title": "Top", "tiles": ["a", "b"]},
                        {"title": "More", "tiles": ["c", "t"]}],
           "tiles": [
               {"id": "a", "type": "stat", "title": "Total", "query": "q0", "column": "total"},
               {"id": "b", "type": "stat", "title": "Other", "query": "q0", "column": "other"},
               {"id": "c", "type": "table", "title": "Rows", "query": "q0"},
               {"id": "t", "type": "text", "text": "hello"}]}
    out.update(kw)
    return out


@pytest.fixture
async def base():
    prep = await d.prepare_spec(raw_spec())
    return prep["stored"], prep["queries"]


def ch(stored, **kw):
    out = {"sections": [{"title": s.get("title"), "tiles": list(s["tiles"])}
                        for s in stored["sections"]]}
    out.update(kw)
    return out


def err(stored, changes):
    with pytest.raises(ds.SpecError) as e:
        ds.layout_tweak(stored, changes)
    return e.value.findings


async def test_identity_changes_nothing(base):
    stored, _ = base
    assert ds.layout_tweak(stored, ch(stored)) == stored


async def test_does_not_mutate_input(base):
    stored, _ = base
    snap = copy.deepcopy(stored)
    ds.layout_tweak(stored, ch(stored, widths={"a": "full"}))
    assert stored == snap


async def test_reorder_and_cross_section(base):
    stored, _ = base
    c = ch(stored)
    c["sections"] = [{"title": "Top", "tiles": ["b"]},
                     {"title": "More", "tiles": ["a", "c", "t"]}]
    out = ds.layout_tweak(stored, c)
    assert [s["tiles"] for s in out["sections"]] == [["b"], ["a", "c", "t"]]
    assert [t["id"] for t in out["tiles"]] == [t["id"] for t in stored["tiles"]]
    assert "a" in ds.layout_summary(stored, out)["moved"]


async def test_width_title_hidden_range_section_title(base):
    stored, _ = base
    c = ch(stored, widths={"a": "full", "c": "two_thirds"}, titles={"b": "Second"},
           hidden=["t"], default_range="last_30_days")
    c["sections"][0]["title"] = "Headline"
    out = ds.layout_tweak(stored, c)
    byid = {t["id"]: t for t in out["tiles"]}
    assert byid["a"]["width"] == "full" and byid["c"]["width"] == "two_thirds"
    assert byid["b"]["title"] == "Second" and byid["t"]["hidden"] is True
    assert byid["a"]["hidden"] is False
    assert out["date_control"]["default"] == "last_30_days"
    assert out["sections"][0]["title"] == "Headline"
    s = ds.layout_summary(stored, out)
    assert s["widths"] == {"a": "full", "c": "two_thirds"} and s["hidden"] == ["t"]
    assert "b" in s["renamed"] and s["default_range"] == "last_30_days"


async def test_content_never_changes(base):
    stored, _ = base
    out = ds.layout_tweak(stored, ch(stored, widths={"a": "half"}, titles={"a": "X"}))

    def strip(s):
        return [{k: v for k, v in t.items() if k not in ("title", "width", "hidden")}
                for t in s["tiles"]]
    assert strip(out) == strip(stored)


async def test_tile_set_must_match(base):
    stored, _ = base
    c = ch(stored)
    c["sections"][1]["tiles"].remove("t")
    assert err(stored, c)[0]["rule"] == "layout"
    c = ch(stored)
    c["sections"][1]["tiles"].append("zz")
    assert err(stored, c)[0]["rule"] == "layout"
    c = ch(stored)
    c["sections"][1]["tiles"].append("a")  # duplicate
    assert err(stored, c)[0]["rule"] == "layout"


async def test_empty_section_and_count_change(base):
    stored, _ = base
    c = ch(stored)
    c["sections"] = [{"title": "Top", "tiles": []},
                     {"title": "More", "tiles": ["a", "b", "c", "t"]}]
    assert err(stored, c)[0]["rule"] == "layout"
    c = ch(stored)
    c["sections"].append({"title": "x", "tiles": []})
    assert err(stored, c)[0]["rule"] == "layout"
    c = ch(stored)
    c["sections"] = [{"title": "all", "tiles": ["a", "b", "c", "t"]}]
    assert err(stored, c)[0]["rule"] == "layout"


async def test_unknown_width_preset_and_ids(base):
    stored, _ = base
    assert err(stored, ch(stored, widths={"a": "huge"}))[0]["rule"] == "layout"
    assert err(stored, ch(stored, widths={"zz": "full"}))[0]["rule"] == "layout"
    assert err(stored, ch(stored, titles={"zz": "x"}))[0]["rule"] == "layout"
    assert err(stored, ch(stored, hidden=["zz"]))[0]["rule"] == "layout"
    assert err(stored, ch(stored, default_range="nope"))[0]["rule"] == "layout"


async def test_default_range_must_be_offered(base):
    stored, _ = base
    f = err(stored, ch(stored, default_range="this_fy"))
    assert f[0]["rule"] == "layout" and "offered" in f[0]["message"]


async def test_default_range_without_date_control():
    raw = {k: v for k, v in raw_spec().items() if k != "date_control"}
    prep = await d.prepare_spec(raw)
    f = err(prep["stored"], ch(prep["stored"], default_range="last_7_days"))
    assert f[0]["rule"] == "layout"


async def test_all_hidden_refused(base):
    stored, _ = base
    assert err(stored, ch(stored, hidden=["a", "b", "c", "t"]))[0]["rule"] == "layout"


async def test_title_validated_as_plain_text(base):
    stored, _ = base
    with pytest.raises(ds.SpecError):
        ds.layout_tweak(stored, ch(stored, titles={"a": "bad‮title"}))
    with pytest.raises(ds.SpecError):
        ds.layout_tweak(stored, ch(stored, titles={"a": "x" * 121}))
    c = ch(stored)
    c["sections"][0]["title"] = "y" * 61
    with pytest.raises(ds.SpecError):
        ds.layout_tweak(stored, c)


async def test_v1_base_refused():
    with pytest.raises(ds.SpecError) as e:
        ds.layout_tweak({"spec_version": 1, "tiles": [], "layout": {}}, {"sections": []})
    assert e.value.findings[0]["rule"] == "layout"


# ---------------------------------------------------------------- the audited save

@pytest.fixture
def no_tenant(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a layout tweak must not touch the tenant or the model")
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    monkeypatch.setattr(semanticmodel, "load_model", boom)
    monkeypatch.setattr(modelcompiler, "compile_tile", boom, raising=False)


def tweak_conn(stored, queries, *, current=True, version=3, spec=True):
    row = {"id": 40, "version": version, "title": "Ops",
           "spec": json.dumps(stored) if spec else None,
           "queries": json.dumps(queries), "renderer": dr.renderer_for(2)}
    return Conn([("FOR UPDATE", {"id": 7, "slug": "ops",
                                 "current_version_id": 40 if current else None}),
                 ("SELECT id, version, title, spec", row),
                 ("FROM people WHERE is_primary", 5),
                 ("INSERT INTO dashboards", {"id": 7, "created": False}),
                 ("COALESCE(MAX(version)", 4), ("INSERT INTO dashboard_versions", 41),
                 ("INSERT INTO actions", 99)])


async def test_save_writes_draft_and_action(base, no_tenant):
    stored, queries = base
    conn = tweak_conn(stored, queries)
    out = await d.save_layout_tweak(conn, slug="ops", base_version=3,
                                    changes=ch(stored, widths={"a": "full"}, hidden=["t"]))
    assert out["version"] == 4 and out["version_id"] == 41 and out["action_id"] == 99
    assert isinstance(out["warnings"], list)
    ins = next(c for c in conn.calls if "INSERT INTO dashboard_versions" in c[1])
    assert json.loads(ins[2][4]) == queries            # queries verbatim
    assert ins[2][6] == dr.renderer_for(2)
    assert ins[2][7].startswith("Owner layout change:")
    assert ins[2][8] == "operator"
    new = json.loads(ins[2][5])
    assert next(t for t in new["tiles"] if t["id"] == "a")["width"] == "full"
    act = next(c for c in conn.calls if "INSERT INTO actions" in c[1])
    assert "'dashboard_layout'" in act[1] and "'executed'" in act[1] and "'operator'" in act[1]
    payload = json.loads(act[2][0])
    assert payload["base_version"] == 3 and payload["version"] == 4
    assert payload["version_id"] == 41 and payload["decided_via"] == "ops_api"
    assert payload["hidden"] == ["t"] and payload["widths"] == {"a": "full"}
    assert act[2][1] == 5
    assert any("FOR UPDATE" in c[1] for c in conn.calls)


async def test_stale_base_is_409(base, no_tenant):
    stored, queries = base
    with pytest.raises(DashboardError) as e:
        await d.save_layout_tweak(tweak_conn(stored, queries, version=5), slug="ops",
                                  base_version=3, changes=ch(stored))
    assert e.value.status == 409 and "reload" in str(e.value)
    with pytest.raises(DashboardError) as e:
        await d.save_layout_tweak(tweak_conn(stored, queries, current=False), slug="ops",
                                  base_version=3, changes=ch(stored))
    assert e.value.status == 409


async def test_html_or_v1_is_409(base, no_tenant):
    stored, queries = base
    with pytest.raises(DashboardError) as e:
        await d.save_layout_tweak(tweak_conn(stored, queries, spec=False), slug="ops",
                                  base_version=3, changes=ch(stored))
    assert e.value.status == 409 and "spec v2" in str(e.value)
    v1 = {"spec_version": 1, "title": "x", "tiles": [], "layout": {"rows": []}}
    with pytest.raises(DashboardError) as e:
        await d.save_layout_tweak(tweak_conn(v1, queries), slug="ops", base_version=3,
                                  changes={"sections": []})
    assert e.value.status == 409


async def test_missing_dashboard_404_and_bad_input(base):
    stored, queries = base
    with pytest.raises(DashboardError) as e:
        await d.save_layout_tweak(Conn(), slug="ops", base_version=3, changes=ch(stored))
    assert e.value.status == 404
    with pytest.raises(DashboardError):
        await d.save_layout_tweak(Conn(), slug="ops", base_version="x", changes=ch(stored))
    with pytest.raises(DashboardError):
        await d.save_layout_tweak(Conn(), slug="ops", base_version=3, changes=[])


async def test_spec_error_propagates_and_writes_nothing(base, no_tenant):
    stored, queries = base
    conn = tweak_conn(stored, queries)
    with pytest.raises(ds.SpecError):
        await d.save_layout_tweak(conn, slug="ops", base_version=3,
                                  changes=ch(stored, widths={"a": "huge"}))
    assert not any("INSERT" in c[1] for c in conn.calls)


async def test_lint_warnings_returned(base, no_tenant, monkeypatch):
    from app import dashboarddesign
    monkeypatch.setattr(dashboarddesign, "design_lint",
                        lambda spec, stored, queries, **k: [{"rule": "x", "message": "m"}])
    stored, queries = base
    out = await d.save_layout_tweak(tweak_conn(stored, queries), slug="ops", base_version=3,
                                    changes=ch(stored))
    assert out["warnings"] == [{"rule": "x", "message": "m"}]


async def test_spec_version_on_version_rows():
    stored_v2 = {"spec_version": 2, "title": "t", "tiles": [], "sections": []}
    stored_v1 = {"title": "t", "tiles": [], "layout": {"rows": []}}
    conn = Conn([("SELECT version, spec, queries", [
        {"version": 1, "spec": json.dumps(stored_v1), "queries": "[]"},
        {"version": 2, "spec": json.dumps(stored_v2), "queries": "[]"},
        {"version": 3, "spec": None, "queries": "[]"}])])
    vs = [{"version": 1}, {"version": 2}, {"version": 3}]
    await d._p18_dashboard_extras(conn, 7, vs)
    assert [v["spec_version"] for v in vs] == [1, 2, None]
    assert all("format" in v and "tiles" in v for v in vs)


class _Rollback(Exception):
    pass


@pytest.mark.skipif(not os.environ.get("DASH_TEST_DATABASE_URL"), reason="needs a real Postgres")
async def test_real_db_roundtrip():
    import asyncpg
    conn = await asyncpg.connect(os.environ["DASH_TEST_DATABASE_URL"])
    try:
        prep = await d.prepare_spec(raw_spec())
        slug = "lt-real"
        try:
            async with conn.transaction():
                await conn.execute("DELETE FROM dashboards WHERE slug = $1", slug)
                await conn.execute(
                    "INSERT INTO people (name, is_primary, active) SELECT 'Owner', true, true "
                    "WHERE NOT EXISTS (SELECT 1 FROM people WHERE is_primary AND active)")
                saved = await d.save_spec_version(
                    conn, slug=slug, stored=prep["stored"], queries=prep["queries"],
                    title=prep["title"], renderer=prep["renderer"], author="agent")
                await d.operator_set_version(conn, slug=slug, version=saved["version"],
                                             op="publish")
                out = await d.save_layout_tweak(
                    conn, slug=slug, base_version=saved["version"],
                    changes=ch(prep["stored"], widths={"a": "full"}))
                assert out["version"] == saved["version"] + 1
                row = await conn.fetchrow(
                    "SELECT status, author FROM dashboard_versions WHERE id = $1",
                    out["version_id"])
                assert row["status"] == "draft" and row["author"] == "operator"
                n = await conn.fetchval(
                    "SELECT count(*) FROM actions WHERE kind = 'dashboard_layout' AND id = $1",
                    out["action_id"])
                assert n == 1
                raise _Rollback()
        except _Rollback:
            pass
    finally:
        await conn.close()


async def test_unhashable_tile_ids_are_a_finding_not_a_crash(base):
    stored, _ = base
    c = ch(stored)
    c["sections"][0]["tiles"] = [["a"], "b"]
    assert err(stored, c)[0]["rule"] == "layout"
    assert err(stored, ch(stored, hidden=[["a"]]))[0]["rule"] == "layout"


async def test_unchanged_widths_are_not_written(base):
    stored, _ = base
    widths = {t["id"]: ds._eff_width(t) for t in stored["tiles"]}
    widths["a"] = "full"
    out = ds.layout_tweak(stored, ch(stored, widths=widths))
    changed = [t["id"] for t in out["tiles"]
               if t.get("width") != next(s for s in stored["tiles"] if s["id"] == t["id"]).get("width")]
    assert changed == ["a"] or changed == [] and ds._eff_width(stored["tiles"][0]) == "full"
