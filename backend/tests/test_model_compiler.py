"""Phase 16 plan 02: the pure measure compiler (MODEL-04). No DB, no LLM, no clock."""
import ast
import itertools
import random
from datetime import date
from pathlib import Path

import pytest

from app import modelcompiler as mc
from app.guardrails import validate_readonly_sql
from app.modelcompiler import (
    InvalidRequest, NotModeled, canonical_request, compile_measure, derived_select_refs,
    make_request, measure_time_dimension, model_from_rows, normalize_definition, plan_joins,
    supports_time, validate_expr,
)

Q3 = dict(start="2026-07-01", end="2026-10-01")

COLUMNS = {
    "public.regions": {"id": "integer", "name": "text"},
    "public.sales_reps": {"id": "integer", "full_name": "text", "region_id": "integer",
                          "hired_on": "date", "is_active": "boolean"},
    "public.customers": {"id": "integer", "name": "text", "segment": "text", "region_id": "integer",
                         "rep_id": "integer", "city": "text", "credit_limit": "numeric(12,2)",
                         "created_at": "date", "is_active": "boolean"},
    "public.products": {"id": "integer", "sku": "text", "name": "text", "category": "text",
                        "unit_price": "numeric(10,2)", "unit_cost": "numeric(10,2)",
                        "is_discontinued": "boolean"},
    "public.sales_orders": {"id": "integer", "customer_id": "integer", "order_date": "date",
                            "status": "text", "channel": "text"},
    "public.order_lines": {"id": "integer", "order_id": "integer", "product_id": "integer",
                           "qty": "integer", "unit_price": "numeric(10,2)",
                           "discount_pct": "numeric(5,2)"},
    "public.invoices": {"id": "integer", "order_id": "integer", "customer_id": "integer",
                        "invoice_date": "date", "due_date": "date", "amount": "numeric(12,2)"},
    "public.payments": {"id": "integer", "invoice_id": "integer", "paid_at": "date",
                        "amount": "numeric(12,2)", "method": "text"},
    "public.inventory_snapshots": {"product_id": "integer", "snapshot_date": "date",
                                   "on_hand": "integer"},
}

FKS = [
    ("sales_reps_region", "sales_reps", "region_id", "regions"),
    ("customers_region", "customers", "region_id", "regions"),
    ("customers_rep", "customers", "rep_id", "sales_reps"),
    ("sales_orders_customer", "sales_orders", "customer_id", "customers"),
    ("order_lines_order", "order_lines", "order_id", "sales_orders"),
    ("order_lines_product", "order_lines", "product_id", "products"),
    ("invoices_order", "invoices", "order_id", "sales_orders"),
    ("invoices_customer", "invoices", "customer_id", "customers"),
    ("payments_invoice", "payments", "invoice_id", "invoices"),
    ("inventory_snapshots_product", "inventory_snapshots", "product_id", "products"),
]
RELS = [dict(name=n, from_relation=f"public.{f}", from_columns=[fc], to_relation=f"public.{t}",
             to_columns=["id"], cardinality="many_to_one", status="active") for n, f, fc, t in FKS]

TABLES = [dict(name=r.split(".")[1], relation=r, object_kind="base", role=None, grain=None,
               primary_key=[], agg_time_dimension=None, status="active") for r in COLUMNS]
for t in TABLES:
    if t["relation"] == "public.sales_orders":
        t["agg_time_dimension"] = "public.sales_orders.order_date"


def _m(**kw):
    base = dict(label=None, description=None, base_relation=None, agg=None, expr=None,
                agg_time_dimension=None, filter=[], numerator=None, denominator=None,
                input_metrics=None, non_additive_dimension=None, additivity="additive",
                unit=None, format=None, status="active")
    base.update(kw)
    return base


