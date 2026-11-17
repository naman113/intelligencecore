"""Quick 019 (audit Fix 3): the memory each run used, stored on the run and
served by GET /api/runs/{id}/memory, plus what the run stored afterwards.
Pure: fake pools, no DB/LLM/graph."""

import json

import pytest
from fastapi import HTTPException

from app import runsapi, worker

USED = {
    "lessons": [{"id": 31, "text": "Filter VehicleTelemetry by plate first."}],
    "learnings": [{"id": 7, "kind": "fact", "source": "dashboard",
                   "text": "[fact, from dashboard] Acme pays net-30"},
                  {"id": 8, "kind": "preference", "source": "person:3",
                   "text": "[preference, from person:3] Reports in lakh"}],
    "graph": [{"uuid": "e-1", "kind": "fact", "group_id": "global", "block": "global",
               "text": "Northwind ships with BlueDart"}],
    "cases": [{"id": 19, "text": "Case #19: Collect payment"}],
    "group": {"purpose": False, "members": False},
    "chars": 420, "budget_chars": 6000, "dropped": 2,
}


# ── worker: stored fail-soft before the run ──────────────────────────────

class _Pool:
    def __init__(self, boom=False):
        self.boom = boom
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if self.boom:
            raise RuntimeError("db blip")


async def test_store_memory_used_writes_jsonb():
    pool = _Pool()
    await worker._store_memory_used(pool, 12, USED)
    (sql, args), = pool.executed
    assert "UPDATE task_runs SET memory_used = $2::jsonb WHERE id = $1" in sql
    assert args[0] == 12 and json.loads(args[1]) == USED


async def test_store_memory_used_is_fail_soft_and_skips_nothing_to_store():
    await worker._store_memory_used(_Pool(boom=True), 12, USED)   # must not raise
    pool = _Pool()
    await worker._store_memory_used(pool, 12, {})
    await worker._store_memory_used(pool, 12, None)
    assert pool.executed == []


async def test_process_one_stores_provenance_before_the_run(monkeypatch):
    from tests.test_thread_summary import WorkerPool, _wire_process_one
    pool = WorkerPool()
    _, run_calls = _wire_process_one(monkeypatch, pool)
    order = []

    async def assemble(content, thread_id, sender_name=None):
        return "MEM", USED

    async def run(history, user_message, context_text=""):
        order.append(("run", any("memory_used" in s for s, _ in pool.executed)))
        return "All good."

    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance", assemble)
    monkeypatch.setattr(worker.graph, "run", run)
    assert await worker._process_one() is True
    stored = [a for s, a in pool.executed if "memory_used" in s]
    assert stored and stored[0][0] == 9 and json.loads(stored[0][1]) == USED
    assert order == [("run", True)], "provenance is written before the agent starts"


async def test_process_one_replies_even_when_storing_provenance_fails(monkeypatch):
    from tests.test_thread_summary import WorkerPool, _wire_process_one
    pool = WorkerPool()
    _, run_calls = _wire_process_one(monkeypatch, pool)
    real_execute = pool.execute

    async def execute(sql, *args):
        if "memory_used" in sql:
            raise RuntimeError("column missing")
        return await real_execute(sql, *args)

    async def assemble(content, thread_id, sender_name=None):
        return "MEM", USED

    monkeypatch.setattr(pool, "execute", execute)
    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance", assemble)
    assert await worker._process_one() is True
    assert run_calls and run_calls[0]["context_text"] == "MEM"
    assert any("status = 'done'" in s for s, _ in pool.executed)


# ── GET /api/runs/{id}/memory ────────────────────────────────────────────

