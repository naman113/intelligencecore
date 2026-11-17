"""Phase 16 plan 03: real-Postgres proof of the model_change executor through actionsvc.approve.

Skipped without DASH_TEST_DATABASE_URL. Enable against the p26 stack with:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9263/agent_memory
The schema catalog and relationship statistics are monkeypatched (no business database needed).
"""
import copy
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from test_model_compiler import COLUMNS, MEASURES

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("DASH_TEST_DATABASE_URL"),
                       reason="DASH_TEST_DATABASE_URL not set (real-Postgres model tests)"),
]

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
REL_NAME = "order_lines__order_id__to__sales_orders"
STATS = {"sql": "x", "from_rows": 100, "from_nonnull": 100, "matched": 99, "match_rate": 0.99,
         "duplicate_keys": 0, "null_keys": 0}


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def mdb(monkeypatch):
    import asyncpg

    from app import actionsvc, semanticmodel

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "model_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        setup = await asyncpg.connect(url)
        await setup.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        await setup.close()
        pool = await asyncpg.create_pool(url, min_size=2, max_size=4)

        async def get_pool():
            return pool

        async def catalog():
            return copy.deepcopy(COLUMNS)

        state = {"stats": dict(STATS)}

        async def stats(defn):
            return dict(state["stats"])

        monkeypatch.setattr(actionsvc, "get_pool", get_pool)
        monkeypatch.setattr(semanticmodel, "get_pool", get_pool)
        monkeypatch.setattr(semanticmodel, "catalog_columns", catalog)
        monkeypatch.setattr(semanticmodel, "relationship_stats", stats)
        monkeypatch.setattr(semanticmodel.warehouse, "warehouse_mode", lambda: False)
        yield types.SimpleNamespace(pool=pool, state=state)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def mdef(name):
    d = next(m for m in MEASURES if m["name"] == name)
    return {k: v for k, v in d.items() if k != "status"}


async def propose(pool, obj, name, definition, op="create", origin="agent", evidence=None):
    payload = {"_summary": f"{op} {obj} {name}", "object": obj, "name": name, "op": op,
               "definition": definition, "before": None, "origin": origin, "thread_id": None,
               "evidence": evidence or {}}
    return await pool.fetchval(
        "INSERT INTO actions (kind, payload, tier, status) "
        "VALUES ('model_change', $1::jsonb, 'draft', 'proposed') RETURNING id", json.dumps(payload))


async def approve(aid):
    from app import actionsvc
    return await actionsvc.approve(aid)


def rel_def():
    return {"name": REL_NAME, "from_relation": "public.order_lines", "from_columns": ["order_id"],
            "to_relation": "public.sales_orders", "to_columns": ["id"], "cardinality": "many_to_one"}


async def seed_basics(m):
    """Approve the relationship and the two base measures through the real path."""
    for obj, name, d, kw in (("relationship", REL_NAME, rel_def(), {"origin": "foreign_key"}),
                             ("measure", "net_sales", mdef("net_sales"), {}),
                             ("measure", "order_count", mdef("order_count"), {})):
        r = await approve(await propose(m.pool, obj, name, d, **kw))
        assert r["status"] == "executed", (name, r)


async def count(pool, table, where="true"):
    return await pool.fetchval(f"SELECT count(*) FROM {table} WHERE {where}")


async def test_approve_twice_executes_once(mdb):
    aid = await propose(mdb.pool, "measure", "customer_count", mdef("customer_count"))
    first = await approve(aid)
    second = await approve(aid)
    assert first == {"action": aid, "status": "executed"}
    assert "error" in second
    rows = await mdb.pool.fetch("SELECT * FROM model_measures WHERE name = 'customer_count'")
    assert len(rows) == 1 and rows[0]["action_id"] == aid and rows[0]["status"] == "active"


async def test_update_changes_row_in_place(mdb):
    a1 = await propose(mdb.pool, "measure", "customer_count", mdef("customer_count"))
    await approve(a1)
    row1 = await mdb.pool.fetchrow("SELECT id, label FROM model_measures WHERE name = 'customer_count'")
    a2 = await propose(mdb.pool, "measure", "customer_count",
                       {**mdef("customer_count"), "label": "Customers"}, op="update")
    assert (await approve(a2))["status"] == "executed"
    row2 = await mdb.pool.fetchrow(
        "SELECT id, label, action_id FROM model_measures WHERE name = 'customer_count'")
    assert row2["id"] == row1["id"] and row2["label"] == "Customers" and row2["action_id"] == a2
    assert await count(mdb.pool, "model_measures") == 1


