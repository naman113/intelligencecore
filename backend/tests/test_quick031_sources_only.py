"""Quick 031: a sources-only install (no business database, registered sources only) can model.

The real schemadiscovery -> semanticmodel -> modeltools chain runs here; only the I/O seams are
faked (the warehouse reader SQL, the agent-memory pool, the source registry lookup). With no
business database URL, the business_db catalog is empty instead of BusinessDbUnreachable. With a
URL set and the link missing or failing, every caller still fails closed.
"""
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from app import freshness, guardrails, schemadiscovery as sd, semanticmodel, sourcesvc, warehouse
from app import modeltools as mt
from app.agents import graph as g
from app.config import Settings
from app.guardrails import BusinessDbUnreachable, SQLGuardrailError

TODAY = date(2026, 10, 27)
SRC = "distributor"


def _col(name, type_, pk=False, fk=None, nullable=True):
    return {"name": name, "type": type_, "nullable": nullable, "pk": pk, "fk": fk, "comment": None}


SRC_ROWS = [
    {"schema_name": "src_distributor", "table_name": "customers", "relkind": "r", "table_comment": None,
     "row_estimate": 50, "checks": None,
     "columns": json.dumps([_col("id", "integer", pk=True, nullable=False), _col("name", "text")])},
    {"schema_name": "src_distributor", "table_name": "orders", "relkind": "r", "table_comment": None,
     "row_estimate": 300, "checks": None,
     "columns": json.dumps([_col("id", "integer", pk=True, nullable=False),
                            _col("customer_id", "integer", fk={"table": "customers", "column": "id"}),
                            _col("order_date", "date"), _col("amount", "numeric")])},
]

ORDERS_TABLE = {"name": "orders", "relation": "src_distributor.orders", "object_kind": "base", "role": "fact",
                "grain": "one row per order", "primary_key": ["id"],
                "agg_time_dimension": "src_distributor.orders.order_date", "status": "active"}
ORDER_COUNT = {"name": "order_count", "type": "simple", "base_relation": "src_distributor.orders",
               "agg": "count", "expr": None, "agg_time_dimension": "src_distributor.orders.order_date",
               "filter": None, "numerator": None, "denominator": None, "input_metrics": None,
               "non_additive_dimension": None, "additivity": "additive", "unit": None,
               "format": None, "label": "Orders", "description": None, "status": "active"}


class FakePool:
    """agent_memory: the source registry, model rows and the actions table."""

    def __init__(self, st):
        self.st = st
        self.inserts = []

    async def fetch(self, sql, *a):
        if "FROM sources WHERE status = 'active'" in sql:
            return [{"name": SRC}]
        if "FROM model_tables WHERE object_kind IN" in sql:
            return []
        if sql.startswith("SELECT * FROM model_tables"):
            return list(self.st["tables"])
        if sql.startswith("SELECT * FROM model_measures"):
            return list(self.st["measures"])
        if sql.startswith("SELECT * FROM model_"):
            return []
        if "FROM actions WHERE kind = 'model_change'" in sql:
            return [{"id": i, "payload": json.dumps(p), "created_at": "2026-10-27T00:00:00"}
                    for i, p in self.inserts]
        raise AssertionError(f"unexpected fetch: {sql[:80]}")

    async def fetchrow(self, sql, *a):
        return None  # no active model object of that name

    async def fetchval(self, sql, *a):
        assert sql.lstrip().startswith("INSERT INTO actions"), sql[:80]
        new_id = 500 + len(self.inserts)
        self.inserts.append((new_id, json.loads(a[0])))
        return new_id


