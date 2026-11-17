"""Phase 22 plan 06 task 2: bind_v2 and render_spec_v2 (the C-02 render form)."""
import copy
import json

import pytest

from app import dashboardspec as ds
from tests.test_compiler_tiles import build

CTRL = {"default": "last_7_days"}
Q1 = {"id": "q1", "sql": "SELECT 1 AS a, 2 AS b, 3 AS c", "refresh_minutes": 60}


@pytest.fixture(scope="module")
def model():
    return build()


def mk(tiles, sections=None, **kw):
    ids = [t["id"] for t in tiles]
    out = {"spec_version": 2, "title": "T", "sections": sections or [{"tiles": ids}],
           "tiles": tiles}
    out.update(kw)
    return out


def tile(tid, typ="stat", date="control", measure="kg_in", **kw):
    t = {"id": tid, "type": typ, "title": "Tile", "date": date,
         "measure": measure if isinstance(measure, dict) else {"name": measure}}
    t.update(kw)
    return t


def multi(tid, typ, names, **kw):
    t = {"id": tid, "type": typ, "title": "Tile", "date": "control",
         "measures": {"names": names}}
    t.update(kw)
    return t


def build_all(model, raw, declared=()):
    spec = ds.parse(raw)
    compiled = ds.compile_measures(spec, model)
    stored = ds.bind(spec, list(declared), compiled)
    qs = ds.stored_queries(list(declared), compiled, ds.MAX_QUERIES_V2)
    return spec, stored, qs


def bound(model, tiles, **kw):
    _, stored, qs = build_all(model, mk(tiles, **kw))
    return stored, qs


def findings(model, tiles, declared=(), **kw):
    with pytest.raises(ds.SpecError) as e:
        build_all(model, mk(tiles, **kw), declared)
    return e.value.findings


def has(found, rule, frag="", path=None):
    return any(f["rule"] == rule and frag in f["message"] and (path is None or f["path"] == path)
               for f in found)


# ---------------------------------------------------------------- bind defaults

def test_controlled_ranked_bar_x_is_member_key(model):
    stored, _ = bound(model, [tile("r", "ranked_bar", measure={
        "name": "kg_in", "by": ["customers.name"]})], date_control=CTRL)
    t = stored["tiles"][0]
    assert t["x"] == "customers__name" and t["y"] == [{"column": "kg_in", "dashed": False}]
    assert t["qid"] == "m_r"


@pytest.mark.parametrize("typ", ["donut", "aging", "funnel"])
def test_member_types_use_by_column_under_grain(model, typ):
    stored, _ = bound(model, [tile("r", typ, date="fixed", measure={
        "name": "kg_in", "by": ["customers.name"]})])
    assert stored["tiles"][0]["x"] == "customers__name"


def test_member_type_without_by_is_a_binding_finding(model):
    f = findings(model, [tile("r", "donut")], date_control=CTRL)
    assert has(f, "binding", "by dimension", "tiles[0].measure.by")


def test_line_under_control_gets_time_key_and_series(model):
    stored, _ = bound(model, [tile("l", "line", measure={
        "name": "kg_in", "by": ["customers.name"]})], date_control=CTRL)
    t = stored["tiles"][0]
    assert t["x"] == "metric_time__day" and t["series"] == "customers__name"


def test_fixed_bar_without_grain_uses_by_for_x_and_second_by_for_series(model):
    stored, _ = bound(model, [tile("b", "bar", date="fixed", measure={
        "name": "kg_in", "by": ["customers.name"]})])
    t = stored["tiles"][0]
    assert t["x"] == "customers__name" and "series" not in t


def test_stat_gets_column_and_time_key(model):
    stored, _ = bound(model, [tile("s")], date_control=CTRL)
    t = stored["tiles"][0]
    assert t["column"] == "kg_in" and t["x"] == "metric_time__day"


def test_fixed_stat_without_grain_has_no_x(model):
    stored, _ = bound(model, [tile("s", date="fixed")])
    assert "x" not in stored["tiles"][0]


def test_sparkline_and_bullet_defaults(model):
    stored, _ = bound(model, [tile("sp", "sparkline"),
                              tile("bu", "bullet", target={"value": 10})], date_control=CTRL)
    sp, bu = stored["tiles"]
    assert sp["x"] == "metric_time__day" and sp["y"][0]["column"] == "kg_in"
    assert bu["column"] == "kg_in"


def test_multi_measure_y_one_ref_per_name_and_combo_defaults(model):
    stored, _ = bound(model, [
        multi("l", "line", ["kg_in", "kg_out"]),
        multi("c", "combo", ["kg_in", "kg_out", "rolls_in"]),
        multi("p", "paired_bar", ["rolls_in", "dispatches"])], date_control=CTRL)
    l, c, p = stored["tiles"]
    assert [y["column"] for y in l["y"]] == ["kg_in", "kg_out"]
    assert [(y["column"], y["as"]) for y in c["y"]] == [
        ("kg_in", "bar"), ("kg_out", "line"), ("rolls_in", "line")]
    assert [y["column"] for y in p["y"]] == ["rolls_in", "dispatches"]
    assert l["x"] == "metric_time__day"


