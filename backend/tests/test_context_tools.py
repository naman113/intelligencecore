"""propose_context_change: validation, live re-resolution, actions-only writes,
registration in the agent graph and superset containment."""

import json

import pytest

from app import contexttools as ct, schemadiscovery as sd
from app.agents import graph as g

pytestmark = pytest.mark.asyncio

SRC = "business_db"
T_ORDERS = f"{SRC}.public.sales_orders"
T_REGIONS = f"{SRC}.public.regions"
C_DATE = f"{T_ORDERS}.order_date"


def _col(name, type_="integer", nullable=False, pk=False, fk=None):
    return {"name": name, "type": type_, "nullable": nullable, "pk": pk, "fk": fk,
            "comment": None}


CATALOG = {"source": SRC, "truncated": False, "tables": [
    {"schema": "public", "table": "sales_orders", "object_kind": "table",
     "comment": None, "row_estimate": 5, "checks": [],
     "columns": [_col("order_id", pk=True), _col("order_date", "date"),
                 _col("region_id", fk={"table": "regions", "column": "region_id"})]},
    {"schema": "public", "table": "regions", "object_kind": "table",
     "comment": None, "row_estimate": 3, "checks": [],
     "columns": [_col("region_id", pk=True), _col("name", "text")]},
]}


class FakePool:
    def __init__(self, approved=None, insert_result=1, sources=("business_db",)):
        self.sql: list[tuple[str, tuple]] = []
        self.approved = approved or {}      # ref -> (description, signature)
        self.insert_result = insert_result
        self.sources = set(sources)
        self.hidden: set[str] = set()
        self.payloads: list[dict] = []
        self.insert_args: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.sql.append((sql, args))
        ref = args[-1]
        hit = self.approved.get(ref)
        if hit is None:
            return None
        return {"description": hit[0], "signature": hit[1]}

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        if sql.lstrip().startswith("INSERT INTO actions"):
            self.payloads.append(json.loads(args[0]))
            self.insert_args.append(args)
            r = self.insert_result
            return r(len(self.payloads)) if callable(r) else r
        if "FROM context_tables" in sql:
            return "hidden" if args[0] in self.hidden else None
        if "FROM context_sources" in sql:
            return 1 if args[0] in self.sources else None
        return None

    async def execute(self, sql, *args):
        self.sql.append((sql, args))


@pytest.fixture
def env(monkeypatch):
    state = {"introspect_calls": 0, "logs": []}
    pool = FakePool()
    state["pool"] = pool

    async def get_pool():
        return state["pool"]

    async def introspect(source=SRC):
        state["introspect_calls"] += 1
        return CATALOG

    async def log_tool(name, args, result, start, refused=False):
        state["logs"].append({"name": name, "args": args, "result": result,
                              "refused": refused})

    monkeypatch.setattr(ct, "get_pool", get_pool)
    monkeypatch.setattr(sd, "introspect", introspect)
    monkeypatch.setattr(ct, "_log_tool", log_tool)
    g.ctx_run_id.set(77)
    g.ctx_phone.set("+15550001")
    return state


async def call(**kw):
    return await ct.propose_context_change.ainvoke(kw)


def _no_context_writes(pool):
    for sql, _ in pool.sql:
        low = " ".join(sql.lower().split())
        for t in ("context_sources", "context_tables", "context_columns",
                  "business_context"):
            assert f"insert into {t}" not in low and f"update {t}" not in low


async def test_bad_kind_refused_without_sql(env):
    out = await call(kind="policy", ref="x", text="hello")
    assert out.startswith("REFUSED")
    assert env["pool"].sql == []
    assert env["logs"][-1]["refused"] is True


async def test_empty_text_refused(env):
    out = await call(kind="column", ref=C_DATE, text="   ")
    assert out.startswith("REFUSED") and env["pool"].payloads == []


async def test_text_too_long_refused(env):
    out = await call(kind="column", ref=C_DATE, text="x" * 1501)
    assert out.startswith("REFUSED") and env["pool"].payloads == []


async def test_control_chars_stripped(env):
    await call(kind="column", ref=C_DATE, text="Order\x00 date\x07 of the sale\n ok")
    assert env["pool"].payloads[0]["text"] == "Order date of the sale\n ok"


async def test_unknown_column_refused(env):
    out = await call(kind="column", ref=f"{T_ORDERS}.invented", text="hi")
    assert out.startswith("REFUSED") and "discover_schema" in out
    assert env["pool"].payloads == []


async def test_table_ref_for_column_kind_refused(env):
    out = await call(kind="column", ref=T_ORDERS, text="hi")
    assert out.startswith("REFUSED") and env["pool"].payloads == []