MEASURES = [
    _m(name="order_count", type="simple", base_relation="public.sales_orders", agg="count", expr="id",
       agg_time_dimension="public.sales_orders.order_date",
       filter=[dict(dimension="public.sales_orders.status", op="not_in", values=["cancelled"])]),
    _m(name="net_sales", type="simple", base_relation="public.order_lines", agg="sum",
       expr="qty * unit_price * (1 - discount_pct / 100)",
       agg_time_dimension="public.sales_orders.order_date",
       filter=[dict(dimension="public.sales_orders.status", op="not_in", values=["cancelled", "returned"])]),
    _m(name="invoiced_amount", type="simple", base_relation="public.invoices", agg="sum", expr="amount",
       agg_time_dimension="public.invoices.invoice_date"),
    _m(name="collected_amount", type="simple", base_relation="public.payments", agg="sum", expr="amount",
       agg_time_dimension="public.payments.paid_at"),
    _m(name="average_order_value", type="ratio", numerator=dict(name="net_sales"),
       denominator=dict(name="order_count")),
    _m(name="stock_on_hand", type="simple", base_relation="public.inventory_snapshots", agg="sum",
       expr="on_hand", agg_time_dimension="public.inventory_snapshots.snapshot_date",
       non_additive_dimension=dict(name="public.inventory_snapshots.snapshot_date", window_choice="max",
                                   window_groupings=[]), additivity="semi_additive"),
    _m(name="net_sales_mom_growth", type="derived", expr="(current - previous) / NULLIF(previous, 0)",
       input_metrics=[dict(name="net_sales", alias="current"),
                      dict(name="net_sales", alias="previous", offset_window="1 month")]),
    _m(name="customer_count", type="simple", base_relation="public.customers", agg="count", expr="id"),
    _m(name="customer_ratio", type="ratio", numerator=dict(name="net_sales"),
       denominator=dict(name="customer_count")),
    _m(name="customer_growth", type="derived", expr="a - b",
       input_metrics=[dict(name="customer_count", alias="a"),
                      dict(name="customer_count", alias="b", offset_window="1 month")]),
]


def build(rows_shuffled=None, extra_rels=(), drop_rels=(), extra_columns=None, extra_measures=()):
    rels = [r for r in RELS if r["name"] not in drop_rels] + list(extra_rels)
    tables, measures = list(TABLES), list(MEASURES) + list(extra_measures)
    cols = {k: dict(v) for k, v in COLUMNS.items()}
    cols.update(extra_columns or {})
    if rows_shuffled is not None:
        rng = random.Random(rows_shuffled)
        for lst in (rels, tables, measures):
            rng.shuffle(lst)
    return model_from_rows(tables, rels, measures, cols)


@pytest.fixture(scope="module")
def model():
    return build()


def req(measure, **kw):
    return make_request(measure, **kw)


GOLDEN_NET_SALES = (
    'SELECT CAST(DATE_TRUNC(\'MONTH\', CAST("t1"."order_date" AS TIMESTAMP)) AS DATE) AS "metric_time__month", '
    '"t3"."name" AS "regions__name", '
    'SUM("t0"."qty" * "t0"."unit_price" * (1 - "t0"."discount_pct" / 100)) AS "net_sales" '
    'FROM "public"."order_lines" AS "t0" '
    'LEFT JOIN "public"."sales_orders" AS "t1" ON "t0"."order_id" = "t1"."id" '
    'LEFT JOIN "public"."customers" AS "t2" ON "t1"."customer_id" = "t2"."id" '
    'LEFT JOIN "public"."regions" AS "t3" ON "t2"."region_id" = "t3"."id" '
    'WHERE NOT "t1"."status" IN (\'cancelled\', \'returned\') '
    'AND "t1"."order_date" >= CAST(\'2026-07-01\' AS DATE) AND "t1"."order_date" < CAST(\'2026-10-01\' AS DATE) '
    'GROUP BY 1, 2 ORDER BY 1, 2'
)


# ---------------------------------------------------------------- make_request

def test_filters_canonical_order_and_dedupe(model):
    f1 = {"dimension": "public.customers.segment", "op": "in", "values": ["Retail", "Online"]}
    f2 = {"dimension": "public.sales_orders.channel", "op": "=", "values": ["web"]}
    a = compile_measure(model, req("order_count", filters=[f1, f2]))
    b = compile_measure(model, req("order_count", filters=[f2, f1, f1]))
    assert a.sql == b.sql
    assert len(req("order_count", filters=[f1, f1]).filters) == 1