def test_memory_view_marks_learnings_retired_since_the_run():
    statuses = {31: {"status": "active", "status_reason": None},
                7: {"status": "superseded", "status_reason": "merged"}}
    out = runsapi.memory_view(5, json.dumps(USED), statuses, [])   # jsonb arrives as str
    assert out["recorded"] is True and out["run_id"] == 5
    assert out["used"]["lessons"][0]["status"] == "active"
    learn = {l["id"]: l for l in out["used"]["learnings"]}
    assert learn[7]["status"] == "superseded" and learn[7]["status_reason"] == "merged"
    assert learn[8]["status"] == "deleted"
    assert out["used"]["graph"] == USED["graph"] and out["used"]["dropped"] == 2


def test_memory_view_stored_outcomes():
    stored = [{"id": 40, "kind": "lesson", "content": "Filter by plate", "source": "dashboard",
               "revisions": 0, "status": "active", "status_reason": None, "updated_at": None},
              {"id": 7, "kind": "fact", "content": "Acme pays net-45", "source": "dashboard",
               "revisions": 2, "status": "active", "status_reason": None, "updated_at": None}]
    out = runsapi.memory_view(5, None, {}, stored)
    assert out["recorded"] is False and out["used"] is None
    assert [(s["id"], s["outcome"]) for s in out["stored"]] == [(40, "new"), (7, "updated")]


def test_memory_view_tolerates_partial_or_bad_records():
    out = runsapi.memory_view(5, "not json", {}, [])
    assert out["recorded"] is False
    out = runsapi.memory_view(5, {"chars": 0}, {}, [])
    assert out["recorded"] is True
    assert out["used"]["lessons"] == [] and out["used"]["group"] == {"purpose": False,
                                                                    "members": False}


class _RoutePool:
    def __init__(self, row):
        self.row = row
        self.fetches = []

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        if "WHERE id = ANY" in sql:
            return [{"id": 31, "status": "active", "status_reason": None}]
        if "source_run = $1" in sql:
            return [{"id": 41, "kind": "fact", "content": "x" * 20, "source": None,
                     "revisions": 0, "status": "active", "status_reason": None,
                     "updated_at": None}]
        return []


async def test_run_memory_route(monkeypatch):
    pool = _RoutePool({"id": 5, "memory_used": json.dumps(USED)})

    async def get_pool():
        return pool
    monkeypatch.setattr(runsapi, "get_pool", get_pool)
    out = await runsapi.run_memory(5)
    assert out["recorded"] and out["stored"][0]["id"] == 41
    ids_sql, ids_args = next(f for f in pool.fetches if "ANY" in f[0])
    assert sorted(ids_args[0]) == [7, 8, 31]
    assert {l["id"]: l["status"] for l in out["used"]["learnings"]} == {7: "deleted",
                                                                       8: "deleted"}


async def test_run_memory_route_404(monkeypatch):
    async def get_pool():
        return _RoutePool(None)
    monkeypatch.setattr(runsapi, "get_pool", get_pool)
    with pytest.raises(HTTPException) as e:
        await runsapi.run_memory(999)
    assert e.value.status_code == 404


def test_route_is_mounted_with_the_ops_key():
    from fastapi.testclient import TestClient
    from app import main
    found = []
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inh = list(r.include_context.dependencies)
            for s in r.original_router.routes:
                if s.path == "/api/runs/{run_id}/memory":
                    found.append(inh + list(s.dependencies))
    assert len(found) == 1
    assert any(getattr(d, "dependency", None) is main._require_ops_key for d in found[0])
    assert TestClient(main.app).get("/api/runs/1/memory").status_code == 401


def test_both_operator_uis_read_the_route():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    console = (root / "backend" / "app" / "dashboard.html").read_text(encoding="utf-8")
    assert "/api/runs/${id}/memory" in console and "Learned from this run" in console
    lib = (root / "frontend" / "src" / "lib" / "runs.ts").read_text(encoding="utf-8")
    assert "/api/runs/${id}/memory" in lib


def test_schema_adds_memory_used_additively():
    from pathlib import Path
    schema = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
        encoding="utf-8")
    assert "ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS memory_used JSONB;" in schema
