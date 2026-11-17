"""Phase 22 plan 08 task 1: dashboarddesign.design_lint and wireframe (pure, one test per rule)."""
import pytest

from app import dashboarddesign as dd
from app import dashboardspec as ds

Q = {"id": "q1", "sql": "SELECT 1 AS a, 2 AS b", "refresh_minutes": 30}
DC = {"default": "last_7_days"}


def spec(tiles, **kw):
    out = {"spec_version": 2, "title": "T", "queries": [Q],
           "sections": [{"tiles": [t["id"] for t in tiles]}], "tiles": tiles}
    out.update(kw)
    return ds.SpecV2.model_validate(out)


def stat(tid="s", **kw):
    t = {"id": tid, "type": "stat", "title": "Total this month", "query": "q1", "column": "a"}
    t.update(kw)
    return t


def ctl(tid="c", typ="stat", **kw):
    t = {"id": tid, "type": typ, "title": "Kg", "date": "control",
         "measure": {"name": "kg_in"}}
    t.update(kw)
    return t


def chart(tid="ch", typ="line", y=None, **kw):
    t = {"id": tid, "type": typ, "title": "Trend last 30 days", "query": "q1", "x": "a",
         "y": y or [{"column": "b"}]}
    t.update(kw)
    return t


def lint(sp, stored=None, queries=None, **kw):
    return dd.design_lint(sp, stored or {}, queries or [], **kw)


def rules(found):
    return [f["rule"] for f in found]


def test_v1_spec_gets_nothing():
    assert dd.design_lint(object(), {}, []) == []


def test_finding_shape():
    f = lint(spec([stat(compare="previous_row")]))[0]
    assert set(f) == {"rule", "path", "message", "level"} and f["level"] == "warning"


def test_good_direction_missing_warns():
    assert "design_good_direction" in rules(lint(spec([stat(compare="previous_row")])))


def test_good_direction_controlled_default_comparison_warns():
    assert "design_good_direction" in rules(lint(spec([ctl()], date_control=DC)))


def test_good_direction_explicit_none_is_fine():
    sp = spec([stat(compare="previous_row", good_direction="none")])
    assert "design_good_direction" not in rules(lint(sp))


def test_good_direction_table_delta():
    t = {"id": "t", "type": "table", "title": "T last week", "query": "q1",
         "columns": [{"column": "a", "delta": True}, {"column": "b"}]}
    found = [f for f in lint(spec([t])) if f["rule"] == "design_good_direction"]
    assert [f["path"] for f in found] == ["tiles[0].columns[0].good_direction"]


def test_series_more_than_four_y():
    ys = [{"column": c} for c in "abcde"]
    assert "design_series" in rules(lint(spec([chart(y=ys)])))


def test_series_four_y_ok():
    ys = [{"column": c} for c in "abcd"]
    assert "design_series" not in rules(lint(spec([chart(y=ys)])))


def test_series_distinct_sample_values():
    sp = spec([chart(series="s")])
    stored = {"tiles": [{"id": "ch", "qid": "q1"}]}
    rows = [{"s": str(i)} for i in range(5)]
    found = lint(sp, stored, samples={"rows": {"q1": rows}})
    assert "design_series" in rules(found)


def donut(**kw):
    t = {"id": "d", "type": "donut", "title": "Mix last month", "query": "q1", "x": "a",
         "y": [{"column": "b"}]}
    t.update(kw)
    return t


def test_donut_parts_over_five():
    stored = {"tiles": [{"id": "d", "qid": "q1"}]}
    found = lint(spec([donut()]), stored, samples={"counts": {"q1": 8}})
    hit = [f for f in found if f["rule"] == "design_donut_parts"]
    assert hit and "ranked_bar" in hit[0]["message"]


def test_donut_five_parts_ok():
    stored = {"tiles": [{"id": "d", "qid": "q1"}]}
    assert "design_donut_parts" not in rules(
        lint(spec([donut()]), stored, samples={"counts": {"q1": 5}}))


def combo(**kw):
    t = {"id": "cb", "type": "combo", "title": "Mix last 30 days", "date": "control",
         "measures": {"names": ["kg_in", "revenue"]}}
    t.update(kw)
    return t


