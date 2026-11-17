"""Phase 22 plan 06 task 1: compile_measures_v2 (frozen governed queries for v2 bindings)."""
import pytest

from app import dashboardspec as ds
from app.guardrails import validate_readonly_sql
from tests.test_compiler_tiles import build


@pytest.fixture(scope="module")
def model():
    return build()


def spec(tiles, **kw):
    ids = [t["id"] for t in tiles]
    out = {"spec_version": 2, "title": "T", "sections": [{"tiles": ids}], "tiles": tiles}
    out.update(kw)
    return ds.parse(out)


def tile(tid, typ="stat", date="control", measure="kg_in", **kw):
    t = {"id": tid, "type": typ, "title": "Tile", "date": date,
         "measure": measure if isinstance(measure, dict) else {"name": measure}}
    t.update(kw)
    return t


CTRL = {"default": "last_7_days"}


def run(model, tiles, **kw):
    return ds.compile_measures_v2(spec(tiles, **kw), model)


def err(model, tiles, **kw):
    with pytest.raises(ds.SpecError) as e:
        run(model, tiles, **kw)
    return e.value.findings


def test_controlled_tile_stores_daily_horizon_with_parts(model):
    (e,) = run(model, [tile("a", measure="avg_kg")], date_control=CTRL)
    r = e["request"]
    assert (r["kind"], r["grain"], r["window_days"], r["horizon_days"], r["follow"]) \
        == ("tile", "day", 400, 400, "control")
    assert e["columns"] == ["metric_time__day", "avg_kg", "avg_kg__sum", "avg_kg__count"]
    assert r["metas"][0]["kind"] == "avg" and r["metas"][0]["parts"]["sum"] == "avg_kg__sum"
    assert e["id"] == "m_a" and e["source"] == "measure" and e["measure"] == "avg_kg"
    validate_readonly_sql(e["sql"])


def test_entry_has_exactly_the_seven_keys(model):
    from app.dashboards import MEASURE_QUERY_KEYS
    (e,) = run(model, [tile("a")], date_control=CTRL)
    assert set(e) == MEASURE_QUERY_KEYS


def test_horizon_setting_is_used(model):
    (e,) = run(model, [tile("a")], date_control={"default": "today", "horizon_days": 200})
    assert e["request"]["window_days"] == 200 and e["request"]["horizon_days"] == 200


def test_by_dimension_caps_horizon_at_90(model):
    (e,) = run(model, [tile("a", "bar", measure={
        "name": "kg_in", "by": ["customers.name"]})], date_control=CTRL)
    assert e["request"]["horizon_days"] == 90 and e["request"]["window_days"] == 90
    assert e["columns"][:2] == ["metric_time__day", "customers__name"]


def test_short_horizon_not_raised_by_by(model):
    (e,) = run(model, [tile("a", "bar", measure={
        "name": "kg_in", "by": ["customers.name"]})],
        date_control={"default": "today", "horizon_days": 45})
    assert e["request"]["horizon_days"] == 45


def test_followed_filters_append_dimension_without_duplicates(model):
    flt = [{"id": "cust", "dimension": "customers.name", "label": "Customer"}]
    (e,) = run(model, [tile("a", "bar", follow_filters=True)], date_control=CTRL, filters=flt)
    assert e["request"]["horizon_days"] == 90
    assert e["columns"].count("customers__name") == 1
    (e2,) = run(model, [tile("a", "bar", follow_filters=True, measure={
        "name": "kg_in", "by": ["customers.name"]})], date_control=CTRL, filters=flt)
    assert e2["columns"].count("customers__name") == 1


def test_identical_bindings_share_a_query_and_ids_suffix_on_collision(model):
    es = run(model, [tile("a"), tile("b"), tile("c", measure="kg_out")], date_control=CTRL)
    assert [e["id"] for e in es] == ["m_a", "m_c"]
    es = run(model, [tile("a"), tile("a" * 3, date="fixed", measure="kg_in", **{}),
                     tile("b", measure="kg_out")], date_control=CTRL)
    assert len({e["id"] for e in es}) == len(es)