@pytest.mark.parametrize("kw", [
    dict(filters=[{"dimension": "public.customers.segment", "op": "like", "values": ["x"]}]),
    dict(filters=[{"dimension": "public.customers.segment", "op": "=", "values": ["a;b"]}]),
    dict(filters=[{"dimension": "public.customers.segment", "op": "in", "values": [str(i) for i in range(51)]}]),
    dict(filters=[{"dimension": "public.customers.segment", "op": "=", "values": ["x" * 201]}]),
    dict(grain="hour"),
    dict(start="2026-13-01"),
    dict(start="2026-10-01", end="2026-09-01"),
    dict(by=[f"public.t{i}.c" for i in range(6)]),
    dict(filters=[{"dimension": f"public.t.c{i}", "op": "is_null"} for i in range(11)]),
    dict(window_days=0), dict(window_days=3661),
    dict(window_days=5, start="2026-01-01"),
    dict(by=["public.a.name", "other.a.name"]),
])
def test_make_request_rejects(kw):
    with pytest.raises(InvalidRequest):
        make_request("order_count", **kw)


def test_make_request_accepts_iso_strings():
    r = make_request("order_count", start="2026-07-01", end="2026-10-01")
    assert r.start == date(2026, 7, 1) and r.end == date(2026, 10, 1)


# ---------------------------------------------------------------- validate_expr

@pytest.mark.parametrize("e", [
    "qty * unit_price * (1 - discount_pct / 100)",
    "CASE WHEN status = 'paid' THEN amount ELSE 0 END",
    "COALESCE(amount, 0)", "NULLIF(qty, 0)",
])
def test_validate_expr_accepts(e):
    cols = {"qty": "integer", "unit_price": "numeric", "discount_pct": "numeric", "status": "text",
            "amount": "numeric"}
    assert validate_expr(e, cols) is not None


@pytest.mark.parametrize("e", ["(SELECT 1)", "pg_sleep(1)", "other.amount", "sum(amount)",
                               "row_number() OVER ()", "amount; DROP", "nope + 1", "*"])
def test_validate_expr_rejects(e):
    with pytest.raises(InvalidRequest):
        validate_expr(e, {"amount": "numeric"})


def test_validate_expr_star_only_when_allowed():
    assert validate_expr("*", {}, allow_star=True) is not None


# ---------------------------------------------------------------- normalize_definition

def test_normalize_measure_rejections():
    ok = dict(name="m", type="simple", base_relation="public.t", agg="sum", expr="x")
    assert normalize_definition("measure", ok)["name"] == "m"
    bad = [
        {**ok, "name": "Bad Name"}, {**ok, "agg": "median"}, {**ok, "base_relation": None},
        {**ok, "agg": None}, {**ok, "filter": [{"dimension": "nope", "op": "=", "values": ["a"]}]},
        {**ok, "expr": "sum(x)"},
        dict(name="r", type="ratio", numerator=dict(name="a")),
        dict(name="d", type="derived", expr="a"),
        dict(name="d", type="derived", expr="a - b",
             input_metrics=[dict(name="x", alias="a", offset_window="2 fortnights"), dict(name="y", alias="b")]),
        dict(name="d", type="derived", expr="a - zz", input_metrics=[dict(name="x", alias="a")]),
    ]
    for b in bad:
        with pytest.raises(InvalidRequest):
            normalize_definition("measure", b)


