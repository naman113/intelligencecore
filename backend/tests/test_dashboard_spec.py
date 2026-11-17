"""Phase 18 plan 01: the pure chart-spec engine (SPEC-01, SPEC-03). No DB, no LLM."""
import copy
import json
from pathlib import Path

import pytest

from app import dashboardspec as ds
from app.guardrails import validate_readonly_sql
from tests.test_model_compiler import build

SCHEMA = Path(__file__).resolve().parents[2] / "docs" / "dashboard-spec.v1.schema.json"
OPEN_SQL = ("SELECT c.name AS customer, sum(i.amount) AS open_amount FROM invoices i "
            "JOIN customers c ON c.id = i.customer_id GROUP BY c.name")


def good() -> dict:
    return {
        "spec_version": 1,
        "title": "Sales overview",
        "subtitle": "Northwind Supply",
        "queries": [{"id": "open_invoices", "sql": OPEN_SQL, "refresh_minutes": 30}],
        "layout": {"rows": [["net", "trend"], ["regions", "open"], ["about"]]},
        "tiles": [
            {"id": "net", "type": "stat", "title": "Net sales",
             "measure": {"name": "net_sales", "grain": "month", "window": {"last_periods": 3}},
             "pick": "last_complete", "compare": "previous_row", "good_direction": "up",
             "sparkline": True, "format": "compact"},
            {"id": "trend", "type": "line", "title": "Net sales by month",
             "measure": {"name": "net_sales", "grain": "month", "by": ["regions.name"],
                         "window": {"last_periods": 13}}},
            {"id": "regions", "type": "bar", "title": "Net sales by region",
             "measure": {"name": "net_sales", "by": ["regions.name"],
                         "window": {"last_days": 90}, "top": 8}},
            {"id": "open", "type": "table", "title": "Open invoices", "query": "open_invoices",
             "columns": [{"column": "customer"},
                         {"column": "open_amount", "format": "currency", "currency": "USD"}]},
            {"id": "about", "type": "text", "text": "Figures exclude cancelled orders.\nUpdated nightly."},
        ],
    }


def refused(raw) -> list[dict]:
    with pytest.raises(ds.SpecError) as e:
        ds.parse(raw)
    return e.value.findings


def rules(raw) -> set:
    return {f["rule"] for f in refused(raw)}


def paths(raw) -> set:
    return {f["path"] for f in refused(raw)}


@pytest.fixture(scope="module")
def model():
    return build()


# ------------------------------------------------------------------ accept

def test_good_spec_parses_from_dict_and_json():
    assert ds.parse(good()).title == "Sales overview"
    assert ds.parse(json.dumps(good())).tiles[0].id == "net"


def test_hostile_text_is_accepted_as_text():
    s = good()
    s["title"] = "<img src=//x.invalid/a onerror=alert(1)>"
    s["subtitle"] = "javascript:alert(1) \"quoted\" 'single' `tick`"
    s["tiles"][4]["text"] = "</script><script>fetch('https://x.invalid')</script>"
    s["tiles"][0]["note"] = "<b>bold</b>\nsecond line"
    sp = ds.parse(s)
    assert sp.title == s["title"]
    assert sp.tiles[4].text == s["tiles"][4]["text"]


def test_newline_allowed_in_note_and_text_only():
    s = good()
    s["tiles"][0]["note"] = "a\nb"
    s["tiles"][4]["text"] = "a\nb"
    ds.parse(s)
    s["title"] = "a\nb"
    assert "title" in paths(s)


@pytest.mark.parametrize("field,bad", [
    ("title", "\u0007bell"), ("title", "‮rsl"), ("title", "x\u0085y"),
    ("title", "   "), ("title", "x\x7fy"), ("title", "a\tb"), ("title", "a⁧b"),
])
def test_control_and_bidi_text_refused(field, bad):
    s = good()
    s[field] = bad
    assert field in paths(s)


def test_bidi_in_note_and_label_refused():
    s = good()
    s["tiles"][0]["note"] = "x‮y"
    assert "tiles[0].note" in paths(s)
    s = good()
    s["tiles"][1]["y"] = [{"column": "net_sales", "label": "a\u0085b"}]
    assert "tiles[1].y[0].label" in paths(s)


