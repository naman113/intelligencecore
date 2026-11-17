"""Phase 22 plan 03: date-control compiler support (naive timestamps, kinds, tiles, presets, Top N + Other)."""
import sqlglot
from sqlglot import exp

from app import modelcompiler as mc
from app.guardrails import validate_readonly_sql
from app.modelcompiler import (
    InvalidRequest, NotModeled, compile_measure, make_request, model_from_rows,
)

TSTZ = "timestamp with time zone"
TS = "timestamp without time zone"

COLUMNS = {
    "public.customers": {"id": "integer", "name": "text"},
    "public.rolls": {"id": "integer", "weigh_date": TSTZ, "kg": "numeric", "customer_id": "integer",
                     "status": "text"},
    "public.dispatches": {"id": "integer", "dispatch_date": "date", "kg_out": "numeric",
                          "customer_id": "integer"},
    "public.stock": {"id": "integer", "snapshot_date": "date", "stock_kg": "numeric"},
    "public.items": {"id": "integer", "qty": "numeric"},
    "public.events": {"id": "integer", "happened_at": TS, "amount": "numeric", "customer_id": "integer"},
}
RELS = [
    dict(name="rolls_customer", from_relation="public.rolls", from_columns=["customer_id"],
         to_relation="public.customers", to_columns=["id"], cardinality="many_to_one", status="active"),
    dict(name="dispatches_customer", from_relation="public.dispatches", from_columns=["customer_id"],
         to_relation="public.customers", to_columns=["id"], cardinality="many_to_one", status="active"),
    dict(name="events_customer", from_relation="public.events", from_columns=["customer_id"],
         to_relation="public.customers", to_columns=["id"], cardinality="many_to_one", status="active"),
]
TABLES = [dict(name=r.split(".")[1], relation=r, object_kind="base", role=None, grain=None,
               primary_key=[], agg_time_dimension=None, status="active") for r in COLUMNS]


def _m(**kw):
    base = dict(label=None, description=None, base_relation=None, agg=None, expr=None,
                agg_time_dimension=None, filter=[], numerator=None, denominator=None,
                input_metrics=None, non_additive_dimension=None, additivity="additive",
                unit=None, format=None, status="active")
    base.update(kw)
    return base