def test_combo_units_differ():
    found = lint(spec([combo()], date_control=DC), units={"kg_in": "kg", "revenue": "USD"})
    assert "design_combo_units" in rules(found)


def test_combo_same_unit_ok():
    found = lint(spec([combo()], date_control=DC), units={"kg_in": "kg", "revenue": "kg"})
    assert "design_combo_units" not in rules(found)


def test_combo_unknown_unit_ok():
    assert "design_combo_units" not in rules(
        lint(spec([combo()], date_control=DC), units={"kg_in": "kg"}))


def test_title_period_missing():
    assert "design_title_period" in rules(lint(spec([stat(title="Revenue")])))


@pytest.mark.parametrize("title", ["Revenue today", "Kg last 30 days", "Orders FY25",
                                   "Weekly total", "Sales since April", "Top 10 parts"])
def test_title_period_present(title):
    assert "design_title_period" not in rules(lint(spec([stat(title=title)])))


def test_title_period_ignores_controlled_tiles():
    sp = spec([ctl(title="Revenue")], date_control=DC)
    assert "design_title_period" not in rules(lint(sp))


def wide(width=None):
    t = {"id": "t", "type": "table", "title": "Parts last week", "query": "q1",
         "columns": [{"column": c} for c in "abcdefg"]}
    if width:
        t["width"] = width
    return t


def test_wide_table_half_warns():
    assert "design_wide_table" in rules(lint(spec([wide("half")])))


def test_wide_table_full_or_default_ok():
    assert "design_wide_table" not in rules(lint(spec([wide("full")])))
    assert "design_wide_table" not in rules(lint(spec([wide()])))


def test_distinct_custom_controlled_tile():
    qs = [{"id": "m_c_p", "request": {"kind": "presets", "tile": "c"}}]
    found = lint(spec([ctl()], date_control=DC), queries=qs)
    hit = [f for f in found if f["rule"] == "design_distinct_custom"]
    assert hit and "Not available" in hit[0]["message"]


def test_distinct_custom_absent_without_presets_query():
    assert "design_distinct_custom" not in rules(lint(spec([ctl()], date_control=DC)))


def with_sql(sql):
    return ds.SpecV2.model_validate({
        "spec_version": 2, "title": "T", "queries": [dict(Q, sql=sql)],
        "sections": [{"tiles": ["s"]}], "tiles": [stat()]})


def test_raw_sql_names_the_measure():
    sp = with_sql('SELECT sum("weight") AS a FROM public.lots')
    found = lint(sp, measure_sql={"kg_in": ("sum", "weight", "public.lots")})
    hit = [f for f in found if f["rule"] == "design_raw_sql"]
    assert hit and "kg_in" in hit[0]["message"]


def test_raw_sql_other_table_not_flagged():
    sp = with_sql("SELECT sum(weight) AS a FROM other")
    assert "design_raw_sql" not in rules(
        lint(sp, measure_sql={"kg_in": ("sum", "weight", "public.lots")}))


def test_owed_on_sales_total():
    found = lint(spec([stat(title="Sales this month", owed_when={"op": ">", "value": 0})]))
    hit = [f for f in found if f["rule"] == "design_owed"]
    assert hit and "owes an action" in hit[0]["message"]


def test_owed_on_overdue_ok():
    found = lint(spec([stat(title="Overdue invoices today", owed_when={"op": ">", "value": 0})]))
    assert "design_owed" not in rules(found)


def test_tone_on_single_series():
    sp = spec([chart(y=[{"column": "b", "tone": "inward"}])])
    assert "design_tone" in rules(lint(sp))


def test_tone_on_multi_series_ok():
    sp = spec([chart(y=[{"column": "a", "tone": "inward"}, {"column": "b"}])])
    assert "design_tone" not in rules(lint(sp))


def test_tone_pair_without_note():
    ys = [{"column": "a", "tone": "accent"}, {"column": "b", "tone": "outward"}]
    assert "design_tone_pair" in rules(lint(spec([chart(y=ys)])))
    assert "design_tone_pair" not in rules(lint(spec([chart(y=ys, note="Blue in, gold out")])))