def test_normalize_relationship_table_index():
    base = dict(name="r1", from_relation="public.a", from_columns=["x"], to_relation="public.b", to_columns=["id"])
    assert normalize_definition("relationship", base)["cardinality"] == "many_to_one"
    for bad in [{**base, "to_columns": ["id", "id2"]}, {**base, "cardinality": "many_to_many"},
                {**base, "from_columns": list("abcde"), "to_columns": list("abcde")}]:
        with pytest.raises(InvalidRequest):
            normalize_definition("relationship", bad)
    with pytest.raises(InvalidRequest):
        normalize_definition("table", dict(name="t", relation="public.t", role="bridge"))
    assert normalize_definition("table", dict(name="t", relation="public.t", role="fact"))["role"] == "fact"
    with pytest.raises(InvalidRequest):
        normalize_definition("index", dict(name="ix", relation="public.t", columns=["a"]))
    with pytest.raises(InvalidRequest):
        normalize_definition("index", dict(name="Bad-Ix", relation="model.t", columns=["a"]))
    assert normalize_definition("index", dict(name="ix", relation="model.t", columns=["a"]))["relation"] == "model.t"


def test_normalize_view_stores_sql_and_refs():
    out = normalize_definition("view", dict(
        name="v", sql="SELECT p.category, SUM(l.qty) AS units FROM public.order_lines l "
                      "JOIN public.products p ON p.id = l.product_id GROUP BY 1"))
    assert out["refs"] == ["public.order_lines", "public.products"] and "SELECT" in out["sql"]
    with pytest.raises(InvalidRequest):
        normalize_definition("view", dict(name="v", sql="SELECT * FROM orders"))
    with pytest.raises(InvalidRequest):
        normalize_definition("nothing", {})


# ---------------------------------------------------------------- simple compile

def test_order_count_simple(model):
    c = compile_measure(model, req("order_count"))
    assert c.sql == ('SELECT COUNT("t0"."id") AS "order_count" FROM "public"."sales_orders" AS "t0" '
                     'WHERE NOT "t0"."status" IN (\'cancelled\')')
    assert c.columns == ("order_count",) and c.joins == ()
    assert validate_readonly_sql(c.sql)


def test_net_sales_golden(model):
    c = compile_measure(model, req("net_sales", by=["public.regions.name"], grain="month", **Q3))
    assert c.sql == GOLDEN_NET_SALES
    assert c.columns == ("metric_time__month", "regions__name", "net_sales")
    assert c.joins == ("order_lines_order", "sales_orders_customer", "customers_region")
    assert "CURRENT_DATE" not in c.sql


def test_timestamptz_time_column():
    cols = {"public.events": {"id": "integer", "at": "timestamp with time zone"}}
    ms = [_m(name="ev", type="simple", base_relation="public.events", agg="count", expr="id",
             agg_time_dimension="public.events.at")]
    m = model_from_rows([], [], ms, cols)
    c = compile_measure(m, req("ev", grain="day"))
    assert "CAST(DATE_TRUNC('DAY', \"t0\".\"at\") AS DATE)" in c.sql


def test_fan_out_refused(model):
    with pytest.raises(NotModeled):
        compile_measure(model, req("order_count", by=["public.products.category"]))
    with pytest.raises(NotModeled, match="repeat rows"):
        compile_measure(model, req("order_count", by=["public.order_lines.qty"]))


def test_no_path_names_relation():
    m = build(extra_columns={"public.orphans": {"id": "integer", "tag": "text"}})
    with pytest.raises(NotModeled, match="public.orphans"):
        compile_measure(m, req("order_count", by=["public.orphans.tag"]))


def test_ambiguous_equal_length_paths():
    extra = dict(name="products_to_reps", from_relation="public.products", from_columns=["id"],
                 to_relation="public.sales_reps", to_columns=["id"], cardinality="many_to_one", status="active")
    m = build(extra_rels=[extra])
    with pytest.raises(NotModeled, match="ambiguous"):
        compile_measure(m, req("net_sales", by=["public.regions.name"], grain="month", **Q3))


def _shipments(drop_ship=False):
    rels = [
        dict(name="shipments_bill_region", from_relation="public.shipments", from_columns=["bill_region_id"],
             to_relation="public.regions", to_columns=["id"], cardinality="many_to_one", status="active"),
        dict(name="shipments_ship_region", from_relation="public.shipments", from_columns=["ship_region_id"],
             to_relation="public.regions", to_columns=["id"], cardinality="many_to_one", status="active"),
    ]
    if drop_ship:
        rels = rels[:1]
    cols = {**COLUMNS, "public.shipments": {"id": "integer", "bill_region_id": "integer",
                                            "ship_region_id": "integer", "shipped_on": "date"}}
    ms = [_m(name="shipment_count", type="simple", base_relation="public.shipments", agg="count", expr="id",
             agg_time_dimension="public.shipments.shipped_on")]
    return model_from_rows([], rels, ms, cols)


