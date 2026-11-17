"""Quick 054: a simple measure may add up a column of a table linked many-to-one.

Mill asked for checked gross profit: cost sits on rolls, sale value on dispatch_lines, sale
date on dispatches. The compiler refused any qualified column in an expression, so cost of sold
rolls needed a warehouse view. Now expr may name schema.table.column of a relation reached along
approved many-to-one paths (the same paths filters and time columns use); the many side stays
refused. No DB, no LLM: the real-Postgres approval lives in test_model_db.py."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from app import modelcompiler as mc
from app import modeflip, modeltools, skillservice
from app.guardrails import validate_readonly_sql
from app.modelcompiler import (
    InvalidRequest, NotModeled, compile_measure, expr_relations, make_request, normalize_definition,
    validate_expr,
)
from test_model_compiler import Q3, _m, build

COGS = _m(name="cogs", type="simple", base_relation="public.order_lines", agg="sum",
          expr="qty * public.products.unit_cost", agg_time_dimension="public.sales_orders.order_date")
MARGIN = _m(name="gross_margin_pct", type="derived", expr="100.0 * (s - c) / nullif(s, 0)",
            input_metrics=[dict(name="net_sales", alias="s"), dict(name="cogs", alias="c")])
COGS_RATIO = _m(name="cost_share", type="ratio", numerator=dict(name="cogs"),
                denominator=dict(name="net_sales"))
STOCK_VALUE = _m(name="stock_value", type="simple", base_relation="public.inventory_snapshots", agg="sum",
                 expr="on_hand * public.products.unit_cost",
                 agg_time_dimension="public.inventory_snapshots.snapshot_date",
                 non_additive_dimension=dict(name="public.inventory_snapshots.snapshot_date",
                                             window_choice="max", window_groupings=[]),
                 additivity="semi_additive")
CATEGORIES_SOLD = _m(name="categories_sold", type="simple", base_relation="public.order_lines",
                     agg="count_distinct", expr="public.products.category",
                     agg_time_dimension="public.sales_orders.order_date")
THREE_HOPS = _m(name="lines_region_tagged", type="simple", base_relation="public.order_lines", agg="count",
              expr="public.regions.id", agg_time_dimension="public.sales_orders.order_date")
EXTRA = [COGS, MARGIN, COGS_RATIO, STOCK_VALUE, CATEGORIES_SOLD, THREE_HOPS]

GOLDEN_COGS = (
    'SELECT CAST(DATE_TRUNC(\'MONTH\', CAST("t1"."order_date" AS TIMESTAMP)) AS DATE) AS "metric_time__month", '
    'SUM("t0"."qty" * "t2"."unit_cost") AS "cogs" '
    'FROM "public"."order_lines" AS "t0" '
    'LEFT JOIN "public"."sales_orders" AS "t1" ON "t0"."order_id" = "t1"."id" '
    'LEFT JOIN "public"."products" AS "t2" ON "t0"."product_id" = "t2"."id" '
    'WHERE "t1"."order_date" >= CAST(\'2026-07-01\' AS DATE) AND "t1"."order_date" < CAST(\'2026-10-01\' AS DATE) '
    'GROUP BY 1 ORDER BY 1'
)


@pytest.fixture(scope="module")
def model():
    return build(extra_measures=EXTRA)


def _sql(model, name, **kw):
    c = compile_measure(model, make_request(name, **kw))
    validate_readonly_sql(c.sql)
    return c


# ---------------------------------------------------------------- definitions


@pytest.mark.parametrize("expr", [
    "qty * public.products.unit_cost", 'public.rolls."Meters"', "public.order_lines.qty",
    "coalesce(sale_value, round(coalesce(sale_rate, public.dispatches.sale_rate, 0) * CASE WHEN "
    "coalesce(sale_rate_basis, public.dispatches.sale_rate_basis) = 'Meter' THEN coalesce(public.rolls.\"Meters\", 0) "
    "ELSE public.rolls.net_weight END, 2))",
])
def test_simple_measure_accepts_schema_table_column(expr):
    d = normalize_definition("measure", dict(name="x", type="simple", base_relation="public.order_lines",
                                             agg="sum", expr=expr))
    assert d["expr"] == expr


@pytest.mark.parametrize("expr, words", [
    ("products.unit_cost", "schema.table.column"),
    ("cat.public.products.unit_cost", "schema.table.column"),
    ('"".products.unit_cost', "schema.table.column"),
])
def test_two_or_four_part_names_refused(expr, words):
    with pytest.raises(InvalidRequest, match=words):
        normalize_definition("measure", dict(name="x", type="simple", base_relation="public.order_lines",
                                             agg="sum", expr=expr))


def test_derived_expressions_and_public_validate_expr_stay_unqualified():
    with pytest.raises(InvalidRequest, match="qualified columns are not allowed"):
        normalize_definition("measure", dict(name="x", type="derived", expr="a - public.products.unit_cost",
                                             input_metrics=[dict(name="net_sales", alias="a")]))
    with pytest.raises(InvalidRequest, match="qualified columns are not allowed"):
        validate_expr("public.products.unit_cost", {"unit_cost": "numeric"})


def test_expr_relations():
    assert expr_relations("qty * public.products.unit_cost + public.products.unit_price") == ("public.products",)
    assert expr_relations("coalesce(a, public.rolls.x, public.dispatches.y)") == (
        "public.dispatches", "public.rolls")
    for junk in (None, "", "qty * unit_price", "x;", "((", "products.unit_cost", 42):
        assert expr_relations(junk) == ()


# ---------------------------------------------------------------- compile


def test_cogs_golden_and_other_measures_not_disturbed(model):
    """Pre-change SQL of existing measures is pinned by the goldens in test_model_compiler.py."""
    c = _sql(model, "cogs", grain="month", **Q3)
    assert c.sql == GOLDEN_COGS
    assert "cogs counts public.products values once per public.order_lines row" in c.notes
    plain = build()
    for name in ("net_sales", "order_count", "stock_on_hand", "average_order_value"):
        r = make_request(name, grain="month", **Q3)
        assert compile_measure(model, r).sql == compile_measure(plain, r).sql


def test_linked_table_joined_once_when_also_grouped(model):
    c = _sql(model, "cogs", by=["public.products.category"], grain="month", **Q3)
    assert c.sql.count('LEFT JOIN "public"."products"') == 1
    assert '"t2"."category" AS "products__category"' in c.sql and 'SUM("t0"."qty" * "t2"."unit_cost")' in c.sql
    assert "order_lines_product" in c.joins


def test_three_hops_and_no_time_request(model):
    c = _sql(model, "lines_region_tagged", grain="month", **Q3)
    assert 'LEFT JOIN "public"."regions"' in c.sql and 'COUNT("t' in c.sql
    assert 'SUM("t0"."qty" * "t1"."unit_cost")' in _sql(model, "cogs").sql  # no date: products is t1


def test_many_side_refused_in_plain_words():
    bad = _m(name="order_qty", type="simple", base_relation="public.sales_orders", agg="sum",
             expr="public.order_lines.qty", agg_time_dimension="public.sales_orders.order_date")
    with pytest.raises(NotModeled) as e:
        compile_measure(build(extra_measures=[bad]), make_request("order_qty", grain="month", **Q3))
    msg = str(e.value)
    assert "many side" in msg and "inflate" in msg and "Base the measure on public.order_lines" in msg


def test_no_path_and_unknown_column_refused():
    m = build(extra_columns={"public.orphans": {"id": "integer", "cost": "numeric"}},
              extra_measures=[_m(name="o", type="simple", base_relation="public.order_lines", agg="sum",
                                 expr="public.orphans.cost"),
                              _m(name="u", type="simple", base_relation="public.order_lines", agg="sum",
                                 expr="public.products.nope")])
    with pytest.raises(NotModeled, match="no approved relationship path from public.order_lines to public.orphans"):
        compile_measure(m, make_request("o"))
    with pytest.raises(NotModeled, match="unknown column public.products.nope .schema, table and column are matched exactly"):
        compile_measure(m, make_request("u"))


def test_hop_limit_keeps_its_own_message_on_a_one_to_one_chain():
    cols = {f"public.x{i}": {"id": "integer", "v": "numeric"} for i in range(1, 6)}
    rels = [dict(name=f"x{i}_x{i + 1}", from_relation=f"public.x{i}", from_columns=["id"],
                 to_relation=f"public.x{i + 1}", to_columns=["id"], cardinality="one_to_one", status="active")
            for i in range(1, 5)]
    m = build(extra_columns=cols, extra_rels=rels,
              extra_measures=[_m(name="far", type="simple", base_relation="public.x1", agg="sum",
                                 expr="public.x5.v")])
    with pytest.raises(NotModeled) as e:
        compile_measure(m, make_request("far"))
    assert "4 hops" in str(e.value) and "many side" not in str(e.value)


def test_ambiguous_path_refused():
    extra = dict(name="order_lines_alt_product", from_relation="public.order_lines", from_columns=["id"],
                 to_relation="public.products", to_columns=["id"], cardinality="many_to_one", status="active")
    with pytest.raises(NotModeled, match="ambiguous"):
        compile_measure(build(extra_rels=[extra], extra_measures=[COGS]), make_request("cogs"))


def test_semi_additive_value_uses_the_linked_alias(model):
    sql = _sql(model, "stock_value", grain="month", **Q3).sql
    assert '"t0"."on_hand" * "t1"."unit_cost" AS "val"' in sql
    assert 'LEFT JOIN "public"."products" AS "t1"' in sql


def test_ratio_and_derived_over_a_linked_measure(model):
    for name in ("gross_margin_pct", "cost_share"):
        sql = _sql(model, name, grain="month", **Q3).sql
        assert 'SUM("t0"."qty" * "t2"."unit_cost")' in sql


def test_tile_and_presets_paths(model):
    t = mc.compile_tile(model, ["cogs", "net_sales"], grain="month", window_days=90)
    validate_readonly_sql(t.sql)
    assert '"unit_cost"' in t.sql and 'LEFT JOIN "public"."products"' in t.sql
    p = mc.compile_presets(model, "categories_sold", presets=["today", "this_fy"], fy_start_month=4)
    validate_readonly_sql(p.sql)
    assert 'COUNT(DISTINCT "t' in p.sql and '"category"' in p.sql


def test_deterministic_across_row_order():
    r = make_request("cogs", by=["public.products.category"], grain="month", **Q3)
    sqls = {compile_measure(build(rows_shuffled=s, extra_measures=EXTRA), r).sql for s in range(6)}
    assert len(sqls) == 1


# ---------------------------------------------------------------- refs and dependents


def test_model_refs_include_expression_relations():
    d = normalize_definition("measure", dict(name="x", type="simple", base_relation="public.order_lines",
                                             agg="sum", expr="qty * model.costs.unit_cost",
                                             agg_time_dimension="public.sales_orders.order_date"))
    refs = modeltools._model_refs("measure", d)
    assert "model.costs" in refs and "public.order_lines" in refs


class _Pool:
    def __init__(self, data):
        self.data = data

    async def fetch(self, sql, *a):
        for k, v in self.data.items():
            if k in sql:
                return v
        return []


async def test_dependents_count_a_measure_whose_expression_reads_the_table(monkeypatch):
    pool = _Pool({"FROM model_measures": [
        {"name": "cogs", "base_relation": "public.order_lines", "expr": "qty * public.products.unit_cost"},
        {"name": "rev", "base_relation": "public.order_lines", "expr": "qty * unit_price"}]})

    async def gp():
        return pool
    monkeypatch.setattr(modeflip, "get_pool", gp)
    monkeypatch.setattr(modeflip, "_search_path", lambda: ["public"])
    d = await modeflip.dependents("public", "products")
    assert d["model_objects"] == 1 and d["titles"]["model_objects"] == ["cogs"]


def test_measure_relations():
    d = dict(type="simple", base_relation="public.order_lines", expr="qty * model.costs.unit_cost",
             agg_time_dimension="public.sales_orders.order_date",
             filter=[dict(dimension="model.flags.ok", op="=", values=[True])],
             non_additive_dimension=dict(name="public.snap.day", window_groupings=["public.wh.name"]))
    assert mc.measure_relations(d) == {"public.order_lines", "model.costs", "public.sales_orders",
                                       "model.flags", "public.snap", "public.wh"}
    assert mc.measure_relations(dict(type="derived", expr="a - public.x.y")) == set()


@pytest.mark.parametrize("measure", [
    dict(name="cogs", type="simple", base_relation="public.order_lines", expr="qty * model.costs.unit_cost"),
    dict(name="n", type="simple", base_relation="public.order_lines", expr="qty",
         filter=[dict(dimension="model.costs.kind", op="=", values=["x"])]),
])
def test_retiring_a_view_a_measure_reads_is_refused(monkeypatch, measure):
    monkeypatch.setattr(modeltools.warehouse, "warehouse_mode", lambda: True)
    st = modeltools._State({"tables": [], "measures": [measure]}, None, set(), None, None)
    with pytest.raises(modeltools._Refused, match="an active measure is built on this view"):
        modeltools._check_state(st, "view", "retire", "costs", {}, {"name": "costs"})
    modeltools._check_state(st, "view", "retire", "other", {}, {"name": "other"})


# ---------------------------------------------------------------- skill

ROOT = Path(__file__).resolve().parents[2]
SKILL = skillservice.library_root() / "intelligence-corner" / "data-modeling" / "SKILL.md"
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
PREV_MD5 = "167c9339cb9ddf3f9486281521b43303"  # unedited Quick 037 body


def _text():
    return SKILL.read_bytes().decode("ascii").replace("\r\n", "\n")


def _md5(t):
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def test_skill_teaches_linked_columns_and_examples_compile():
    t = _text()
    assert "schema.table.column" in t and "no view and no warehouse" in t and "many side" in t
    assert "a derived measure cannot use another derived measure" in t
    assert "once per base row" in t and "credit limit" in t and "same filters" in t and "read as it is now" in t
    shapes = t[t.index("## Item shapes"):t.index("## Naming")]
    items = [json.loads(m) for m in re.findall(r"^[A-Z][^:{]*: (\{.*\})$", shapes, re.M)]
    measures = [i for i in items if i.get("object") == "measure"]
    assert len(measures) >= 6
    for i in measures:
        d = normalize_definition("measure", {k: v for k, v in i.items() if k != "object"})
        if d["type"] == "simple":
            assert d["base_relation"], i["name"]
    cogs = next(i for i in measures if i["name"] == "cost_of_goods_sold")
    net = next(i for i in measures if i["name"] == "net_sales")
    assert cogs["filter"] == net["filter"], "cost carries the revenue filters"
    margin = next(i for i in measures if i["name"] == "gross_margin_pct")
    assert margin["expr"].startswith("100.0 *"), "no integer division"
    d = normalize_definition("measure", {k: v for k, v in cogs.items() if k != "object"})
    m = build(extra_measures=[_m(**{**d, "status": "active"})])
    assert '"unit_cost"' in compile_measure(m, make_request("cost_of_goods_sold", grain="month", **Q3)).sql


def test_both_schema_copies_equal_the_file_and_the_guard_takes_the_previous_body():
    for tag in ("$skill_data_modeling$", "$p16_data_modeling$"):
        assert SCHEMA.count(tag) == 2
        a = SCHEMA.index(tag) + len(tag)
        assert SCHEMA[a:SCHEMA.index(tag, a)].strip() == _text().strip(), tag
    end = SCHEMA.index("$p16_data_modeling$", SCHEMA.index("$p16_data_modeling$") + 1)
    guard = SCHEMA[end:SCHEMA.index(");", end)]
    assert PREV_MD5 in guard and _md5(_text()) not in guard
    fx = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v4.md").read_bytes().decode("ascii")
    assert _md5(fx) == PREV_MD5 and "schema.table.column" not in fx
    assert SCHEMA.count("-- Quick 054:") == 1