async def test_reject_changes_no_model_row(mdb):
    from app import actionsvc
    aid = await propose(mdb.pool, "measure", "customer_count", mdef("customer_count"))
    res = await actionsvc.reject(aid)
    assert res == {"rejected": aid} or res.get("rejected") == aid
    assert await mdb.pool.fetchval("SELECT status FROM actions WHERE id = $1", aid) == "rejected"
    for t in ("model_tables", "model_relationships", "model_measures", "model_indexes"):
        assert await count(mdb.pool, t) == 0


async def test_relationship_ratio_pop_sequence(mdb):
    await seed_basics(mdb)
    for name in ("average_order_value", "net_sales_mom_growth"):
        r = await approve(await propose(mdb.pool, "measure", name, mdef(name)))
        assert r["status"] == "executed", (name, r)
    rel = await mdb.pool.fetchrow("SELECT * FROM model_relationships WHERE name = $1", REL_NAME)
    assert rel["origin"] == "foreign_key" and float(rel["match_rate"]) == 0.99
    names = {r["name"] for r in await mdb.pool.fetch("SELECT name FROM model_measures WHERE status = 'active'")}
    assert names == {"net_sales", "order_count", "average_order_value", "net_sales_mom_growth"}


async def test_evidence_match_rate_is_not_stored(mdb):
    mdb.state["stats"]["match_rate"] = 0.5
    aid = await propose(mdb.pool, "relationship", REL_NAME, {**rel_def(), "match_rate": 1.0},
                        origin="agent", evidence={"match_rate": 1.0})
    assert (await approve(aid))["status"] == "executed"
    rel = await mdb.pool.fetchrow("SELECT origin, match_rate FROM model_relationships")
    assert rel["origin"] == "agent" and float(rel["match_rate"]) == 0.5


async def test_non_unique_to_side_fails_and_writes_nothing(mdb):
    mdb.state["stats"]["duplicate_keys"] = 4
    aid = await propose(mdb.pool, "relationship", REL_NAME, rel_def(), origin="foreign_key")
    assert (await approve(aid))["status"] == "failed"
    assert await count(mdb.pool, "model_relationships") == 0


async def test_measure_without_relationship_fails(mdb):
    aid = await propose(mdb.pool, "measure", "net_sales", mdef("net_sales"))
    assert (await approve(aid))["status"] == "failed"
    assert await count(mdb.pool, "model_measures") == 0


async def test_retire_sets_status_and_keeps_row(mdb):
    await seed_basics(mdb)
    aid = await propose(mdb.pool, "measure", "order_count", {}, op="retire")
    assert (await approve(aid))["status"] == "executed"
    row = await mdb.pool.fetchrow("SELECT status FROM model_measures WHERE name = 'order_count'")
    assert row["status"] == "retired"
    from app import semanticmodel
    model = await semanticmodel.load_model()
    assert [m.name for m in model.measures] == ["net_sales"]


async def test_retire_blocked_while_ratio_uses_it(mdb):
    await seed_basics(mdb)
    await approve(await propose(mdb.pool, "measure", "average_order_value", mdef("average_order_value")))
    aid = await propose(mdb.pool, "measure", "order_count", {}, op="retire")
    assert (await approve(aid))["status"] == "failed"
    assert await mdb.pool.fetchval("SELECT status FROM model_measures WHERE name = 'order_count'") == "active"


async def test_table_time_dimension_reach(mdb):
    await seed_basics(mdb)
    d = {"name": "order_lines", "relation": "public.order_lines", "role": "fact",
         "primary_key": ["id"], "agg_time_dimension": "public.sales_orders.order_date"}
    aid = await propose(mdb.pool, "table", "order_lines", d)
    assert (await approve(aid))["status"] == "executed"
    row = await mdb.pool.fetchrow("SELECT * FROM model_tables WHERE name = 'order_lines'")
    assert row["agg_time_dimension"] == "public.sales_orders.order_date" and list(row["primary_key"]) == ["id"]