def test_parallel_relationships_are_ambiguous():
    with pytest.raises(NotModeled, match="ambiguous"):
        compile_measure(_shipments(), req("shipment_count", by=["public.regions.name"]))
    c = compile_measure(_shipments(drop_ship=True), req("shipment_count", by=["public.regions.name"]))
    assert c.joins == ("shipments_bill_region",)


def test_time_dimension_on_reachable_relation(model):
    assert compile_measure(model, req("net_sales", grain="month"))
    m = build(drop_rels=("order_lines_order",))
    with pytest.raises(NotModeled):
        compile_measure(m, req("net_sales", grain="month"))


def test_hop_limit():
    chain_cols = {f"public.c{i}": {"id": "integer", "nxt": "integer", "label": "text"} for i in range(6)}
    rels = [dict(name=f"c{i}_c{i + 1}", from_relation=f"public.c{i}", from_columns=["nxt"],
                 to_relation=f"public.c{i + 1}", to_columns=["id"], cardinality="many_to_one", status="active")
            for i in range(5)]
    ms = [_m(name="cnt", type="simple", base_relation="public.c0", agg="count", expr="id")]
    m = model_from_rows([], rels, ms, chain_cols)
    assert compile_measure(m, req("cnt", by=["public.c3.label"]))
    with pytest.raises(NotModeled, match="hops"):
        compile_measure(m, req("cnt", by=["public.c4.label"]))


def test_regions_three_hops_allowed(model):
    assert plan_joins(model, "public.order_lines", ["public.regions"])[-1].to_alias == "t3"


def test_no_time_dimension_refusals(model):
    with pytest.raises(NotModeled):
        compile_measure(model, req("customer_count", grain="month"))
    with pytest.raises(NotModeled):
        compile_measure(model, req("customer_count", **Q3))
    with pytest.raises(NotModeled):
        compile_measure(model, req("nonexistent"))
    assert compile_measure(model, req("customer_count")).columns == ("customer_count",)


def test_unknown_column_refused(model):
    with pytest.raises(NotModeled):
        compile_measure(model, req("order_count", by=["public.customers.nothing"]))


def test_request_filter_through_join(model):
    c = compile_measure(model, req("order_count", filters=[
        {"dimension": "public.customers.segment", "op": "=", "values": ["Retail"]}]))
    assert c.joins == ("sales_orders_customer",)
    assert "\"t1\".\"segment\" = 'Retail'" in c.sql


def test_literal_escaping_and_mixed_case():
    cols = {"public.o": {"id": "integer", "OrderDate": "date", "who": "text"}}
    ms = [_m(name="oc", type="simple", base_relation="public.o", agg="count", expr="id",
             agg_time_dimension="public.o.OrderDate")]
    m = model_from_rows([], [], ms, cols)
    c = compile_measure(m, req("oc", grain="day", filters=[
        {"dimension": "public.o.who", "op": "=", "values": ["O'Brien"]}]))
    assert "'O''Brien'" in c.sql and '"OrderDate"' in c.sql
    assert validate_readonly_sql(c.sql)


def test_plan_joins_deterministic(model):
    a = plan_joins(model, "public.customers", ["public.sales_reps", "public.regions"])
    b = plan_joins(model, "public.customers", ["public.regions", "public.sales_reps"])
    assert a == b and [s.to_alias for s in a] == ["t1", "t2"]


# ---------------------------------------------------------------- ratio / derived / semi-additive

def test_ratio_no_dims_cross_join(model):
    c = compile_measure(model, req("average_order_value"))
    assert c.sql.startswith("WITH ") and "CROSS JOIN" in c.sql and "CAST(" in c.sql
    assert "NULLIF(" in c.sql and "FULL" not in c.sql
    assert c.columns == ("average_order_value",)


