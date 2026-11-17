"""Phase 16 plan 05: read-only /api/model routes (fakes only; no tenant reads)."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import guardrails, modelapi, modeltools, semanticmodel
from test_model_compiler import COLUMNS, MEASURES, RELS, TABLES, build

TODAY = date(2026, 10, 15)
_JSON = ("filter", "numerator", "denominator", "input_metrics", "non_additive_dimension")


def _flat():
    out = []
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inherited = list(r.include_context.dependencies)
            for sub in r.original_router.routes:
                out.append((sub.path, set(sub.methods or ()), inherited + list(sub.dependencies)))
        elif hasattr(r, "path"):
            out.append((r.path, set(getattr(r, "methods", None) or ()),
                        list(getattr(r, "dependencies", []))))
    return out


def _model_routes():
    return [t for t in _flat() if t[0].startswith("/api/model")]


def _stringify(m):
    """Mimic asyncpg: jsonb columns arrive as strings."""
    d = dict(m)
    for k in _JSON:
        if d.get(k) is not None:
            d[k] = json.dumps(d[k])
    return d


class Pool:
    def __init__(self, rows=()):
        self.rows, self.sql = list(rows), []

    async def fetch(self, sql, *args):
        self.sql.append(" ".join(sql.split()))
        return self.rows


@pytest.fixture
def wired(monkeypatch):
    tables = [dict(t, derived_sql=None, label=None, description=None, meta={}, action_id=1,
                   approved_at="2026-10-01") for t in TABLES]
    tables.append(dict(TABLES[0], name="v_sales", relation="model.v_sales", object_kind="view",
                       derived_sql="select 1", meta={}, status="active"))
    tables.append(dict(TABLES[0], name="old", relation="public.old", status="retired", meta={}))
    rels = [dict(r, origin="foreign_key", match_rate=None) for r in RELS]
    rels[0]["match_rate"] = 0.998
    rows = {"tables": tables,
            "relationships": rels,
            "measures": [_stringify(m) for m in MEASURES],
            "indexes": []}

    async def load_rows(include_retired=False):
        if include_retired:
            return rows
        return {k: [r for r in v if r.get("status", "active") == "active"] for k, v in rows.items()}

    async def load_model():
        return build()

    async def objs():
        return {"v_sales": "v"}

    async def boom(*a, **k):
        raise AssertionError("tenant SQL must not run")

    monkeypatch.setattr(semanticmodel, "load_rows", load_rows)
    monkeypatch.setattr(semanticmodel, "load_model", load_model)
    monkeypatch.setattr(semanticmodel, "warehouse_objects", objs)
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    monkeypatch.setattr(modeltools, "_today", lambda: TODAY)
    return rows


def test_route_set_and_methods():
    paths = {p for p, _, _ in _model_routes()}
    assert paths == {f"/api/model/{x}" for x in ("tables", "relationships", "measures", "pending")}
    for _, methods, _ in _model_routes():
        assert methods <= {"GET", "HEAD"}


def test_every_route_requires_ops_key():
    for path, _, deps in _model_routes():
        assert any(getattr(d, "dependency", None) is main._require_ops_key for d in deps), path
    c = TestClient(main.app)
    for x in ("tables", "relationships", "measures", "pending"):
        assert c.get(f"/api/model/{x}").status_code == 401


def test_modelapi_source_has_no_tenant_reader():
    src = open(modelapi.__file__, encoding="utf-8").read()
    assert "run_readonly_sql" not in src


async def test_tables_default_active_and_warehouse_flag(wired):
    out = await modelapi.list_tables()
    names = {t["name"] for t in out["tables"]}
    assert "old" not in names and "v_sales" in names
    by = {t["name"]: t for t in out["tables"]}
    assert by["v_sales"]["in_warehouse"] is True
    assert by["customers"]["in_warehouse"] is None
    assert isinstance(by["customers"]["primary_key"], list)
    assert out["indexes"] == []


async def test_tables_retired_and_all(wired):
    assert [t["name"] for t in (await modelapi.list_tables("retired"))["tables"]] == ["old"]
    allt = await modelapi.list_tables("all")
    assert "old" in {t["name"] for t in allt["tables"]}


async def test_tables_bad_status_422(wired):
    with pytest.raises(HTTPException) as e:
        await modelapi.list_tables("bogus")
    assert e.value.status_code == 422


async def test_relationships_lists_and_match_rate(wired):
    out = (await modelapi.list_relationships())["relationships"]
    assert len(out) == len(RELS)
    first = next(r for r in out if r["name"] == RELS[0]["name"])
    assert first["match_rate"] == 0.998 and first["from_columns"] == ["region_id"]
    assert any(r["match_rate"] is None for r in out)


async def test_measures_decode_jsonb_and_compile(wired):
    out = {m["name"]: m for m in (await modelapi.list_measures())["measures"]}
    assert isinstance(out["order_count"]["filter"], list)
    assert isinstance(out["average_order_value"]["numerator"], dict)
    assert isinstance(out["net_sales_mom_growth"]["input_metrics"], list)
    assert "compile_error" not in out["order_count"] and out["order_count"]["compiled_sql"]
    assert out["order_count"]["kind"] in ("sum", "fixed", "distinct")  # C-03 kind rides along


async def test_measures_canonical_ratio_and_derived_compile(wired):
    out = {m["name"]: m for m in (await modelapi.list_measures())["measures"]}
    for n in ("average_order_value", "net_sales_mom_growth"):
        assert "compile_error" not in out[n], out[n].get("compile_error")
        assert "metric_time__month" in out[n]["compiled_sql"]
        assert out[n]["sample_range"] == {"start": "2026-09-15", "end": "2026-10-15"}


async def test_measures_canonical_no_time_measure_has_no_range(wired):
    out = {m["name"]: m for m in (await modelapi.list_measures())["measures"]}
    cc = out["customer_count"]
    assert cc["compiled_sql"] and cc["sample_range"] is None
    assert "2026-" not in cc["compiled_sql"]


async def test_measures_compile_error_is_text(wired):
    wired["measures"].append(_stringify(dict(MEASURES[0], name="broken", base_relation="public.nope")))
    out = {m["name"]: m for m in (await modelapi.list_measures())["measures"]}
    assert isinstance(out["broken"].get("compile_error"), str)


async def test_pending_decodes_payload_and_caps(monkeypatch):
    pool = Pool([{"id": 7, "payload": json.dumps({"object": "measure", "name": "x", "op": "create",
                                                  "_summary": "Add x"}),
                  "created_at": datetime(2026, 10, 1)}])

    async def gp():
        return pool
    monkeypatch.setattr(modelapi, "db", SimpleNamespace(get_pool=gp))
    out = await modelapi.list_pending()
    assert out["pending"][0] == {"action_id": 7, "object": "measure", "name": "x", "op": "create",
                                 "summary": "Add x", "created_at": datetime(2026, 10, 1)}
    assert "model_change" in pool.sql[0] and "LIMIT 200" in pool.sql[0]
