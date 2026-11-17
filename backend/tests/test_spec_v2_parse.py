"""Phase 22 (DCAP-01..04, 09..12): the spec v2 contract of dashboardspec.parse."""
import copy
import json

import pytest

from app import dashboardspec as ds

CONTROL = {"default": "last_7_days"}


def text(tid="a"):
    return {"id": tid, "type": "text", "text": "x"}


def spec(tiles=None, **kw):
    tiles = tiles if tiles is not None else [text()]
    ids = [t["id"] for t in tiles]
    out = {"spec_version": 2, "title": "T",
           "sections": [{"tiles": ids[i:i + 16]} for i in range(0, len(ids), 16)],
           "tiles": tiles}
    out.update(kw)
    return out


def m_tile(tid="m", typ="stat", **kw):
    t = {"id": tid, "type": typ, "title": "Tile", "measure": {"name": "tons"}}
    t.update(kw)
    return t


def findings(obj):
    with pytest.raises(ds.SpecError) as e:
        ds.parse(obj)
    return e.value.findings


def has(found, rule, frag="", path=None):
    return any(f["rule"] == rule and frag in f["message"] and (path is None or f["path"] == path)
               for f in found)


def ctl(**kw):
    return spec([m_tile(date="control", **kw)], date_control=copy.deepcopy(CONTROL))


# ------------------------------------------------------------------ basics and dispatch

def test_minimal_v2_parses_to_specv2():
    assert isinstance(ds.parse(spec()), ds.SpecV2)


def test_parse_accepts_json_text():
    assert isinstance(ds.parse(json.dumps(spec())), ds.SpecV2)


def test_v1_still_parses_to_spec():
    v1 = {"spec_version": 1, "title": "T", "layout": {"rows": [["a"]]}, "tiles": [text()]}
    assert isinstance(ds.parse(v1), ds.Spec)


def test_v1_refuses_v2_keys():
    v1 = {"spec_version": 1, "title": "T", "layout": {"rows": [["a"]]}, "tiles": [text()],
          "sections": [{"tiles": ["a"]}]}
    assert has(findings(v1), "spec", path="sections")
    v1.pop("sections")
    v1["number_locale"] = "en-IN"
    assert has(findings(v1), "spec", path="number_locale")


def test_v2_refuses_layout_and_unknown_keys_at_depth():
    assert has(findings(spec(layout={"rows": [["a"]]})), "spec", path="layout")
    t = text()
    t["colour"] = "red"
    assert has(findings(spec([t])), "spec", path="tiles[0].colour")
    s = spec()
    s["sections"][0]["css"] = "x"
    assert has(findings(s), "spec", path="sections[0].css")


def test_paths_skip_tile_type_tags():
    t = {"id": "c", "type": "combo", "title": "C", "query": "q", "x": "d",
         "y": [{"column": "a", "as": "bar"}], "bogus": 1}
    found = findings(spec([text(), t], queries=[{"id": "q", "sql": "select 1 as d"}]))
    assert has(found, "spec", path="tiles[1].bogus")


# ------------------------------------------------------------------ size and caps

def test_size_v2_boundaries():
    pad = []
    for i in range(35):
        pad.append({"id": f"t{i}", "type": "text", "text": "y" * 1990})
    obj = spec(pad)
    n = ds._size(obj)
    assert 65536 < n < 131072
    assert isinstance(ds.parse(obj), ds.SpecV2)
    assert isinstance(ds.parse(json.dumps(obj)), ds.SpecV2)
    over = json.dumps(spec([text()], subtitle="s"))
    over = over[:-1] + ',"zz":"' + "z" * 131100 + '"}'
    f = findings(over)
    assert f == [{"rule": "size", "path": "spec",
                  "message": f"spec is {len(over.encode())} bytes; at most 131072"}]