def test_horizon_this_fy_needs_732():
    t = ctl(comparison="none", good_direction="up")
    sp = spec([t], date_control={"default": "this_fy", "presets": ["this_fy"],
                                 "horizon_days": 400})
    assert "design_horizon" in rules(lint(sp))
    sp = spec([t], date_control={"default": "this_fy", "presets": ["this_fy"],
                                 "horizon_days": 732})
    assert "design_horizon" not in rules(lint(sp))


def test_horizon_last_year_with_long_presets():
    t = ctl(comparison="last_year", good_direction="up")
    sp = spec([t], date_control={"default": "last_7_days",
                                 "presets": ["last_7_days", "last_90_days"],
                                 "horizon_days": 400})
    assert "design_horizon" in rules(lint(sp))
    sp = spec([t], date_control={"default": "last_7_days", "presets": ["last_7_days"],
                                 "horizon_days": 400})
    assert "design_horizon" not in rules(lint(sp))


def test_last_year_on_a_split_or_filtered_tile_warns_about_90_days():
    dc = {"default": "last_7_days", "presets": ["last_7_days"], "horizon_days": 400}

    def tile_warnings(t, **kw):
        sp = spec([t], date_control=dc, **kw)
        return [f for f in lint(sp) if f["rule"] == "design_horizon" and f["path"].startswith("tiles[")]

    plain = ctl(comparison="last_year", good_direction="up")
    assert not tile_warnings(plain)
    filtered = ctl(comparison="last_year", good_direction="up", follow_filters=True)
    assert tile_warnings(filtered, filters=[{"id": "b", "label": "Branch", "dimension": "branches.name"}])
    assert not tile_warnings(filtered)  # no page filter, nothing narrows the tile
    ghost = ctl(typ="line", ghost="last_year", measure={"name": "kg_in", "by": ["customers.name"]})
    assert tile_warnings(ghost)
    assert not tile_warnings(ctl(typ="line", ghost="last_year"))


def test_text_tile_with_a_date_control_does_not_break_lint():
    text = {"id": "t", "type": "text", "text": "Notes for the reader."}
    sp = spec([text, ctl(comparison="last_year", good_direction="up")], date_control=DC)
    lint(sp)  # AttributeError on the text tile's missing date before the fix
    assert isinstance(lint(spec([text, ctl()], date_control=DC)), list)


def test_unused_control():
    assert "design_unused_control" in rules(lint(spec([stat()], date_control=DC)))
    assert "design_unused_control" not in rules(lint(spec([ctl()], date_control=DC)))


FLT = [{"id": "f", "dimension": "lots.supplier", "label": "Supplier"}]


def test_unused_filter():
    assert "design_unused_filter" in rules(lint(spec([ctl()], filters=FLT, date_control=DC)))
    followed = spec([ctl(follow_filters=True)], filters=FLT, date_control=DC)
    assert "design_unused_filter" not in rules(lint(followed))


def test_naive_time_quotes_the_note():
    note = "time column public.events.happened_at has no time zone; it is read as UTC"
    found = lint(spec([stat()]), notes=[note, "something else"])
    hit = [f for f in found if f["rule"] == "design_naive_time"]
    assert len(hit) == 1 and note in hit[0]["message"]


def test_filter_over_fifty_values_is_an_error():
    sp = spec([ctl(follow_filters=True)], filters=FLT, date_control=DC)
    err = [f for f in lint(sp, samples={"filters": {"f": 51}}) if f["level"] == "error"]
    assert [f["rule"] for f in err] == ["filters"] and "51" in err[0]["message"]
    assert not [f for f in lint(sp, samples={"filters": {"f": 50}}) if f["level"] == "error"]


def test_size_error_rows_and_bytes():
    sp = spec([stat()])
    qs = [{"id": "q1"}]
    over = lint(sp, queries=qs, samples={"counts": {"q1": 5000}, "bytes": {"q1": 10}},
                caps=(4000, 10 ** 9))
    assert [f["rule"] for f in over if f["level"] == "error"] == ["size"]
    big = lint(sp, queries=qs, samples={"counts": {"q1": 100}, "bytes": {"q1": 5000}},
               caps=(4000, 100000))
    assert [f["rule"] for f in big if f["level"] == "error"] == ["size"]
    ok = lint(sp, queries=qs, samples={"counts": {"q1": 100}, "bytes": {"q1": 50}},
              caps=(4000, 100000))
    assert not [f for f in ok if f["level"] == "error"]