def test_ratio_by_dim_with_grain_spine(model):
    c = compile_measure(model, req("average_order_value", by=["public.customers.segment"], grain="month", **Q3))
    assert c.columns == ("metric_time__month", "customers__segment", "average_order_value")
    assert '"keys" AS (' in c.sql and "UNION" in c.sql and "FULL" not in c.sql
    assert c.sql.count("IS NOT DISTINCT FROM") == 4
    assert validate_readonly_sql(c.sql)


def test_input_filter_only_on_that_input():
    ms = [_m(name="web_sales", type="ratio",
             numerator=dict(name="net_sales", filter=[dict(dimension="public.sales_orders.channel", op="=",
                                                           values=["web"])]),
             denominator=dict(name="net_sales"))]
    c = compile_measure(build(extra_measures=ms), req("web_sales"))
    assert c.sql.count("'web'") == 1


def test_derived_period_over_period(model):
    c = compile_measure(model, req("net_sales_mom_growth", grain="month", start="2026-09-01", end="2026-11-01"))
    assert "INTERVAL '1 MONTH'" in c.sql
    assert "CAST('2026-08-01' AS DATE)" in c.sql and "CAST('2026-10-01' AS DATE)" in c.sql
    assert "CAST('2026-09-01' AS DATE)" in c.sql and "CAST('2026-11-01' AS DATE)" in c.sql
    assert 'NULLIF("m1"."v", 0)' in c.sql
    with pytest.raises(NotModeled):
        compile_measure(model, req("net_sales_mom_growth"))


def test_derived_unknown_alias_invalid():
    bad = _m(name="badgrowth", type="derived", expr="current - zzz",
             input_metrics=[dict(name="net_sales", alias="current")])
    with pytest.raises(InvalidRequest):
        compile_measure(build(extra_measures=[bad]), req("badgrowth"))


def test_derived_depth_and_missing_input():
    d2 = _m(name="growth2", type="derived", expr="a", input_metrics=[dict(name="net_sales_mom_growth", alias="a")])
    with pytest.raises(NotModeled, match="net_sales_mom_growth"):
        compile_measure(build(extra_measures=[d2]), req("growth2", grain="month"))
    miss = _m(name="orphan_ratio", type="ratio", numerator=dict(name="ghost"), denominator=dict(name="order_count"))
    with pytest.raises(NotModeled, match="ghost"):
        compile_measure(build(extra_measures=[miss]), req("orphan_ratio"))


def test_semi_additive(model):
    c = compile_measure(model, req("stock_on_hand"))
    assert "MAX(" in c.sql and '"pick"' in c.sql and c.sql.count("SUM(") == 1
    c2 = compile_measure(model, req("stock_on_hand", grain="month", **Q3))
    assert '"kp"' in c2.sql and "IS NOT DISTINCT FROM" in c2.sql
    ms = _m(name="stock_min", type="simple", base_relation="public.inventory_snapshots", agg="sum", expr="on_hand",
            agg_time_dimension="public.inventory_snapshots.snapshot_date",
            non_additive_dimension=dict(name="public.inventory_snapshots.snapshot_date", window_choice="min"))
    assert "MIN(" in compile_measure(build(extra_measures=[ms]), req("stock_min")).sql


def test_semi_additive_groupings():
    base = dict(type="simple", base_relation="public.inventory_snapshots", agg="sum", expr="on_hand",
                agg_time_dimension="public.inventory_snapshots.snapshot_date")
    g = _m(name="stock_g", **base, non_additive_dimension=dict(
        name="public.inventory_snapshots.snapshot_date", window_groupings=["public.products.category"]))
    n = _m(name="stock_n", **base, non_additive_dimension=dict(name="public.inventory_snapshots.snapshot_date"))
    m = build(extra_measures=[g, n])
    glob = compile_measure(m, req("stock_n", by=["public.products.category"]))
    assert '"g0"' not in glob.sql
    grouped = compile_measure(m, req("stock_g", by=["public.products.category"]))
    assert '"g0"' in grouped.sql