def test_v1_size_text_unchanged():
    for n in (70000, 140000):
        raw = json.dumps({"spec_version": 1, "title": "T", "pad": "z" * n})
        f = findings(raw)
        assert f[0]["rule"] == "size" and f[0]["message"].endswith("at most 65536")
        assert "131072" not in f[0]["message"]
    html = "<html>" + "a" * 100000 + "</html>"
    f = findings(html)
    assert f[0]["rule"] == "size" and f[0]["message"].endswith("at most 65536")


def test_string_mentioning_v2_but_not_v2_gets_v1_size_text():
    raw = '<p>"spec_version": 2,</p>' + "a" * 100000
    f = findings(raw)
    assert f[0]["message"].endswith("at most 65536")


def test_tile_cap():
    tiles = [text(f"t{i}") for i in range(41)]
    assert findings(spec(tiles))
    assert isinstance(ds.parse(spec([text(f"t{i}") for i in range(40)])), ds.SpecV2)


def test_chart_cap():
    def chart(i):
        return {"id": f"c{i}", "type": "donut", "title": "D", "query": "q", "x": "a",
                "y": [{"column": "b"}]}
    q = [{"id": "q", "sql": "select 1 as a, 2 as b"}]
    assert isinstance(ds.parse(spec([chart(i) for i in range(12)], queries=q)), ds.SpecV2)
    f = findings(spec([chart(i) for i in range(13)], queries=q))
    assert has(f, "charts", "13 chart tiles; at most 12")


def test_stat_table_text_are_not_charts():
    tiles = [m_tile(f"s{i}") for i in range(13)]
    assert isinstance(ds.parse(spec(tiles)), ds.SpecV2)


def test_query_cap():
    qs = [{"id": f"q{i}", "sql": "select 1 as a"} for i in range(31)]
    assert findings(spec(queries=qs))
    assert isinstance(ds.parse(spec(queries=qs[:30])), ds.SpecV2)


# ------------------------------------------------------------------ sections

def test_section_placement_rules():
    s = spec([text("a"), text("b")])
    s["sections"] = [{"tiles": ["a"]}]
    assert has(findings(s), "layout", "not placed")
    s["sections"] = [{"tiles": ["a", "b"]}, {"tiles": ["a"]}]
    assert has(findings(s), "layout", "twice", path="sections[1].tiles[0]")
    s["sections"] = [{"tiles": ["a", "b", "zz"]}]
    assert has(findings(s), "layout", "unknown", path="sections[0].tiles[2]")


def test_section_and_section_size_caps():
    tiles = [text(f"t{i}") for i in range(17)]
    s = spec(tiles)
    s["sections"] = [{"tiles": [t["id"] for t in tiles]}]
    assert has(findings(s), "layout", "at most 16")
    tiles = [text(f"t{i}") for i in range(11)]
    s = spec(tiles)
    s["sections"] = [{"tiles": [t["id"]]} for t in tiles]
    assert has(findings(s), "layout", "at most 10")


def test_duplicate_tile_ids():
    s = spec([text("a"), text("a")])
    assert has(findings(s), "layout", "duplicate tile id")


# ------------------------------------------------------------------ locale and date control

def test_number_locale():
    for loc in ds.LOCALES:
        assert ds.parse(spec(number_locale=loc)).number_locale == loc
    for bad in ("hi-IN", "en_IN"):
        assert findings(spec(number_locale=bad))


def test_date_control_defaults():
    dc = ds.parse(spec(date_control={})).date_control
    assert (dc.default, dc.presets, dc.horizon_days, dc.fy_start_month) == \
        ("last_7_days", None, 400, 4)


def test_date_control_rules():
    f = findings(spec(date_control={"presets": ["today"]}))
    assert has(f, "binding", "default")
    assert isinstance(ds.parse(spec(date_control={"presets": ["today", "last_7_days"]})),
                      ds.SpecV2)
    for h in (30, 733):
        assert findings(spec(date_control={"horizon_days": h}))
    assert findings(spec(date_control={"presets": ["last_week", "last_7_days"]}))
    assert findings(spec(date_control={"fy_start_month": 13}))