@pytest.fixture
def st(monkeypatch):
    s = {"url": False, "link": [], "tables": [], "measures": [], "wh_sql": [], "tenant_sql": [],
         "logs": []}
    pool = FakePool(s)
    s["pool"] = pool

    async def wh_sql(sql, limit=None):
        s["wh_sql"].append(sql)
        if sql == sd.WAREHOUSE_LINK_STATUS_SQL:
            return s["link"]
        if f"s.source = '{SRC}'" in sql:
            return SRC_ROWS
        if sql == sd.WAREHOUSE_CATALOG_SQL:
            return []
        raise AssertionError(f"unexpected catalog SQL: {sql[:80]}")

    async def tenant_sql(sql, limit=None):
        s["tenant_sql"].append(sql)
        if "row_count" in sql:
            return [{"row_count": 300, "distinct_keys": 300}]
        if "AS day" in sql:
            return [{"day": date(2026, 10, 20), "rows": 9}]
        if "metric_time__day" in sql:
            return [{"metric_time__day": date(2026, 10, 20), "order_count": 9}]
        if "metric_time__month" in sql:
            return [{"metric_time__month": date(2026, 10, 1), "order_count": 41}]
        return [{"order_count": 300}]

    async def get_source(name):
        return {"name": name, "status": "active"} if name == SRC else None

    async def get_pool():
        return pool

    async def warehouse_objects():
        return {}

    async def relationship_stats(defn):
        return {"sql": "SELECT 1", "from_rows": 300, "from_nonnull": 300, "matched": 300,
                "match_rate": 1.0, "duplicate_keys": 0, "null_keys": 0, "from_duplicate_keys": 0}

    async def no_facts():
        raise RuntimeError("no facts in this test")

    async def log_tool(name, args, result, start, refused=False):
        s["logs"].append({"name": name, "result": result, "refused": refused})

    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    monkeypatch.setattr(guardrails, "_business_url_set", lambda: s["url"])
    monkeypatch.setattr(sd, "run_readonly_sql", wh_sql)
    monkeypatch.setattr(sourcesvc, "get_source", get_source)
    monkeypatch.setattr(semanticmodel, "get_pool", get_pool)
    monkeypatch.setattr(semanticmodel, "run_readonly_sql", tenant_sql)
    monkeypatch.setattr(semanticmodel, "warehouse_objects", warehouse_objects)
    monkeypatch.setattr(semanticmodel, "relationship_stats", relationship_stats)
    monkeypatch.setattr(mt, "get_pool", get_pool)
    monkeypatch.setattr(mt, "run_readonly_sql", tenant_sql)
    monkeypatch.setattr(mt, "_log_tool", log_tool)
    monkeypatch.setattr(mt, "_today", lambda: TODAY)
    monkeypatch.setattr(freshness, "load_facts", no_facts)
    g.ctx_run_id.set(31)
    g.ctx_phone.set("ui:q031")
    return s


# -- discovery ------------------------------------------------------------------

async def test_business_catalog_empty_without_business_url(st):
    cat = await sd.introspect()
    assert cat == {"source": "business_db", "tables": [], "truncated": False}
    assert st["wh_sql"] == []  # neither the link status nor the catalog is read


async def test_resolve_business_ref_is_none_and_source_ref_resolves(st):
    assert await sd.resolve("business_db.public.orders") is None
    hit = await sd.resolve("distributor.src_distributor.orders.order_date")
    assert hit and hit["kind"] == "column" and hit["source"] == SRC


@pytest.mark.parametrize("link", [[], [{"status": "error"}]])
async def test_business_url_set_link_down_still_fails_closed(st, link):
    st["url"], st["link"] = True, link
    with pytest.raises(BusinessDbUnreachable):
        await sd.introspect()
    with pytest.raises(BusinessDbUnreachable):
        await semanticmodel.catalog_columns()
    with pytest.raises(BusinessDbUnreachable):
        await semanticmodel.foreign_keys()
    assert "catalog unavailable (BusinessDbUnreachable)" in await mt.get_model.ainvoke({})
    out = json.loads(await mt.get_model.ainvoke({"section": "candidates"}))
    assert out["catalog_error"] == "BusinessDbUnreachable"


async def test_discover_business_db_says_not_configured(st, monkeypatch):
    called = []

    async def fresh(max_age=0.0, timeout=20.0):
        called.append(1)
        return {"status": "not_configured"}
    async def known(source="business_db"):
        return {"tables": {}, "columns": {}, "pending": set()}

    async def samples(schema, table):
        return {}
    monkeypatch.setattr(warehouse, "ensure_fresh", fresh)
    monkeypatch.setattr(sd, "known_state", known)
    monkeypatch.setattr(sd, "sample_values", samples)
    with pytest.raises(SQLGuardrailError) as e:
        await sd.discover()
    assert str(e.value) == sd.NO_BUSINESS_DB and called == []
    d = await sd.discover(SRC)  # the registered source still discovers
    assert {t["table"] for t in d["tables"]} == {"customers", "orders"}


# -- model plumbing over src_ tables ----------------------------------------------

async def test_catalog_columns_and_foreign_keys_over_sources(st):
    cols = await semanticmodel.catalog_columns()
    assert cols["src_distributor.orders"]["order_date"] == "date"
    assert set(cols) == {"src_distributor.customers", "src_distributor.orders"}
    fks = await semanticmodel.foreign_keys()
    assert fks == [{"schema": "src_distributor", "table": "orders", "column": "customer_id",
                    "to_table": "customers", "to_column": "id"}]


async def test_get_model_lists_src_candidates(st):
    out = json.loads(await mt.get_model.ainvoke({"section": "candidates"}))
    assert "catalog_error" not in out
    assert {c["relation"] for c in out["items"]} == {"src_distributor.customers", "src_distributor.orders"}
    assert out["unseeded_foreign_keys"] == 1
    summary = await mt.get_model.ainvoke({})
    assert "2 catalog tables not modeled yet" in summary and "1 foreign keys not proposed yet" in summary


