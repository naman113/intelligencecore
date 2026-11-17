"""Phase 16 plan 04: proposal tools, evidence, FK seeding, ordering and registration (fakes only)."""
import copy
import json
from datetime import date
from decimal import Decimal

import pytest

from app import modeltools as mt
from app import modelcompiler as mc
from app import semanticmodel, warehouse
from app.agents import graph as g
from test_model_compiler import COLUMNS, FKS, MEASURES, RELS, TABLES


TODAY = date(2026, 10, 15)


class FakePool:
    def __init__(self):
        self.inserts = []   # (payload dict, run_id, id)
        self.dups = set()

    async def fetchval(self, sql, *args):
        assert sql.lstrip().startswith("INSERT INTO actions")
        payload = json.loads(args[0])
        key = (payload["object"], payload["name"])
        if key in self.dups:
            return None
        self.dups.add(key)
        new_id = 100 + len(self.inserts)
        self.inserts.append((payload, args[1], new_id))
        return new_id

    async def fetch(self, sql, *args):
        return [{"id": i, "payload": json.dumps(p)} for p, _, i in self.inserts]


class Env:
    pass


@pytest.fixture
def env(monkeypatch):
    e = Env()
    e.pool = FakePool()
    e.rows = {"tables": copy.deepcopy(TABLES), "relationships": copy.deepcopy(RELS),
              "measures": copy.deepcopy(MEASURES), "indexes": []}
    e.columns = copy.deepcopy(COLUMNS)
    e.wh = {}
    e.fks = [{"schema": "public", "table": f, "column": fc, "to_table": t, "to_column": "id"}
             for _, f, fc, t in FKS]
    e.sql = []
    e.logs = []
    e.mode = False
    e.stats = {"sql": "SELECT 1", "from_rows": 1000, "from_nonnull": 1000, "matched": 998,
               "match_rate": 0.998, "duplicate_keys": 0, "null_keys": 0}
    e.fail_sql = False
    e.measure_col = "x"

    async def get_pool():
        return e.pool

    async def load_rows(include_retired=False):
        return e.rows

    async def load_model():
        return mc.model_from_rows(e.rows["tables"], e.rows["relationships"], e.rows["measures"], e.columns)

    async def catalog_columns():
        return e.columns

    async def foreign_keys():
        return e.fks

    async def warehouse_objects():
        return e.wh

    async def current_definition(obj, name):
        pool = {"table": ("tables", "base"), "view": ("tables", "view"),
                "materialized_view": ("tables", "matview"), "relationship": ("relationships", None),
                "measure": ("measures", None), "index": ("indexes", None)}[obj]
        for r in e.rows[pool[0]]:
            if r["name"] == name and (pool[1] is None or r.get("object_kind") == pool[1]):
                return {k: v for k, v in r.items() if k != "status"}
        return None

    async def relationship_stats(defn):
        return dict(e.stats)

    async def run_sql(sql):
        e.sql.append(sql)
        if e.fail_sql:
            raise RuntimeError("boom")
        if "metric_time__day" in sql:
            return [{"metric_time__day": date(2026, 10, 1), e.measure_col: Decimal("12.5")},
                    {"metric_time__day": date(2026, 10, 2), e.measure_col: None}]
        if "row_count" in sql:
            return [{"row_count": 100, "distinct_keys": 100}]
        if "AS day" in sql:
            return [{"day": date(2026, 10, 14), "rows": 7}]
        if sql.startswith("SELECT * FROM ("):
            return [{"product_id": 1, "on_hand": 3}]
        return [{e.measure_col: Decimal("42")}]

    async def log_tool(name, args, result, start, refused=False):
        e.logs.append({"name": name, "result": result, "refused": refused})

    monkeypatch.setattr(mt, "get_pool", get_pool)
    monkeypatch.setattr(mt, "_log_tool", log_tool)
    monkeypatch.setattr(mt, "_today", lambda: TODAY)
    monkeypatch.setattr(mt, "run_readonly_sql", run_sql)
    monkeypatch.setattr(semanticmodel, "load_rows", load_rows)
    monkeypatch.setattr(semanticmodel, "load_model", load_model)
    monkeypatch.setattr(semanticmodel, "catalog_columns", catalog_columns)
    monkeypatch.setattr(semanticmodel, "foreign_keys", foreign_keys)
    monkeypatch.setattr(semanticmodel, "warehouse_objects", warehouse_objects)
    monkeypatch.setattr(semanticmodel, "current_definition", current_definition)
    monkeypatch.setattr(semanticmodel, "relationship_stats", relationship_stats)
    monkeypatch.setattr(semanticmodel, "run_readonly_sql", run_sql)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: e.mode)
    g.ctx_run_id.set(77)
    g.ctx_phone.set("ui:test")
    return e