# ------------------------------------------------------------------ strictness

@pytest.mark.parametrize("v", [True, 1.0, "1", 2, 0])
def test_spec_version_refused(v):
    s = good()
    s["spec_version"] = v
    assert "spec_version" in paths(s)


def test_unknown_keys_refused_at_every_depth():
    cases = [
        lambda s: s.update(extra=1),
        lambda s: s["tiles"][0].update(color="red"),
        lambda s: s["tiles"][1]["measure"].update(sql="select 1"),
        lambda s: s["tiles"][0]["measure"]["window"].update(x=1),
        lambda s: s["queries"][0].update(foo=1),
        lambda s: s["layout"].update(foo=1),
    ]
    for mut in cases:
        s = good()
        mut(s)
        assert "spec" in rules(s), mut


def test_series_ref_extra_key_refused():
    s = good()
    s["tiles"][1]["y"] = [{"column": "net_sales", "color": "#fff"}]
    assert "tiles[1].y[0].color" in paths(s)


def test_collects_every_problem_in_one_pass():
    s = good()
    s["tiles"][2]["query"] = "open_invoices"          # both query and measure
    s["layout"]["rows"] = [["net", "trend"]]          # tiles missing
    s["spec_version"] = 3
    found = refused(s)
    assert len(found) >= 3
    ps = {f["path"] for f in found}
    assert "spec_version" in ps and "tiles[2]" in ps


def test_html_and_non_json_refused_with_message():
    f = refused("<!doctype html><p>x</p>")
    assert "HTML dashboards are no longer accepted" in f[0]["message"]
    assert "HTML" in refused("[1, 2]")[0]["message"]
    assert "HTML" in refused(42)[0]["message"]


# ------------------------------------------------------------------ queries and binding

def test_declared_m_prefix_refused():
    s = good()
    s["queries"][0]["id"] = "m_sales"
    s["tiles"][3]["query"] = "m_sales"
    assert "query" in rules(s) and "queries[0].id" in paths(s)


def test_measure_source_declaration_refused():
    s = good()
    s["queries"].append({"id": "x1", "source": "measure", "sql": "select 1"})
    assert "query" in rules(s)


def test_kpi_and_forecast_queries_accepted():
    s = good()
    s["queries"] += [{"id": "kp", "source": "kpi_history", "watch_id": 3},
                     {"id": "fc", "source": "forecast", "forecast_id": 2}]
    ds.parse(s)


def test_duplicate_query_id_refused():
    s = good()
    s["queries"].append(dict(s["queries"][0]))
    assert "queries[1].id" in paths(s)


def test_binding_exactly_one():
    s = good()
    s["tiles"][3]["measure"] = {"name": "net_sales"}
    assert "binding" in rules(s)
    s = good()
    del s["tiles"][3]["query"]
    assert "binding" in rules(s)


def test_undeclared_query_ref_refused():
    s = good()
    s["tiles"][3]["query"] = "nope"
    assert "tiles[3].query" in paths(s)


def test_layout_faults():
    for rows in ([["net", "trend"], ["regions", "open"]],                       # omit
                 [["net", "trend"], ["regions", "open"], ["about", "net"]],      # repeat
                 [["net", "trend"], ["regions", "open"], ["about", "ghost"]],    # unknown
                 [["net"]] * 13,
                 [["net", "trend", "regions", "open", "about"]]):
        s = good()
        s["layout"]["rows"] = rows
        assert refused(s), rows
    s = good()
    s["layout"]["rows"] = [["net", "trend"], ["regions", "open"]]
    assert "layout" in rules(s)


def _chart(i, kind="line"):
    return {"id": f"c{i}", "type": kind, "title": "t", "query": "open_invoices", "x": "customer",
            "y": [{"column": "open_amount"}]}