def test_window_days_uses_current_date(model):
    c = compile_measure(model, req("order_count", grain="day", window_days=30))
    assert "CURRENT_DATE" in c.sql and c.notes
    assert "CURRENT_DATE" not in compile_measure(model, req("order_count", grain="day", **Q3)).sql


def test_supports_time_and_canonical(model):
    for name in ("order_count", "net_sales", "stock_on_hand", "average_order_value", "net_sales_mom_growth"):
        assert supports_time(model, model.measure(name)), name
    for name in ("customer_count", "customer_ratio"):
        assert not supports_time(model, model.measure(name)), name
    assert measure_time_dimension(model, model.measure("net_sales")) == "public.sales_orders.order_date"
    assert measure_time_dimension(model, model.measure("customer_count")) is None


def test_canonical_request_and_compile_every_measure(model):
    s, e = date(2026, 9, 1), date(2026, 10, 1)
    for m in model.measures:
        r = canonical_request(model, m, "month", s, e)
        if supports_time(model, m):
            assert (r.grain, r.start, r.end) == ("month", s, e)
        else:
            assert (r.grain, r.start, r.end) == (None, None, None)
        if m.name == "customer_growth":
            with pytest.raises(NotModeled):
                compile_measure(model, r)
            continue
        c = compile_measure(model, r)
        if m.name == "net_sales_mom_growth":
            assert "CAST('2026-08-01' AS DATE)" in c.sql
        assert validate_readonly_sql(c.sql)


# ---------------------------------------------------------------- derived SELECT refs

def test_derived_select_refs_ok():
    sql = ("SELECT p.category, SUM(l.qty) AS units FROM public.order_lines l "
           "JOIN public.products p ON p.id = l.product_id GROUP BY 1")
    assert derived_select_refs(sql) == ("public.order_lines", "public.products")
    assert derived_select_refs("WITH x AS (SELECT * FROM public.t) SELECT * FROM x") == ("public.t",)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders", "SELECT * FROM ic_meta.link_state", "SELECT * FROM pg_catalog.pg_class",
    "SELECT * FROM information_schema.tables", "SELECT current_setting('x')",
    "SELECT pg_read_file('/etc/passwd')", "SELECT version()", "SELECT dblink('a','b')",
    "SELECT row_number() OVER () FROM public.t", "SELECT 1; SELECT 2",
])
def test_derived_select_refs_refuses(sql):
    with pytest.raises(InvalidRequest):
        derived_select_refs(sql)


# Phase 16 plan 09: CTE names resolve by scope; row locks, recursion, duplicates refused.
_SHADOW_PG = ("SELECT * FROM (WITH pg_roles AS (SELECT 1 AS q) SELECT q FROM pg_roles) s "
              "CROSS JOIN pg_roles")
_SHADOW_SO = ("SELECT * FROM (WITH sales_orders AS (SELECT 1 AS q) SELECT q FROM sales_orders) s "
              "CROSS JOIN sales_orders")


@pytest.mark.parametrize("sql", [
    _SHADOW_PG,
    _SHADOW_SO,
    "SELECT * FROM (WITH x AS (SELECT 1 AS q) SELECT q FROM x) s CROSS JOIN x",
    "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS q) SELECT * FROM a",
    "WITH t AS (SELECT * FROM t) SELECT * FROM t",
    "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    "WITH x AS (SELECT 1 AS q) SELECT * FROM x CROSS JOIN (WITH x AS (SELECT 2 AS q) SELECT q FROM x) s",
    "WITH pg_stat AS (SELECT * FROM public.t) SELECT * FROM pg_stat",
    "WITH ic_x AS (SELECT * FROM public.t) SELECT * FROM ic_x",
    "SELECT * FROM public.t FOR UPDATE",
    "SELECT * FROM public.t FOR SHARE NOWAIT",
])
def test_derived_select_refs_refuses_cte_scope_and_locks(sql):
    with pytest.raises(InvalidRequest):
        derived_select_refs(sql)


