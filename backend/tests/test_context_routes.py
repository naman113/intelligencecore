"""Read-only /api/context routes: shape, auth, whitelists, merge of pending actions."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import contextapi


class Pool:
    def __init__(self, tables=(), columns=(), rules=(), sources=(), actions=()):
        self.tables, self.columns, self.rules = list(tables), list(columns), list(rules)
        self.sources, self.actions = list(sources), list(actions)
        self.sql = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        s = " ".join(sql.split())
        if "FROM actions" in s and "payload->>'kind'" in s:
            kinds = args[0]
            return [a for a in self.actions
                    if json.loads(a["payload"])["kind"] in kinds]
        if s.startswith("SELECT s.name"):
            return self.sources
        if "FROM context_columns" in s:
            return self.columns
        if "FROM context_tables" in s:
            return self.tables
        if "FROM business_context" in s:
            return self.rules
        if "FROM context_sources" in s:
            return self.sources
        raise AssertionError(s)


def _wire(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(contextapi, "db", SimpleNamespace(get_pool=gp))
    return pool


def _action(i, **payload):
    return {"id": i, "payload": json.dumps(payload), "created_at": "2026-10-24"}


TABLE = {"ref": "business_db.public.customers", "schema_name": "public",
         "table_name": "customers", "object_kind": "table", "description": "Customers.",
         "status": "active", "approved_at": None}
UNDOC = dict(TABLE, ref="business_db.public.regions", table_name="regions", description=None)
HIDDEN = dict(TABLE, ref="business_db.public.audit", table_name="audit", status="hidden")


def _flat():
    """(path, methods, dependencies) for every app route; newer FastAPI wraps an
    included router in an _IncludedRouter, so unwrap it (router-level
    dependencies live on include_context)."""
    out = []
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inherited = list(r.include_context.dependencies)
            for sub in r.original_router.routes:
                out.append((sub.path, set(sub.methods or ()),
                            inherited + list(sub.dependencies)))
        elif hasattr(r, "path"):
            out.append((r.path, set(getattr(r, "methods", None) or ()),
                        list(getattr(r, "dependencies", []))))
    return out


def _ctx_routes():
    return [t for t in _flat() if t[0].startswith("/api/context")]


def test_route_set_and_methods():
    paths = {p for p, _, _ in _ctx_routes()}
    assert paths == {f"/api/context/{x}" for x in
                     ("sources", "tables", "columns", "rules", "search")}
    for _, methods, _ in _ctx_routes():
        assert methods <= {"GET", "HEAD"}


def test_no_discover_route_anywhere():
    assert not [p for p, _, _ in _flat() if "discover" in p]


def test_every_route_requires_ops_key():
    for path, _, deps in _ctx_routes():
        assert any(getattr(d, "dependency", None) is main._require_ops_key
                   for d in deps), path
    assert TestClient(main.app).get("/api/context/tables").status_code == 401


@pytest.mark.asyncio
async def test_bad_filters_422(monkeypatch):
    _wire(monkeypatch, Pool())
    for coro in (contextapi.list_tables(status="bogus"),
                 contextapi.list_columns(status="bogus"),
                 contextapi.list_rules(kind="bogus"),
                 contextapi.list_rules(status="hidden"),
                 contextapi.search(q="")):
        with pytest.raises(HTTPException) as e:
            await coro
        assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_tables_merge_and_filter(monkeypatch):
    acts = [_action(7, kind="table", ref="business_db.public.orders", schema="public",
                    table="orders", text="Orders.", before=None),
            _action(8, kind="table", ref=TABLE["ref"], text="New text")]
    _wire(monkeypatch, Pool(tables=[TABLE, UNDOC, HIDDEN], actions=acts))
    allv = (await contextapi.list_tables())["tables"]
    by = {t["ref"]: t for t in allv}
    assert by["business_db.public.orders"]["status"] == "proposed"
    assert by["business_db.public.orders"]["pending"]["action_id"] == 7
    assert by["business_db.public.orders"]["pending"]["text"] == "Orders."
    assert by[TABLE["ref"]]["status"] == "approved"
    assert by[TABLE["ref"]]["pending"]["action_id"] == 8
    assert by[UNDOC["ref"]]["status"] == "undocumented"
    assert by[HIDDEN["ref"]]["status"] == "hidden"
    only = (await contextapi.list_tables(status="proposed"))["tables"]
    assert [t["ref"] for t in only] == ["business_db.public.orders"]
    assert len((await contextapi.list_tables(limit=0))["tables"]) == 1
    assert len((await contextapi.list_tables(limit=99999))["tables"]) == 4


@pytest.mark.asyncio
async def test_columns_filter_by_table_and_pending(monkeypatch):
    col = {"ref": "business_db.public.customers.name", "table_ref": TABLE["ref"],
           "column_name": "name", "data_type": "text", "is_nullable": False,
           "is_primary_key": False, "references_ref": None, "description": "Name.",
           "status": "active", "approved_at": None}
    acts = [_action(3, kind="column", ref="business_db.public.orders.total",
                    column="total", text="Total", structure={"type": "numeric"})]
    _wire(monkeypatch, Pool(columns=[col], actions=acts))
    allc = (await contextapi.list_columns())["columns"]
    assert {c["status"] for c in allc} == {"approved", "proposed"}
    mine = (await contextapi.list_columns(table=TABLE["ref"]))["columns"]
    assert [c["column"] for c in mine] == ["name"]
    prop = (await contextapi.list_columns(status="proposed"))["columns"]
    assert prop[0]["data_type"] == "numeric" and prop[0]["pending"]["action_id"] == 3


@pytest.mark.asyncio
async def test_rules_decode_meta_and_pending(monkeypatch):
    rule = {"kind": "kpi", "ref": "kpi:revenue", "title": "Revenue", "body": "Sum.",
            "meta": json.dumps({"tables": ["business_db.public.invoices"]}),
            "status": "approved", "updated_at": None}
    acts = [_action(5, kind="rule", ref="rule:exclude-void", title="Void", text="x",
                    tables=["business_db.public.invoices"])]
    _wire(monkeypatch, Pool(rules=[rule], actions=acts))
    allr = (await contextapi.list_rules())["rules"]
    by = {r["ref"]: r for r in allr}
    assert by["kpi:revenue"]["tables"] == ["business_db.public.invoices"]
    assert by["rule:exclude-void"]["status"] == "proposed"
    only = (await contextapi.list_rules(status="proposed"))["rules"]
    assert [r["ref"] for r in only] == ["rule:exclude-void"]


@pytest.mark.asyncio
async def test_sources_and_search(monkeypatch):
    src = {"name": "business_db", "kind": "postgres", "description": None,
           "status": "active", "tables_approved": 1, "columns_approved": 2, "pending": 3}
    pool = _wire(monkeypatch, Pool(sources=[src]))
    assert (await contextapi.list_sources())["sources"][0]["pending"] == 3

    async def fake(q, limit=8):
        return [{"q": q, "limit": limit}]
    monkeypatch.setattr(contextapi.knowledge, "search_business_context", fake)
    assert await contextapi.search(q="revenue", limit=4) == [{"q": "revenue", "limit": 4}]
    assert pool.sql


@pytest.mark.asyncio
async def test_handlers_issue_no_writes(monkeypatch):
    pool = _wire(monkeypatch, Pool(tables=[TABLE]))
    await contextapi.list_sources()
    await contextapi.list_tables()
    await contextapi.list_columns()
    await contextapi.list_rules()
    for sql in pool.sql:
        up = sql.upper()
        assert "INSERT " not in up and "UPDATE " not in up and "DELETE " not in up


@pytest.mark.asyncio
async def test_rules_carry_origin_and_pending_edit(monkeypatch):
    rules = [{"kind": "rule", "ref": "r1", "title": "T", "body": "b", "meta": "{}",
              "status": "approved", "origin": "agent", "updated_at": "x"},
             {"kind": "rule", "ref": "r2", "title": "T2", "body": "b2", "meta": "{}",
              "status": "approved", "updated_at": "x"}]
    acts = [_action(1, kind="rule", ref="r1", text="new", origin="agent",
                    edited_by="owner")]
    _wire(monkeypatch, Pool(rules=rules, actions=acts))
    out = (await contextapi.list_rules())["rules"]
    by = {i["ref"]: i for i in out}
    assert by["r1"]["origin"] == "agent" and by["r2"]["origin"] is None
    assert by["r1"]["pending"]["origin"] == "agent"
    assert by["r1"]["pending"]["edited_by"] == "owner"