def test_chart_and_tile_limits():
    s = good()
    s["tiles"] = [_chart(i) for i in range(7)]
    s["layout"]["rows"] = [[f"c{i}"] for i in range(7)]
    assert "charts" in rules(s)
    s["tiles"] = [_chart(i) for i in range(6)] + [
        {"id": "sp", "type": "sparkline", "title": "s", "query": "open_invoices",
         "x": "customer", "y": [{"column": "open_amount"}]}]
    s["layout"]["rows"] = [[f"c{i}"] for i in range(6)] + [["sp"]]
    ds.parse(s)                                       # sparklines do not count
    s["tiles"] = [_chart(i) for i in range(25)]
    s["layout"]["rows"] = [["c0"]]
    assert refused(s)


def test_size_limit():
    s = good()
    s["tiles"][4]["text"] = "x" * 2000
    s["queries"] = [{"id": "q%d" % i, "sql": "select " + "1," * 5000 + "1"} for i in range(20)]
    f = refused(s)
    assert f[0]["rule"] == "size"


@pytest.mark.parametrize("mut", [
    lambda t: t["measure"].pop("grain") or t["measure"].__setitem__("window", {"last_periods": 3}),
    lambda t: t["measure"].update(top=5),
    lambda t: t["measure"].update(window="this_month"),
    lambda t: t["measure"].update(window={"start": "2026-02-01", "end": "2026-01-01"}),
    lambda t: t["measure"].update(window={"start": "2026-13-01", "end": "2026-14-01"}),
    lambda t: t.update(format="currency"),
    lambda t: t.update(currency="USD"),
    lambda t: t.update(grain="month"),
])
def test_measure_and_format_rules_refused(mut):
    s = good()
    t = s["tiles"][1]
    mut(t)
    assert refused(s)


def test_top_only_on_bar_stacked_table():
    s = good()
    s["tiles"][1]["measure"] = {"name": "net_sales", "top": 3}
    assert "binding" in rules(s)
    s = good()
    s["tiles"][2]["measure"]["top"] = 3
    ds.parse(s)


def test_last_complete_needs_grain():
    s = good()
    del s["tiles"][0]["measure"]["grain"]
    s["tiles"][0]["measure"].pop("window")
    assert "binding" in rules(s)


def test_stat_rules():
    s = good()
    s["tiles"][0]["measure"]["by"] = ["regions.name"]
    assert "binding" in rules(s)
    s = good()
    s["tiles"][0].update(measure=None)
    s["tiles"][0].pop("measure")
    s["tiles"][0]["query"] = "open_invoices"
    assert "binding" in rules(s)                     # query stat needs column


def test_query_bound_chart_needs_x_and_y():
    s = good()
    s["tiles"][1] = {"id": "trend", "type": "line", "title": "t", "query": "open_invoices"}
    assert "binding" in rules(s)


def test_text_tile_has_no_binding_fields():
    s = good()
    s["tiles"][4]["query"] = "open_invoices"
    assert refused(s)
    s = good()
    s["tiles"][4]["format"] = "number"
    assert refused(s)


def test_limits_on_lists():
    s = good()
    s["tiles"][1]["y"] = [{"column": "c%d" % i} for i in range(7)]
    s["tiles"][1].pop("measure")
    s["tiles"][1]["query"] = "open_invoices"
    s["tiles"][1]["x"] = "customer"
    assert any(p.startswith("tiles[1].y") for p in paths(s))


def test_owed_when_and_compare_column():
    s = good()
    s["tiles"][0]["owed_when"] = {"op": ">", "value": 10}
    s["tiles"][0]["compare"] = {"column": "net_sales"}
    ds.parse(s)
    s["tiles"][0]["owed_when"] = {"op": ">", "value": True}
    assert refused(s)


# ------------------------------------------------------------------ schema

def test_schema_text_shape():
    t = ds.schema_text()
    assert t.endswith("\n") and t == json.dumps(json.loads(t), sort_keys=True, indent=2) + "\n"
    assert t.count('"additionalProperties": false') >= 10
    assert '"discriminator"' in t and '"propertyName": "type"' in t


def test_committed_schema_matches_models():
    # git may check the file out with CRLF on Windows; the content is what is pinned
    assert SCHEMA.read_bytes().replace(b"\r\n", b"\n") == ds.schema_text().encode("utf-8")