def test_paired_bar_needs_exactly_two_names(model):
    f = findings(model, [multi("p", "paired_bar", ["kg_in", "kg_out", "rolls_in"])],
                 date_control=CTRL)
    assert has(f, "binding", "exactly two")


def test_combo_over_one_measure_is_a_binding_finding(model):
    f = findings(model, [tile("c", "combo")], date_control=CTRL)
    assert has(f, "binding", "combo")


def test_measures_refused_on_other_types(model):
    f = findings(model, [multi("s", "stat", ["kg_in", "kg_out"])], date_control=CTRL)
    assert has(f, "binding", "not measures", "tiles[0].measures")
    f = findings(model, [multi("s", "donut", ["kg_in", "kg_out"])], date_control=CTRL)
    assert has(f, "binding")


def test_table_default_columns_are_keys_plus_names_never_parts(model):
    stored, qs = bound(model, [multi("t", "table", ["rolls_in", "avg_kg", "return_rate"])],
                       date_control=CTRL)
    cols = [c["column"] for c in stored["tiles"][0]["columns"]]
    assert cols == ["metric_time__day", "rolls_in", "avg_kg", "return_rate"]
    assert "avg_kg__sum" in qs[0]["columns"]


def test_fixed_table_default_columns_include_by(model):
    stored, _ = bound(model, [tile("t", "table", date="fixed", measure={
        "name": "kg_in", "by": ["customers.name", "rolls.status"]})])
    cols = [c["column"] for c in stored["tiles"][0]["columns"]]
    assert cols == ["customers__name", "rolls__status", "kg_in"]


def test_unknown_column_gives_columns_finding_with_valid_names(model):
    f = findings(model, [tile("l", "line", x="nope", measure={"name": "kg_in"})],
                 date_control=CTRL)
    assert has(f, "columns", "valid: metric_time__day")
    f = findings(model, [tile("t", "table", date="fixed", measure="kg_in",
                              columns=[{"column": "zzz"}])])
    assert has(f, "columns", "zzz")
    f = findings(model, [tile("t", "table", date="fixed", measure="kg_in",
                              sort={"column": "zzz"})])
    assert has(f, "columns", "zzz")
    f = findings(model, [tile("t", "bullet", measure="kg_in", target={"column": "zzz"})],
                 date_control=CTRL)
    assert has(f, "columns", "zzz")


def test_query_bound_tiles_check_columns_and_default_table_columns(model):
    t = {"id": "q", "type": "table", "title": "Q", "query": "q1"}
    _, stored, qs = build_all(model, mk([t], queries=[Q1]), [Q1])
    assert [c["column"] for c in stored["tiles"][0]["columns"]] == ["a", "b", "c"]
    bad = {"id": "d", "type": "donut", "title": "D", "query": "q1", "x": "a", "y": [
        {"column": "zz"}]}
    f = findings(model, [bad], declared=[Q1], queries=[Q1])
    assert has(f, "columns", "zz")


# ---------------------------------------------------------------- render form

def render(model, tiles, **kw):
    stored, qs = bound(model, tiles, **kw)
    return ds.render_spec(stored, qs), stored, qs


def test_render_keys_and_defaults(model):
    r, _, _ = render(model, [tile("s")], date_control=CTRL, subtitle="Sub")
    assert set(r) == {"render_version", "title", "subtitle", "number_locale", "timezone",
                      "date_control", "filters", "sections", "tiles", "refresh"}
    assert r["render_version"] == 2 and r["timezone"] is None
    assert r["date_control"] == {"default": "last_7_days", "presets": list(ds.PRESETS),
                                 "horizon_days": 400, "fy_start_month": 4}
    t = r["tiles"][0]
    assert t["width"] == "quarter" and t["follow"] == "control" and t["horizon_days"] == 400
    assert t["presets_qid"] is None and t["follow_filters"] is False
    assert t["measures"][0]["column"] == "kg_in" and t["measures"][0]["label"] == "Kg in"
    assert t["measures"][0]["description"] == "Kilos received"
    assert t["measures"][0]["date_basis"] == "weigh date"
    assert t["by_columns"] == [] and r["refresh"] == {"m_s": 15}


def test_no_date_control_renders_null(model):
    r, _, _ = render(model, [tile("s", date="fixed")])
    assert r["date_control"] is None and r["tiles"][0]["follow"] == "fixed"
    assert r["tiles"][0]["horizon_days"] is None


