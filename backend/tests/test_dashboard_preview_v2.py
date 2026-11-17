"""Phase 22 plan 08 task 2: the v2 preview branch, filter cardinality, size estimates, the save
refusal and the docstring budget. Fakes only; no DB, LLM or network."""
import json

import pytest

from app import dashsettings
from app import dashboardtools as dt
from tests.test_dashboard_spec_tools import env, preview, save, sql_spec  # noqa: F401


# Lengths of the two docstrings at commit 5ae19a6 (read once with git show).
OLD_PREVIEW_DOC = 1281
OLD_SAVE_DOC = 885


def v2_sql_spec(title="Total", **kw):
    out = {"spec_version": 2, "title": "Ops",
           "queries": [{"id": "q0", "sql": "SELECT 1 AS total, 2 AS other",
                        "refresh_minutes": 30}],
           "sections": [{"title": "Top", "tiles": ["s"]}],
           "tiles": [{"id": "s", "type": "stat", "title": title, "query": "q0",
                      "column": "total"}]}
    out.update(kw)
    return out


@pytest.mark.asyncio
async def test_v2_preview_shape(env):
    d = json.loads(await preview(v2_sql_spec()))
    assert d["ok"] is True and d["errors"] == []
    assert d["renderer"] == "ic-render-2.0.0"
    assert d["wireframe"].startswith("Page 'Ops'")
    assert "Section 1 'Top': 1 tiles - 1 stat (2-up on phones)" in d["wireframe"]
    assert [w["rule"] for w in d["warnings"]] == ["design_title_period"]
    assert "fix" not in d["warnings"][0] and "level" not in d["warnings"][0]
    rows_cap, bytes_cap = dashsettings.snapshot_caps()
    assert d["size"] == [{"id": "q0", "rows": 3, "est_bytes": d["size"][0]["est_bytes"],
                          "rows_cap": rows_cap, "bytes_cap": bytes_cap}]
    assert d["size"][0]["est_bytes"] > 0
    assert d["queries"][0]["id"] == "q0" and d["size_bytes"] > 0
    assert d["tiles"][0]["binding"] == "query"
    assert env["conn"].events == [] and env["conn"].inserts == []


@pytest.mark.asyncio
async def test_v2_clean_spec_has_no_warnings(env):
    d = json.loads(await preview(v2_sql_spec(title="Total this month")))
    assert d["warnings"] == [] and d["ok"] is True


@pytest.mark.asyncio
async def test_v2_preview_id_saves_exactly_that_spec(env):
    from app import dashguard
    tok = dashguard.begin()
    try:
        d = json.loads(await preview(v2_sql_spec(title="Total this month")))
        assert d["preview_id"].startswith("pv_") and d["preview_rounds_left"] == 2
        out = await dt.save_dashboard_version.ainvoke(
            {"slug": "ops", "change_note": "first", "preview_id": d["preview_id"]})
        assert out.startswith("Saved draft v1")
        assert env["save_kw"]["renderer"] == "ic-render-2.0.0"
    finally:
        dashguard.end(tok)


@pytest.mark.asyncio
async def test_over_cap_query_blocks_preview_id(env, monkeypatch):
    monkeypatch.setattr(dashsettings, "snapshot_caps", lambda: (2, 10 ** 9))
    d = json.loads(await preview(v2_sql_spec(title="Total this month")))
    assert d["ok"] is False and "preview_id" not in d
    assert [e["rule"] for e in d["errors"]] == ["size"]
    assert "level" not in d["errors"][0]


@pytest.mark.asyncio
async def test_over_bytes_cap_is_an_error(env, monkeypatch):
    monkeypatch.setattr(dashsettings, "snapshot_caps", lambda: (10 ** 6, 20))
    d = json.loads(await preview(v2_sql_spec(title="Total this month")))
    assert d["ok"] is False and d["errors"][0]["rule"] == "size"


@pytest.mark.asyncio
async def test_filter_over_fifty_blocks_preview_id(env, monkeypatch):
    async def counts(stored, qs, deadline):
        return {"f": 51}
    monkeypatch.setattr(dt, "_filter_counts", counts)
    spec = v2_sql_spec(title="Total this month",
                       filters=[{"id": "f", "dimension": "lots.supplier", "label": "Supplier"}],
                       date_control={"default": "last_7_days"})
    d = json.loads(await preview(spec))
    assert d["ok"] is False and "preview_id" not in d
    assert d["errors"][0]["rule"] == "filters" and "51" in d["errors"][0]["message"]


@pytest.mark.asyncio
async def test_filter_counts_query_text_and_limit(env, monkeypatch):
    seen = []

    async def run_sql(sql, **kw):
        seen.append(sql)
        return [{"n": 7}]
    monkeypatch.setattr(dt.guardrails, "run_readonly_sql", run_sql)
    stored = {"filters": [{"id": f"f{i}", "dimension": "lots.supplier", "label": "S"}
                          for i in range(5)],
              "tiles": [{"id": "t", "qid": "q", "follow_filters": True}]}
    qs = [{"id": "q", "sql": "SELECT 1", "columns": ["metric_time__day", "lots__supplier"]}]
    out = await dt._filter_counts(stored, qs, 10 ** 12)
    assert out == {"f0": 7, "f1": 7, "f2": 7}  # at most 3 counts
    assert seen[0] == 'SELECT count(DISTINCT "lots__supplier") AS n FROM (SELECT 1\n) AS _f'