# ------------------------------------------------------------------ compile

def compiled(model, raw=None):
    sp = ds.parse(raw or good())
    return sp, ds.compile_measures(sp, model)


def test_compile_shares_identical_requests(model):
    s = good()
    s["tiles"][4:4] = [{"id": "net2", "type": "stat", "title": "Net again",
                        "measure": {"name": "net_sales", "grain": "month",
                                    "window": {"last_periods": 3}, "refresh_minutes": 10},
                        "pick": "last_complete"}]
    s["layout"]["rows"].append(["net2"])
    sp, comp = compiled(model, s)
    ids = [e["id"] for e in comp]
    assert ids == ["m_net", "m_trend", "m_regions"]
    assert comp[0]["refresh_minutes"] == 10
    for e in comp:
        assert list(e) == ["id", "source", "measure", "request", "columns", "sql",
                           "refresh_minutes"] or set(e) == {"id", "source", "measure", "request",
                                                            "columns", "sql", "refresh_minutes"}
        assert e["source"] == "measure"
        validate_readonly_sql(e["sql"])


def test_windows_compile_relative(model):
    _, comp = compiled(model)
    by_id = {e["id"]: e for e in comp}
    assert by_id["m_net"]["request"]["window_days"] == 93
    assert by_id["m_trend"]["request"]["window_days"] == 403
    assert by_id["m_regions"]["request"]["window_days"] == 90
    for e in comp:
        assert "CURRENT_DATE" in e["sql"]
        assert "CAST('20" not in e["sql"]


def test_default_window_and_fixed_window(model):
    s = good()
    s["tiles"][1]["measure"].pop("window")
    _, comp = compiled(model, s)
    assert {e["id"]: e for e in comp}["m_trend"]["request"]["window_days"] == ds.DEFAULT_WINDOW_DAYS["month"]
    s = good()
    s["tiles"][1]["measure"]["window"] = {"start": "2026-01-01", "end": "2026-04-01"}
    _, comp = compiled(model, s)
    e = {e["id"]: e for e in comp}["m_trend"]
    assert e["request"]["start"] == "2026-01-01" and "2026-01-01" in e["sql"]
    assert "window_days" not in e["request"]


def test_ordering_and_limit(model):
    _, comp = compiled(model)
    by_id = {e["id"]: e for e in comp}
    assert 'ORDER BY "metric_time__month", "regions__name"' in by_id["m_trend"]["sql"]
    assert by_id["m_trend"]["sql"].startswith("SELECT * FROM (")
    sql = by_id["m_regions"]["sql"]
    assert sql.endswith('ORDER BY "net_sales" DESC NULLS LAST LIMIT 8')
    s = good()
    s["tiles"][2]["measure"].pop("top")
    _, comp = compiled(model, s)
    assert "LIMIT" not in {e["id"]: e for e in comp}["m_regions"]["sql"]


def test_derived_measure_wrapper_passes_guardrails(model):
    s = good()
    s["tiles"][1]["measure"] = {"name": "net_sales_mom_growth", "grain": "month"}
    _, comp = compiled(model, s)
    validate_readonly_sql(comp[1]["sql"])


def test_not_modeled_and_invalid(model):
    s = good()
    s["tiles"][1]["measure"]["name"] = "nope"
    with pytest.raises(ds.SpecError) as e:
        compiled(model, s)
    f = e.value.findings[0]
    assert f["rule"] == "not_modeled" and f["path"] == "tiles[1].measure"
    s = good()
    s["tiles"][2]["measure"] = {"name": "customer_count", "by": ["regions.name"],
                                "window": {"last_days": 30}}
    with pytest.raises(ds.SpecError) as e:
        compiled(model, s)
    assert e.value.findings[0]["rule"] == "measure"
    s = good()
    s["tiles"][2]["measure"]["by"] = ["nothing.here"]
    with pytest.raises(ds.SpecError) as e:
        compiled(model, s)
    assert e.value.findings[0]["rule"] == "measure"