def test_filters_rules():
    flt = [{"id": f"f{i}", "dimension": "t.c", "label": "L"} for i in range(4)]
    assert findings(spec(date_control={}, filters=flt))
    assert isinstance(ds.parse(spec(date_control={}, filters=flt[:3])), ds.SpecV2)
    assert has(findings(spec(filters=flt[:1])), "binding", "date_control")
    dup = [flt[0], dict(flt[1], id="f0")]
    assert has(findings(spec(date_control={}, filters=dup)), "binding", "duplicate filter")


# ------------------------------------------------------------------ binding rules

def test_exactly_one_binding():
    both = m_tile(query="q")
    assert has(findings(spec([both], queries=[{"id": "q", "sql": "select 1 as a"}])),
               "binding", "exactly one")
    none = {"id": "m", "type": "stat", "title": "T"}
    assert has(findings(spec([none])), "binding", "exactly one")
    two = m_tile(measures={"names": ["a", "b"]})
    assert has(findings(spec([two])), "binding", "exactly one")


def test_measures_names_rules():
    ok = {"id": "m", "type": "table", "title": "T", "measures": {"names": ["a", "b"]}}
    assert isinstance(ds.parse(spec([ok])), ds.SpecV2)
    for names in (["a"], list("abcdefg"), ["a", "a"]):
        bad = dict(ok, measures={"names": names})
        assert findings(spec([bad]))


def test_date_control_tile_rules():
    assert isinstance(ds.parse(ctl()), ds.SpecV2)
    assert isinstance(ds.parse(ctl(measure={"name": "t", "grain": "day"})), ds.SpecV2)
    no_spec = spec([m_tile(date="control")])
    assert has(findings(no_spec), "binding", "date_control")
    q = {"id": "m", "type": "bar", "title": "T", "query": "q", "x": "a", "y": [{"column": "b"}],
         "date": "control"}
    f = findings(spec([q], queries=[{"id": "q", "sql": "select 1 as a, 2 as b"}],
                      date_control={}))
    assert has(f, "binding", "measure or measures")
    f = findings(ctl(measure={"name": "t", "window": {"last_days": 5}}))
    assert has(f, "binding", "window")
    f = findings(ctl(measure={"name": "t", "grain": "month"}))
    assert has(f, "binding", "grain")


def test_follow_filters_rules():
    assert has(findings(spec([m_tile(follow_filters=True)])), "binding", "follow_filters")
    flt = [{"id": "f", "dimension": "t.c", "label": "L"}]
    s = ctl(follow_filters=True)
    s["filters"] = flt
    assert isinstance(ds.parse(s), ds.SpecV2)


def test_other_and_top_rules():
    t = m_tile(typ="bar", measure={"name": "t", "by": ["t.a"], "other": True})
    assert has(findings(spec([t])), "binding", "other needs top")
    t = m_tile(typ="bar", measure={"name": "t", "by": ["t.a", "t.b"], "top": 5, "other": True})
    assert has(findings(spec([t])), "binding", "other needs top")
    t = m_tile(typ="bar", measure={"name": "t", "by": ["t.a"], "top": 5, "other": True})
    assert isinstance(ds.parse(spec([t])), ds.SpecV2)
    t = m_tile(typ="bar", date="control", measure={"name": "t", "by": ["t.a"], "top": 5})
    assert has(findings(spec([t], date_control={})), "binding", "top")


def test_control_dimension_limit():
    m = {"name": "t", "by": ["t.a", "t.b"]}
    flt = [{"id": "f", "dimension": "t.c", "label": "L"},
           {"id": "g", "dimension": "t.a", "label": "L"}]
    s = spec([m_tile(typ="bar", date="control", measure=m)], date_control={}, filters=flt)
    assert isinstance(ds.parse(s), ds.SpecV2)
    s["tiles"][0]["follow_filters"] = True
    assert has(findings(s), "binding", "distinct dimensions")
    s["filters"] = flt[1:]
    assert isinstance(ds.parse(s), ds.SpecV2)