async def propose(*items):
    return await mt.propose_model_change.ainvoke({"items": list(items)})


def measure(name, **kw):
    d = {"name": name, "type": "simple", "base_relation": "public.order_lines", "agg": "count",
         "agg_time_dimension": "public.sales_orders.order_date"}
    d.update(kw)
    return {"object": "measure", "definition": d}


# -- batching and the measure card ---------------------------------------------

async def test_thirteen_items_refused_and_nothing_written(env):
    out = await propose(*[measure(f"m{i}") for i in range(13)])
    assert out == "REFUSED: at most 12 items per call"
    assert env.pool.inserts == []
    assert env.logs[-1]["refused"] is True


async def test_simple_measure_card_has_sql_and_samples(env):
    env.measure_col = "line_count"
    out = await propose(measure("line_count"))
    assert "proposed #100" in out
    payload, run_id, _ = env.pool.inserts[0]
    assert run_id == 77
    assert sorted(payload) == sorted(["_summary", "object", "name", "op", "definition", "before",
                                      "origin", "thread_id", "evidence"])
    assert payload["origin"] == "agent" and payload["op"] == "create"
    assert payload["thread_id"] == "ui:test"
    ev = payload["evidence"]
    assert "metric_time__day" in ev["sql"]
    assert "'2026-09-15'" in ev["sql"] and "'2026-10-15'" in ev["sql"]
    assert ev["samples"] == [{"day": "2026-10-01", "value": 12.5}, {"day": "2026-10-02", "value": None}]
    assert ev["sample_range"] == {"start": "2026-09-15", "end": "2026-10-15"}
    assert env.logs[-1]["refused"] is False


async def test_no_time_measure_gets_single_sample_value(env):
    env.measure_col = "city_count"
    out = await propose({"object": "measure", "definition": {
        "name": "city_count", "type": "simple", "base_relation": "public.customers",
        "agg": "count_distinct", "expr": "city"}})
    assert "proposed" in out
    ev = env.pool.inserts[0][0]["evidence"]
    assert ev["samples"] == [] and ev["sample_value"] == 42.0 and ev["sample_range"] is None
    assert "metric_time" not in ev["sql"]
    assert len(env.sql) == 1


async def test_no_time_ratio_is_not_refused(env):
    env.measure_col = "sales_per_customer"
    out = await propose({"object": "measure", "definition": {
        "name": "sales_per_customer", "type": "ratio", "numerator": {"name": "net_sales"},
        "denominator": {"name": "customer_count"}}})
    assert "proposed" in out
    assert env.pool.inserts[0][0]["evidence"]["sample_range"] is None


async def test_ratio_with_time_inputs_gets_daily_samples(env):
    env.measure_col = "sales_per_order"
    await propose({"object": "measure", "definition": {
        "name": "sales_per_order", "type": "ratio", "numerator": {"name": "net_sales"},
        "denominator": {"name": "order_count"}}})
    assert len(env.pool.inserts[0][0]["evidence"]["samples"]) == 2


async def test_evidence_query_error_refuses_item(env):
    env.fail_sql = True
    out = await propose(measure("line_count"))
    assert "refused: evidence failed: RuntimeError" in out
    assert env.pool.inserts == []
    assert env.logs[-1]["refused"] is True


async def test_measure_not_modeled_reason_refuses(env):
    env.rows["relationships"] = []   # order_lines cannot reach sales_orders
    out = await propose(measure("line_count"))
    assert "refused:" in out and "no approved relationship path" in out
    assert env.pool.inserts == []


async def test_duplicate_proposal_reports_pending(env):
    await propose(measure("line_count"))
    out = await propose(measure("line_count"))
    assert "1. measure line_count: pending" in out
    assert len(env.pool.inserts) == 1


