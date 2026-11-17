"""Phase 16 plan 03: the model_change executor with fakes (no DB, no warehouse, no LLM)."""
import copy
import json
import re
from datetime import date

import pytest

from app import actionsvc, semanticmodel, warehouse
from app import modelcompiler as mc
from test_model_compiler import COLUMNS, MEASURES, RELS, TABLES

pytestmark = pytest.mark.asyncio


class FakeConn:
    def __init__(self, pool):
        self.pool = pool
        self.closed = False

    def transaction(self):
        return _Ctx(None)

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))


class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, tables, rels, measures, indexes=()):
        self.state = {"model_tables": copy.deepcopy(tables), "model_relationships": copy.deepcopy(rels),
                      "model_measures": copy.deepcopy(measures), "model_indexes": list(indexes)}
        self.executed = []
        self.action_id = 77
        self.action_queries = []

    def _rows(self, sql, args):
        table = re.search(r"FROM (model_\w+)", sql).group(1)
        rows = self.state[table]
        if "status = 'active'" in sql:
            rows = [r for r in rows if r.get("status", "active") == "active"]
        return table, rows

    async def fetch(self, sql, *args):
        return self._rows(sql, args)[1]

    async def fetchrow(self, sql, *args):
        _, rows = self._rows(sql, args)
        rows = [r for r in rows if r["name"] == args[0]]
        if "object_kind = $2" in sql:
            rows = [r for r in rows if r.get("object_kind") == args[1]]
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        if "FROM actions" in sql:
            self.action_queries.append((sql, args))
            return self.action_id
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def acquire(self):
        return _Ctx(FakeConn(self))


def writes(pool, table):
    return [(s, a) for s, a in pool.executed if f"INTO {table}" in s]


def md(name):
    d = next(m for m in MEASURES if m["name"] == name)
    return {k: v for k, v in d.items() if k != "status"}


@pytest.fixture
def env(monkeypatch):
    def make(drop_measures=(), drop_rels=(), drop_tables=(), extra_tables=()):
        pool = FakePool(
            [t for t in TABLES if t["name"] not in drop_tables] + list(extra_tables),
            [r for r in RELS if r["name"] not in drop_rels],
            [m for m in MEASURES if m["name"] not in drop_measures])

        async def get_pool():
            return pool

        async def catalog():
            return copy.deepcopy(COLUMNS)

        monkeypatch.setattr(semanticmodel, "get_pool", get_pool)
        monkeypatch.setattr(semanticmodel, "catalog_columns", catalog)
        monkeypatch.setattr(semanticmodel.warehouse, "warehouse_mode", lambda: False)
        return pool
    return make


def payload(obj, name, definition, op="create", origin="agent", **kw):
    return {"_summary": "s", "object": obj, "name": name, "op": op, "definition": definition,
            "before": None, "origin": origin, "thread_id": None, "evidence": {}, **kw}


def rel_def(name="order_lines__order_id__to__sales_orders", **kw):
    d = {"name": name, "from_relation": "public.order_lines", "from_columns": ["order_id"],
         "to_relation": "public.sales_orders", "to_columns": ["id"], "cardinality": "many_to_one"}
    d.update(kw)
    return d


def stats_stub(monkeypatch, *, dup=0, nonnull=2184, matched=2180, null_keys=0):
    seen = []

    async def fake(sql):
        seen.append(sql)
        if "duplicate_keys" in sql:
            return [{"duplicate_keys": dup, "null_keys": null_keys}]
        return [{"from_rows": nonnull, "from_nonnull": nonnull, "matched": matched}]

    monkeypatch.setattr(semanticmodel, "run_readonly_sql", fake)
    return seen


# ---------------------------------------------------------------- dispatch

async def test_execute_dispatches_model_change(monkeypatch):
    got = []

    async def fake_apply(p):
        got.append(p)
        return "sentinel"

    monkeypatch.setattr(semanticmodel, "apply", fake_apply)
    assert await actionsvc._execute("model_change", {"x": 1}) == "sentinel"
    assert got == [{"x": 1}]


# ---------------------------------------------------------------- measures

async def test_measure_create_ignores_forged_sql_and_upserts(env):
    pool = env(drop_measures=["net_sales"])
    p = payload("measure", "net_sales", {**md("net_sales"), "sql": "DROP TABLE x"},
                evidence={"sql": "SELECT pg_sleep(9)"})
    assert await semanticmodel.apply(p) is True
    w = writes(pool, "model_measures")
    assert len(w) == 1 and "ON CONFLICT (name) DO UPDATE" in w[0][0]
    assert w[0][1][0] == "net_sales" and w[0][1][-1] == 77
    assert not any("DROP TABLE" in str(a) for _, a in pool.executed)


