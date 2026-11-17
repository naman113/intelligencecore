"""Phase 11 (WR-14): contextapi route output keys must match the committed live
fixtures (captured from a real p11 stack by 11-capture-context-fixtures.sh)."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import contextapi, db

LIVE = Path(__file__).resolve().parent.parent.parent / "frontend/tests/fixtures/live"
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
REF_RE = re.compile(r"^business_db\.[^.]+\.[^.]+(\.[^.]+)?$")


def _load(name):
    return json.loads((LIVE / f"{name}.json").read_text(encoding="utf-8"))[name]


def _act(kind, ref, extra=None):
    p = {"kind": kind, "ref": ref, "text": "t", "source": "business_db",
         "origin": "agent", "schema": "public", "table": "t", "column": "c"}
    p.update(extra or {})
    return {"id": 1, "payload": json.dumps(p), "created_at": NOW}


class FakePool:
    def __init__(self, rows, acts):
        self.rows, self.acts = rows, acts

    async def fetch(self, sql, *a):
        return self.acts if "FROM actions" in sql and "context_change" in sql \
            and "COUNT" not in sql.upper() and "SELECT id, payload" in sql else self.rows


@pytest.fixture
def wire(monkeypatch):
    def _w(rows, acts):
        async def gp():
            return FakePool(rows, acts)
        monkeypatch.setattr(db, "get_pool", gp)
    return _w


def _keys(items):
    return {frozenset(i) for i in items}


async def test_sources_keys(wire):
    wire([{"name": "business_db", "kind": "postgres", "description": None,
           "status": "active", "tables_approved": 1, "columns_approved": 0,
           "pending": 2}], [])
    out = (await contextapi.list_sources())["sources"]
    assert _keys(out) == _keys(_load("sources"))


async def test_tables_keys(wire):
    row = {"ref": "business_db.public.t", "schema_name": "public", "table_name": "t",
           "object_kind": "table", "description": "d", "status": "active",
           "approved_at": NOW}
    wire([row], [_act("table", "business_db.public.u", {"object_kind": "table"})])
    out = (await contextapi.list_tables())["tables"]
    assert any(i["pending"] for i in out)
    assert _keys(out) == _keys(_load("tables"))


async def test_columns_keys(wire):
    row = {"ref": "business_db.public.t.c", "table_ref": "business_db.public.t",
           "column_name": "c", "data_type": "integer", "is_nullable": True,
           "is_primary_key": False, "references_ref": None, "description": "d",
           "status": "active", "approved_at": NOW}
    wire([row], [_act("column", "business_db.public.t.d")])
    out = (await contextapi.list_columns())["columns"]
    assert any(i["pending"] for i in out)
    assert _keys(out) == _keys(_load("columns"))


async def test_rules_keys(wire):
    row = {"kind": "rule", "ref": "rule:x", "title": "x", "body": "b",
           "meta": json.dumps({"tables": []}), "status": "approved",
           "origin": "agent", "updated_at": NOW}
    wire([row], [_act("kpi", "kpi:y", {"title": "y"})])
    out = (await contextapi.list_rules())["rules"]
    assert _keys(out) == _keys(_load("rules"))


def test_pending_object_keys():
    pend = [i["pending"] for n in ("tables", "columns", "rules")
            for i in _load(n) if i["pending"]]
    assert pend
    assert all(set(p) == {"action_id", "text", "before", "created_at",
                          "origin", "edited_by"} for p in pend)


def test_fixture_formats():
    for n in ("tables", "columns"):
        assert all(REF_RE.match(i["ref"]) for i in _load(n))
    assert all(REF_RE.match(i["table_ref"]) for i in _load("columns"))
    assert [s["name"] for s in _load("sources")] == ["business_db"]
    assert all(isinstance(s["pending"], int) for s in _load("sources"))
    assert all(r["origin"] in ("agent", "human", None) for r in _load("rules"))