ROLL_T = "public.rolls.weigh_date"
DISP_T = "public.dispatches.dispatch_date"
MEASURES = [
    _m(name="rolls_in", type="simple", base_relation="public.rolls", agg="count", expr="id",
       agg_time_dimension=ROLL_T),
    _m(name="kg_in", type="simple", base_relation="public.rolls", agg="sum", expr="kg",
       agg_time_dimension=ROLL_T, label="Kg in", description="Kilos received"),
    _m(name="kg_returned", type="simple", base_relation="public.rolls", agg="sum", expr="kg",
       agg_time_dimension=ROLL_T, filter=[dict(dimension="public.rolls.status", op="=", values=["returned"])]),
    _m(name="dispatches", type="simple", base_relation="public.dispatches", agg="count", expr="id",
       agg_time_dimension=DISP_T),
    _m(name="kg_out", type="simple", base_relation="public.dispatches", agg="sum", expr="kg_out",
       agg_time_dimension=DISP_T),
    _m(name="avg_kg", type="simple", base_relation="public.rolls", agg="average", expr="kg",
       agg_time_dimension=ROLL_T),
    _m(name="min_kg", type="simple", base_relation="public.rolls", agg="min", expr="kg",
       agg_time_dimension=ROLL_T),
    _m(name="max_kg", type="simple", base_relation="public.rolls", agg="max", expr="kg",
       agg_time_dimension=ROLL_T),
    _m(name="distinct_customers", type="simple", base_relation="public.rolls", agg="count_distinct",
       expr="customer_id", agg_time_dimension=ROLL_T, additivity="additive",
       filter=[dict(dimension="public.rolls.status", op="!=", values=["void"])]),
    _m(name="rolls_ok", type="simple", base_relation="public.rolls", agg="sum_boolean", expr="kg > 0",
       agg_time_dimension=ROLL_T),
    _m(name="stock_kg", type="simple", base_relation="public.stock", agg="sum", expr="stock_kg",
       agg_time_dimension="public.stock.snapshot_date",
       non_additive_dimension=dict(name="public.stock.snapshot_date", window_choice="max",
                                   window_groupings=[]), additivity="semi_additive"),
    _m(name="stock_open", type="simple", base_relation="public.stock", agg="sum", expr="stock_kg",
       agg_time_dimension="public.stock.snapshot_date",
       non_additive_dimension=dict(name="public.stock.snapshot_date", window_choice="min",
                                   window_groupings=[]), additivity="semi_additive"),
    _m(name="kg_flagged", type="simple", base_relation="public.rolls", agg="sum", expr="kg",
       agg_time_dimension=ROLL_T, additivity="non_additive"),
    _m(name="items_qty", type="simple", base_relation="public.items", agg="sum", expr="qty"),
    _m(name="events_amount", type="simple", base_relation="public.events", agg="sum", expr="amount",
       agg_time_dimension="public.events.happened_at"),
    _m(name="return_rate", type="ratio", numerator=dict(name="kg_in", filter=[
        dict(dimension="public.rolls.status", op="!=", values=["void"])]), denominator=dict(name="kg_in")),
    _m(name="stock_turn", type="ratio", numerator=dict(name="kg_out"), denominator=dict(name="stock_kg")),
    _m(name="avg_ratio", type="ratio", numerator=dict(name="avg_kg"), denominator=dict(name="kg_in")),
    _m(name="offset_ratio", type="ratio", numerator=dict(name="kg_in", offset_window="1 month"),
       denominator=dict(name="kg_in")),
    _m(name="flagged_ratio", type="ratio", numerator=dict(name="kg_flagged"), denominator=dict(name="kg_in")),
    _m(name="net_pct", type="derived", expr="(a - b) / a * 100",
       input_metrics=[dict(name="kg_in", alias="a"), dict(name="kg_returned", alias="b")]),
    _m(name="neg_plus", type="derived", expr="-a + 2",
       input_metrics=[dict(name="kg_in", alias="a")]),
    _m(name="safe_pct", type="derived", expr="COALESCE(a, 0) / b",
       input_metrics=[dict(name="kg_in", alias="a"), dict(name="kg_returned", alias="b")]),
    _m(name="case_pct", type="derived", expr="CASE WHEN b > 0 THEN a / b ELSE 0 END",
       input_metrics=[dict(name="kg_in", alias="a"), dict(name="kg_returned", alias="b")]),
    _m(name="kg_growth", type="derived", expr="a - b",
       input_metrics=[dict(name="kg_in", alias="a"), dict(name="kg_in", alias="b", offset_window="1 month")]),
    _m(name="x" * 58, type="simple", base_relation="public.rolls", agg="average", expr="kg",
       agg_time_dimension=ROLL_T),
]


def build(extra=()):
    return model_from_rows(list(TABLES), list(RELS), list(MEASURES) + list(extra),
                           {k: dict(v) for k, v in COLUMNS.items()})


import pytest  # noqa: E402


@pytest.fixture(scope="module")
def model():
    return build()


def _parse(sql):
    return sqlglot.parse_one(sql, read="postgres")


# ---------------------------------------------------------------- naive timestamps (D-08)

def _req(name, **kw):
    return make_request(name, grain="day", window_days=30, **kw)


def test_naive_utc_wraps_naive_time_column(model):
    c = compile_measure(model, _req("events_amount"), naive_utc=True)
    assert "AT TIME ZONE 'UTC'" in c.sql
    assert '"t0"."happened_at" AT TIME ZONE \'UTC\'' in c.sql
    # period key and window filter both read the wrapped column
    assert c.sql.count("AT TIME ZONE 'UTC'") >= 3
    assert "time column public.events.happened_at has no time zone; it is read as UTC" in c.notes
    validate_readonly_sql(c.sql)


def test_naive_default_keeps_stored_clock(model):
    c = compile_measure(model, _req("events_amount"))
    assert "AT TIME ZONE" not in c.sql
    assert "time column public.events.happened_at has no time zone; days follow its stored clock" in c.notes


def test_zoned_and_date_columns_never_wrapped(model):
    for name in ("kg_in", "dispatches"):
        c = compile_measure(model, _req(name), naive_utc=True)
        assert "AT TIME ZONE" not in c.sql
        assert not [n for n in c.notes if "no time zone" in n]