# -- ordering rules ------------------------------------------------------------

async def test_ratio_with_inactive_input_refused(env):
    out = await propose({"object": "measure", "definition": {
        "name": "ghost_ratio", "type": "ratio", "numerator": {"name": "nope"},
        "denominator": {"name": "order_count"}}})
    assert "input measure nope is not active" in out


async def test_create_when_active_exists_refused(env):
    out = await propose(measure("order_count"))
    assert "order_count is already approved; use op update" in out


async def test_update_and_retire_need_active_object(env):
    out = await propose({"object": "measure", "op": "update", "definition": {
        "name": "ghost", "type": "simple", "base_relation": "public.customers", "agg": "count"}},
        {"object": "measure", "op": "retire", "name": "ghost"})
    assert out.count("refused: no active measure named ghost") == 2


async def test_retire_measure_used_by_ratio_refused(env):
    out = await propose({"object": "measure", "op": "retire", "name": "net_sales"})
    assert "uses net_sales as an input" in out


async def test_retire_unused_measure_carries_before(env):
    out = await propose({"object": "measure", "op": "retire", "name": "collected_amount"})
    assert "proposed" in out
    payload = env.pool.inserts[0][0]
    assert payload["op"] == "retire" and payload["before"]["name"] == "collected_amount"
    assert payload["_summary"] == "Retire measure collected_amount"
    assert payload["definition"] is None


async def test_model_relation_without_active_row_refused(env):
    out = await propose(measure("stock_value", base_relation="model.weekly_stock", agg="sum",
                                expr="on_hand", agg_time_dimension=None))
    assert "model.weekly_stock has no active model view" in out


async def test_view_needs_warehouse_mode(env):
    item = {"object": "view", "definition": {
        "name": "stock_by_product",
        "sql": "SELECT product_id, sum(on_hand) AS on_hand FROM public.inventory_snapshots GROUP BY product_id"}}
    assert "refused: needs the warehouse" in await propose(item)
    env.mode = True
    out = await propose(item)
    assert "proposed" in out
    ev = env.pool.inserts[0][0]["evidence"]
    assert ev["preview_rows"] == [{"product_id": 1, "on_hand": 3}] and "product_id" in ev["columns"]


async def test_view_recreate_allowed_when_warehouse_object_missing(env):
    env.mode = True
    env.rows["tables"].append({"name": "stock_by_product", "relation": "model.stock_by_product",
                               "object_kind": "view", "status": "active"})
    item = {"object": "view", "definition": {
        "name": "stock_by_product", "sql": "SELECT product_id FROM public.inventory_snapshots"}}
    assert "proposed" in await propose(item)
    env.pool.dups.clear()
    env.wh = {"stock_by_product": "v"}
    assert "already approved" in await propose(item)


async def test_index_needs_active_matview(env):
    env.mode = True
    item = {"object": "index", "definition": {"name": "ix_w", "relation": "model.weekly", "columns": ["week"]}}
    assert "indexes need an active materialized view" in await propose(item)
    env.rows["tables"].append({"name": "weekly", "relation": "model.weekly", "object_kind": "matview",
                               "status": "active"})
    out = await propose(item)
    assert "proposed" in out
    p = env.pool.inserts[0][0]
    assert p["evidence"] == {} and p["_summary"] == "Index model.weekly (week)"


# -- relationships and match rate ----------------------------------------------

def rel_item(**kw):
    d = {"name": "reps_region_alt", "from_relation": "public.sales_reps", "from_columns": ["region_id"],
         "to_relation": "public.regions", "to_columns": ["id"]}
    d.update(kw)
    return {"object": "relationship", "definition": d}


async def test_relationship_card_shows_match_rate(env):
    out = await propose(rel_item())
    assert "proposed" in out
    p = env.pool.inserts[0][0]
    assert p["origin"] == "agent"
    assert "origin" not in p["definition"] and "match_rate" not in p["definition"]
    ev = p["evidence"]
    assert ev["match_rate"] == 0.998 and ev["matched"] == 998 and ev["from_nonnull"] == 1000
    assert "(match 99.8%)" in p["_summary"]