async def test_measure_uses_canonical_request(env, monkeypatch):
    env(drop_measures=["net_sales"])
    seen = []
    real = mc.canonical_request

    def spy(model, measure, grain, start, end):
        seen.append((measure.name, grain, start, end))
        return real(model, measure, grain, start, end)

    monkeypatch.setattr(mc, "canonical_request", spy)
    assert await semanticmodel.apply(payload("measure", "net_sales", md("net_sales"))) is True
    assert seen == [("net_sales", "month", date(2026, 1, 1), date(2026, 1, 31))]


async def test_pop_measure_approves(env):
    pool = env(drop_measures=["net_sales_mom_growth"])
    assert await semanticmodel.apply(payload("measure", "net_sales_mom_growth", md("net_sales_mom_growth"))) is True
    assert len(writes(pool, "model_measures")) == 1


async def test_ratio_measure_approves(env):
    pool = env(drop_measures=["average_order_value"])
    assert await semanticmodel.apply(payload("measure", "average_order_value", md("average_order_value"))) is True
    assert len(writes(pool, "model_measures")) == 1


async def test_simple_measure_without_time_dimension_approves(env):
    pool = env(drop_measures=["customer_count"])
    assert await semanticmodel.apply(payload("measure", "customer_count", md("customer_count"))) is True
    assert len(writes(pool, "model_measures")) == 1


async def test_pop_offset_over_input_without_time_dimension_refused(env):
    pool = env(drop_measures=["customer_growth"])
    assert await semanticmodel.apply(payload("measure", "customer_growth", md("customer_growth"))) is False
    assert not writes(pool, "model_measures")


async def test_measure_unknown_column_refused(env):
    pool = env(drop_measures=["net_sales"])
    d = {**md("net_sales"), "expr": "qty * no_such_column"}
    assert await semanticmodel.apply(payload("measure", "net_sales", d)) is False
    assert not pool.executed


async def test_ratio_with_inactive_input_refused(env):
    pool = env(drop_measures=["average_order_value", "order_count"])
    assert await semanticmodel.apply(payload("measure", "average_order_value", md("average_order_value"))) is False
    assert not pool.executed


async def test_measure_cycle_refused(env):
    pool = env()
    d = {"name": "net_sales_mom_growth", "type": "derived", "expr": "a - b",
         "input_metrics": [{"name": "net_sales_mom_growth", "alias": "a"}, {"name": "net_sales", "alias": "b"}]}
    assert await semanticmodel.apply(payload("measure", "net_sales_mom_growth", d, op="update")) is False
    assert not pool.executed


# ---------------------------------------------------------------- state checks and retire

async def test_create_when_active_exists_refused(env):
    pool = env()
    assert await semanticmodel.apply(payload("measure", "net_sales", md("net_sales"))) is False
    assert not pool.executed


async def test_update_and_retire_without_active_row_refused(env):
    pool = env(drop_measures=["net_sales"])
    assert await semanticmodel.apply(payload("measure", "net_sales", md("net_sales"), op="update")) is False
    assert await semanticmodel.apply(payload("measure", "net_sales", {}, op="retire")) is False
    assert not pool.executed


async def test_update_measure_upserts(env):
    pool = env()
    d = {**md("order_count"), "label": "Orders"}
    assert await semanticmodel.apply(payload("measure", "order_count", d, op="update")) is True
    assert writes(pool, "model_measures")[0][1][1] == "Orders"


async def test_retire_measure_used_by_ratio_refused(env):
    pool = env()
    assert await semanticmodel.apply(payload("measure", "net_sales", {}, op="retire")) is False
    assert not pool.executed


async def test_retire_sets_status_never_deletes(env):
    pool = env()
    assert await semanticmodel.apply(payload("measure", "invoiced_amount", {}, op="retire")) is True
    sql, args = pool.executed[0]
    assert sql.startswith("UPDATE model_measures SET status = 'retired'")
    assert args[0] == "invoiced_amount"
    assert not any("DELETE" in s.upper() for s, _ in pool.executed)