@pytest.mark.parametrize("sql,refs", [
    ("WITH x AS (SELECT * FROM public.t) SELECT * FROM x", ("public.t",)),
    ("WITH a AS (SELECT * FROM public.t), b AS (SELECT * FROM a) SELECT * FROM b", ("public.t",)),
    ("WITH x AS (SELECT * FROM public.t) SELECT * FROM x UNION ALL SELECT * FROM x", ("public.t",)),
    ("SELECT * FROM (WITH x AS (SELECT id FROM public.t) SELECT id FROM x) s", ("public.t",)),
    ("WITH x AS (SELECT id FROM public.t) SELECT * FROM public.u WHERE id IN (SELECT id FROM x)",
     ("public.t", "public.u")),
])
def test_derived_select_refs_cte_scope_ok(sql, refs):
    assert derived_select_refs(sql) == refs


def test_derived_view_normalize_refuses_shadowing():
    with pytest.raises(InvalidRequest):
        normalize_definition("view", {"name": "v_shadow", "sql": _SHADOW_SO})
    with pytest.raises(InvalidRequest):
        normalize_definition("materialized_view", {"name": "mv_lock", "sql": "SELECT * FROM public.t FOR UPDATE"})


# ---------------------------------------------------------------- sweep, determinism, purity

def _sweep_requests(model):
    out = []
    for m in model.measures:
        for by, grain, rng in itertools.product(
                [(), ("public.regions.name",), ("public.customers.segment",)], [None, "month"], [None, Q3]):
            out.append(req(m.name, by=by, grain=grain, **(rng or {})))
    return out


def test_guardrails_sweep_and_determinism(model):
    ok = 0
    shuffled = build(rows_shuffled=7)
    for r in _sweep_requests(model):
        try:
            a = compile_measure(model, r)
        except NotModeled:
            with pytest.raises(NotModeled):
                compile_measure(shuffled, r)
            continue
        assert validate_readonly_sql(a.sql) == a.sql
        assert a.sql == compile_measure(model, r).sql == compile_measure(shuffled, r).sql
        ok += 1
    assert ok >= 40


def test_no_full_join_anywhere(model):
    for r in _sweep_requests(model):
        try:
            assert "FULL" not in compile_measure(model, r).sql
        except NotModeled:
            pass


def test_purity_import_allowlist():
    src = Path(mc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed = {"dataclasses", "datetime", "re", "typing", "collections.abc", "sqlglot", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name in allowed, a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                assert node.module == "guardrails"
            else:
                assert node.module in allowed, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "today", "utcnow", "time"}


def _window_dates(sql, today):
    """The calendar dates the compiled CURRENT_DATE window admits, evaluated for a fixed today."""
    import re
    from datetime import timedelta
    lo = int(re.search(r">= CURRENT_DATE - (\d+)", sql).group(1))
    hi = int(re.search(r"< CURRENT_DATE \+ (\d+)", sql).group(1))
    d0, d1 = today - timedelta(days=lo), today + timedelta(days=hi)  # [d0, d1)
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days)]


def test_window_days_covers_exactly_n_dates_ending_today(model):
    # Quick 045: last_days 7 is today plus the 6 days before it (it used to be 8 dates).
    today = date(2026, 11, 4)
    for n in (1, 7, 30):
        sql = compile_measure(model, req("order_count", grain="day", window_days=n)).sql
        days = _window_dates(sql, today)
        assert len(days) == n and days[-1] == today
    sql = compile_measure(model, req("order_count", grain="day", window_days=7)).sql
    assert "CURRENT_DATE - 6" in sql and "CURRENT_DATE + 1" in sql
    assert _window_dates(sql, today)[0] == date(2026, 10, 29)


def test_window_days_offset_path_shifts_the_same_n_days(model):
    sql = compile_measure(model, req("net_sales_mom_growth", grain="day", window_days=7)).sql
    assert "CURRENT_DATE - 6 - INTERVAL '1 MONTH'" in sql
    assert "CURRENT_DATE + 1 - INTERVAL '1 MONTH'" in sql
