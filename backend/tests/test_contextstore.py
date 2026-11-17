"""contextstore.search / apply with a recording fake pool and fake embedder."""

import json

import pytest

from app import contextstore as cs

VEC = "[" + ",".join(["0.1000000"] * 1536) + "]"


class FakeConn:
    hidden_status = "active"

    def __init__(self, log, fetch_rows, hidden_status="active"):
        self.hidden_status = hidden_status
        self.log = log
        self.fetch_rows = fetch_rows

    def transaction(self):
        log = self.log

        class T:
            async def __aenter__(s):
                log.append(("BEGIN", ()))

            async def __aexit__(s, *e):
                log.append(("END", ()))
                return False
        return T()

    async def execute(self, sql, *args):
        self.log.append((sql, args))

    async def fetchval(self, sql, *args):
        self.log.append((sql, args))
        if sql.startswith("SELECT status FROM context_tables"):
            return self.hidden_status
        return 7

    async def fetch(self, sql, *args):
        self.log.append((sql, args))
        return self.fetch_rows.pop(0) if self.fetch_rows else []


class FakePool:
    def __init__(self, fetch_rows=None):
        self.hidden_status = "active"
        self.log = []
        self.fetch_rows = fetch_rows or []

    def acquire(self):
        pool = self

        class A:
            async def __aenter__(s):
                return FakeConn(pool.log, pool.fetch_rows, pool.hidden_status)

            async def __aexit__(s, *e):
                return False
        return A()


@pytest.fixture
def env(monkeypatch):
    pool = FakePool()
    calls = []

    async def gp():
        return pool

    async def emb(text):
        calls.append(text)
        return VEC

    monkeypatch.setattr(cs, "get_pool", gp)
    monkeypatch.setattr(cs, "_embed_or_none", emb)
    return pool, calls


def _sqls(pool):
    return [s for s, _ in pool.log if s not in ("BEGIN", "END")]


TABLE = {"kind": "table", "ref": "business_db.public.customers", "text": "Customers we sell to.",
         "schema": "public", "table": "customers", "object_kind": "table", "source": "business_db"}
COLUMN = {"kind": "column", "ref": "business_db.public.customers.region_id",
          "text": "Region the customer belongs to.", "schema": "public", "table": "customers",
          "column": "region_id", "structure": {"type": "integer", "nullable": False, "pk": False,
                                                "fk": {"table": "regions", "column": "id"}},
          "signature": "abcdef0123456789", "references_ref": "business_db.public.regions.id"}


async def test_apply_table(env):
    pool, calls = env
    assert await cs.apply(dict(TABLE)) is True
    assert len(calls) == 1
    sqls = _sqls(pool)
    assert sum("INSERT INTO context_tables" in s and "ON CONFLICT (ref) DO UPDATE" in s for s in sqls) == 1
    assert sum("INSERT INTO business_context" in s and "ON CONFLICT (kind, ref) DO UPDATE" in s
               for s in sqls) == 1
    assert pool.log[0][0] == "BEGIN" or any(s == "BEGIN" for s, _ in pool.log)
    assert calls[0].startswith("Table public.customers (table) in source business_db.")


async def test_apply_column_keeps_table_description(env):
    pool, calls = env
    assert await cs.apply(dict(COLUMN)) is True
    sqls = _sqls(pool)
    parent = [s for s in sqls if "INSERT INTO context_tables" in s]
    assert len(parent) == 1 and "description" not in parent[0]
    assert any("INSERT INTO context_columns" in s and "ON CONFLICT (ref) DO UPDATE" in s for s in sqls)
    assert "references regions.id" in calls[0] and "primary key" not in calls[0]


async def test_apply_rule_only_business_context(env):
    pool, calls = env
    ok = await cs.apply({"kind": "rule", "ref": "rule:fiscal-year", "title": "Fiscal year",
                         "text": "The fiscal year starts in April."})
    assert ok is True
    sqls = _sqls(pool)
    assert not any("context_tables" in s or "context_columns" in s for s in sqls)
    assert calls[0] == "Business rule Fiscal year: The fiscal year starts in April."