async def test_action_id_query_shape(env):
    pool = env(drop_measures=["net_sales"])
    await semanticmodel.apply(payload("measure", "net_sales", md("net_sales")))
    sql, args = pool.action_queries[0]
    assert sql == ("SELECT id FROM actions WHERE kind = 'model_change' AND status = 'approved' "
                   "AND payload->>'object' = $1 AND payload->>'name' = $2 ORDER BY id DESC LIMIT 1")
    assert args == ("measure", "net_sales")


async def test_apply_never_raises(env, monkeypatch):
    env(drop_measures=["net_sales"])

    async def boom():
        raise RuntimeError("secret detail")

    monkeypatch.setattr(semanticmodel, "load_model", boom)
    assert await semanticmodel.apply(payload("measure", "net_sales", md("net_sales"))) is False
    assert await semanticmodel.apply({"object": "bogus"}) is False
    assert await semanticmodel.apply({}) is False


# ---------------------------------------------------------------- relationships

async def test_origin_duplicate_keys_refused(env, monkeypatch):
    pool = env(drop_rels=["order_lines_order"])
    stats_stub(monkeypatch, dup=3)
    assert await semanticmodel.apply(payload("relationship", rel_def()["name"], rel_def(), origin="foreign_key")) is False
    assert not pool.executed


async def test_origin_taken_from_payload_and_match_rate_recomputed(env, monkeypatch):
    pool = env(drop_rels=["order_lines_order"])
    stats_stub(monkeypatch)
    p = payload("relationship", rel_def()["name"], {**rel_def(), "origin": "agent", "match_rate": 1.0},
                origin="foreign_key", evidence={"match_rate": 1.0})
    assert await semanticmodel.apply(p) is True
    args = writes(pool, "model_relationships")[0][1]
    assert args[6] == "foreign_key"
    assert float(args[7]) == 0.9982


async def test_origin_bogus_or_missing_refused(env, monkeypatch):
    pool = env(drop_rels=["order_lines_order"])
    stats_stub(monkeypatch)
    n = rel_def()["name"]
    assert await semanticmodel.apply(payload("relationship", n, rel_def(), origin="bogus")) is False
    p = payload("relationship", n, rel_def())
    del p["origin"]
    assert await semanticmodel.apply(p) is False
    assert not pool.executed


async def test_origin_zero_nonnull_stores_null_rate(env, monkeypatch):
    pool = env(drop_rels=["order_lines_order"])
    stats_stub(monkeypatch, nonnull=0, matched=0)
    assert await semanticmodel.apply(payload("relationship", rel_def()["name"], rel_def(), origin="agent")) is True
    assert writes(pool, "model_relationships")[0][1][7] is None


async def test_relationship_unknown_column_refused(env, monkeypatch):
    pool = env(drop_rels=["order_lines_order"])
    stats_stub(monkeypatch)
    d = rel_def(from_columns=["nope"])
    assert await semanticmodel.apply(payload("relationship", d["name"], d, origin="agent")) is False
    assert not pool.executed


async def test_relationship_stats_sql_quotes_identifiers(monkeypatch):
    seen = stats_stub(monkeypatch)
    st = await semanticmodel.relationship_stats(rel_def())
    assert st["match_rate"] == 0.9982 and st["duplicate_keys"] == 0
    assert all('"public"."sales_orders"' in s for s in seen)
    assert 'LEFT JOIN t ON f."order_id" = t."id"' in seen[0]


# ---------------------------------------------------------------- tables

def table_def(name="order_lines", relation="public.order_lines", **kw):
    d = {"name": name, "relation": relation, "role": "fact", "grain": "one row per line",
         "primary_key": ["id"], "agg_time_dimension": None}
    d.update(kw)
    return d


async def test_table_create_ok(env):
    pool = env(drop_tables=["order_lines"])
    assert await semanticmodel.apply(payload("table", "order_lines", table_def())) is True
    assert len(writes(pool, "model_tables")) == 1


async def test_table_unknown_relation_refused(env):
    pool = env()
    d = table_def("ghost", "public.ghost")
    assert await semanticmodel.apply(payload("table", "ghost", d)) is False
    assert not pool.executed


async def test_table_time_dimension_on_text_column_refused(env):
    pool = env(drop_tables=["order_lines"])
    d = table_def(agg_time_dimension="public.sales_orders.status")
    assert await semanticmodel.apply(payload("table", "order_lines", d)) is False
    assert not pool.executed