def test_default_call_is_unchanged_for_zoned_columns(model):
    a = compile_measure(model, _req("kg_in"))
    b = compile_measure(model, _req("kg_in"), naive_utc=False)
    assert a == b


def test_is_naive_ts():
    assert mc._is_naive_ts("timestamp without time zone")
    assert mc._is_naive_ts("timestamp")
    assert not mc._is_naive_ts("timestamp with time zone")
    assert not mc._is_naive_ts("timestamptz")
    assert not mc._is_naive_ts("date")


# ---------------------------------------------------------------- measure_kind (C-03)

@pytest.mark.parametrize("name,kind", [
    ("kg_in", "sum"), ("rolls_in", "sum"), ("rolls_ok", "sum"),
    ("min_kg", "min"), ("max_kg", "max"), ("avg_kg", "avg"),
    ("distinct_customers", "distinct"),
    ("stock_kg", "closing"), ("stock_open", "opening"),
    ("kg_flagged", "fixed"),
    ("return_rate", "ratio"), ("net_pct", "formula"), ("neg_plus", "formula"),
    ("stock_turn", "fixed"), ("avg_ratio", "fixed"), ("offset_ratio", "fixed"), ("flagged_ratio", "fixed"),
    ("safe_pct", "fixed"), ("case_pct", "fixed"), ("kg_growth", "fixed"),
    ("items_qty", "now"),
])
def test_measure_kind(model, name, kind):
    assert mc.measure_kind(model, model.measure(name)) == kind


def test_kind_ignores_the_additivity_default(model):
    # count_distinct and average carry additivity "additive" by default, yet are never "sum"
    assert model.measure("distinct_customers").additivity == "additive"
    assert model.measure("avg_kg").additivity == "additive"
    assert mc.measure_kind(model, model.measure("distinct_customers")) == "distinct"
    assert mc.measure_kind(model, model.measure("avg_kg")) == "avg"


def test_semi_additive_sum_is_never_sum(model):
    m = model.measure("stock_kg")
    assert m.agg == "sum"
    assert mc.measure_kind(model, m) == "closing"


# ---------------------------------------------------------------- formula_tree (C-06)

def test_formula_tree_nested():
    assert mc.formula_tree("(kg_value - kg_returned) / kg") == {
        "op": "/", "a": {"op": "-", "a": {"in": "kg_value"}, "b": {"in": "kg_returned"}}, "b": {"in": "kg"}}


def test_formula_tree_unary_minus():
    assert mc.formula_tree("-a + 2") == {
        "op": "+", "a": {"op": "-", "a": {"num": 0}, "b": {"in": "a"}}, "b": {"num": 2}}


def test_formula_tree_numbers():
    assert mc.formula_tree("a * 100") == {"op": "*", "a": {"in": "a"}, "b": {"num": 100}}
    t = mc.formula_tree("a * 1.5")
    assert t["b"] == {"num": 1.5} and isinstance(t["b"]["num"], float)


def test_formula_tree_of_measure(model):
    t = mc.formula_tree(model.measure("net_pct"))
    assert t["op"] == "*" and t["b"] == {"num": 100}
    assert mc.formula_tree(model.measure("kg_in")) is None


@pytest.mark.parametrize("expr", ["COALESCE(a, 0) / b", "CASE WHEN a > 0 THEN 1 ELSE 0 END", "a % 2", "ABS(a)",
                                  "a / 'x'", "a +", ""])
def test_formula_tree_refuses_other_nodes(expr):
    assert mc.formula_tree(expr) is None


def test_formula_tree_unknown_alias_refused(model):
    from dataclasses import replace
    m = model.measure("net_pct")
    assert mc.formula_tree(replace(m, expr="a - zzz")) is None