async def test_apply_kpi_tables_meta(env):
    pool, _ = env
    tabs = ["business_db.public.sales_orders", "business_db.public.regions"]
    assert await cs.apply({"kind": "kpi", "ref": "kpi:revenue", "title": "Revenue",
                           "text": "Sum of order totals.", "tables": tabs})
    ins = [a for s, a in pool.log if "INSERT INTO business_context" in s][0]
    meta = [json.loads(x) for x in ins if isinstance(x, str) and x.startswith("{")][0]
    assert meta["tables"] == tabs and meta["kpi_name"] == "Revenue"


async def test_apply_kpi_default_tables_and_slug_name(env):
    pool, _ = env
    assert await cs.apply({"kind": "kpi", "ref": "kpi:churn", "text": "Lost customers."})
    ins = [a for s, a in pool.log if "INSERT INTO business_context" in s][0]
    meta = [json.loads(x) for x in ins if isinstance(x, str) and x.startswith("{")][0]
    assert meta["tables"] == [] and meta["kpi_name"] == "churn"


async def test_apply_source(env):
    pool, _ = env
    assert await cs.apply({"kind": "source", "ref": "business_db", "text": "The ERP database."})
    sqls = _sqls(pool)
    assert any("INSERT INTO context_sources" in s and "description" in s for s in sqls)
    assert any("INSERT INTO business_context" in s for s in sqls)


async def test_embedding_failure_still_writes(monkeypatch, env):
    pool, _ = env

    async def none(text):
        return None
    monkeypatch.setattr(cs, "_embed_or_none", none)
    assert await cs.apply(dict(TABLE)) is True
    ins = [a for s, a in pool.log if "INSERT INTO business_context" in s][0]
    assert None in ins
    assert VEC not in ins


async def test_embed_or_none_guards(monkeypatch):
    class S:
        openai_api_key = ""
    monkeypatch.setattr(cs, "get_settings", lambda: S())
    cs.reset_clients()
    assert await cs._embed_or_none("x") is None

    S.openai_api_key = "k"

    class Emb:
        class embeddings:
            @staticmethod
            async def create(**kw):
                class D:
                    embedding = [0.1] * 3
                class R:
                    data = [D()]
                return R()
    monkeypatch.setattr(cs, "_client", lambda: Emb())
    assert await cs._embed_or_none("x") is None  # wrong dimension


@pytest.mark.parametrize("bad", [
    {"kind": "nope", "ref": "x", "text": "t"},
    {"kind": "rule", "ref": "rule:x", "text": ""},
    {"kind": "rule", "ref": "rule:x", "text": "a" * 1501},
    {"kind": "rule", "text": "t"},
    {"kind": "column", "ref": "r", "text": "t", "schema": "public"},
    {"kind": "kpi", "ref": "kpi:x", "text": "t", "tables": "business_db.public.a"},
    {"kind": "kpi", "ref": "kpi:x", "text": "t", "tables": [f"t{i}" for i in range(21)]},
    {"kind": "kpi", "ref": "kpi:x", "text": "t", "tables": [1]},
])
async def test_invalid_payload_no_sql(env, bad):
    pool, calls = env
    assert await cs.apply(bad) is False
    assert pool.log == [] and calls == []


async def test_no_destructive_sql(env):
    pool, _ = env
    for p in (TABLE, COLUMN, {"kind": "rule", "ref": "rule:a", "text": "t"}):
        await cs.apply(dict(p))
    await cs.search("customers")
    for s in _sqls(pool):
        assert "DELETE" not in s.upper() and "TRUNCATE" not in s.upper()


def _row(kind, ref, score, meta):
    return {"kind": kind, "ref": ref, "doc": f"doc {ref}", "score": score, "meta": meta}


