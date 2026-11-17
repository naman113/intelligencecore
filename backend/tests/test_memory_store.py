"""store_learning + shared dedup/merge path (memory.py).

Pure logic — embeddings, LLM, pool, and graph mirror are all faked. Proves:
  * >= 0.97 cosine match  -> "duplicate", no write at all
  * 0.85–0.97 match       -> "merged", UPDATE of the existing row (source untouched)
  * below 0.85 / no match -> "stored", INSERT carrying the source column
  * reflect_and_store(source=...) passes source through to inserted rows
"""

import json
import sys
import types

import pytest

from app import memory

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- fakes

class FakePool:
    def __init__(self, match=None):
        self.match = match
        self.fetchrow_calls = []
        self.executes = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self.match

    async def execute(self, sql, *args):
        self.executes.append((sql, args))

    async def fetchval(self, sql, *args):
        # Quick 009: INSERT/UPDATE ... RETURNING (row id / revisions for the
        # graph mirror). Recorded like execute so the write assertions hold;
        # reads (the run-duration lookup) are not writes.
        if not sql.lstrip().upper().startswith(("INSERT", "UPDATE")):
            return None
        self.executes.append((sql, args))
        return 1


class FakeOpenAI:
    """Constructor-compatible stand-in; chat path only used by reflection test."""

    def __init__(self, *a, **k):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))
        self._payload = "{}"

    async def _create(self, **kw):
        msg = types.SimpleNamespace(content=self._payload)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])


def _wire(monkeypatch, pool, merged_text="merged learning content"):
    async def fake_embed(text):
        return "[0.1,0.2]"

    async def fake_pool():
        return pool

    async def fake_merge(client, old, new):
        return merged_text

    async def fake_to_graph(learning_id, content, kind, revisions=0, *, pool=None):
        return False

    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr(memory, "get_pool", fake_pool)
    monkeypatch.setattr(memory, "_merge_learnings", fake_merge)
    monkeypatch.setattr(memory, "to_graph", fake_to_graph)
    monkeypatch.setattr(memory, "AsyncOpenAI", FakeOpenAI)


# ---------------------------------------------------------------- store_learning

async def test_near_identical_is_duplicate_no_write(monkeypatch):
    pool = FakePool(match={"id": 7, "content": "old", "score": 0.98})
    _wire(monkeypatch, pool)
    out = await memory.store_learning("Client always wants PDF reports", "preference",
                                      source="chat")
    assert out == "duplicate"
    assert pool.executes == []


async def test_midband_merges_via_update(monkeypatch):
    pool = FakePool(match={"id": 42, "content": "old learning", "score": 0.90})
    _wire(monkeypatch, pool, merged_text="the merged version of it")
    out = await memory.store_learning("Client now wants XLSX not PDF reports",
                                      "preference", source="chat")
    assert out == "merged"
    assert len(pool.executes) == 1
    sql, args = pool.executes[0]
    assert "UPDATE learnings" in sql
    assert args[0] == 42                          # targets the matched row
    assert args[1] == "the merged version of it"  # merged content written
    # merged rows keep their existing source — UPDATE must not assign it
    set_clause = sql.split("SET", 1)[1]
    assert "source =" not in set_clause.replace("source_run", "")


async def test_low_similarity_inserts_with_source(monkeypatch):
    pool = FakePool(match={"id": 1, "content": "unrelated", "score": 0.30})
    _wire(monkeypatch, pool)
    out = await memory.store_learning("Monthly spend lives in Invoices.TotalAmount",
                                      "fact", source="dashboard")
    assert out == "stored"
    assert len(pool.executes) == 1
    sql, args = pool.executes[0]
    assert "INSERT INTO learnings" in sql
    # INSERT arg order: (thread_id, kind, content, embedding, source_run, source)
    assert args[5] == "dashboard"


async def test_no_match_inserts(monkeypatch):
    pool = FakePool(match=None)
    _wire(monkeypatch, pool)
    out = await memory.store_learning("Deliveries table keys on order_id", "fact")
    assert out == "stored"
    sql, args = pool.executes[0]
    assert "INSERT INTO learnings" in sql
    assert args[5] is None  # source column explicitly null


async def test_exact_097_boundary_is_duplicate(monkeypatch):
    pool = FakePool(match={"id": 3, "content": "old", "score": 0.97})
    _wire(monkeypatch, pool)
    out = await memory.store_learning("Client always wants PDF reports",
                                      "preference")
    assert out == "duplicate"
    assert pool.executes == []


async def test_exact_085_boundary_is_merged(monkeypatch):
    pool = FakePool(match={"id": 4, "content": "old", "score": 0.85})
    _wire(monkeypatch, pool, merged_text="boundary merged content")
    out = await memory.store_learning("Client now wants XLSX reports instead",
                                      "preference")
    assert out == "merged"
    assert len(pool.executes) == 1
    assert "UPDATE learnings" in pool.executes[0][0]


async def test_store_learning_carries_thread_and_run(monkeypatch):
    """remember_fact attribution: thread_id/run_id kwargs land in the INSERT."""
    pool = FakePool(match=None)
    _wire(monkeypatch, pool)
    out = await memory.store_learning("Deliveries table keys on order_id", "fact",
                                      source="tool:remember_fact",
                                      thread_id="911234567890", run_id=77)
    assert out == "stored"
    sql, args = pool.executes[0]
    assert "INSERT INTO learnings" in sql
    assert args[0] == "911234567890"  # thread_id
    assert args[4] == 77              # source_run
    assert args[5] == "tool:remember_fact"


async def test_invalid_kind_rejected(monkeypatch):
    pool = FakePool()
    _wire(monkeypatch, pool)
    with pytest.raises(ValueError):
        await memory.store_learning("long enough content here", "opinion")
    with pytest.raises(ValueError):
        await memory.store_learning("short", "fact")
    assert pool.executes == []


# ---------------------------------------------------------------- reflection path

async def test_reflection_passes_source_to_insert(monkeypatch):
    pool = FakePool(match=None)
    _wire(monkeypatch, pool)

    async def effective_configs():
        return {"reflection": {"prompt": None, "model": "test-model",
                               "max_tokens": 100, "temperature": 0.0}}

    stub = types.ModuleType("app.agents.graph")
    stub.effective_configs = effective_configs
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

    payload = json.dumps({"learnings": [
        {"kind": "fact", "content": "Monthly spend lives in Invoices.TotalAmount"},
    ]})
    orig_init = FakeOpenAI.__init__

    def init_with_payload(self, *a, **k):
        orig_init(self, *a, **k)
        self._payload = payload

    monkeypatch.setattr(FakeOpenAI, "__init__", init_with_payload)

    n = await memory.reflect_and_store(run_id=9, thread_id="t1", user_message="u",
                                       reply="r", tool_log=[], source="reflection:9")
    assert n == 1
    assert len(pool.executes) == 1
    sql, args = pool.executes[0]
    assert "INSERT INTO learnings" in sql
    assert "reflection:9" in args   # source stored
    assert 9 in args                # source_run stored
    assert "t1" in args             # thread_id stored