async def test_slug_validation(env):
    assert (await call(kind="rule", ref="rule:Bad Slug", text="t")).startswith("REFUSED")
    assert (await call(kind="rule", ref="kpi:ok", text="t")).startswith("REFUSED")
    assert (await call(kind="kpi", ref="kpi:" + "a" * 65, text="t")).startswith("REFUSED")
    assert env["pool"].payloads == []
    out = await call(kind="rule", ref="rule:net-terms", text="Net 30.")
    assert "1 proposed" in out


async def test_unknown_source_refused(env):
    out = await call(kind="source", ref="mystery", text="x")
    assert out.startswith("REFUSED") and env["pool"].payloads == []
    out = await call(kind="source", ref="business_db", text="The ERP database.")
    assert "1 proposed" in out
    assert env["pool"].payloads[0]["source"] == "business_db"


async def test_valid_column_inserts_one_actions_row(env):
    out = await call(kind="column", ref=C_DATE, text="Date the order was placed.")
    assert "1 proposed" in out
    pool = env["pool"]
    inserts = [s for s, _ in pool.sql if s.lstrip().startswith("INSERT INTO actions")]
    assert len(inserts) == 1
    assert "'context_change'" in inserts[0] and "'draft'" in inserts[0]
    assert "'proposed'" in inserts[0]
    assert pool.insert_args[0][1] == 77
    p = pool.payloads[0]
    assert p["kind"] == "column" and p["ref"] == C_DATE
    assert p["_summary"] == "Describe column sales_orders.order_date"
    assert p["schema"] == "public" and p["table"] == "sales_orders"
    assert p["column"] == "order_date" and p["object_kind"] == "table"
    assert p["structure"]["type"] == "date"
    assert p["signature"] == sd.signature(
        sd.structure_of(CATALOG["tables"][0]["columns"][1], "table"))
    assert p["origin"] == "agent" and p["thread_id"] == "+15550001"
    assert p["before"] is None
    _no_context_writes(pool)


async def test_structure_comes_from_catalog_not_model(env):
    await call(kind="column", ref=C_DATE, text="Order date.")
    assert env["pool"].payloads[0]["structure"]["type"] == "date"


async def test_fk_column_records_references_ref(env):
    await call(kind="column", ref=f"{T_ORDERS}.region_id", text="Region of the order.")
    assert env["pool"].payloads[0]["references_ref"] == f"{T_REGIONS}.region_id"


async def test_approved_unchanged_refused_without_revise(env):
    sig = sd.signature(sd.structure_of(CATALOG["tables"][0]["columns"][1], "table"))
    env["pool"].approved[C_DATE] = ("Old text.", sig)
    out = await call(kind="column", ref=C_DATE, text="New text.")
    assert "revise" in out and env["pool"].payloads == []
    out = await call(kind="column", ref=C_DATE, text="New text.", revise=True)
    assert "1 proposed" in out
    assert env["pool"].payloads[0]["before"] == "Old text."


async def test_approved_changed_signature_allowed_without_revise(env):
    env["pool"].approved[C_DATE] = ("Old text.", "stale-signature")
    out = await call(kind="column", ref=C_DATE, text="Updated for new type.")
    assert "1 proposed" in out
    assert env["pool"].payloads[0]["before"] == "Old text."


async def test_approved_table_refused_without_revise(env):
    env["pool"].approved[T_ORDERS] = ("Orders.", None)
    out = await call(kind="table", ref=T_ORDERS, text="More orders.")
    assert "revise" in out and env["pool"].payloads == []


async def test_table_with_columns_map_writes_one_row_each(env):
    out = await call(kind="table", ref=T_ORDERS, text="Customer sales orders.",
                     columns={"order_date": "When placed.", "order_id": "Order key.",
                              "nope": "invented"})
    assert "2 proposed" not in out and "3 proposed" in out
    assert "nope" in out and "Skipped" in out
    pool = env["pool"]
    assert [p["kind"] for p in pool.payloads] == ["table", "column", "column"]
    assert env["introspect_calls"] == 1
    _no_context_writes(pool)


async def test_table_columns_only_without_table_text(env):
    out = await call(kind="table", ref=T_ORDERS, text="",
                     columns={"order_date": "When placed."})
    assert "1 proposed" in out
    assert [p["kind"] for p in env["pool"].payloads] == ["column"]


async def test_already_pending_is_not_an_error(env):
    env["pool"].insert_result = None
    out = await call(kind="column", ref=C_DATE, text="Order date.")
    assert "0 proposed" in out and "1 already pending" in out
    assert "TOOL ERROR" not in out and "REFUSED" not in out
    insert = [s for s, _ in env["pool"].sql if s.lstrip().startswith("INSERT INTO actions")][0]
    assert "ON CONFLICT ((payload->>'kind'), (payload->>'ref'))" in insert
    assert "DO NOTHING RETURNING id" in " ".join(insert.split())