# ------------------------------------------------------------------ series, ghost, new types

QS = [{"id": "q", "sql": "select 1 as a, 2 as b, 3 as c"}]


def qt(typ, **kw):
    t = {"id": "c", "type": typ, "title": "C", "query": "q", "x": "a",
         "y": [{"column": "b"}]}
    t.update(kw)
    return spec([t], queries=QS)


def test_series_tone_and_as():
    ok = qt("bar", y=[{"column": "b", "tone": "inward"}, {"column": "c", "tone": "accent"}])
    assert isinstance(ds.parse(ok), ds.SpecV2)
    assert findings(qt("bar", y=[{"column": "b", "tone": "red"}]))
    assert has(findings(qt("bar", y=[{"column": "b", "as": "bar"}])), "binding", "combo")


def test_ghost_values():
    for g in ("none", "previous_period", "last_year"):
        s = qt("line", ghost=g, date="control", measure=None)
    assert ds.parse(qt("bar")).tiles[0].ghost == "none"
    for bad in (True, "yoy"):
        assert findings(qt("area", ghost=bad))
    ok = spec([m_tile(typ="line", date="control", ghost="last_year")], date_control={})
    assert ds.parse(ok).tiles[0].ghost == "last_year"
    assert has(findings(spec([m_tile(typ="line", ghost="last_year")])), "binding", "ghost")


def test_combo():
    y = [{"column": "b", "as": "bar"}, {"column": "c", "as": "line"}]
    assert isinstance(ds.parse(qt("combo", y=y)), ds.SpecV2)
    assert findings(qt("combo", y=[y[0]]))
    assert has(findings(qt("combo", y=[y[0], dict(y[0], column="c")])), "binding", "bar or line")
    assert findings(qt("combo", y=y * 3))


def test_exact_y_counts():
    two = [{"column": "b"}, {"column": "c"}]
    assert isinstance(ds.parse(qt("paired_bar", y=two)), ds.SpecV2)
    assert findings(qt("paired_bar"))
    for typ in ("aging", "waterfall", "calendar", "donut", "ranked_bar", "funnel"):
        assert isinstance(ds.parse(qt(typ)), ds.SpecV2), typ
        assert findings(qt(typ, y=two)), typ


def test_bullet():
    ok = qt("bullet", column="b", target={"value": 10}, bands=[1, 5, 9])
    del ok["tiles"][0]["x"]
    del ok["tiles"][0]["y"]
    assert isinstance(ds.parse(ok), ds.SpecV2)
    bad = copy.deepcopy(ok)
    del bad["tiles"][0]["target"]
    assert has(findings(bad), "binding", "target")
    bad = copy.deepcopy(ok)
    del bad["tiles"][0]["column"]
    assert has(findings(bad), "binding", "column")
    for bands in ([5, 1], [1, 2, 3, 4], []):
        bad = copy.deepcopy(ok)
        bad["tiles"][0]["bands"] = bands
        assert findings(bad)


def test_target_shape_and_placement():
    ok = m_tile(target={"column": "tgt", "label": "Plan"})
    assert isinstance(ds.parse(spec([ok])), ds.SpecV2)
    assert findings(spec([m_tile(target={"value": 1, "column": "c"})]))
    assert findings(spec([m_tile(target={})]))
    for typ in ("table", "donut"):
        bad = m_tile(typ=typ, target={"value": 3})
        assert has(findings(spec([bad])), "binding", "target"), typ


def test_ranked_bar_and_stacked_percent():
    assert ds.parse(qt("ranked_bar", top=50)).tiles[0].top == 50
    for top in (0, 51):
        assert findings(qt("ranked_bar", top=top))
    two = [{"column": "b"}, {"column": "c"}]
    assert ds.parse(qt("stacked_bar", y=two, percent=True)).tiles[0].percent is True


