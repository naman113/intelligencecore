"""Phase 15 (SRC-09): discovery over registered sources."""
import json

import pytest

from app import contexttools as ct, schemadiscovery as sd, sourcesvc, warehouse

NAME = "northwind_my"
SCHEMA = "src_northwind_my"


def _row(table, cols):
    return {"schema_name": SCHEMA, "table_name": table, "relkind": "r", "table_comment": None,
            "row_estimate": 5, "columns": json.dumps(cols), "checks": None}


COLS = [{"name": "id", "type": "integer", "nullable": False, "pk": True, "fk": None, "comment": None},
        {"name": "name", "type": "text", "nullable": True, "pk": False, "fk": None, "comment": None}]


class _S:
    sql_row_limit = 500


@pytest.fixture
def env(monkeypatch):
    rec = []

    async def fake(sql):
        rec.append(sql)
        if sql == sd.WAREHOUSE_LINK_STATUS_SQL:
            return [{"status": "ok"}]
        return [_row("suppliers", COLS)]

    async def gs(n):
        return {"name": n, "status": "active", "tables": []} if n == NAME else None

    async def ls():
        return [{"name": NAME, "status": "active"}, {"name": "old_one", "status": "disabled"}]

    async def known(source):
        return {"tables": {}, "columns": {}, "pending": set()}

    async def samples(schema, table):
        return {"name": ["acme"]}

    monkeypatch.setattr(sd, "run_readonly_sql", fake)
    monkeypatch.setattr(sd, "get_settings", lambda: _S())
    monkeypatch.setattr(sourcesvc, "get_source", gs)
    monkeypatch.setattr(sourcesvc, "list_sources", ls)
    monkeypatch.setattr(sd, "known_state", known)
    monkeypatch.setattr(sd, "sample_values", samples)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    monkeypatch.setattr(sd, "business_db_configured", lambda: True)  # Quick 031: a business DB is set
    return rec


async def test_introspect_registered_uses_source_literal(env):
    cat = await sd.introspect(NAME)
    assert env == [sd._source_catalog_sql(NAME)]
    assert f"s.source = '{NAME}'" in env[0] and "business_db" not in env[0]
    assert cat["source"] == NAME and cat["tables"][0]["table"] == "suppliers"


async def test_business_db_sql_unchanged(env):
    await sd.introspect("business_db")
    assert env == [sd.WAREHOUSE_LINK_STATUS_SQL, sd.WAREHOUSE_CATALOG_SQL]


async def test_invalid_or_unknown_name_refused_before_sql(env):
    for bad in ("x'; drop table t;--", "Bad Name", "nope"):
        with pytest.raises(ValueError):
            await sd.introspect(bad)
    assert env == []


async def test_needs_warehouse(env, monkeypatch):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    with pytest.raises(ValueError, match="need the warehouse"):
        await sd.introspect(NAME)


async def test_resolve_registered_refs(env):
    t = await sd.resolve(f"{NAME}.{SCHEMA}.suppliers")
    assert t["kind"] == "table" and t["source"] == NAME
    c = await sd.resolve(f"{NAME}.{SCHEMA}.suppliers.name")
    assert c["kind"] == "column" and c["source"] == NAME and c["schema"] == SCHEMA
    assert await sd.resolve("ghost.src_ghost.t.c") is None


async def test_discover_no_relink_for_registered(env, monkeypatch):
    calls = []

    async def ef(max_age=0):
        calls.append(1)
    monkeypatch.setattr(warehouse, "ensure_fresh", ef)
    d = await sd.discover(NAME)
    assert calls == [] and d["source"] == NAME
    assert f"{NAME}.{SCHEMA}.suppliers" in sd.format_discovery(d)
    await sd.discover("business_db")
    assert calls == [1]


async def test_discover_schema_refusal_lists_active_sources(env, monkeypatch):
    async def lt(*a, **k):
        pass
    monkeypatch.setattr(ct, "_log_tool", lt)
    out = await ct.discover_schema.ainvoke({"source": "nope"})
    assert out == "REFUSED: unknown source 'nope'. Known sources: business_db, northwind_my."
    out = await ct.discover_schema.ainvoke({"source": NAME})
    assert "suppliers" in out


async def test_propose_registered_column_payload_source(env, monkeypatch):
    payloads = []

    class Pool:
        async def fetchrow(self, sql, *a):
            return None

        async def fetchval(self, sql, *a):
            if "INSERT INTO actions" in sql:
                payloads.append(json.loads(a[0]))
                return 1
            return None

    async def gp():
        return Pool()

    async def lt(*a, **k):
        pass
    monkeypatch.setattr(ct, "get_pool", gp)
    monkeypatch.setattr(ct, "_log_tool", lt)
    out = await ct.propose_context_change.ainvoke(
        {"kind": "column", "ref": f"{NAME}.{SCHEMA}.suppliers.name", "text": "Supplier name."})
    assert "1 proposed" in out
    assert payloads[0]["source"] == NAME and payloads[0]["schema"] == SCHEMA