def test_id_truncation_and_collision(model):
    long_a = "a" * 48
    s = {"spec_version": 1, "title": "t",
         "layout": {"rows": [[long_a], [long_a[:47] + "b"]]},
         "tiles": [{"id": long_a, "type": "stat", "title": "x", "measure": {"name": "net_sales"}},
                   {"id": long_a[:47] + "b", "type": "stat", "title": "y",
                    "measure": {"name": "invoiced_amount"}}]}
    _, comp = compiled(model, s)
    ids = [e["id"] for e in comp]
    assert len(set(ids)) == 2 and all(len(i) <= 48 for i in ids)
    assert ids[0] == "m_" + "a" * 46


def test_no_measures_means_no_compile(model):
    s = good()
    s["tiles"] = [s["tiles"][3], s["tiles"][4]]
    s["layout"]["rows"] = [["open"], ["about"]]
    sp = ds.parse(s)
    assert not ds.has_measures(sp) and ds.compile_measures(sp, model) == []
    assert ds.has_measures(ds.parse(good()))


# ------------------------------------------------------------------ sql_columns

def test_sql_columns():
    assert ds.sql_columns(OPEN_SQL) == ["customer", "open_amount"]
    assert ds.sql_columns("SELECT name, 1 AS one FROM customers") == ["name", "one"]


@pytest.mark.parametrize("sql", ["SELECT * FROM customers", "SELECT sum(amount) FROM invoices",
                                 "SELECT c.* FROM customers c"])
def test_sql_columns_refuses_unaliased(sql):
    with pytest.raises(ds.SpecError) as e:
        ds.sql_columns(sql)
    assert e.value.findings[0]["rule"] == "columns"


# ------------------------------------------------------------------ bind

def bound(model, raw=None):
    sp = ds.parse(raw or good())
    comp = ds.compile_measures(sp, model)
    declared = [q.model_dump(mode="json", exclude_none=True) for q in sp.queries]
    return sp, declared, comp, ds.bind(sp, declared, comp)


def test_bind_fills_defaults(model):
    _, declared, comp, st = bound(model)
    t = {x["id"]: x for x in st["tiles"]}
    assert t["net"]["qid"] == "m_net" and t["net"]["column"] == "net_sales"
    assert t["net"]["x"] == "metric_time__month"
    assert t["trend"]["x"] == "metric_time__month" and t["trend"]["series"] == "regions__name"
    assert t["trend"]["y"][0]["column"] == "net_sales"
    assert t["regions"]["x"] == "regions__name" and "series" not in t["regions"]
    assert t["open"]["qid"] == "open_invoices"
    assert "qid" not in t["about"]
    assert t["open"]["columns"][0]["column"] == "customer"


def test_bind_table_all_columns(model):
    s = good()
    s["tiles"][3]["columns"] = []
    _, _, _, st = bound(model, s)
    assert [c["column"] for c in {x["id"]: x for x in st["tiles"]}["open"]["columns"]] == \
        ["customer", "open_amount"]


def test_bind_refuses_unknown_column(model):
    s = good()
    s["tiles"][3]["columns"] = [{"column": "nope"}]
    with pytest.raises(ds.SpecError) as e:
        bound(model, s)
    f = e.value.findings[0]
    assert f["rule"] == "columns" and "customer" in f["message"]


def test_bind_refuses_measure_shape_faults(model):
    s = good()
    s["tiles"][1]["measure"].pop("grain")
    s["tiles"][1]["measure"].pop("window")
    with pytest.raises(ds.SpecError):
        bound(model, s)
    s = good()
    s["tiles"][2]["measure"] = {"name": "net_sales", "window": {"last_days": 30}}
    with pytest.raises(ds.SpecError):
        bound(model, s)
    s = good()
    s["tiles"][2] = {"id": "regions", "type": "stacked_bar", "title": "s",
                     "measure": {"name": "net_sales", "by": ["regions.name"]}}
    with pytest.raises(ds.SpecError):
        bound(model, s)


def test_bind_stacked_bar_with_grain_and_by(model):
    s = good()
    s["tiles"][2] = {"id": "regions", "type": "stacked_bar", "title": "s",
                     "measure": {"name": "net_sales", "grain": "month", "by": ["regions.name"]}}
    _, _, _, st = bound(model, s)
    t = {x["id"]: x for x in st["tiles"]}["regions"]
    assert t["x"] == "metric_time__month" and t["series"] == "regions__name"