async def test_relationship_with_non_unique_to_side_refused(env):
    env.stats["duplicate_keys"] = 3
    out = await propose(rel_item())
    assert "is not unique, so this is not many-to-one" in out
    assert env.pool.inserts == []


async def test_relationship_unknown_column_refused(env):
    out = await propose(rel_item(from_columns=["nope"]))
    assert "does not exist" in out


# -- tables and time reach -----------------------------------------------------

def table_item(**kw):
    d = {"name": "order_lines", "relation": "public.order_lines", "role": "fact",
         "grain": "one row per order line", "primary_key": ["id"],
         "agg_time_dimension": "public.sales_orders.order_date"}
    d.update(kw)
    return {"object": "table", "definition": d}


async def test_table_time_reach_proposed_without_rows_per_day(env):
    env.rows["tables"] = []
    out = await propose(table_item())
    assert "proposed" in out
    p = env.pool.inserts[0][0]
    ev = p["evidence"]
    assert "rows_per_day" not in ev
    assert ev["note"] == "rows_per_day omitted: time column is on public.sales_orders"
    assert ev["row_count"] == 100 and ev["key_unique"] is True
    assert p["_summary"] == "Set table order_lines: fact, one row per order line"


async def test_table_time_reach_refused_when_not_reachable(env):
    env.rows["tables"] = []
    env.rows["relationships"] = []
    out = await propose(table_item())
    assert ("needs an approved relationship from public.order_lines to public.sales_orders; "
            "approve relationships first") in out
    assert env.pool.inserts == []


async def test_table_own_time_column_has_rows_per_day(env):
    env.rows["tables"] = []
    await propose(table_item(name="sales_orders", relation="public.sales_orders",
                             agg_time_dimension="public.sales_orders.order_date"))
    ev = env.pool.inserts[0][0]["evidence"]
    assert ev["rows_per_day"] == [{"day": "2026-10-14", "rows": 7}]
    assert any("'2026-09-15'" in s and "'2026-10-16'" in s for s in env.sql)


async def test_table_time_column_must_exist_and_be_a_date(env):
    env.rows["tables"] = []
    bad = await propose(table_item(agg_time_dimension="public.sales_orders.nope"))
    assert "time column does not exist" in bad
    notdate = await propose(table_item(agg_time_dimension="public.sales_orders.status"))
    assert "must be a date or timestamp" in notdate


# -- foreign-key seeding -------------------------------------------------------

async def seed():
    return await mt.propose_relationships_from_keys.ainvoke({})


def empty_store(env):
    env.rows = {"tables": [], "relationships": [], "measures": [], "indexes": []}


async def test_seed_from_keys_with_empty_context_store(env):
    empty_store(env)
    out = await seed()
    assert out.startswith("10 proposed, 0 pending, 0 refused")
    assert len(env.pool.inserts) == 10
    first = {p["name"]: p for p, _, _ in env.pool.inserts}["order_lines__order_id__to__sales_orders"]
    assert first["origin"] == "foreign_key"
    d = first["definition"]
    assert d["from_relation"] == "public.order_lines" and d["from_columns"] == ["order_id"]
    assert d["to_relation"] == "public.sales_orders" and d["to_columns"] == ["id"]
    assert first["evidence"]["match_rate"] == 0.998
    assert all(mc.NAME_RE.match(p["name"]) and len(p["name"]) <= 63 for p, _, _ in env.pool.inserts)


async def test_seed_second_call_writes_nothing(env):
    empty_store(env)
    await seed()
    out = await seed()
    assert len(env.pool.inserts) == 10
    assert "0 proposed" in out and "approved or pending" in out


async def test_seed_skips_approved_relationships(env):
    out = await seed()   # all 10 already approved in the fixture rows
    assert len(env.pool.inserts) == 0
    assert "0 proposed" in out


async def test_seed_skips_multiple_fks_to_same_table(env):
    empty_store(env)
    env.fks = [
        {"schema": "public", "table": "invoices", "column": "customer_id", "to_table": "customers", "to_column": "id"},
        {"schema": "public", "table": "invoices", "column": "bill_to_id", "to_table": "customers", "to_column": "id"},
    ]
    out = await seed()
    assert env.pool.inserts == []
    assert "multiple foreign keys to the same table (composite or role-playing)" in out


