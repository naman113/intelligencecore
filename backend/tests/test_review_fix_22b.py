"""Phase 22 review fixes (batch B): renderer/spec_version match, hidden tile data, design lint."""
import json

import pytest

from app import dashboarddesign as dd
from app import dashboards as d
from app import dashboards_serve as ds
from app.dashboards import DashboardError
from tests.test_dashboard_design_lint import lint, spec, stat, ctl, with_sql
from tests.test_spec_v2_store import save_conn


@pytest.mark.parametrize("stored,renderer", [
    ({"spec_version": 2}, "ic-render-1.0.0"),
    ({"spec_version": 1}, "ic-render-2.0.0"),
    ({"x": 1}, "ic-render-2.0.0"),
])
async def test_save_refuses_renderer_that_does_not_match_spec_version(stored, renderer):
    with pytest.raises(DashboardError) as e:
        await d.save_spec_version(save_conn(), slug="ops", stored=stored, queries=[],
                                  title="T", renderer=renderer)
    assert "renderer does not match spec_version" in str(e.value)


class Conn:
    def __init__(self, stored, queries, renderer="ic-render-2.0.0"):
        self.row = {"spec": json.dumps(stored), "queries": json.dumps(queries),
                    "renderer": renderer}
        self.calls = 0

    async def fetchrow(self, sql, *a):
        self.calls += 1
        if sql == ds.FORMAT_SQL:
            return {"is_spec": True, "renderer": self.row["renderer"]}
        return self.row


async def test_hidden_tile_query_is_not_servable_and_answer_is_cached():
    ds._V2_QIDS.clear()
    queries = [{"id": "q_vis", "sql": "SELECT 1 AS a", "refresh_minutes": 30},
               {"id": "q_hid", "sql": "SELECT 2 AS a", "refresh_minutes": 30}]
    stored = {"spec_version": 2, "title": "T", "sections": [{"tiles": ["a", "b"]}],
              "tiles": [
                  {"id": "a", "type": "stat", "title": "A", "query": "q_vis", "qid": "q_vis",
                   "column": "a"},
                  {"id": "b", "type": "stat", "title": "B", "query": "q_hid", "qid": "q_hid",
                   "column": "a", "hidden": True}]}
    conn = Conn(stored, queries)
    allowed = await ds._v2_data_qids(conn, 901)
    assert "q_vis" in allowed and "q_hid" not in allowed
    await ds._v2_data_qids(conn, 901)
    assert conn.calls == 2
    ds._V2_QIDS.clear()


async def test_v1_version_is_not_restricted():
    ds._V2_QIDS.clear()
    assert await ds._v2_data_qids(Conn({}, [], renderer="ic-render-1.0.0"), 902) is None


def test_last_year_needs_history_for_last_month_and_91_day_ranges():
    t = ctl(comparison="last_year", good_direction="up")
    for presets, horizon, warns in (
            (["last_month"], 400, True), (["last_month"], 427, False),
            (["last_90_days"], 430, True), (["last_90_days"], 455, False)):
        sp = spec([t], date_control={"default": presets[0], "presets": presets,
                                     "horizon_days": horizon})
        found = [f for f in lint(sp) if f["rule"] == "design_horizon"]
        assert bool(found) is warns, (presets, horizon)


@pytest.mark.parametrize("agg,expr,sql", [
    ("average", "weight", "SELECT avg(weight) AS a FROM public.lots"),
    ("count_distinct", "lot", "SELECT count(DISTINCT lot) AS a FROM public.lots"),
    ("sum_boolean", "ok", "SELECT sum(CASE WHEN ok THEN 1 ELSE 0 END) AS a FROM public.lots"),
])
def test_raw_sql_matches_non_sql_agg_names(agg, expr, sql):
    found = lint(with_sql(sql), measure_sql={"m": (agg, expr, "public.lots")})
    assert any(f["rule"] == "design_raw_sql" for f in found)