def test_fixed_tile_keeps_v1_window_semantics(model):
    (e,) = run(model, [tile("a", "line", date="fixed", measure={
        "name": "kg_in", "grain": "month", "window": {"last_periods": 3}})])
    assert "date_trunc('month', CURRENT_DATE) - interval '2 month'" in e["sql"]
    assert e["request"]["follow"] == "fixed" and e["request"]["horizon_days"] is None
    assert e["request"]["window_days"] == 93
    assert e["sql"].rstrip().endswith('ORDER BY "metric_time__month"')


def test_fixed_top_limit_and_order(model):
    (e,) = run(model, [tile("a", "ranked_bar", date="fixed", measure={
        "name": "kg_in", "by": ["customers.name"], "top": 5})])
    assert 'ORDER BY "kg_in" DESC NULLS LAST LIMIT 5' in e["sql"]
    assert e["request"]["top"] == 5


def test_fixed_top_with_other_keeps_other_row(model):
    (e,) = run(model, [tile("a", "ranked_bar", date="fixed", measure={
        "name": "kg_in", "by": ["customers.name"], "top": 5, "other": True})])
    assert "'Other'" in e["sql"] and e["request"]["other"] is True
    assert "LIMIT" not in e["sql"].split("'Other'")[0].split("_u")[-1]


def test_other_on_non_sum_is_a_measure_finding(model):
    f = err(model, [tile("a", "ranked_bar", date="fixed", measure={
        "name": "avg_kg", "by": ["customers.name"], "top": 5, "other": True})])
    assert f[0]["rule"] == "measure" and "avg_kg" in f[0]["message"]


def test_fixed_table_components_only_with_totals(model):
    base = {"measure": {"name": "avg_kg", "by": ["customers.name"]}}
    (a,) = run(model, [tile("a", "table", date="fixed", **base)])
    (b,) = run(model, [tile("a", "table", date="fixed", totals=True, **base)])
    assert "avg_kg__sum" not in a["columns"] and "avg_kg__sum" in b["columns"]


def test_multi_measure_table_is_one_query_with_parts(model):
    t = {"id": "t", "type": "table", "title": "Daily", "date": "control",
         "measures": {"names": ["rolls_in", "dispatches", "return_rate"]}}
    (e,) = run(model, [t], date_control=CTRL)
    assert e["columns"][0] == "metric_time__day"
    assert {"rolls_in", "dispatches", "return_rate", "return_rate__num", "return_rate__den"} \
        <= set(e["columns"])
    assert [m["column"] for m in e["request"]["metas"]] == ["rolls_in", "dispatches", "return_rate"]
    assert {m["date_basis"] for m in e["request"]["metas"]} == {"weigh date", "dispatch date"}
    assert e["request"]["names"] == ["rolls_in", "dispatches", "return_rate"]


def test_formula_measure_carries_tree(model):
    (e,) = run(model, [tile("a", measure="net_pct")], date_control=CTRL)
    m = e["request"]["metas"][0]
    assert m["kind"] == "formula" and m["formula"]["op"] and m["parts"]


def test_closing_measure_compiles_with_daily_balance(model):
    (e,) = run(model, [tile("a", measure="stock_kg")], date_control=CTRL)
    assert e["request"]["metas"][0]["kind"] == "closing"


def test_now_measure_has_no_grain_or_window(model):
    (e,) = run(model, [tile("a", measure="items_qty")], date_control=CTRL)
    r = e["request"]
    assert r["follow"] == "now" and r["grain"] is None and "window_days" not in r
    assert r["horizon_days"] is None and e["columns"] == ["items_qty"]


def test_now_measure_cannot_follow_page_filters(model):
    flt = [{"id": "c", "dimension": "customers.name", "label": "C"}]
    f = err(model, [tile("a", measure="items_qty", follow_filters=True)],
            date_control=CTRL, filters=flt)
    assert f[0]["rule"] == "binding" and f[0]["path"] == "tiles[0]"         and "cannot follow page filters" in f[0]["message"]


def test_now_mixed_with_other_measures_is_refused(model):
    t = {"id": "t", "type": "table", "title": "x", "date": "control",
         "measures": {"names": ["items_qty", "kg_in"]}}
    f = err(model, [t], date_control=CTRL)
    assert f[0]["rule"] == "measure" and "items_qty" in f[0]["message"]


def test_fixed_kind_measure_refused_under_control(model):
    f = err(model, [tile("a", measure="kg_growth")], date_control=CTRL)
    assert f[0]["rule"] == "measure" and "kg_growth" in f[0]["message"]
    f = err(model, [tile("a", measure="kg_flagged")], date_control=CTRL)
    assert f[0]["rule"] == "measure" and "kg_flagged" in f[0]["message"]