def test_horizon_for_by_tile_is_90_and_plain_is_400(model):
    r, _, _ = render(model, [tile("s"), tile("b", "bar", measure={
        "name": "kg_in", "by": ["customers.name"]})], date_control=CTRL)
    assert [t["horizon_days"] for t in r["tiles"]] == [400, 90]


def test_now_tile_follows_now_with_null_horizon(model):
    r, _, _ = render(model, [tile("n", measure="items_qty")], date_control=CTRL)
    t = r["tiles"][0]
    assert t["follow"] == "now" and t["horizon_days"] is None
    assert t["measures"][0]["kind"] == "now" and t["measures"][0]["date_basis"] is None


def test_presets_qid_and_refresh_include_presets_query(model):
    r, _, qs = render(model, [tile("d", measure="distinct_customers")], date_control=CTRL)
    assert r["tiles"][0]["presets_qid"] == "m_d_p"
    assert set(r["refresh"]) == {"m_d", "m_d_p"}
    assert [q["id"] for q in qs] == ["m_d", "m_d_p"]


def test_by_columns_values(model):
    flt = [{"id": "ch", "dimension": "rolls.status", "label": "Status"}]
    r, _, qs = render(model, [
        tile("f", "table", date="fixed", measure={
            "name": "kg_in", "by": ["customers.name", "rolls.status"]}),
        tile("c", "table", follow_filters=True, measure={
            "name": "kg_in", "by": ["customers.name"]}),
        tile("n", "stat", date="fixed")], date_control=CTRL, filters=flt)
    f, c, n = r["tiles"]
    assert f["by_columns"] == ["customers__name", "rolls__status"]
    assert c["by_columns"] == ["customers__name"]
    assert "rolls__status" in next(q for q in qs if q["id"] == "m_c")["columns"]
    assert n["by_columns"] == []
    assert r["filters"] == [{"id": "ch", "label": "Status", "column": "rolls__status"}]


def test_query_bound_tile_render(model):
    t = {"id": "q", "type": "table", "title": "Q", "query": "q1"}
    _, stored, qs = build_all(model, mk([t], queries=[Q1]), [Q1])
    r = ds.render_spec(stored, qs)["tiles"][0]
    assert r["by_columns"] == [] and r["measures"] == [] and r["follow"] == "fixed"
    assert r["qid"] == "q1" and r["presets_qid"] is None and r["horizon_days"] is None


def test_hidden_tiles_removed_and_empty_sections_dropped(model):
    tiles = [tile("a"), tile("b", hidden=True), tile("c", measure="kg_out")]
    secs = [{"title": "One", "tiles": ["a", "b"]}, {"title": "Two", "tiles": ["c"]}]
    r, _, _ = render(model, tiles, sections=secs, date_control=CTRL)
    assert [t["id"] for t in r["tiles"]] == ["a", "c"]
    assert r["sections"][0]["tiles"] == ["a"]
    tiles2 = [tile("a"), tile("b", hidden=True)]
    r, _, _ = render(model, tiles2, sections=[{"tiles": ["a"]}, {"tiles": ["b"]}],
                     date_control=CTRL)
    assert len(r["sections"]) == 1 and set(r["refresh"]) == {"m_a"}


def test_render_leaks_no_sql_binding_window_or_filter_values(model):
    secret = "ZZ-secret-value"
    t = tile("a", "bar", measure={"name": "kg_in", "by": ["customers.name"], "filters": [
        {"dimension": "rolls.status", "op": "=", "values": [secret]}]})
    r, _, _ = render(model, [t, tile("s", date="fixed", measure={
        "name": "kg_in", "window": {"last_days": 30}})], date_control=CTRL)
    out = json.dumps(r)
    for bad in ("sql", "SELECT", "window", "names", secret, "last_days", "filters\": [{\"dim"):
        assert bad not in out, bad
    for tl in r["tiles"]:
        assert "measure" not in tl and "query" not in tl


def test_text_tile_and_widths(model):
    tiles = [{"id": "x", "type": "text", "text": "hi"}, tile("t", "table", date="fixed"),
             tile("s", date="fixed", width="third")]
    r, _, _ = render(model, tiles)
    assert [t["width"] for t in r["tiles"]] == ["full", "full", "third"]
    assert r["tiles"][0]["text"] == "hi" and r["tiles"][0]["qid"] is None