async def test_kpi_with_tables_validated_against_one_snapshot(env):
    out = await call(kind="kpi", ref="kpi:revenue", text="Sum of order totals.",
                     tables=[T_ORDERS, T_REGIONS, T_ORDERS])
    assert "1 proposed" in out
    assert env["pool"].payloads[0]["tables"] == [T_ORDERS, T_REGIONS]
    assert env["introspect_calls"] == 1


async def test_rule_without_tables_has_empty_list(env):
    await call(kind="rule", ref="rule:net-terms", text="Net 30.")
    assert env["pool"].payloads[0]["tables"] == []
    assert env["introspect_calls"] == 0


async def test_unknown_or_column_table_ref_refused(env):
    out = await call(kind="kpi", ref="kpi:x", text="t", tables=[T_ORDERS, "business_db.public.ghost"])
    assert out.startswith("REFUSED") and "ghost" in out
    out = await call(kind="kpi", ref="kpi:x", text="t", tables=[C_DATE])
    assert out.startswith("REFUSED") and C_DATE in out
    assert env["pool"].payloads == []


async def test_more_than_20_tables_refused(env):
    many = [f"business_db.public.t{i}" for i in range(21)]
    out = await call(kind="kpi", ref="kpi:x", text="t", tables=many)
    assert out.startswith("REFUSED") and "20" in out and env["pool"].payloads == []


async def test_tables_on_non_kpi_kind_refused(env):
    for kind, ref in (("table", T_ORDERS), ("column", C_DATE), ("source", "business_db")):
        out = await call(kind=kind, ref=ref, text="t", tables=[T_ORDERS])
        assert out.startswith("REFUSED") and "kpi and rule" in out
    assert env["pool"].payloads == []


async def test_call_is_logged_without_full_text(env):
    await call(kind="column", ref=C_DATE, text="secret body text here")
    log = env["logs"][-1]
    assert log["name"] == "propose_context_change"
    assert "secret body" not in json.dumps(log["args"])
    assert log["args"]["n_tables"] == 0


async def test_tool_error_is_returned_as_string(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(sd, "introspect", boom)
    out = await call(kind="column", ref=C_DATE, text="x")
    assert out.startswith("TOOL ERROR")


def test_args_schema_exposes_expected_fields():
    props = ct.propose_context_change.args_schema.model_json_schema()["properties"]
    assert sorted(props) == ["columns", "kind", "ref", "revise", "tables", "text", "title"]

    def types(p):
        return {p.get("type")} | {a.get("type") for a in p.get("anyOf", [])}
    assert "object" in types(props["columns"])
    assert "array" in types(props["tables"])


def test_tool_source_never_writes_context_rows():
    src = open(ct.__file__, encoding="utf-8").read().lower()
    assert "contextstore" not in src
    for t in ("context_sources", "context_tables", "context_columns", "business_context"):
        assert f"insert into {t}" not in src and f"update {t}" not in src


# -- registration and containment -----------------------------------------

def test_registry_has_context_tools():
    reg = g._tool_registry()
    assert reg["discover_schema"] is ct.discover_schema
    assert reg["propose_context_change"] is ct.propose_context_change


def test_superset_tail_and_containment():
    sup = g.AGENT_TOOL_SUPERSET
    for agent in ("supervisor", "data"):
        assert sup[agent][-2:] == ["discover_schema", "propose_context_change"]
    for agent in ("analysis", "document", "drawing", "reflection"):
        assert "discover_schema" not in sup[agent]
        assert "propose_context_change" not in sup[agent]
    assert sup["document"].index("get_document") == sup["document"].index("search_documents") - 1         or sup["document"].index("get_document") < sup["document"].index("search_documents")


def test_default_configs_include_context_tools():
    for agent in ("supervisor", "data"):
        tools = g.DEFAULT_CONFIGS[agent]["tools"]
        assert "discover_schema" in tools and "propose_context_change" in tools
    for agent in ("analysis", "document", "drawing"):
        assert "propose_context_change" not in g.DEFAULT_CONFIGS[agent]["tools"]


def test_merge_config_can_disable_but_not_extend():
    merged = g._merge_config("data", {"disabled_tools": ["propose_context_change"],
                                      "tools": ["run_python"]})
    assert "propose_context_change" not in merged["tools"]
    assert "discover_schema" in merged["tools"]
    assert "run_python" not in merged["tools"]
    assert set(merged["tools"]) <= set(g.AGENT_TOOL_SUPERSET["data"])


# Phase 12 (WR-03)
async def test_hidden_table_refused_for_table_and_column(env):
    pool = env["pool"]
    pool.hidden.add(T_ORDERS)
    msg = f"REFUSED: {T_ORDERS} is hidden by the owner; do not propose context for it."
    assert await call(kind="table", ref=T_ORDERS, text="x") == msg
    assert await call(kind="column", ref=C_DATE, text="x") == msg
    assert not pool.payloads