async def test_search_vector_path(monkeypatch, env):
    pool, _ = env
    pool.fetch_rows = [[_row("column", "a.b.c.d", 0.8,
                             json.dumps({"table_name": "c", "column_name": "d", "source": "a"}))]]
    out = await cs.search("what is d", limit=1)
    sqls = _sqls(pool)
    assert "SET LOCAL hnsw.iterative_scan = relaxed_order" in sqls
    assert "SET LOCAL hnsw.ef_search = 100" in sqls
    sel = [s for s in sqls if "ORDER BY embedding <=> $1::vector" in s][0]
    assert "status = 'approved'" in sel
    assert len(out) == 1
    assert set(out[0]) == {"score", "type", "text", "table_name", "column_name", "kpi_name",
                           "source", "ref"}
    assert out[0]["table_name"] == "c" and out[0]["type"] == "column"


async def test_search_fts_only_without_embedding(monkeypatch, env):
    pool, _ = env

    async def none(text):
        return None
    monkeypatch.setattr(cs, "_embed_or_none", none)
    pool.fetch_rows = [[_row("kpi", "kpi:r", None, {"kpi_name": "Revenue"})]]
    out = await cs.search("revenue")
    sqls = _sqls(pool)
    assert not any("hnsw" in s for s in sqls)
    assert any("to_tsvector" in s for s in sqls)
    assert out[0]["score"] is None and out[0]["kpi_name"] == "Revenue"


async def test_search_orders_scored_first(env):
    pool, _ = env
    pool.fetch_rows = [[_row("table", "a", 0.5, {})], [_row("rule", "b", None, {})]]
    out = await cs.search("q", limit=5)
    assert [o["ref"] for o in out] == ["a", "b"]


def test_valid_rejects_spoofed_or_hidden_payloads():
    from app import contextstore as cs
    ok = {"kind": "rule", "ref": "rule:a-b", "text": "fine"}
    assert cs._valid(ok)
    assert not cs._valid({**ok, "ref": "kpi:a-b"})            # slug/kind mismatch
    assert not cs._valid({**ok, "ref": "rule:Bad Slug"})
    assert not cs._valid({**ok, "text": "hi\u202edden"})      # bidi override
    assert not cs._valid({**ok, "text": "tag\U000e0041"})     # unicode tag char
    col = {"kind": "column", "ref": "business_db.public.t.c", "text": "x",
           "schema": "public", "table": "t", "column": "c"}
    assert cs._valid(col)
    assert not cs._valid({**col, "ref": "business_db.public.other.c"})


# Phase 12 (WR-03 / WR-11)
def _bc(pool):
    return [(s, a) for s, a in pool.log if s not in ("BEGIN", "END") and "INSERT INTO business_context" in s][0]


async def test_apply_hidden_table_stays_hidden(env):
    pool, _ = env
    pool.hidden_status = "hidden"
    assert await cs.apply(dict(TABLE)) is True
    sql, args = _bc(pool)
    assert "CASE WHEN $10" in sql and "business_context.status = 'hidden'" in sql
    assert args[-1] is True


async def test_apply_hidden_table_column_born_hidden(env):
    pool, _ = env
    pool.hidden_status = "hidden"
    assert await cs.apply(dict(COLUMN)) is True
    col = [(s, a) for s, a in pool.log if "INSERT INTO context_columns" in s][0]
    assert "CASE WHEN $10 THEN 'hidden' ELSE 'active' END" in col[0] and col[1][-1] is True
    assert _bc(pool)[1][-1] is True


async def test_apply_active_table_not_hidden(env):
    pool, _ = env
    assert await cs.apply(dict(TABLE)) is True
    assert _bc(pool)[1][-1] is False


async def test_apply_rule_skips_hidden_lookup(env):
    pool, _ = env
    pool.hidden_status = "hidden"
    await cs.apply({"kind": "rule", "ref": "rule:x", "title": "X", "text": "t"})
    assert not any(s.startswith("SELECT status FROM context_tables") for s, _ in pool.log)
    assert _bc(pool)[1][-1] is False


@pytest.mark.parametrize("extra,want", [({"edited_by": "owner"}, "human"),
                                        ({"origin": "human"}, "agent"),
                                        ({"edited_by": "agent"}, "agent")])
async def test_apply_origin_from_edited_by(env, extra, want):
    pool, _ = env
    await cs.apply({"kind": "rule", "ref": "rule:x", "title": "X", "text": "t", **extra})
    assert _bc(pool)[1][-2] == want