def _balanced(n):
    if n == 1:
        return "a"
    return "(" + _balanced(n // 2) + " + " + _balanced(n - n // 2) + ")"


def test_formula_tree_size_and_depth_limits():
    assert mc.formula_tree(_balanced(25)) is not None   # 25 leaves + 24 ops = 49 nodes, depth 6
    assert mc.formula_tree(_balanced(26)) is None       # 51 nodes
    assert mc.formula_tree("a" + " + (a" * 10 + ")" * 10) is not None
    assert mc.formula_tree("a" + " + (a" * 12 + ")" * 12) is None  # depth above 12


# ---------------------------------------------------------------- compile_tile

def _tile(model, names, **kw):
    r = mc.compile_tile(model, names, **kw)
    validate_readonly_sql(r.sql)
    return r


def test_tile_three_sum_measures_one_select(model):
    r = _tile(model, ["rolls_in", "kg_in", "dispatches"], grain="day", window_days=400, components=True)
    assert r.columns == ("metric_time__day", "rolls_in", "kg_in", "dispatches")
    tree = _parse(r.sql)
    assert isinstance(tree, exp.Select)
    assert [e.alias for e in tree.expressions] == list(r.columns)
    assert tree.args.get("order") is not None
    assert len(tree.args["joins"]) == 3
    assert all(j.args["on"].find(exp.NullSafeEQ) for j in tree.args["joins"])
    assert tree.find(exp.Union) is not None


def test_tile_each_measure_keys_on_its_own_time_column(model):
    r = _tile(model, ["kg_in", "dispatches"], grain="day", window_days=30)
    tree = _parse(r.sql)
    ctes = {c.alias: c.this.sql(dialect="postgres") for c in tree.ctes}
    assert "weigh_date" in ctes["p0"] and "dispatch_date" not in ctes["p0"]
    assert "dispatch_date" in ctes["p1"] and "weigh_date" not in ctes["p1"]


def test_tile_no_keys_uses_cross_join(model):
    r = _tile(model, ["kg_in", "dispatches"], window_days=30)
    assert r.columns == ("kg_in", "dispatches")
    tree = _parse(r.sql)
    assert tree.find(exp.Union) is None
    assert [j.args.get("kind") for j in tree.args["joins"]] == ["CROSS"]


def test_tile_avg_parts(model):
    r = _tile(model, ["avg_kg"], grain="day", window_days=30, components=True)
    assert r.columns == ("metric_time__day", "avg_kg", "avg_kg__sum", "avg_kg__count")
    meta = r.metas[0]
    assert meta["kind"] == "avg"
    assert meta["parts"] == {"sum": "avg_kg__sum", "count": "avg_kg__count"}
    assert "AVG(" in r.sql and "SUM(" in r.sql and "COUNT(" in r.sql


def test_tile_ratio_parts_apply_input_filters(model):
    r = _tile(model, ["return_rate"], grain="day", window_days=30, components=True)
    assert r.columns == ("metric_time__day", "return_rate", "return_rate__num", "return_rate__den")
    assert r.metas[0]["parts"] == {"num": "return_rate__num", "den": "return_rate__den"}
    ctes = {c.alias: c.this.sql(dialect="postgres") for c in _parse(r.sql).ctes}
    assert "<> 'void'" in ctes["p1"]      # the numerator input's own filter
    assert "<> 'void'" not in ctes["p2"]  # the denominator has none


def test_tile_formula_parts_and_tree(model):
    r = _tile(model, ["net_pct"], grain="day", window_days=30, components=True)
    assert r.columns == ("metric_time__day", "net_pct", "net_pct__in_a", "net_pct__in_b")
    meta = r.metas[0]
    assert meta["kind"] == "formula"
    assert meta["parts"] == {"a": "net_pct__in_a", "b": "net_pct__in_b"}
    assert meta["formula"]["op"] == "*"


def test_tile_metas_shape(model):
    r = _tile(model, ["kg_in", "stock_kg", "stock_open", "return_rate"], grain="day", window_days=30,
              components=True)
    by = {m["column"]: m for m in r.metas}
    assert set(by["kg_in"]) == {"column", "kind", "parts", "formula", "choice", "label", "description",
                                "date_basis"}
    assert by["kg_in"]["label"] == "Kg in" and by["kg_in"]["description"] == "Kilos received"
    assert by["kg_in"]["date_basis"] == "weigh date"
    assert by["stock_kg"]["choice"] == "closing" and by["stock_open"]["choice"] == "opening"
    assert by["kg_in"]["choice"] is None and by["kg_in"]["parts"] is None
    assert by["return_rate"]["date_basis"] == "weigh date"
    assert [m["column"] for m in r.metas] == ["kg_in", "stock_kg", "stock_open", "return_rate"]


def test_tile_part_name_too_long(model):
    with pytest.raises(InvalidRequest, match="is too long to store its parts"):
        mc.compile_tile(model, ["x" * 58], grain="day", window_days=30, components=True)


def test_tile_now_measure_cannot_mix(model):
    with pytest.raises(NotModeled, match="measure items_qty has no date; put it in its own tile"):
        mc.compile_tile(model, ["kg_in", "items_qty"], window_days=30)
    r = _tile(model, ["items_qty"], components=True)
    assert r.metas[0]["kind"] == "now" and r.metas[0]["date_basis"] is None


def test_tile_fixed_measure_refused_with_components(model):
    with pytest.raises(NotModeled, match="kg_growth compares periods or is not additive, so it cannot follow"):
        mc.compile_tile(model, ["kg_growth"], grain="day", window_days=30, components=True)
    # a fixed-window tile may still show it
    assert _tile(model, ["kg_flagged"], grain="day", window_days=30).columns[-1] == "kg_flagged"


def test_tile_limits(model):
    with pytest.raises(InvalidRequest):
        mc.compile_tile(model, [], window_days=30)
    seven = ["rolls_in", "kg_in", "kg_returned", "dispatches", "kg_out", "avg_kg", "min_kg"]
    with pytest.raises(InvalidRequest):
        mc.compile_tile(model, seven, window_days=30)
    with pytest.raises(InvalidRequest):
        mc.compile_tile(model, ["kg_in", "kg_in"], window_days=30)
    _tile(model, seven[:6], grain="day", window_days=30)


def test_tile_by_dimension_and_naive(model):
    r = _tile(model, ["events_amount", "kg_in"], by=["public.customers.name"], window_days=30, naive_utc=True)
    assert r.columns == ("customers__name", "events_amount", "kg_in")
    assert any("read as UTC" in n for n in r.notes)
    assert "rolls_customer" in r.joins and "events_customer" in r.joins


def test_tile_cte_names_are_quoted(model):
    r = _tile(model, ["kg_in", "avg_kg"], by=["public.customers.name"], window_days=30, components=True)
    assert " FROM p" not in r.sql and " JOIN p" not in r.sql


# ---------------------------------------------------------------- compile_presets (C-05)

def test_presets_sql_shape(model):
    c = mc.compile_presets(model, "distinct_customers", presets=list(mc.DASH_PRESETS), fy_start_month=4)
    validate_readonly_sql(c.sql)
    assert c.columns == ("preset", "period", "value", "today")
    assert c.sql.count("CURRENT_DATE") == 1
    tree = _parse(c.sql)
    top = [s for s in tree.find_all(exp.Select) if s.expressions and s.expressions[0].alias == "preset"]
    assert len(top) == 27
    for s in top:
        today = s.expressions[3]
        assert today.alias == "today"
        fn = today.this
        assert isinstance(fn, exp.TimeToStr)
        assert fn.this.this.sql(dialect="postgres") == 'SELECT "d" FROM "_today"'
        assert fn.args["format"].name == "%Y-%m-%d"
    pairs = {(s.expressions[0].this.name, s.expressions[1].this.name) for s in top}
    assert pairs == {(p, q) for p in mc.DASH_PRESETS for q in mc.DASH_PERIODS}
    assert "TO_CHAR((SELECT \"d\" FROM \"_today\"), 'YYYY-MM-DD')" in c.sql
    assert "AS TEXT" not in c.sql


def test_presets_apply_measure_and_request_filters(model):
    c = mc.compile_presets(model, "distinct_customers",
                           filters=[dict(dimension="public.rolls.status", op="in", values=["ok"])],
                           presets=["today", "this_fy"], fy_start_month=4)
    assert c.sql.count("<> 'void'") == 6
    assert c.sql.count("IN ('ok')") == 6


def test_presets_need_distinct_measure(model):
    with pytest.raises(InvalidRequest):
        mc.compile_presets(model, "kg_in", presets=["today"])
    with pytest.raises(NotModeled):
        mc.compile_presets(model, "nope", presets=["today"])
    with pytest.raises(InvalidRequest):
        mc.compile_presets(model, "distinct_customers", presets=["last_century"])


def test_presets_fy_month_frozen_into_sql(model):
    a = mc.compile_presets(model, "distinct_customers", presets=["this_fy"], fy_start_month=4)
    b = mc.compile_presets(model, "distinct_customers", presets=["this_fy"], fy_start_month=10)
    assert "< 4" in a.sql and "< 10" in b.sql


# ---------------------------------------------------------------- Top N plus Other (C-07)

BY = ["public.customers.name"]


def test_top_other_columns_and_shape(model):
    r = _tile(model, ["kg_in"], by=BY, window_days=30, top=8, other=True)
    assert r.columns == ("customers__name", "kg_in")
    sql = r.sql
    assert "'Other'" in sql
    assert "HAVING COUNT(*) > 0" in sql
    assert "_rn" in sql and "<= 8" in sql and "> 8" in sql
    # the by column is cast to text in the ranked branch and 'Other' is text in the other
    assert 'CAST("customers__name" AS TEXT)' in sql
    tree = _parse(sql)
    outer = tree
    assert [e.alias_or_name for e in outer.expressions] == ["customers__name", "kg_in"]  # no _rn, _o
    assert tree.find(exp.Window) is not None


def test_top_other_sums_every_measure_and_ranks_by_first(model):
    r = _tile(model, ["kg_in", "rolls_in"], by=BY, window_days=30, top=5, other=True)
    assert r.columns == ("customers__name", "kg_in", "rolls_in")
    assert 'SUM("kg_in"), SUM("rolls_in")' in r.sql
    assert 'ORDER BY "kg_in" DESC NULLS LAST' in r.sql
    assert [m["column"] for m in r.metas] == ["kg_in", "rolls_in"]


def test_top_other_needs_additive_measures(model):
    with pytest.raises(InvalidRequest, match="other needs measures that add up; avg_kg does not"):
        mc.compile_tile(model, ["kg_in", "avg_kg"], by=BY, window_days=30, top=5, other=True)
    with pytest.raises(InvalidRequest, match="other needs measures that add up; stock_kg does not"):
        mc.compile_tile(model, ["stock_kg"], by=["public.stock.snapshot_date"], window_days=30, top=5,
                        other=True)


def test_top_without_other_keeps_v1_semantics(model):
    r = _tile(model, ["kg_in", "rolls_in"], by=BY, window_days=30, top=8)
    assert r.columns == ("customers__name", "kg_in", "rolls_in")
    assert 'ORDER BY "kg_in" DESC NULLS LAST' in r.sql
    assert r.sql.rstrip().endswith("LIMIT 8")
    assert "'Other'" not in r.sql


@pytest.mark.parametrize("kw", [
    dict(by=BY, grain="day", top=5),                               # grain
    dict(by=[], top=5),                                            # no by
    dict(by=BY + ["public.rolls.status"], top=5),                  # two by
    dict(by=BY),                                                   # no top
])
def test_other_refused_shapes(model, kw):
    with pytest.raises(InvalidRequest):
        mc.compile_tile(model, ["kg_in"], window_days=30, other=True, **kw)


def test_top_range_checked(model):
    with pytest.raises(InvalidRequest):
        mc.compile_tile(model, ["kg_in"], by=BY, window_days=30, top=0)


def test_other_sql_passes_guardrails(model):
    r = mc.compile_tile(model, ["kg_in", "dispatches"], by=BY, window_days=30, top=3, other=True)
    validate_readonly_sql(r.sql)
    assert "_t" in r.sql


# ---------------------------------------------------------------- review fixes (batch A)

def _count_ratio_model():
    return build([_m(name="count_div", type="derived", expr="a / b",
                     input_metrics=[dict(name="rolls_in", alias="a"), dict(name="dispatches", alias="b")])])


def test_tile_derived_over_counts_divides_as_numeric():
    m = _count_ratio_model()
    r = mc.compile_tile(m, ["count_div"], grain="day", window_days=30, components=True)
    assert r.metas[0]["kind"] == "formula"
    assert 'CAST("m0"."v" AS DECIMAL) / CAST("m1"."v" AS DECIMAL)' in r.sql


def test_v1_compile_measure_derived_sql_is_unchanged():
    m = _count_ratio_model()
    c = compile_measure(m, make_request("count_div", grain="day", window_days=30))
    assert 'CAST("m0"."v"' not in c.sql
    assert '"m0"."v" / "m1"."v"' in c.sql


def test_preset_bounds_sql_current_date_occurs_once():
    from app import dashdates as dd
    sql = mc.preset_bounds_sql(dd.PRESETS, 4)
    assert sql.count("CURRENT_DATE") == 1
    assert sql.count("(SELECT \"d\" FROM \"_today\")") > 27