def test_two_distinct_measures_in_one_controlled_tile_refused(model):
    m2 = {"name": "distinct_customers2", "type": "simple", "base_relation": "public.rolls",
          "agg": "count_distinct", "expr": "id", "agg_time_dimension": "public.rolls.weigh_date"}
    from tests.test_compiler_tiles import _m
    mdl = build([_m(**m2)])
    t = {"id": "t", "type": "table", "title": "x", "date": "control",
         "measures": {"names": ["distinct_customers", "distinct_customers2"]}}
    f = err(mdl, [t], date_control=CTRL)
    assert f[0]["rule"] == "measure" and "distinct_customers" in f[0]["message"]


def test_distinct_tile_adds_presets_query(model):
    es = run(model, [tile("a", measure="distinct_customers")], date_control=CTRL)
    assert [e["id"] for e in es] == ["m_a", "m_a_p"]
    p = es[1]
    assert p["request"]["kind"] == "presets" and p["request"]["tile"] == "a"
    assert len(p["request"]["presets"]) == 9 and p["request"]["fy_start_month"] == 4
    assert p["columns"] == ["preset", "period", "value", "today"]
    validate_readonly_sql(p["sql"])
    es = run(model, [tile("a", measure="distinct_customers")],
             date_control={"default": "today", "presets": ["today", "last_7_days"],
                           "fy_start_month": 10})
    assert es[1]["request"]["presets"] == ["today", "last_7_days"]
    assert es[1]["request"]["fy_start_month"] == 10


def test_distinct_with_by_or_followed_filters_is_a_binding_finding(model):
    f = err(model, [tile("a", "bar", measure={
        "name": "distinct_customers", "by": ["customers.name"]})], date_control=CTRL)
    assert f[0]["rule"] == "binding" and f[0]["path"] == "tiles[0]" \
        and "distinct_customers counts distinct values" in f[0]["message"]
    flt = [{"id": "c", "dimension": "customers.name", "label": "C"}]
    f = err(model, [tile("a", "bar", measure="distinct_customers", follow_filters=True)],
            date_control=CTRL, filters=flt)
    assert f[0]["rule"] == "binding"


def test_presets_respect_binding_filters(model):
    t = tile("a", measure={"name": "distinct_customers", "filters": [
        {"dimension": "rolls.status", "op": "=", "values": ["ok"]}]})
    es = run(model, [t], date_control=CTRL)
    assert "ok" in es[1]["sql"] and es[1]["request"]["filters"][0]["values"] == ["ok"]


def test_unknown_measure_is_not_modeled_finding(model):
    f = err(model, [tile("a", measure="nope")], date_control=CTRL)
    assert f[0]["rule"] == "not_modeled" and f[0]["path"] == "tiles[0].measure"
    t = {"id": "t", "type": "table", "title": "x", "measures": {"names": ["kg_in", "nope"]}}
    f = err(model, [t])
    assert f[0]["path"] == "tiles[0].measures"


def test_invalid_request_becomes_measure_finding(model):
    f = err(model, [tile("a", "ranked_bar", date="fixed", measure={
        "name": "kg_in", "by": ["nope.nothing"]})])
    assert f[0]["rule"] == "measure"


def test_naive_utc_passes_through(model):
    t = tile("a", measure="events_amount")
    (a,) = ds.compile_measures(spec([t], date_control=CTRL), model)
    (b,) = ds.compile_measures(spec([t], date_control=CTRL), model, naive_utc=True)
    assert "AT TIME ZONE 'UTC'" not in a["sql"] and "AT TIME ZONE 'UTC'" in b["sql"]


def test_v1_compile_measures_unchanged(model):
    v1 = ds.parse({"spec_version": 1, "title": "T", "layout": {"rows": [["a"]]}, "tiles": [{
        "id": "a", "type": "line", "title": "x",
        "measure": {"name": "kg_in", "grain": "month", "window": {"last_periods": 3}}}]})
    (e,) = ds.compile_measures(v1, model)
    assert set(e["request"]) == {"measure", "by", "grain", "filters", "window_days"}
    assert e["sql"].startswith("SELECT * FROM (")
    assert e["sql"].rstrip().endswith('ORDER BY "metric_time__month"')