async def test_table_time_reach_through_relationship(env):
    d = table_def(agg_time_dimension="public.sales_orders.order_date")
    pool = env(drop_tables=["order_lines"])
    assert await semanticmodel.apply(payload("table", "order_lines", d)) is True
    assert len(writes(pool, "model_tables")) == 1
    pool = env(drop_tables=["order_lines"], drop_rels=["order_lines_order"])
    assert await semanticmodel.apply(payload("table", "order_lines", d)) is False
    assert not pool.executed


async def test_table_time_reach_missing_column_refused(env):
    pool = env(drop_tables=["order_lines"])
    d = table_def(agg_time_dimension="public.sales_orders.no_such_date")
    assert await semanticmodel.apply(payload("table", "order_lines", d)) is False
    assert not pool.executed


# ---------------------------------------------------------------- warehouse objects

class DDL:
    def __init__(self, monkeypatch, fail=None):
        self.calls, self.conns, self.fail = [], [], fail
        self.locked_during_connect = []
        mode = {"on": True}
        monkeypatch.setattr(semanticmodel.warehouse, "warehouse_mode", lambda: mode["on"])
        self.mode = mode
        monkeypatch.setattr(semanticmodel.warehouse, "connect", self.connect)
        for fn in ("ensure_model_schema", "check_reader_refs", "create_model_view", "create_model_matview",
                   "refresh_model_matview", "create_model_index", "drop_model_object"):
            monkeypatch.setattr(semanticmodel.whlink, fn, self._make(fn))

        async def objs():
            return {}
        monkeypatch.setattr(semanticmodel, "warehouse_objects", objs)

    async def connect(self, role, **kw):
        self.locked_during_connect.append(warehouse._lock().locked())
        c = type("C", (), {"closed": False, "role": role})()

        async def close():
            c.closed = True
        c.close = close
        self.conns.append(c)
        return c

    def _make(self, fn):
        async def f(conn, *a, **k):
            self.calls.append((fn, a))
            if self.fail == fn:
                raise semanticmodel.whlink.ModelDDLRefused("refused")
            if fn.startswith("create_model_") and fn != "create_model_index":
                return {"d": "date", "total": "numeric"}
        return f


VIEW_SQL = "SELECT order_date, count(*) AS n FROM public.sales_orders GROUP BY order_date"


async def test_view_outside_warehouse_mode_never_connects(env, monkeypatch):
    pool = env()
    called = []

    async def no_connect(*a, **k):
        called.append(1)

    monkeypatch.setattr(semanticmodel.warehouse, "connect", no_connect)
    for obj in ("view", "materialized_view"):
        assert await semanticmodel.apply(payload(obj, "v1", {"sql": VIEW_SQL})) is False
    d = {"name": "ix", "relation": "model.v1", "columns": ["n"]}
    assert await semanticmodel.apply(payload("index", "ix", d)) is False
    assert not called and not pool.executed


async def test_view_create_runs_ddl_as_owner_under_lock(env, monkeypatch):
    pool = env()
    ddl = DDL(monkeypatch)
    assert await semanticmodel.apply(payload("view", "daily_orders", {"sql": VIEW_SQL})) is True
    names = [c[0] for c in ddl.calls]
    assert names == ["ensure_model_schema", "check_reader_refs", "create_model_view"]
    assert ddl.calls[1][1][0] == ["public.sales_orders"]
    assert ddl.conns[0].role == "owner" and ddl.conns[0].closed
    assert ddl.locked_during_connect == [True]
    args = writes(pool, "model_tables")[0][1]
    assert args[1] == "model.daily_orders" and args[2] == "view" and "sales_orders" in args[3]
    assert json.loads(args[5]) == {"columns": {"d": "date", "total": "numeric"}}


async def test_view_ddl_refusal_closes_connection_and_writes_nothing(env, monkeypatch):
    pool = env()
    ddl = DDL(monkeypatch, fail="check_reader_refs")
    assert await semanticmodel.apply(payload("view", "daily_orders", {"sql": VIEW_SQL})) is False
    assert ddl.conns[0].closed
    assert not pool.executed


async def test_view_with_ic_schema_refused_before_ddl(env, monkeypatch):
    pool = env()
    ddl = DDL(monkeypatch)
    assert await semanticmodel.apply(payload("view", "bad", {"sql": "SELECT detail FROM ic_meta.link_state"})) is False
    assert not ddl.calls and not pool.executed