def test_bind_kpi_columns():
    s = good()
    s["queries"].append({"id": "kp", "source": "kpi_history", "watch_id": 1})
    s["tiles"][3] = {"id": "open", "type": "line", "title": "k", "query": "kp", "x": "day",
                     "y": [{"column": "value"}]}
    s["tiles"][0:3] = []
    s["layout"]["rows"] = [["open"], ["about"]]
    sp = ds.parse(s)
    declared = [q.model_dump(mode="json", exclude_none=True) for q in sp.queries]
    st = ds.bind(sp, declared, [])
    assert st["tiles"][0]["qid"] == "kp"
    s["tiles"][0]["x"] = "nope"
    sp = ds.parse(s)
    with pytest.raises(ds.SpecError):
        ds.bind(sp, declared, [])


def test_stored_queries_cap(model):
    _, declared, comp, _ = bound(model)
    out = ds.stored_queries(declared, comp)
    assert [q["id"] for q in out] == ["open_invoices", "m_net", "m_trend", "m_regions"]
    with pytest.raises(ds.SpecError) as e:
        ds.stored_queries([{"id": "q%d" % i} for i in range(18)], comp)
    assert e.value.findings[0]["rule"] == "queries"


# ------------------------------------------------------------------ render

def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def test_render_spec_has_no_sql_or_binding(model):
    _, declared, comp, st = bound(model)
    qs = ds.stored_queries(declared, comp)
    r = ds.render_spec(st, qs)
    assert r["render_version"] == 1 and r["subtitle"] == "Northwind Supply"
    assert r["rows"] == [["net", "trend"], ["regions", "open"], ["about"]]
    assert not ({"measure", "query", "sql", "request", "filters"} & set(_walk(r)))
    blob = json.dumps(r)
    for q in qs:
        assert q["sql"] not in blob
    assert r["refresh"] == {"m_net": 15, "m_trend": 15, "m_regions": 15, "open_invoices": 30}
    tiles = {t["id"]: t for t in r["tiles"]}
    assert tiles["about"]["qid"] is None and tiles["about"]["text"].startswith("Figures")
    assert tiles["net"]["grain"] == "month" and tiles["net"]["owed_when"] is None
    assert tiles["regions"]["horizontal"] is False and tiles["trend"]["band"] is None
    assert tiles["open"]["columns"][1]["currency"] == "USD"
    assert tiles["net"]["pick"] == "last_complete" and tiles["net"]["sparkline"] is True


def test_authoring_form_round_trips(model):
    _, _, _, st = bound(model)
    af = ds.authoring_form(st)
    assert not any("qid" in t for t in af["tiles"])
    ds.parse(af)


# ------------------------------------------------------------------ diff and summary

def test_tile_diff(model):
    _, _, _, st = bound(model)
    assert ds.tile_diff(None, st)["added"] == sorted(t["id"] for t in st["tiles"])
    new = copy.deepcopy(st)
    new["tiles"][0]["title"] = "Changed"
    new["tiles"][1]["qid"] = "other"                    # qid is ignored
    new["tiles"].pop()
    new["tiles"].append({"id": "zeta", "type": "text", "text": "z"})
    new["tiles"].append({"id": "alpha", "type": "text", "text": "a"})
    d = ds.tile_diff(st, new)
    assert d == {"added": ["alpha", "zeta"], "removed": ["about"], "changed": ["net"]}


def test_summary(model):
    _, declared, comp, st = bound(model)
    qs = ds.stored_queries(declared, comp)
    sm = ds.summary(st, qs)
    assert sm["measures"] == ["net_sales"]
    assert {t["id"]: t["binding"] for t in sm["tiles"]} == {
        "net": "measure", "trend": "measure", "regions": "measure", "open": "query",
        "about": "none"}
    assert sm["queries"][0] == {"id": "open_invoices", "source": "sql"}
    assert sm["queries"][1]["source"] == "measure"