@pytest.mark.asyncio
async def test_filter_counts_skip_tiles_that_do_not_follow(env):
    stored = {"filters": [{"id": "f", "dimension": "lots.supplier", "label": "S"}],
              "tiles": [{"id": "t", "qid": "q"}]}
    assert await dt._filter_counts(stored, [{"id": "q", "sql": "SELECT 1"}], 10 ** 12) == {}


@pytest.mark.asyncio
async def test_save_refuses_filter_over_fifty(env, monkeypatch):
    async def counts(stored, qs, deadline):
        return {"f": 51}
    monkeypatch.setattr(dt, "_filter_counts", counts)
    out = await save(v2_sql_spec(title="Total this month"))
    assert out == "REFUSED: filter f has 51 values; at most 50"
    assert env["conn"].events == []


@pytest.mark.asyncio
async def test_save_allows_fifty_and_warnings(env, monkeypatch):
    async def counts(stored, qs, deadline):
        return {"f": 50}
    monkeypatch.setattr(dt, "_filter_counts", counts)
    out = await save(v2_sql_spec(title="Total"))  # a design warning, not an error
    assert out.startswith("Saved draft v1")


@pytest.mark.asyncio
async def test_v1_preview_keys_unchanged(env):
    d = json.loads(await preview(sql_spec()))
    assert set(d) == {"ok", "errors", "warnings", "tiles", "measures", "queries", "size_bytes",
                      "next"}


def test_fit_v2_trims_warnings_last_and_counts(monkeypatch):
    out = {"ok": True, "errors": [], "queries": [],
           "warnings": [{"rule": "r", "path": "p", "message": "x" * 500} for _ in range(60)]}
    fitted = dt._fit_v2(out)
    assert len(json.dumps(fitted)) <= dt.OUTPUT_CAP
    assert fitted["warnings_trimmed"] == 60 - len(fitted["warnings"]) > 0


def test_fit_v2_leaves_small_output_alone():
    out = {"ok": True, "errors": [], "warnings": [{"rule": "r"}], "queries": []}
    assert "warnings_trimmed" not in dt._fit_v2(out)


def test_docstrings_describe_v2_within_budget():
    for tool, old in ((dt.preview_dashboard, OLD_PREVIEW_DOC),
                      (dt.save_dashboard_version, OLD_SAVE_DOC)):
        doc = tool.description
        assert len(doc) <= old, (tool.name, len(doc))
        assert "spec_version: 2" in doc
        assert 'use_skill("dashboard-authoring", part="reference/spec-v2.md")' in doc
        for name in ("stat", "table", "text", "line", "area", "bar", "stacked_bar", "sparkline",
                     "combo", "paired_bar", "aging", "bullet", "waterfall", "calendar", "donut",
                     "ranked_bar", "funnel"):
            assert name in doc


def test_compile_notes_reach_the_request():
    from app import dashboardspec as ds
    from tests.test_compiler_tiles import build
    spec = ds.parse({"spec_version": 2, "title": "T", "date_control": {"default": "last_7_days"},
                     "sections": [{"tiles": ["s"]}],
                     "tiles": [{"id": "s", "type": "stat", "title": "Amount", "date": "control",
                                "measure": {"name": "events_amount"}}]})
    entries = ds.compile_measures(spec, build())
    assert isinstance(entries[0]["request"]["notes"], list)
    assert any("has no time zone" in n for n in entries[0]["request"]["notes"])


@pytest.mark.asyncio
async def test_preview_with_date_control_and_text_tile_does_not_error(env):
    spec = v2_sql_spec(date_control={"default": "last_7_days"})
    spec["sections"][0]["tiles"].append("n")
    spec["tiles"].append({"id": "n", "type": "text", "text": "Read me."})
    out = await preview(spec)
    assert not out.startswith("TOOL ERROR"), out
    assert "AttributeError" not in out


def _filter_spec():
    return v2_sql_spec(title="Total this month",
                       filters=[{"id": "f", "dimension": "lots.supplier", "label": "Supplier"}],
                       date_control={"default": "last_7_days"})


@pytest.mark.asyncio
async def test_uncounted_filter_is_a_preview_error_without_preview_id(env, monkeypatch):
    async def counts(stored, qs, deadline):
        return {}
    monkeypatch.setattr(dt, "_filter_counts", counts)
    monkeypatch.setattr(dt, "_filter_targets", lambda stored, qs: [("f", "c", "SELECT 1")])
    d = json.loads(await preview(_filter_spec()))
    assert d["ok"] is False and "preview_id" not in d
    assert d["errors"][0]["rule"] == "filters" and "could not count" in d["errors"][0]["message"]


@pytest.mark.asyncio
async def test_save_refuses_uncounted_filter(env, monkeypatch):
    async def counts(stored, qs, deadline):
        return {}
    monkeypatch.setattr(dt, "_filter_counts", counts)
    monkeypatch.setattr(dt, "_filter_targets", lambda stored, qs: [("f", "c", "SELECT 1")])
    out = await save(_filter_spec())
    assert out == "REFUSED: could not count the values of filter f; try again"
    assert env["conn"].events == []


@pytest.mark.asyncio
async def test_save_with_spec_refuses_query_over_snapshot_caps(env, monkeypatch):
    monkeypatch.setattr(dashsettings, "snapshot_caps", lambda: (2, 10 ** 9))
    out = await save(v2_sql_spec(title="Total this month"))
    assert out.startswith("REFUSED: Query q0 returns 3 rows")
    assert env["conn"].events == []
    monkeypatch.setattr(dashsettings, "snapshot_caps", lambda: (10 ** 6, 20))
    out = await save(v2_sql_spec(title="Total this month"))
    assert out.startswith("REFUSED: Query q0")