async def test_matview_refresh_touches_updated_at_only(env, monkeypatch):
    mv = {"name": "mv1", "relation": "model.mv1", "object_kind": "matview", "status": "active"}
    pool = env(extra_tables=[mv])
    ddl = DDL(monkeypatch)
    assert await semanticmodel.apply(payload("materialized_view", "mv1", {}, op="refresh")) is True
    assert [c[0] for c in ddl.calls] == ["refresh_model_matview"]
    sql, args = pool.executed[0]
    assert sql.startswith("UPDATE model_tables SET action_id") and "updated_at = now()" in sql


async def test_view_retire_drops_then_retires(env, monkeypatch):
    v = {"name": "v1", "relation": "model.v1", "object_kind": "view", "status": "active"}
    pool = env(extra_tables=[v])
    ddl = DDL(monkeypatch)
    assert await semanticmodel.apply(payload("view", "v1", {}, op="retire")) is True
    assert ddl.calls[0][0] == "drop_model_object" and ddl.calls[0][1] == ("view", "v1")
    assert "status = 'retired'" in pool.executed[0][0]


async def test_view_retire_blocked_by_dependents_keeps_row_active(env, monkeypatch):
    v = {"name": "v1", "relation": "model.v1", "object_kind": "view", "status": "active"}
    pool = env(extra_tables=[v])
    ddl = DDL(monkeypatch, fail="drop_model_object")
    assert await semanticmodel.apply(payload("view", "v1", {}, op="retire")) is False
    assert ddl.conns[0].closed and not pool.executed


async def test_index_create_needs_active_matview(env, monkeypatch):
    pool = env()
    DDL(monkeypatch)
    d = {"name": "ix", "relation": "model.mv1", "columns": ["n"]}
    assert await semanticmodel.apply(payload("index", "ix", d)) is False
    pool = env(extra_tables=[{"name": "mv1", "relation": "model.mv1", "object_kind": "matview", "status": "active"}])
    ddl = DDL(monkeypatch)
    assert await semanticmodel.apply(payload("index", "ix", d)) is True
    assert ddl.calls[-1][0] == "create_model_index" and ddl.calls[-1][1] == ("ix", "mv1", ["n"])
    assert len(writes(pool, "model_indexes")) == 1


async def test_view_recreated_when_warehouse_object_missing(env, monkeypatch):
    v = {"name": "daily_orders", "relation": "model.daily_orders", "object_kind": "view", "status": "active"}
    pool = env(extra_tables=[v])
    DDL(monkeypatch)
    assert await semanticmodel.apply(payload("view", "daily_orders", {"sql": VIEW_SQL})) is True
    assert len(writes(pool, "model_tables")) == 1


# ---------------------------------------------------------------- loader

async def test_load_model_uses_only_active_rows_and_decodes_json(monkeypatch):
    m = dict(md("net_sales"))
    m["filter"] = json.dumps(m["filter"])
    retired = {**md("customer_count"), "status": "retired"}

    class P:
        async def fetch(self, sql, *a):
            if "model_measures" in sql:
                return [{**m, "status": "active"}] if "status = 'active'" in sql else [m, retired]
            if "model_tables" in sql:
                return TABLES
            return RELS
    p = P()

    async def get_pool():
        return p

    async def catalog():
        return COLUMNS
    monkeypatch.setattr(semanticmodel, "get_pool", get_pool)
    monkeypatch.setattr(semanticmodel, "catalog_columns", catalog)
    model = await semanticmodel.load_model()
    assert [x.name for x in model.measures] == ["net_sales"]
    assert model.measures[0].filter[0].op == "not_in"


async def test_warehouse_objects_fail_soft(monkeypatch):
    assert await semanticmodel.warehouse_objects() == {}
    monkeypatch.setattr(semanticmodel.warehouse, "warehouse_mode", lambda: True)

    async def boom(sql):
        raise RuntimeError("x")
    monkeypatch.setattr(semanticmodel, "run_readonly_sql", boom)
    assert await semanticmodel.warehouse_objects() == {}

    async def ok(sql):
        return [{"relname": "v1", "relkind": "v"}]
    monkeypatch.setattr(semanticmodel, "run_readonly_sql", ok)
    assert await semanticmodel.warehouse_objects() == {"v1": "v"}


async def test_no_delete_or_truncate_in_module():
    src = open(semanticmodel.__file__, encoding="utf-8").read()
    assert "DELETE FROM model_" not in src and "TRUNCATE" not in src