async def test_seed_refuses_ambiguous_target_table(env):
    # Quick 027: no same-schema target and no public one -> still ambiguous
    empty_store(env)
    env.columns.pop("public.regions", None)
    env.columns["archive.regions"] = {"id": "integer"}
    env.columns["old.regions"] = {"id": "integer"}
    env.fks = [{"schema": "public", "table": "customers", "column": "region_id",
                "to_table": "regions", "to_column": "id"}]
    out = await seed()
    assert env.pool.inserts == []
    assert "ambiguous across schemas" in out


async def test_q027_seed_prefers_public_target_over_source_table_of_same_name(env):
    empty_store(env)
    env.columns["src_shop.regions"] = {"id": "integer"}
    env.fks = [{"schema": "public", "table": "customers", "column": "region_id",
                "to_table": "regions", "to_column": "id"}]
    out = await seed()
    assert "ambiguous" not in out
    assert env.pool.inserts[0][0]["definition"]["to_relation"] == "public.regions"


async def test_q027_seed_prefers_same_schema_target(env):
    empty_store(env)
    env.columns["archive.regions"] = {"id": "integer"}
    env.columns["archive.customers"] = {"id": "integer", "region_id": "integer"}
    env.fks = [{"schema": "archive", "table": "customers", "column": "region_id",
                "to_table": "regions", "to_column": "id"}]
    await seed()
    d = env.pool.inserts[0][0]["definition"]
    assert d["from_relation"] == "archive.customers" and d["to_relation"] == "archive.regions"


async def test_q027_seed_source_fk_targets_its_own_schema(env):
    empty_store(env)
    env.columns["src_shop.customers"] = {"id": "integer", "region_id": "integer"}
    env.columns["src_shop.regions"] = {"id": "integer"}
    env.fks = [{"schema": "src_shop", "table": "customers", "column": "region_id",
                "to_table": "regions", "to_column": "id"}]
    out = await seed()
    assert "ambiguous" not in out
    d = env.pool.inserts[0][0]["definition"]
    assert d["from_relation"] == "src_shop.customers" and d["to_relation"] == "src_shop.regions"


async def test_q027_seed_source_fk_missing_same_schema_target_skipped(env):
    empty_store(env)
    env.fks = [{"schema": "src_shop", "table": "customers", "column": "region_id",
                "to_table": "regions", "to_column": "id"}]
    out = await seed()
    assert env.pool.inserts == []
    assert "not in the catalog" in out


async def test_seed_caps_at_25_per_call(env):
    empty_store(env)
    env.columns = {"public.hub": {"id": "integer"}}
    env.fks = []
    for i in range(30):
        env.columns[f"public.t{i:02d}"] = {"hub_id": "integer"}
        env.fks.append({"schema": "public", "table": f"t{i:02d}", "column": "hub_id",
                        "to_table": "hub", "to_column": "id"})
    # t## -> hub groups are distinct (one fk per table)
    out = await seed()
    assert len(env.pool.inserts) == 25
    assert "5 more foreign keys remain: call again." in out
    await seed()
    assert len(env.pool.inserts) == 30


# -- get_model -----------------------------------------------------------------

async def test_get_model_shape(env, monkeypatch):
    async def introspect(source="business_db"):
        return {"tables": [{"schema": "public", "table": "regions", "row_estimate": 5},
                           {"schema": "public", "table": "extra", "row_estimate": 9}]}
    monkeypatch.setattr(mt.schemadiscovery, "introspect", introspect)
    env.rows["tables"] = [{"name": "regions", "relation": "public.regions", "object_kind": "base",
                           "status": "active", "id": 1},
                          {"name": "wk", "relation": "model.wk", "object_kind": "matview", "status": "active"}]
    env.wh = {"wk": "m"}
    await env.pool.fetchval("INSERT INTO actions", json.dumps(
        {"object": "measure", "name": "x", "op": "create", "definition": {}}), 1)
    # Quick 037: no arguments = the compact summary; the full rows come one section at a time
    out = await mt.get_model.ainvoke({})
    for k in ("TABLES", "RELATIONSHIPS", "MEASURES", "PENDING PROPOSALS", "CATALOG"):
        assert k in out
    assert "measure x (create)" in out and "1 catalog tables not modeled yet" in out
    assert "foreign keys not proposed yet" in out
    pend = json.loads(await mt.get_model.ainvoke({"section": "pending"}))
    assert pend["items"] == [{"object": "measure", "name": "x", "op": "create", "action_id": 100}]
    cands = json.loads(await mt.get_model.ainvoke({"section": "candidates"}))
    assert [c["relation"] for c in cands["items"]] == ["public.extra"]
    tabs = json.loads(await mt.get_model.ainvoke({"section": "tables"}))["items"]
    assert {t["name"]: t.get("in_warehouse") for t in tabs}["wk"] is True
    assert "id" not in tabs[0]