async def test_one_pending_proposal_per_object_name(mdb):
    import asyncpg
    await propose(mdb.pool, "measure", "customer_count", mdef("customer_count"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await propose(mdb.pool, "measure", "customer_count", mdef("customer_count"))


def _norm(s: str) -> str:
    return s.replace(chr(13), "").strip(" " + chr(10) + chr(9))


async def test_data_modeling_refresh_reaches_unedited_v1_only(mdb):
    root = Path(__file__).resolve().parents[2]
    new = (root / "skills_library" / "intelligence-corner" / "data-modeling" / "SKILL.md").read_bytes().decode("ascii")
    v1 = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v1.md").read_bytes().decode("ascii")
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")

    async def row():
        return await mdb.pool.fetchrow("SELECT body, enabled FROM skills WHERE name = 'data-modeling'")

    r = await row()
    assert _norm(r["body"]) == _norm(new) and r["enabled"] is False
    await mdb.pool.execute("UPDATE skills SET body = $1 WHERE name = 'data-modeling'", v1)
    await mdb.pool.execute(ddl)
    r = await row()
    assert _norm(r["body"]) == _norm(new) and r["enabled"] is False
    edited = v1 + chr(10) + "Owner note." + chr(10)
    await mdb.pool.execute("UPDATE skills SET body = $1, enabled = TRUE WHERE name = 'data-modeling'", edited)
    await mdb.pool.execute(ddl)
    r = await row()
    assert r["body"] == edited and r["enabled"] is True
    await mdb.pool.execute(ddl)
    r2 = await row()
    assert r2["body"] == edited and r2["enabled"] is True


# Quick 032: installs that still carry the unedited 16-10 body also refresh (view-functions line).
async def test_data_modeling_refresh_reaches_unedited_v2(mdb):
    root = Path(__file__).resolve().parents[2]
    new = (root / "skills_library" / "intelligence-corner" / "data-modeling" / "SKILL.md").read_bytes().decode("ascii")
    v2 = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v2.md").read_bytes().decode("ascii")
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    await mdb.pool.execute("UPDATE skills SET body = $1 WHERE name = 'data-modeling'", v2)
    await mdb.pool.execute(ddl)
    r = await mdb.pool.fetchrow("SELECT body, enabled FROM skills WHERE name = 'data-modeling'")
    assert _norm(r["body"]) == _norm(new) and r["enabled"] is False
    assert "A view may use AND/OR/NOT" in r["body"]


# Quick 037: installs that still carry the unedited Quick 032 body also refresh (get_model sections).
async def test_data_modeling_refresh_reaches_unedited_v3(mdb):
    root = Path(__file__).resolve().parents[2]
    new = (root / "skills_library" / "intelligence-corner" / "data-modeling" / "SKILL.md").read_bytes().decode("ascii")
    v3 = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v3.md").read_bytes().decode("ascii")
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    await mdb.pool.execute("UPDATE skills SET body = $1 WHERE name = 'data-modeling'", v3)
    await mdb.pool.execute(ddl)
    r = await mdb.pool.fetchrow("SELECT body, enabled FROM skills WHERE name = 'data-modeling'")
    assert _norm(r["body"]) == _norm(new) and r["enabled"] is False
    assert 'get_model(section="measures", name="<measure>")' in r["body"]


# Quick 054: installs that still carry the unedited Quick 037 body also refresh (linked-column measures).
async def test_data_modeling_refresh_reaches_unedited_v4(mdb):
    root = Path(__file__).resolve().parents[2]
    new = (root / "skills_library" / "intelligence-corner" / "data-modeling" / "SKILL.md").read_bytes().decode("ascii")
    v4 = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v4.md").read_bytes().decode("ascii")
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    await mdb.pool.execute("UPDATE skills SET body = $1, enabled = TRUE WHERE name = 'data-modeling'", v4)
    await mdb.pool.execute(ddl)
    r = await mdb.pool.fetchrow("SELECT body, enabled FROM skills WHERE name = 'data-modeling'")
    assert _norm(r["body"]) == _norm(new) and r["enabled"] is True
    assert "schema.table.column" in r["body"]


# Quick 054: a measure adding up a linked column (cost from the product on each line) approves
# once its relationship is approved, and is refused before that (no path).
async def test_linked_column_measure_needs_its_relationship(mdb):
    await seed_basics(mdb)
    cogs = {"name": "cogs", "type": "simple", "base_relation": "public.order_lines", "agg": "sum",
            "expr": "qty * public.products.unit_cost", "agg_time_dimension": "public.sales_orders.order_date"}
    assert (await approve(await propose(mdb.pool, "measure", "cogs", cogs)))["status"] == "failed"
    assert await count(mdb.pool, "model_measures", "name = 'cogs'") == 0
    rel = {"name": "order_lines__product_id__to__products", "from_relation": "public.order_lines",
           "from_columns": ["product_id"], "to_relation": "public.products", "to_columns": ["id"],
           "cardinality": "many_to_one"}
    assert (await approve(await propose(mdb.pool, "relationship", rel["name"], rel,
                                        origin="foreign_key")))["status"] == "executed"
    assert (await approve(await propose(mdb.pool, "measure", "cogs", cogs)))["status"] == "executed"
    margin = {"name": "gross_margin_pct", "type": "derived", "expr": "(s - c) / nullif(s, 0) * 100",
              "input_metrics": [{"name": "net_sales", "alias": "s"}, {"name": "cogs", "alias": "c"}]}
    assert (await approve(await propose(mdb.pool, "measure", "gross_margin_pct", margin)))["status"] == "executed"
    from app import modelcompiler, semanticmodel
    model = await semanticmodel.load_model()
    sql = modelcompiler.compile_measure(model, modelcompiler.make_request(
        "gross_margin_pct", grain="month", start="2026-07-01", end="2026-10-01")).sql
    assert 'LEFT JOIN "public"."products"' in sql