def test_table_columns():
    cols = [{"column": "a", "bar": True, "due": {"warn_days": 90}},
            {"column": "b", "delta": True, "good_direction": "up", "total": True}]
    t = {"id": "t", "type": "table", "title": "T", "measures": {"names": ["x", "y"]},
         "columns": cols, "sort": {"column": "a", "dir": "desc"}, "totals": True}
    assert isinstance(ds.parse(spec([t])), ds.SpecV2)
    two_bars = dict(t, columns=[{"column": "a", "bar": True}, {"column": "b", "bar": True}])
    assert has(findings(spec([two_bars])), "binding", "bar")
    assert findings(spec([dict(t, columns=[{"column": "a", "due": {"warn_days": 91}}])]))
    assert findings(spec([dict(t, sort={"column": "a", "dir": "up"})]))


def test_hidden_and_width_on_every_type():
    tiles = [text("t0"), m_tile("t1"), m_tile("t2", "table"), qt("donut")["tiles"][0]]
    tiles[3]["id"] = "t3"
    for t in tiles:
        t["hidden"] = True
        t["width"] = "two_thirds"
    s = spec(tiles, queries=QS)
    assert all(t.hidden and t.width == "two_thirds" for t in ds.parse(s).tiles)
    tiles[0]["width"] = "wide"
    assert findings(spec(tiles, queries=QS))


def test_default_width_table():
    assert ds.DEFAULT_WIDTH["stat"] == "quarter" and ds.DEFAULT_WIDTH["bullet"] == "half"
    assert ds.DEFAULT_WIDTH["table"] == "full" and ds.DEFAULT_WIDTH["donut"] == "half"


# ------------------------------------------------------------------ summary and diff

def test_summary_and_diff_read_v2():
    t = {"id": "t", "type": "table", "title": "T", "measures": {"names": ["a", "b"]}}
    stored = ds.parse(spec([t, m_tile("s")])).model_dump(mode="json", exclude_none=True)
    out = ds.summary(stored, [])
    assert out["tiles"][0]["binding"] == "measure" and out["tiles"][0]["ref"] == "a,b"
    assert out["measures"] == ["a", "b", "tons"]
    other = copy.deepcopy(stored)
    other["tiles"][0]["title"] = "U"
    assert ds.tile_diff(stored, other)["changed"] == ["t"]


# ------------------------------------------------------------------ no colour fields

FORBIDDEN = {"color", "colour", "colors", "palette", "css", "style", "script", "html", "code"}


def _walk(node, hits):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties" and isinstance(v, dict):
                hits.extend(n for n in v if n.lower() in FORBIDDEN)
            _walk(v, hits)
    elif isinstance(node, list):
        for v in node:
            _walk(v, hits)


def test_no_colour_css_or_code_fields():
    hits: list = []
    _walk(ds.schema_dict(2), hits)
    assert hits == []


# ------------------------------------------------------------------ review fixes (batch A)

def test_path_keeps_a_field_named_like_a_tile_type():
    t = {"id": "t", "type": "table", "title": "T", "measures": {"names": ["x", "y"]},
         "columns": [{"column": "a", "bar": "yes"}]}
    assert has(findings(spec([t])), "spec", path="tiles[0].columns[0].bar")


def test_multi_measure_other_is_accepted_with_top_and_one_by():
    t = {"id": "t", "type": "table", "title": "T",
         "measures": {"names": ["x", "y"], "by": ["t.a"], "top": 5, "other": True}}
    assert isinstance(ds.parse(spec([t])), ds.SpecV2)
    t["measures"]["other"] = "maybe"
    assert findings(spec([t]))
    t["measures"].update(other=True, top=None)
    assert has(findings(spec([t])), "binding", "other needs top")


def test_locale_lists_are_one_tuple():
    from app import config, dashsettings
    assert ds.LOCALES is config.DASHBOARD_LOCALES is dashsettings.LOCALES
    assert set(ds.SpecV2.model_json_schema()["properties"]["number_locale"]["anyOf"][0]["enum"]) \
        == set(config.DASHBOARD_LOCALES)