# -- source gates and wiring ---------------------------------------------------

def test_tool_source_only_writes_actions():
    src = open(mt.__file__, encoding="utf-8").read()
    for bad in ("INSERT INTO model_", "UPDATE model_", "exec_fmt", 'connect("owner")', "_rules_gate"):
        assert bad not in src
    assert src.count("WHERE kind = 'model_change' AND status = 'proposed' DO NOTHING") == 1


def test_registry_and_superset_wiring():
    reg = g._tool_registry()
    for name in ("get_model", "query_measure", "propose_model_change", "propose_relationships_from_keys"):
        assert name in reg
    sup = g.AGENT_TOOL_SUPERSET
    assert sup["supervisor"][-2:] == ["discover_schema", "propose_context_change"]
    assert sup["data"][-2:] == ["discover_schema", "propose_context_change"]
    tail = sup["supervisor"].index("discover_schema")
    for name in ("get_model", "query_measure", "propose_model_change", "propose_relationships_from_keys"):
        assert sup["supervisor"].index(name) < tail
    assert "get_model" in sup["data"] and "query_measure" in sup["data"]
    assert "propose_model_change" not in sup["data"]
    assert "query_measure" in sup["dashboard"]  # Phase 18 (SPEC-04)
    for agent in ("supervisor", "data"):
        assert "query_measure" in g.DEFAULT_CONFIGS[agent]["tools"]
    assert "get_model" in g.DEFAULT_CONFIGS["supervisor"]["tools"]


def test_two_lane_prompts():
    for text in (g.DEFAULT_PROMPTS["supervisor"], g.DEFAULT_PROMPTS["data"],
                 g.DEFAULT_CONFIGS["supervisor"]["prompt"]):
        assert "TWO LANES" in text
    sup = g.DEFAULT_PROMPTS["supervisor"]
    line = [ln for ln in sup.split("\n") if "TWO LANES (Phase 16)" in ln]
    assert len(line) == 1
    assert "query_measure" in line[0] and "run_sql" in line[0] and "unverified" in line[0]
    assert line[0].startswith("- TWO LANES (Phase 16): ")
    for w in ("get_model", "whether or not", "data agent", "propose_model_change", "Approvals"):
        assert w in line[0], w
    assert "On a NOT MODELED answer, propose the measure with propose_model_change (follow the data-modeling skill) or answer with run_sql" not in line[0]
    assert "TWO LANES (Phase 16)" in g.DEFAULT_PROMPTS["data"]
    assert "\n" not in g.DEFAULT_PROMPTS["data"].split("TWO LANES (Phase 16)")[1]
    dtail = g.DEFAULT_PROMPTS["data"].split("TWO LANES (Phase 16)")[1]
    for w in ("query_measure", "get_model", "run_sql", "unverified", "whether or not"):
        assert w in dtail, w
    assert "when query_measure says NOT MODELED, use run_sql and label the result unverified" not in dtail


async def test_q027_get_model_candidates_include_source_tables(env, monkeypatch):
    async def introspect(source="business_db"):
        return {"tables": [{"schema": "public", "table": "extra", "row_estimate": 9}]}

    async def source_snapshots():
        return [{"tables": [{"schema": "src_shop", "table": "orders", "row_estimate": 3}]}]
    monkeypatch.setattr(mt.schemadiscovery, "introspect", introspect)
    monkeypatch.setattr(semanticmodel, "source_snapshots", source_snapshots)
    out = json.loads(await mt.get_model.ainvoke({"section": "candidates"}))
    assert {c["relation"] for c in out["items"]} == {"public.extra", "src_shop.orders"}