def test_rules_list_matches_functions():
    assert len(dd.RULES) == 15 and len({r.__name__ for r in dd.RULES}) == 15


# ---- wireframe

STORED = {
    "title": "Plant", "number_locale": "en-IN",
    "date_control": {"default": "last_7_days", "horizon_days": 400},
    "sections": [{"title": "Today", "tiles": ["a", "b", "c", "d"]},
                 {"tiles": ["e"]}],
    "tiles": [{"id": "a", "type": "stat", "qid": "m_a"},
              {"id": "b", "type": "stat", "qid": "m_a"},
              {"id": "c", "type": "combo", "title": "Flow", "qid": "m_c"},
              {"id": "d", "type": "text"},
              {"id": "e", "type": "table", "title": "Parts", "qid": "q1", "width": "full"}],
}
QS = [{"id": "m_a", "request": {"follow": "control"}}, {"id": "m_c", "request": {"follow": "now"}},
      {"id": "q1"}]


def test_wireframe_lines():
    lines = dd.wireframe(STORED, QS).split("\n")
    assert lines[0] == ("Page 'Plant': date control default last_7_days, horizon 400 days; "
                        "locale en-IN")
    assert lines[1].startswith("Section 1 'Today': 4 tiles - 2 stats (2-up on phones)")
    assert "combo 'Flow' (half, now)" in lines[1]
    assert lines[2] == "Section 2 '': 1 tiles - table 'Parts' (full, fixed)"


def test_wireframe_follows_control_and_cap():
    stored = dict(STORED, tiles=[dict(t) for t in STORED["tiles"]])
    stored["tiles"][2]["qid"] = "m_a"
    assert "(half, follows control)" in dd.wireframe(stored, QS)
    big = dict(STORED, sections=[{"title": "x" * 50, "tiles": ["c"]} for _ in range(60)])
    assert len(dd.wireframe(big, QS)) <= dd.WIREFRAME_CAP


def test_series_measure_bound_line_counts_stored_y():
    names = ["a1", "a2", "a3", "a4", "a5"]
    t = {"id": "l", "type": "line", "title": "Kg", "date": "control",
         "measures": {"names": names}}
    stored = {"tiles": [{"id": "l", "qid": "m1", "y": [{"column": n} for n in names],
                         "series": None}]}
    assert "design_series" in rules(lint(spec([t]), stored))
    stored["tiles"][0]["y"] = stored["tiles"][0]["y"][:4]
    assert "design_series" not in rules(lint(spec([t]), stored))


def test_series_controlled_bar_by_becomes_series():
    t = ctl("b", "bar", measure={"name": "kg_in", "by": ["customers.name"]})
    stored = {"tiles": [{"id": "b", "qid": "m1", "y": [{"column": "kg_in"}],
                         "series": "customers__name"}]}
    rows = [{"customers__name": str(i), "kg_in": 1} for i in range(5)]
    assert "design_series" in rules(lint(spec([t]), stored, samples={"rows": {"m1": rows}}))


def test_donut_controlled_counts_members_not_rows():
    stored = {"tiles": [{"id": "d", "qid": "q1", "x": "a"}]}
    qs = [{"id": "q1", "request": {"follow": "control"}}]
    samples = {"counts": {"q1": 270}, "members": {"q1": 3}}
    assert "design_donut_parts" not in rules(lint(spec([donut()]), stored, qs, samples=samples))
    samples["members"]["q1"] = 7
    assert "design_donut_parts" in rules(lint(spec([donut()]), stored, qs, samples=samples))


def test_donut_fixed_top_five_plus_other_is_fine():
    stored = {"tiles": [{"id": "d", "qid": "q1", "x": "a"}]}
    qs = [{"id": "q1", "request": {"follow": "fixed", "other": True}}]
    assert "design_donut_parts" not in rules(
        lint(spec([donut()]), stored, qs, samples={"counts": {"q1": 6}}))
