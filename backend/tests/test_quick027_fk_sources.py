"""Quick 027 (audit #2): semanticmodel.foreign_keys also reads registered sources' catalogs."""
import pytest

from app import semanticmodel, warehouse


def _snap(schema, table, col, fk):
    return {"tables": [{"schema": schema, "table": table,
                        "columns": [{"name": col, "type": "integer", "fk": fk}]}]}


class _Pool:
    async def fetch(self, sql, *a):
        assert "FROM sources" in sql
        return [{"name": "shop"}, {"name": "bad"}]


async def test_foreign_keys_include_registered_sources(monkeypatch):
    async def introspect(source="business_db"):
        if source == "business_db":
            return _snap("public", "orders", "customer_id", {"table": "customers", "column": "id"})
        if source == "bad":
            raise ValueError("unknown")
        return _snap("src_shop", "orders", "customer_id", {"table": "customers", "column": "id"})

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(semanticmodel.schemadiscovery, "introspect", introspect)
    monkeypatch.setattr(semanticmodel, "get_pool", get_pool)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    got = await semanticmodel.foreign_keys()
    assert {(f["schema"], f["table"]) for f in got} == {("public", "orders"), ("src_shop", "orders")}


async def test_foreign_keys_business_db_only_without_warehouse(monkeypatch):
    async def introspect(source="business_db"):
        assert source == "business_db"
        return _snap("public", "orders", "customer_id", {"table": "customers", "column": "id"})

    monkeypatch.setattr(semanticmodel.schemadiscovery, "introspect", introspect)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    got = await semanticmodel.foreign_keys()
    assert [f["schema"] for f in got] == ["public"]