def test_daily_operations_like_spec(model):
    tiles = [
        tile("s_rolls", measure="rolls_in", note="Rolls weighed"),
        tile("s_kg", measure="kg_in", comparison="both", good_direction="up"),
        tile("s_dist", measure="distinct_customers"),
        tile("s_stock", measure="stock_kg"),
        multi("flow", "paired_bar", ["rolls_in", "dispatches"], mirror=True),
        multi("daily", "table", ["rolls_in", "kg_in", "dispatches", "kg_out", "return_rate"],
              totals=True, columns=[
                  {"column": "metric_time__day", "label": "Day"}, {"column": "rolls_in"},
                  {"column": "kg_in", "delta": True}, {"column": "dispatches"},
                  {"column": "kg_out"}, {"column": "return_rate", "format": "percent"}]),
        {"id": "txt", "type": "text", "text": "Counts follow their own dates."},
        tile("hid", measure="kg_out", hidden=True),
    ]
    raw = mk(tiles, date_control=CTRL)
    spec, stored, qs = build_all(model, raw)
    r = ds.render_spec(stored, qs)
    assert [t["id"] for t in r["tiles"]] == ["s_rolls", "s_kg", "s_dist", "s_stock", "flow",
                                             "daily", "txt"]
    daily = next(t for t in r["tiles"] if t["id"] == "daily")
    assert {m["date_basis"] for m in daily["measures"]} == {"weigh date", "dispatch date"}
    dq = next(q for q in qs if q["id"] == daily["qid"])
    assert dq["columns"][0] == "metric_time__day" and "return_rate__num" in dq["columns"]
    assert len(qs) <= ds.MAX_QUERIES_V2
    assert next(t for t in r["tiles"] if t["id"] == "s_dist")["presets_qid"]
    # the hidden tile still has its query so unhide needs no recompile
    assert any(q["id"] == "m_hid" for q in qs)


# ---------------------------------------------------------------- round trip

def roundtrip(model, raw, declared=()):
    spec, stored, qs = build_all(model, raw, declared)
    form = ds.authoring_form(stored)
    assert "by_columns" not in json.dumps(form)
    spec2 = ds.parse(copy.deepcopy(form))
    assert isinstance(spec2, ds.SpecV2)
    stored2 = ds.bind(spec2, list(declared), ds.compile_measures(spec2, model))
    assert stored2 == stored
    return stored


def test_round_trip_for_every_shape(model):
    tiles = [
        tile("s"), tile("sp", "sparkline"), tile("bu", "bullet", target={"value": 5}),
        tile("l", "line", ghost="previous_period", measure={
            "name": "kg_in", "by": ["customers.name"]}),
        multi("c", "combo", ["kg_in", "rolls_in"]),
        multi("p", "paired_bar", ["rolls_in", "dispatches"]),
        tile("r", "ranked_bar", measure={"name": "kg_in", "by": ["customers.name"]}),
        tile("f", "stacked_bar", date="fixed", measure={
            "name": "kg_in", "grain": "month", "by": ["customers.name"]}),
        multi("t", "table", ["rolls_in", "kg_in"]),
        {"id": "x", "type": "text", "text": "hi"},
    ]
    roundtrip(model, mk(tiles, date_control=CTRL))
    t = {"id": "q", "type": "table", "title": "Q", "query": "q1"}
    roundtrip(model, mk([t], queries=[Q1]), [Q1])


def test_by_columns_only_in_render_form(model):
    _, stored, qs = build_all(model, mk([tile("b", "bar", measure={
        "name": "kg_in", "by": ["customers.name"]})], date_control=CTRL))
    assert "by_columns" not in json.dumps(stored)
    assert ds.render_spec(stored, qs)["tiles"][0]["by_columns"] == ["customers__name"]
    assert ds._by_columns_v2({"by": ["customers.name", "channels.name"]}) \
        == ["customers__name", "channels__name"]
    assert ds._by_columns_v2({}) == []


def test_v1_render_and_stored_unchanged(model):
    raw = {"spec_version": 1, "title": "T", "layout": {"rows": [["a"]]}, "tiles": [{
        "id": "a", "type": "line", "title": "x",
        "measure": {"name": "kg_in", "grain": "month", "window": {"last_periods": 3}}}]}
    spec = ds.parse(raw)
    comp = ds.compile_measures(spec, model)
    stored = ds.bind(spec, [], comp)
    r = ds.render_spec(stored, comp)
    assert r["render_version"] == 1 and "sections" not in r and r["rows"] == [["a"]]
    assert stored["tiles"][0]["x"] == "metric_time__month"
    assert ds.stored_queries([], comp) == comp


def test_other_row_flag_follows_top_and_other(model):
    r, _, _ = render(model, [
        tile("a", "table", date="fixed", measure={"name": "kg_in", "by": ["customers.name"],
                                                    "top": 3, "other": True}),
        tile("b", "table", date="fixed", measure={"name": "kg_in", "by": ["customers.name"], "top": 3}),
        tile("c", "stat", date="fixed")], date_control=CTRL)
    a, b, c = r["tiles"]
    assert a["other_row"] is True and b["other_row"] is False and c["other_row"] is False
    assert a["other_top"] == 3 and b["other_top"] is None and c["other_top"] is None