async def test_seed_relationships_from_source_keys(st):
    out = await mt.propose_relationships_from_keys.ainvoke({})
    assert out.startswith("1 proposed, 0 pending, 0 refused"), out
    (_, payload), = st["pool"].inserts
    d = payload["definition"]
    assert payload["object"] == "relationship" and payload["origin"] == "foreign_key"
    assert (d["from_relation"], d["to_relation"]) == ("src_distributor.orders", "src_distributor.customers")


async def test_table_role_proposal_over_src_table_validates(st):
    item = {"object": "table", "definition": {
        "name": "orders", "relation": "src_distributor.orders", "role": "fact",
        "grain": "one row per order", "primary_key": ["id"],
        "agg_time_dimension": "src_distributor.orders.order_date"}}
    out = await mt.propose_model_change.ainvoke({"items": [item]})
    assert out.startswith("1 proposed, 0 pending, 0 refused"), out
    (_, payload), = st["pool"].inserts
    assert payload["evidence"]["row_count"] == 300
    assert payload["evidence"]["rows_per_day"] == [{"day": "2026-10-20", "rows": 9}]


async def test_measure_over_src_table_compiles_and_query_measure_runs(st):
    st["tables"] = [dict(ORDERS_TABLE)]
    item = {"object": "measure", "definition": {
        "name": "order_count", "type": "simple", "base_relation": "src_distributor.orders",
        "agg": "count", "agg_time_dimension": "src_distributor.orders.order_date"}}
    out = await mt.propose_model_change.ainvoke({"items": [item]})
    assert out.startswith("1 proposed, 0 pending, 0 refused"), out
    (_, payload), = st["pool"].inserts
    assert '"src_distributor"."orders"' in payload["evidence"]["sql"]
    # approved: the measure is active; query_measure compiles and runs over src_distributor.orders
    st["measures"] = [dict(ORDER_COUNT)]
    doc = json.loads(await mt.query_measure.ainvoke(
        {"measure": "order_count", "grain": "month", "range": "this_year"}))
    assert '"src_distributor"."orders"' in doc["sql"]
    assert doc["rows"] and doc["row_count"] == 1


# -- the Model page: every /api/model/* route answers 200 (the coordinator's scope addition) -----

async def test_every_model_api_route_is_200_without_business_db(st, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.main as main
    from app import modelapi

    async def get_pool():
        return st["pool"]
    st["tables"] = [dict(ORDERS_TABLE)]
    st["measures"] = [dict(ORDER_COUNT)]
    monkeypatch.setattr(modelapi, "db", SimpleNamespace(get_pool=get_pool))
    monkeypatch.setitem(main.app.dependency_overrides, main._require_ops_key, lambda: None)
    paths = sorted(r.path for r in modelapi.router.routes)
    assert paths == [f"/api/model/{x}" for x in ("measures", "pending", "relationships", "tables")]
    c = TestClient(main.app, raise_server_exceptions=False)
    for path in paths:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
    m = c.get("/api/model/measures").json()["measures"][0]
    assert "compile_error" not in m and '"src_distributor"."orders"' in m["compiled_sql"]
    assert c.get("/api/model/tables").json()["tables"][0]["relation"] == "src_distributor.orders"


# -- the business_db table sync never reads absence of config as "tables gone" ---------

async def test_business_db_table_sync_skips_without_business_url(monkeypatch):
    monkeypatch.setattr(guardrails, "_business_url_set", lambda: False)

    async def boom():
        raise AssertionError("no registry writes without a business database")
    monkeypatch.setattr(sourcesvc, "get_pool", boom)
    assert await sourcesvc.sync_business_db_tables() == {"skipped": "not_configured"}


# -- Bug 2: the local compose overlay names the warehouse container ----------------

APP = Path(__file__).resolve().parents[2]


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_Loader.add_constructor("!override", _tag)
_Loader.add_constructor("!reset", _tag)


def _load(name: str) -> dict:
    return yaml.load((APP / name).read_text(encoding="utf-8"), Loader=_Loader)


def test_local_sync_warehouse_host_is_the_warehouse_container_name():
    base, local = _load("docker-compose.yml"), _load("docker-compose.local.yml")
    names = {s.get("container_name") for s in base["services"].values()}
    names |= {s.get("container_name") for s in local["services"].values()}
    host = local["services"]["app"]["environment"]["SYNC_WAREHOUSE_HOST"]
    assert host in names  # `docker inspect <host>` needs a container name, not a service name
    wh = (local["services"].get("warehouse") or {}).get("container_name") \
        or base["services"]["warehouse"]["container_name"]
    assert host == wh == Settings.model_fields["sync_warehouse_host"].default


